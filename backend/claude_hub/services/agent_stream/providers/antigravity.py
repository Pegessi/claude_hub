"""Persistent stream-json transport for the Antigravity CLI (``agy``).

A Chat keeps one ``agyx --input-format stream-json --output-format stream-json``
process and submits one newline-terminated user record per turn. ``result`` ends
that turn, not the process; keeping stdin open avoids repeating CLI sign-in,
model discovery and workspace initialization. ``--conversation`` is needed only
when replacing the process after Stop or a launch-configuration change.

``agyx`` is the existing local proxy/TLS wrapper; Hub does not manage its shared
proxy or read the CLI's credentials or private conversation database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ....models import ChatMode, ManagedSession, StreamModeOption
from .process import ProcessSession

_IDLE_EXIT_GRACE_SECONDS = 2.0
# agy reports its conversation id in the startup ``init`` record. Cold starts
# include sign-in, model discovery and workspace refresh, so allow a full
# provider startup budget without hanging the transport forever.
_ROTATION_INIT_TIMEOUT_SECONDS = 120.0
# Provider death must release the rotation wait promptly: the send holds the
# tailer's send lock, so a 120 s wait would also delay the failure notice and
# Stop handling.
_ROTATION_INIT_POLL_SECONDS = 0.1


class AntigravitySession(ProcessSession):
    """One serial stream-json conversation per live Chat transport."""

    adapter_id = "antigravity-native"
    schema_version = 1

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
        seed_history: Optional[str] = None,
        on_seed_consumed: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> None:
        super().__init__(
            session,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        # The push consumer and the first send may both start the transport.
        self._start_lock = asyncio.Lock()
        self._launch_fingerprint: Optional[str] = None
        # A launch resumes an existing conversation only when it passes a
        # provider-issued id. A fresh process is reusable for Dot rotation.
        self._launch_resume_id: Optional[str] = None
        self._turns_on_process = 0
        self._rotation_init: Optional[asyncio.Future[str]] = None
        # A rotation captures a provider id before Dot associates it. Defer
        # the tab persistence until adoption so a rejected candidate cannot
        # become the resumable identity.
        self._defer_rotation_persist = False
        self._rotation_epoch = 0
        self._init_epoch = 0
        # Stable identity to restore when a prepared rotation is rejected.
        self._rotation_previous_id: Optional[str] = None
        self._rotation_previous_verified = False
        # While replacing a conversation for Dot rotation, an id captured by
        # the retired process must not turn the new launch into a resume.
        self._force_fresh_launch = False
        self._staged_images: List[Path] = []
        self._inflight_images: List[Path] = []

    def accepts_notification(self, record: Dict[str, Any]) -> bool:
        # The CLI runs one input line at a time and ends it with ``result``;
        # SessionCore keeps the next line gated until that result is consumed.
        return True

    def _configuration_fingerprint(self, provider_env: Dict[str, str]) -> str:
        # A conversation id learned from init is not a launch-setting change.
        # Hash env values rather than keeping another copy of credentials.
        encoded = json.dumps(
            {
                "env": provider_env,
                "mode": self._current_mode,
                "solo": self.session.solo_mode,
                "cwd": self._cwd,
            },
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def start(self) -> None:
        async with self._start_lock:
            if (
                self._started
                and self._process is not None
                and self._process.returncode is None
                and self._reader_task is not None
                and not self._reader_task.done()
            ):
                return
            self._started = False
            self._handshake_complete = False
            if self._process is not None or self._reader_task is not None:
                await self._terminate_process()
            # Record what this launch resumes, not what init later learns: only
            # a process launched without a resumed id is a fresh conversation.
            # Compute it in the same synchronous stretch as ``_build_command``.
            provider_env = await self._build_provider_env()
            resume = self._resume_arg()
            self._launch_resume_id = resume[1] if resume else None
            self._turns_on_process = 0
            fingerprint = self._configuration_fingerprint(provider_env)
            cmd = self._build_command(provider_env)
            self._stderr_buffer = b""
            self._exit_error = None
            try:
                self._process = await self._spawn_subprocess(cmd, provider_env)
            except FileNotFoundError:
                self._last_error = f"provider binary not found: {cmd[0]}"
                raise
            except OSError as exc:
                self._last_error = f"failed to launch provider: {exc}"
                raise
            # The shared NDJSON reader also handles a persistent stream: its
            # generation changes on process replacement, never at turn end.
            self._reader_task = asyncio.create_task(
                self._drain_oneshot_stdout(self._stdout_generation)
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            self._launch_fingerprint = fingerprint
            self._last_error = None
            self._started = True
            # agy has no independent RPC handshake. It may emit init only
            # after input, so writable stdin is sufficient for send readiness.
            self._handshake_complete = True
            if not self._conversation_id_verified:
                self._hub_runtime_guidance_injected = False
            self._runtime.logger().info(
                "Antigravity CLI started for session %s (pid=%s)",
                self.session.id,
                self._process.pid,
            )

    async def stop(self) -> None:
        async with self._start_lock:
            self._started = False
            self._handshake_complete = False
            self._launch_fingerprint = None
            process = self._process
            try:
                if (
                    not self.turn_in_flight
                    and process is not None
                    and process.returncode is None
                    and process.stdin is not None
                ):
                    # Retire before EOF so the consumer cannot treat this
                    # intentional idle shutdown as a provider crash.
                    self._invalidate_stdout_stream()
                    try:
                        process.stdin.close()
                        await asyncio.wait_for(process.wait(), timeout=_IDLE_EXIT_GRACE_SECONDS)
                    except (asyncio.TimeoutError, OSError):
                        pass
            finally:
                self._clear_staged_images()
                # Active cancellation skips EOF (which would wait for the
                # turn). The shared teardown also reaps surviving children
                # after a graceful exit or an expired idle grace period.
                await super().stop()

    def _end_turn(self) -> None:
        inflight = self._inflight_images
        self._inflight_images = []
        self._cleanup_images(inflight)
        super()._end_turn()

    async def _prepare_for_send(self) -> None:
        if self.turn_in_flight:
            return
        await self.start()
        provider_env = await self._build_provider_env()
        if self._configuration_fingerprint(provider_env) != self._launch_fingerprint:
            # Model/mode/permissions are process flags; stream-json does not
            # support runtime control messages. Resume only between turns.
            self._runtime.logger().info(
                "Restarting Antigravity CLI after launch configuration changed for session %s",
                self.session.id,
            )
            await self.restart_for_recovery()

    async def cancel_active_turn(self) -> None:
        if self.turn_in_flight:
            # Closing stdin waits for the current turn rather than cancelling
            # it. Reuse the shared bounded process-group teardown and resume.
            await self.restart_for_recovery()

    def available_modes(self) -> List[StreamModeOption]:
        # The probe takes the binary name (agyx), not the provider id.
        if self._runtime.help_confirms_plan_mode("agyx", "--mode"):
            return [self._runtime.default_mode(), self._runtime.plan_mode()]
        return [self._runtime.default_mode()]

    def _resume_arg(self) -> List[str]:
        """agy's ``--conversation <id>`` only accepts an id the CLI itself
        issued (captured from ``init``); never pass an unverified id."""
        if self._force_fresh_launch:
            return []
        sid = self._conversation_id
        if sid and self._conversation_id_verified:
            return ["--conversation", sid]
        return []

    async def rotate_conversation(self) -> str:
        """Start a fresh provider conversation and return its verified id.

        agy issues ids only in its startup ``init`` record, so a rotation uses
        the fresh process already launched for this turn when it has served no
        turn, otherwise replaces a resumed/used process and waits for its init.
        Waiting happens now, before the prompt is written, so Dot can associate
        a real provider id instead of inventing a non-resumable one.
        """
        if not self._defer_rotation_persist:
            # Keep the stable identity across an abandoned rotation so a
            # rejected candidate can be discarded without losing the old one.
            self._rotation_previous_id = self._conversation_id
            self._rotation_previous_verified = self._conversation_id_verified
        self._rotation_epoch += 1
        if not self._has_unused_fresh_process():
            await self._replace_process_for_rotation()
            # The consumer may have started the replacement and captured its
            # init while this task was replacing the process.
            accept_cached = self._init_epoch == self._rotation_epoch
            return await self._await_rotation_conversation_id(accept_cached=accept_cached)
        self._defer_rotation_persist = True
        return await self._await_rotation_conversation_id()

    def adopt_rotated_conversation(self, conversation_id: str) -> None:
        # ``init`` already verified this provider-issued id; commit it as the
        # resumable identity only now that Dot accepted the association.
        if not self._conversation_id_verified or self._conversation_id != conversation_id:
            raise RuntimeError(f"{self.adapter_id} has no matching prepared rotation")
        self._adopt_rotated_conversation_id(conversation_id)
        self._defer_rotation_persist = False
        self._rotation_previous_id = None
        self._rotation_previous_verified = False

    async def discard_rotated_conversation(self, conversation_id: str) -> None:
        # Dot rejected the candidate: retire its process and restore the
        # previous identity instead of making the candidate resumable.
        if not self._defer_rotation_persist or self._conversation_id != conversation_id:
            return
        await self._abort_rotation_process()
        self._restore_rotation_identity()

    def _restore_rotation_identity(self) -> None:
        self._conversation_id = self._rotation_previous_id
        self._conversation_id_verified = self._rotation_previous_verified
        self._force_fresh_launch = False
        self._rotation_previous_id = None
        self._rotation_previous_verified = False
        if self._conversation_id and self._conversation_id_verified:
            self._persist_conversation_id(self._conversation_id, force=True)

    def _has_unused_fresh_process(self) -> bool:
        return (
            self._started
            and self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
            and self._launch_resume_id is None
            and self._turns_on_process == 0
        )

    async def _replace_process_for_rotation(self) -> None:
        # ``_terminate_process`` (unlike ``stop``) does not end the turn that is
        # already in flight; staged images belong to that turn and stay.
        await self._abort_rotation_process()
        # Set after abort (which clears it) and before start, so the
        # replacement's init is deferred until Dot adopts the candidate.
        self._defer_rotation_persist = True
        await self.start()

    async def _abort_rotation_process(self) -> None:
        # Serialize with start(): the consumer may notice ``_started`` drop and
        # launch a replacement while this teardown is in flight.
        async with self._start_lock:
            self._started = False
            self._handshake_complete = False
            self._launch_fingerprint = None
            # This teardown is deliberate: an EOF observed by the consumer is
            # a replacement, not a fatal provider death.
            self._client_requested_stop = True
            self._force_fresh_launch = True
            self._defer_rotation_persist = False
            self._conversation_id = None
            self._conversation_id_verified = False
            self._launch_resume_id = None
            self._turns_on_process = 0
            await self._terminate_process()
            # A record dispatched from the retired process must not reinstate
            # its conversation id before the fresh launch reads it.
            self._conversation_id = None
            self._conversation_id_verified = False

    async def _await_rotation_conversation_id(self, *, accept_cached: bool = True) -> str:
        if accept_cached and self._conversation_id and self._conversation_id_verified:
            self._force_fresh_launch = False
            return self._conversation_id
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[str] = loop.create_future()
        self._rotation_init = ready
        deadline = loop.time() + _ROTATION_INIT_TIMEOUT_SECONDS
        try:
            # ``init`` may have been captured between the check and the future.
            if accept_cached and self._conversation_id and self._conversation_id_verified:
                self._force_fresh_launch = False
                return self._conversation_id
            while not ready.done():
                if not self._rotation_process_alive():
                    detail = (
                        self._exit_error
                        or "Antigravity CLI exited before reporting a conversation id"
                    )
                    # Retire the dead process's EOF so the consumer does not
                    # additionally mark the whole session hard-failed; the
                    # next send/consumer start begins from a clean process.
                    await self._abort_rotation_process()
                    self._restore_rotation_identity()
                    raise RuntimeError(detail)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self._abort_rotation_process()
                    self._restore_rotation_identity()
                    raise RuntimeError("Antigravity CLI did not report a conversation id") from None
                await asyncio.wait({ready}, timeout=min(_ROTATION_INIT_POLL_SECONDS, remaining))
            return ready.result()
        finally:
            if self._rotation_init is ready:
                self._rotation_init = None

    def _rotation_process_alive(self) -> bool:
        process = self._process
        return (
            self._started
            and process is not None
            and process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    def _build_command(self, provider_env: Optional[Dict[str, str]] = None) -> List[str]:
        is_plan = self._current_mode == ChatMode.PLAN.value
        cmd = [
            "agyx",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ]
        # Slash-command expansion is irrelevant to headless Hub turns, but agy
        # explicitly makes ``--mode plan`` a no-op when it is disabled (it
        # warns and runs unrestricted), so only suppress it outside plan mode.
        if not is_plan:
            cmd.append("--disable-slash-commands")
        if self.session.solo_mode and not is_plan:
            cmd.append("--dangerously-skip-permissions")
        if is_plan:
            cmd.extend(["--mode", "plan"])
        if provider_env:
            model = provider_env.get("ANTIGRAVITY_MODEL", "").strip()
            if model:
                cmd.extend(["--model", model])
        cmd.extend(self._resume_arg())
        return cmd

    def _build_stdin(self, text: str) -> str:
        return json.dumps({"event": "user", "message": {"content": text}})

    async def _build_provider_env(self) -> Dict[str, str]:
        """Skip Hub's direct/proxy route selection.

        agyx owns the network path itself (it starts the local eligibility
        bypass proxy that chains to the deployment egress and pins
        SSL_CERT_FILE), so the generic endpoint resolver — which only knows
        claude/codex/cursor/maso — must not reject the provider. Session env
        overrides (e.g. ANTIGRAVITY_MODEL) still apply via ``_build_env``.
        """
        return self._build_env()

    async def _send_text(self, text: str) -> None:
        try:
            await self.start()
            self.check_delivery_admission()
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("Antigravity CLI stdin is unavailable")
            image_paths = self._staged_images
            self._staged_images = []
            if image_paths:
                text = self._runtime.wrap_image_guidance(text, image_paths)
            self._inflight_images = image_paths
            try:
                # EOF is reserved for shutdown; each complete NDJSON line starts
                # one turn in the already initialized CLI conversation.
                process.stdin.write((self._build_stdin(text) + "\n").encode("utf-8"))
                await process.stdin.drain()
                self._turns_on_process += 1
            except OSError as exc:
                # Delivery may have begun. Do not resend implicitly, and retire
                # the stream so a partial submission cannot leak into a retry.
                self._client_requested_stop = True
                await self.stop()
                self._last_error = "Antigravity CLI stdin closed while sending a message"
                raise RuntimeError(self._last_error) from exc
        except asyncio.CancelledError:
            self._clear_staged_images()
            self._end_turn()
            raise

    def _stage_images(self, images: List[bytes]) -> None:
        """Stage images to temp files referenced by path in the next prompt.

        Antigravity's stream-json user message carries a string ``content``
        field, so attached images are staged inside the app-owned 0700 image
        temp directory and referenced by absolute path in a sentinel-wrapped
        prompt block for the CLI's multimodal ``read_file`` tool. Temp files
        are deleted when the turn ends (completion, cancellation, or error)
        and on ``stop``.
        """
        if not images:
            return
        temp_dir = self._runtime.cursor_image_temp_dir()
        for img in images:
            media_type = self._runtime.detect_image_mime(img)
            if media_type is None:
                raise ValueError("unsupported image format or not an image")
            fd, path = tempfile.mkstemp(
                prefix="agy-img-",
                suffix=self._runtime.mime_extension(media_type),
                dir=str(temp_dir),
            )
            f = None
            try:
                f = os.fdopen(fd, "wb")
                f.write(img)
                f.flush()
            except Exception:
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

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Capture the conversation id from agy's ``init`` record."""
        if record.get("event") != "init":
            return
        sid = record.get("conversation_id")
        if isinstance(sid, str) and sid:
            if self._defer_rotation_persist:
                # Dot must associate and adopt this candidate before it becomes
                # the tab's resumable identity.
                self._conversation_id = sid
                self._conversation_id_verified = True
            else:
                self._persist_conversation_id(sid)
            self._handshake_complete = True
            self._init_epoch = self._rotation_epoch
            # This process now has a valid id, so later launches may resume it.
            self._force_fresh_launch = False
            ready = self._rotation_init
            if ready is not None and not ready.done():
                ready.set_result(sid)
