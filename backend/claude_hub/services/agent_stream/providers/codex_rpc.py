"""CodexProtocolSession implementation; compatibility dependencies are explicit."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ....models import ChatMode
from .codex import CodexConfiguredSession, _CodexTurnCancellation
from .contracts import ProviderRpcError


class CodexProtocolSession(CodexConfiguredSession):
    """Transport operations over the state owned by the typed base."""

    async def cancel_active_turn(self) -> None:
        async with self._cancel_lock:
            await self._cancel_active_turn_locked()

    async def _cancel_active_turn_locked(self) -> None:
        initial_turn_id = self._provider_turn_id if self._turn_in_flight else None
        state = _CodexTurnCancellation(
            self._thread_id,
            self._process,
            initial_turn_id,
            initial_turn_id if initial_turn_id not in self._retired_provider_turn_ids else None,
            requires_confirmation=self._native_stop_requires_confirmation,
            unidentified_start=(
                self._turn_in_flight
                and initial_turn_id is None
                and self._native_stop_requires_confirmation
            ),
        )
        self._cancel_state = state
        self._discard_turn_id = initial_turn_id
        self._interrupted.clear()
        if initial_turn_id and initial_turn_id not in self._discarded_provider_turn_ids:
            self._discarded_provider_turn_ids.append(initial_turn_id)
        restart_required = False
        try:
            await self._cancel_observed_turns(state)
        except Exception as exc:
            if (
                state.requires_confirmation
                or self._thread_id != state.thread_id
                or self._process is not state.process
            ):
                # Releasing the guard would claim that a native continuation
                # stopped even though its matching completion never arrived.
                raise RuntimeError("Codex native turn stop could not be confirmed") from exc
            # Preserve the bounded process-replacement fallback for ordinary
            # single-turn Stop when no native continuation was observed.
            self._runtime.logger().exception("codex turn/interrupt failed; restarting app-server")
            restart_required = True
        finally:
            self._cancel_state = None
        self._native_stop_requires_confirmation = False
        self._delivery_uncertain = False
        self._submission_outcome_uncertain = False
        self._pending_questions.clear()
        self._clear_staged_images()
        inflight = self._inflight_images
        self._inflight_images = []
        self._provider_turn_id = None
        self._cleanup_images(inflight)
        if restart_required:
            try:
                await self.restart_for_recovery()
            except Exception:
                # The old server is stopped. Ordinary Stop retains its existing
                # recovery behavior: the next send may relaunch the thread.
                self._runtime.logger().exception("codex app-server recovery restart failed")
        self._end_turn()

    async def _cancel_observed_turns(self, state: _CodexTurnCancellation) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2 * self._runtime.startup_grace_s()
        while True:
            self._drain_cancel_notifications()
            if state.unidentified_start:
                raise RuntimeError("Codex native turn start did not include an id")
            if self._thread_id != state.thread_id or self._process is not state.process:
                raise RuntimeError("Codex thread or process changed during Stop")
            turn_id = state.current_turn_id
            if turn_id is None:
                if (
                    self._turn_in_flight
                    and state.initial_turn_id is None
                    and not state.requires_confirmation
                ):
                    raise RuntimeError("Codex did not return an active turn id")
                # A read_line waiter may already own a dequeued start. Let it
                # join the cancellation fence before declaring the queue idle.
                await asyncio.sleep(0)
                self._drain_cancel_notifications()
                if self._thread_id != state.thread_id or self._process is not state.process:
                    raise RuntimeError("Codex thread or process changed during Stop")
                if state.current_turn_id is None and not state.unidentified_start:
                    return
                continue
            if not state.thread_id:
                raise RuntimeError("Codex did not return an active thread id")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Codex Stop exceeded its confirmation deadline")
            try:
                async with asyncio.timeout(remaining):
                    await self._send_request(
                        "turn/interrupt", {"threadId": state.thread_id, "turnId": turn_id}
                    )
            except Exception:
                # The interrupted turn may have ended while its successor was
                # accepted. The successor still needs its own interrupt.
                if state.current_turn_id != turn_id and turn_id in self._retired_provider_turn_ids:
                    continue
                raise
            while state.current_turn_id == turn_id:
                self._drain_cancel_notifications()
                if state.current_turn_id != turn_id:
                    break
                state.changed.clear()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Codex Stop exceeded its confirmation deadline")
                await asyncio.wait_for(state.changed.wait(), timeout=remaining)

    def _drain_cancel_notifications(self) -> None:
        retained: List[Optional[Dict[str, Any]]] = []
        while not self._notification_queue.empty():
            record = self._notification_queue.get_nowait()
            if record is None or not self._capture_cancel_notification(record):
                retained.append(record)
        for record in retained:
            self._notification_queue.put_nowait(record)

    def _capture_cancel_notification(self, record: Dict[str, Any]) -> bool:
        state = self._cancel_state
        if (
            state is None
            or self._process is not state.process
            or self._thread_id != state.thread_id
        ):
            return False
        params = record.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        if isinstance(thread_id, str) and thread_id and thread_id != state.thread_id:
            return False
        method = record.get("method")
        if isinstance(method, str) and method.startswith(("thread/goal/", "goal/")):
            return False
        turn_id = self._notification_turn_id(record)
        if not turn_id:
            if method != "turn/started":
                return False
            state.unidentified_start = True
            state.requires_confirmation = True
            self._native_stop_requires_confirmation = True
            if not self._turn_in_flight:
                self._begin_turn()
            state.changed.set()
            return True
        if method == "turn/started":
            if turn_id in self._retired_provider_turn_ids:
                return True
            previous = state.current_turn_id
            if previous and previous != turn_id and previous not in self._retired_provider_turn_ids:
                # Codex runs one main turn per thread. A successor start retires
                # the previous turn even if its completion has not been consumed.
                self._retired_provider_turn_ids.append(previous)
            state.requires_confirmation |= turn_id != state.initial_turn_id
            # A failed native Stop must retain this requirement on retry;
            # otherwise the retained successor would look like an ordinary turn.
            self._native_stop_requires_confirmation = state.requires_confirmation
            state.current_turn_id = turn_id
            self._provider_turn_id = turn_id
            self._discard_turn_id = turn_id
            if turn_id not in self._discarded_provider_turn_ids:
                self._discarded_provider_turn_ids.append(turn_id)
            if not self._turn_in_flight:
                self._begin_turn()
            state.changed.set()
            return True
        if method == "turn/completed":
            self._remember_retired_turn(record)
            if turn_id == state.current_turn_id:
                state.current_turn_id = None
                self._interrupted.set()
            state.changed.set()
            return True
        return turn_id in self._discarded_provider_turn_ids

    async def restart_for_recovery(self) -> None:
        await super().restart_for_recovery()
        self._discard_turn_id = None
        self._interrupted.clear()

    async def _send_text(self, text: str) -> None:
        if not self._started or self._process is None:
            await self.start()
        if self._current_mode != ChatMode.DEFAULT.value and not self._mode_discovery_attempted:
            await self._discover_modes()
        input_items: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        # Append any staged images as localImage inputs.
        image_paths = self._staged_images
        self._staged_images = []
        for img_path in image_paths:
            input_items.append({"type": "localImage", "path": str(img_path)})
        params: Dict[str, Any] = {
            "threadId": self._thread_id,
            "input": input_items,
            **self._turn_config(),
        }
        collaboration_mode = self._collaboration_mode_payload()
        if collaboration_mode is not None:
            params["collaborationMode"] = collaboration_mode
        # Transfer ownership of the image temp files to the in-flight turn
        # BEFORE issuing turn/start. The app-server may emit turn/completed
        # (or any notification) before the turn/start response arrives; if we
        # waited for the response to set _inflight_images, the completed
        # handler would see an empty list and the temp files would leak.
        self._inflight_images = image_paths
        previous_turn_id = self._provider_turn_id
        self._turn_start_request_pending = True
        self._turn_start_claimed_while_pending = False
        self._turn_start_claimed_id_while_pending = None
        try:
            self.check_delivery_admission()
            response = await self._send_request("turn/start", params)
            turn = response.get("turn") if isinstance(response, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            claimed_turn_id = self._provider_turn_id
            if (
                claimed_turn_id is not None
                and claimed_turn_id != previous_turn_id
                and turn_id is not None
                and claimed_turn_id != turn_id
            ):
                # The start consumed while this request was pending belongs to a
                # different provider continuation. Preserve it and require its
                # own confirmed Stop rather than treating it as this Hub turn.
                self._native_stop_requires_confirmation = True
            # A queued native continuation can already have been consumed
            # before this RPC response. Do not overwrite its newer turn ID.
            if self._provider_turn_id == previous_turn_id and (
                turn_id is None
                or (
                    turn_id not in self._retired_provider_turn_ids
                    and turn_id != self._discard_turn_id
                )
            ):
                self._provider_turn_id = turn_id
            self._delivery_uncertain = False
        except asyncio.CancelledError:
            claimed_id = self._turn_start_claimed_id_while_pending
            claimed_completed = (
                claimed_id is not None and claimed_id in self._retired_provider_turn_ids
            )
            if self._turn_start_claimed_while_pending and not claimed_completed:
                self._native_stop_requires_confirmation = True
                self._delivery_uncertain = True
            elif not self._turn_start_claimed_while_pending:
                inflight = self._inflight_images
                self._inflight_images = []
                self._cleanup_images(inflight)
                self._end_turn()
            raise
        except Exception as exc:
            claimed_id = self._turn_start_claimed_id_while_pending
            claimed_completed = (
                claimed_id is not None and claimed_id in self._retired_provider_turn_ids
            )
            if self._turn_start_claimed_while_pending and claimed_completed:
                # Matching provider completion is stronger than an ambiguous
                # lost ACK. A definite rejection still reaches the caller, but
                # Input must not add a second terminal edge for the completed
                # execution it could not correlate to this request.
                self._native_stop_requires_confirmation = False
                self._delivery_uncertain = False
                self._last_error = None
                if isinstance(exc, ProviderRpcError):
                    self._submission_outcome_uncertain = True
                    raise
                return
            if self._turn_start_claimed_while_pending:
                # The server exposed a live main turn but did not acknowledge
                # this request. It may be this Hub turn or unrelated native
                # work, so retain ownership and surface an uncertain failure;
                # only matching completion or confirmed Stop may release it.
                self._native_stop_requires_confirmation = True
                self._delivery_uncertain = True
                self._last_error = None
            else:
                # No provider start was observed. The turn never crossed the
                # accepted boundary, so its image files remain ours to delete.
                inflight = self._inflight_images
                self._inflight_images = []
                self._cleanup_images(inflight)
            raise
        finally:
            self._turn_start_request_pending = False
            self._turn_start_claimed_while_pending = False
            self._turn_start_claimed_id_while_pending = None

    def _stage_images(self, images: List[bytes]) -> None:
        """Stage images to temp files for the next turn/start as localImage.

        Codex's ``turn/start`` accepts ``localImage`` input items with a file
        path. We stage the bytes to temp files (validated by magic bytes and
        size) inside the app-owned 0700 Codex image temp directory and attach
        them to the next ``_send_text`` call. Temp files are deleted after the
        turn completes (success or failure) and on ``stop``.
        """
        if not images:
            return
        temp_dir = self._runtime.codex_image_temp_dir()
        for img in images:
            media_type = self._runtime.detect_image_mime(img)
            if media_type is None:
                raise ValueError("unsupported image format or not an image")
            # Use a unique name inside the app-owned temp dir. ``mkstemp``
            # would create the file in the system temp dir; we want it under
            # our 0700 directory so the backend owns the lifecycle.
            fd, path = tempfile.mkstemp(
                prefix="codex-img-",
                suffix=self._runtime.mime_extension(media_type),
                dir=str(temp_dir),
            )
            f = None
            try:
                f = os.fdopen(fd, "wb")
                f.write(img)
                # Flush explicitly so a write error (e.g. ENOSPC) surfaces
                # HERE and triggers the unlink + re-raise below. Without it
                # the bytes sit in the buffer until close(), whose OSError
                # the finally swallows — staging a silently truncated file
                # the model would then read.
                f.flush()
            except Exception:
                # Best-effort cleanup: never let a close/unlink error mask
                # the original write exception.
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            finally:
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
                else:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            # Files are created 0600 by mkstemp, but chmod to defeat umask.
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self._staged_images.append(Path(path))

    def _clear_staged_images(self) -> None:
        staged = self._staged_images
        self._staged_images = []
        self._cleanup_images(staged)

    @staticmethod
    def _cleanup_images(paths: List[Path]) -> None:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    async def _send_request(self, method: str, params: Dict[str, Any]) -> Any:
        """Send a JSON-RPC request and await its matching response.

        The stdout reader dispatches the response (matched by ``id``) to the
        per-request Future. Notifications that arrive before the response are
        placed on the notification queue and never re-queued ahead of the
        response.
        """
        if method == "turn/start":
            self.check_delivery_admission()
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("codex app-server is not running")
        self._jsonrpc_id += 1
        req_id = self._jsonrpc_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        payload = (json.dumps(request) + "\n").encode("utf-8")
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending_requests.pop(req_id, None)
            self._last_error = f"codex {method} write failed: {exc}"
            raise RuntimeError(self._last_error) from exc
        try:
            response = await asyncio.wait_for(future, timeout=self._runtime.startup_grace_s())
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            self._last_error = f"codex {method} timed out"
            raise RuntimeError(self._last_error)
        if "error" in response:
            error = response["error"]
            self._last_error = f"codex {method} error: {error}"
            code = error.get("code") if isinstance(error, dict) else None
            raise ProviderRpcError(self._last_error, code=code if type(code) is int else None)
        return response.get("result")

    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no ``id``, no response expected)."""
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("codex app-server is not running")
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        payload = (json.dumps(notification) + "\n").encode("utf-8")
        proc.stdin.write(payload)
        await proc.stdin.drain()

    async def _handle_server_request(self, record: Dict[str, Any]) -> None:
        """Dispatch a server→client JSON-RPC request.

        The app-server asks the user a question via
        ``item/tool/requestUserInput`` (legacy ``tool/requestUserInput``),
        blocking the turn until we answer. We stash the request and forward a
        synthetic notification so the adapter emits the approval card; the
        answer arrives via :meth:`answer_pending_question`. Any other server
        request is rejected with method-not-found so the app-server does not
        hang waiting for a response we would never send.
        """
        method = record.get("method")
        req_id = record.get("id")
        if not self.accepts_notification(record):
            # A request already in transit when Stop was pressed must not
            # reopen a card or keep the interrupted turn blocked.
            await self._send_jsonrpc_response(
                req_id, error={"code": -32600, "message": "turn interrupted"}
            )
            return
        if method in self._runtime.question_methods() and isinstance(req_id, (int, str)):
            params = record.get("params")
            if isinstance(params, dict):
                # Auto-dismiss a request whose questions are ALL skipped by
                # the adapter (e.g. every question lacks an id or prompt):
                # no approval card is emitted, so nothing can answer this
                # blocking request and the turn would hang until Stop. Reply
                # with the same empty-answers dismissal payload ``answer_pending_question``
                # uses, and do NOT stash it (there is no card to resolve).
                # A free-text question with null/empty options is actionable.
                if not self._runtime.normalize_questions(params.get("questions")):
                    await self._send_jsonrpc_response(req_id, result={"answers": {}})
                    return
                self._pending_questions[req_id] = params
                # Forward as a notification (no ``id``) so the adapter's
                # ``_normalize_notification`` maps it to the approval card.
                await self._notification_queue.put({"method": method, "params": params})
                return
        await self._send_jsonrpc_response(
            req_id,
            error={"code": -32601, "message": f"method not found: {method}"},
        )

    async def _send_jsonrpc_response(
        self,
        req_id: Any,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write a JSON-RPC response for a server→client request."""
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("question response transport is unavailable")
        response: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result if result is not None else {}
        payload = (json.dumps(response) + "\n").encode("utf-8")
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The app-server is gone; nothing to answer.
            raise RuntimeError("question response delivery is uncertain") from None

    async def answer_pending_question(
        self,
        answers: List[Dict[str, Any]],
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        """Answer a pending ``requestUserInput`` question.

        Maps the composer's ``[{questionId, selected: [labels]}]`` payload to
        the app-server's ``{answers: {questionId: {answers: [labels]}}}``
        shape and responds only to matching pending question requests. When
        ``expected_call_id`` is supplied, only the provider request whose
        ``itemId`` produced that exact card can be consumed.
        An empty answers list (dismissal) yields ``{"answers": {}}``.
        Returns ``False`` when no question is pending so the caller can
        deliver the text normally.
        """
        if not self._pending_questions:
            return False
        codex_answers: Dict[str, Dict[str, List[str]]] = {}
        for entry in answers:
            if not isinstance(entry, dict):
                continue
            question_id = entry.get("questionId")
            selected = entry.get("selected")
            if not isinstance(question_id, str) or not isinstance(selected, list):
                continue
            values = [value for value in selected if isinstance(value, str)]
            codex_answers[question_id] = {"answers": values}
        responses: List[Tuple[Any, Dict[str, Any]]] = []
        for req_id, params in list(self._pending_questions.items()):
            item_id = params.get("itemId")
            call_id = str(item_id) if item_id is not None else "request_user_input"
            if expected_call_id is not None and call_id != expected_call_id:
                continue
            ids = {
                question["id"]
                for question in self._runtime.normalize_questions(params.get("questions"))
            }
            if answers and (not ids or not ids <= codex_answers.keys()):
                continue
            self._pending_questions.pop(req_id)
            responses.append(
                (
                    req_id,
                    {"answers": {key: value for key, value in codex_answers.items() if key in ids}},
                )
            )
        # Claim matching requests before yielding so concurrent clicks cannot
        # answer a request twice or dismiss an unrelated question.
        for req_id, result in responses:
            await self._send_jsonrpc_response(req_id, result=result)
        return bool(responses)

    async def read_line(self) -> Optional[Dict[str, Any]]:
        """Await one server notification, or ``None`` on EOF."""
        while True:
            record = await self._notification_queue.get()
            if record is None or not self._capture_cancel_notification(record):
                break
        if record is None or not self.accepts_notification(record):
            return record
        params = record.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        is_child = (
            isinstance(thread_id, str) and bool(thread_id) and thread_id != self.active_thread_id
        )
        if not is_child:
            if record.get("method") == "turn/started":
                # Claim the consumed start before handing it to the tailer. Stop
                # can run as soon as this read_line task completes; keeping the
                # provider id and guard in one synchronous handoff prevents that
                # boundary from looking idle while the provider is already live.
                turn_id = self._notification_turn_id(record)
                previous_turn_id = self._provider_turn_id
                previous_retired = previous_turn_id in self._retired_provider_turn_ids
                provider_initiated = not self._turn_in_flight
                successor = (
                    previous_turn_id is not None
                    and turn_id != previous_turn_id
                    and not (previous_retired and self._turn_start_request_pending)
                )
                self._provider_turn_id = turn_id
                if self._turn_start_request_pending:
                    self._turn_start_claimed_while_pending = True
                    self._turn_start_claimed_id_while_pending = turn_id
                if provider_initiated or successor:
                    # A native continuation may outlive a failed interrupt. It
                    # cannot use the ordinary kill/restart fallback, which could
                    # resume the same provider-owned execution and report a false
                    # successful Stop.
                    self._native_stop_requires_confirmation = True
                if provider_initiated:
                    self._begin_turn()
            elif record.get("method") == "turn/completed":
                completed_turn_id = self._notification_turn_id(record)
                self._remember_retired_turn(record)
                # A normally consumed completion closes its own provider-start
                # confirmation requirement. An older completion arriving after
                # a successor start must not downgrade that successor to the
                # ordinary kill/restart cancellation path. Completions captured
                # by Stop never reach this branch; the cancelling operation
                # clears the flag only after its own confirmation succeeds.
                if completed_turn_id == self._provider_turn_id:
                    self._native_stop_requires_confirmation = False
                    self._delivery_uncertain = False
        return record

    @staticmethod
    def _notification_turn_id(record: Dict[str, Any]) -> Optional[str]:
        params = record.get("params", {})
        if not isinstance(params, dict):
            return None
        turn = params.get("turn")
        value = turn.get("id") if isinstance(turn, dict) else params.get("turnId")
        return value if isinstance(value, str) else None

    def accepts_notification(self, record: Dict[str, Any]) -> bool:
        method = record.get("method")
        if isinstance(method, str) and method.startswith(("thread/goal/", "goal/")):
            return True
        turn_id = self._notification_turn_id(record)
        return not (
            (self._discard_turn_id and turn_id == self._discard_turn_id)
            or turn_id in self._discarded_provider_turn_ids
            or (
                record.get("method") == "turn/started"
                and turn_id in self._retired_provider_turn_ids
            )
        )

    def _remember_retired_turn(self, record: Dict[str, Any]) -> None:
        turn_id = self._notification_turn_id(record)
        if turn_id and turn_id not in self._retired_provider_turn_ids:
            self._retired_provider_turn_ids.append(turn_id)

    async def _handle_notification(self, record: Dict[str, Any]) -> None:
        if self._capture_cancel_notification(record):
            return
        if not self.accepts_notification(record):
            if record.get("method") == "turn/completed":
                # cancel_active_turn already reclaimed the cancelled turn's
                # images. A duplicate completion may arrive after the next
                # turn owns _inflight_images, so it must only confirm Stop.
                self._remember_retired_turn(record)
                self._interrupted.set()
            return
        self._handshake_complete = True
        params = record.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        is_child = (
            isinstance(thread_id, str)
            and bool(thread_id)
            and bool(self.active_thread_id)
            and thread_id != self.active_thread_id
        )
        if record.get("method") == "turn/completed" and not is_child:
            # Keep the turn guard until the tailer consumes completion.
            inflight = self._inflight_images
            self._inflight_images = []
            self._cleanup_images(inflight)
        await self._notification_queue.put(record)

    async def _drain_stdout(self) -> None:
        """Parse stdout lines and dispatch responses vs requests vs notifications.

        Records with an ``id`` but no ``method`` are JSON-RPC responses:
        resolve the matching pending Future. Records with both an ``id`` and a
        ``method`` are server→client requests (e.g. a blocking question):
        answer or reject them so the app-server never hangs. Records without
        an ``id`` are notifications: place them on the notification queue for
        the tailer.
        """
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        # Read in chunks and split on newlines manually — ``readline()``
        # enforces a 64 KiB line limit that large JSON-RPC responses (e.g.
        # tool results) can exceed.
        buffer = b""
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", errors="ignore").strip()
                    if not text:
                        continue
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if "id" in record:
                        if "method" in record:
                            # Server→client request (e.g. a blocking
                            # question): answer or reject it so the app-server
                            # never hangs waiting for a response.
                            await self._handle_server_request(record)
                        else:
                            # Response to one of our requests: resolve the
                            # matching pending Future.
                            req_id = record.get("id")
                            if isinstance(req_id, int):
                                future = self._pending_requests.pop(req_id, None)
                                if future is not None and not future.done():
                                    future.set_result(record)
                    else:
                        await self._handle_notification(record)
            # Process any trailing data without a newline.
            if buffer:
                text = buffer.decode("utf-8", errors="ignore").strip()
                if text:
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        record = None
                    if isinstance(record, dict):
                        if "id" in record:
                            if "method" in record:
                                await self._handle_server_request(record)
                            else:
                                req_id = record.get("id")
                                if isinstance(req_id, int):
                                    future = self._pending_requests.pop(req_id, None)
                                    if future is not None and not future.done():
                                        future.set_result(record)
                        else:
                            await self._handle_notification(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._runtime.logger().exception("codex native transport stdout drain failed")
        finally:
            # If the app-server died mid-turn (EOF or exception before
            # turn/completed), the in-flight image temp files will never be
            # reclaimed by the turn/completed handler. Clean them up here so
            # they do not leak until stop(). Staged images (not yet sent) are
            # also abandoned; a later user send supplies its own attachments.
            inflight = self._inflight_images
            self._inflight_images = []
            self._cleanup_images(inflight)
            staged = self._staged_images
            self._staged_images = []
            self._cleanup_images(staged)
            # Signal EOF to notification consumers.
            await self._notification_queue.put(None)
            # Fail any still-pending requests.
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(RuntimeError("codex app-server exited before response"))
            self._pending_requests.clear()
