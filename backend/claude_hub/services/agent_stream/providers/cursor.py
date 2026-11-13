"""CursorSession implementation via the persistent ``agent acp`` JSON-RPC server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ....models import ChatMode, ManagedSession, StreamModeOption
from ..provider_network import ProviderNetworkError
from .contracts import ProviderRpcError
from .process import ProcessSession

# Cold ACP session initialization loads Cursor's server config, managed skills,
# and model catalog once per persistent process, which can exceed the 10s
# one-shot handshake grace over a corporate proxy.
_ACP_SESSION_INIT_TIMEOUT_S = 60.0
_ACP_CANCEL_CONFIRM_TIMEOUT_S = 10.0

_CONTENT_POLICY_REFUSAL_SUBSTR = "The provider refused to serve this request based on the content"
_CONTENT_POLICY_HINT = (
    " (Cursor/upstream model content safety filter rejected this prompt or context — "
    "not a local login expiry; switch models or start a fresh session to trim context)"
)


def _cursor_config_root(env: Dict[str, str], cwd: str) -> Path:
    """Resolve Cursor's config directory matching ``cursor-config/dist/paths.js``."""
    explicit = env.get("CURSOR_CONFIG_DIR")
    if isinstance(explicit, str) and explicit.strip():
        return Path(explicit.strip()).expanduser()
    xdg = env.get("XDG_CONFIG_HOME")
    if isinstance(xdg, str) and xdg.strip():
        return Path(xdg.strip()).expanduser() / "cursor"
    home = Path(env.get("HOME") or str(Path.home())).expanduser()
    cursor_home = env.get("CURSOR_HOME")
    if isinstance(cursor_home, str) and cursor_home.strip():
        raw = cursor_home.strip()
        if raw.startswith("~/"):
            return home / raw[2:]
        path = Path(raw)
        return path if path.is_absolute() else Path(cwd or ".") / path
    return home / ".cursor"


def _migrate_legacy_cli_store_for_acp(sid: str, cwd: str, env: Dict[str, str]) -> bool:
    """Copy a verified legacy ``~/.cursor/chats/<md5(cwd)>/.../store.db`` into
    ``<config_root>/acp-sessions/<sid>/`` when upgrading an existing Chat tab.

    ``agent --print --resume <sid>`` and ``agent acp`` share the exact same
    SQLite blob-store format (``store.db`` + ``meta.json``), differing only in
    their parent directory.  Never follows symlinks or overwrites an existing
    ACP store.
    """
    if not sid or not cwd or "/" in sid or "\\" in sid or sid in {".", ".."}:
        return False
    from ..._cursor_verify import _cwd_hash, _read_agent_id_from_db

    config_root = _cursor_config_root(env, cwd)
    acp_sessions = config_root / "acp-sessions"
    target_dir = acp_sessions / sid
    target_db = target_dir / "store.db"
    if target_db.exists():
        return True

    home = Path(env.get("HOME") or str(Path.home())).expanduser()
    cursor_home_raw = env.get("CURSOR_HOME")
    if isinstance(cursor_home_raw, str) and cursor_home_raw.strip():
        raw = cursor_home_raw.strip()
        chats_root = (
            home / raw[2:]
            if raw.startswith("~/")
            else (Path(raw) if Path(raw).is_absolute() else Path(cwd) / raw)
        )
    else:
        chats_root = home / ".cursor"
    data_dir = env.get("CURSOR_DATA_DIR")
    if isinstance(data_dir, str) and data_dir.strip():
        try:
            if Path(data_dir.strip()).resolve() != chats_root.resolve():
                return False
        except OSError:
            return False

    bucket = chats_root / "chats" / _cwd_hash(cwd)
    try:
        if not bucket.is_dir() or bucket.is_symlink():
            return False
        matched_db: Optional[Path] = None
        for index, chat_dir in enumerate(bucket.iterdir()):
            if index >= 1000:
                return False
            db = chat_dir / "store.db"
            if chat_dir.is_symlink() or db.is_symlink() or not db.is_file():
                continue
            if db.resolve().parent.parent != bucket.resolve():
                continue
            if _read_agent_id_from_db(db) == sid:
                if matched_db is not None:
                    # Ambiguous duplicate stores — fail closed.
                    return False
                matched_db = db
        if matched_db is None:
            return False
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_db = target_dir / "store.db.migrate-tmp"
        shutil.copy2(matched_db, tmp_db)
        tmp_db.replace(target_db)
        meta_path = target_dir / "meta.json"
        if not meta_path.exists():
            real_cwd = os.path.realpath(cwd)
            meta_path.write_text(
                json.dumps({"schemaVersion": 1, "cwd": real_cwd}),
                encoding="utf-8",
            )
        return True
    except OSError:
        return False


def _format_acp_error_text(text: str) -> str:
    cleaned = text.strip()
    if _CONTENT_POLICY_REFUSAL_SUBSTR in cleaned and _CONTENT_POLICY_HINT not in cleaned:
        return f"{cleaned}{_CONTENT_POLICY_HINT}"
    return cleaned


class CursorSession(ProcessSession):
    """Cursor Agent native persistent transport via ``agent acp`` JSON-RPC stdio.

    ``start`` launches ``agent --trust [--yolo] [--model <slug>] acp``, runs the
    ACP ``initialize`` handshake, and creates (``session/new``) or loads
    (``session/load``) the session so ``sharedServices``, skills, server config,
    and HTTP/2 connection pools stay warm across turns.

    Images are passed directly as base64 ``{type: "image", data, mimeType}``
    blocks in ``session/prompt``, and ``cursor/ask_question`` /
    ``session/request_permission`` server->client RPCs are answered in-turn via
    :meth:`answer_pending_question`.
    """

    adapter_id = "cursor-native"
    schema_version = 1

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
        seed_history: Optional[str] = None,
        on_seed_consumed: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(
            session,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        self._start_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._jsonrpc_id = 0
        self._acp_session_id: Optional[str] = None
        self._launch_fingerprint: Optional[str] = None
        self._applied_mode: Optional[str] = None
        self._replaying_history = False
        self._suppress_active_turn_updates = False
        self._prompt_task: Optional[asyncio.Task[None]] = None
        self._pending_requests: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        self._pending_questions: Dict[Any, Dict[str, Any]] = {}
        self._pending_permissions: Dict[Any, Dict[str, Any]] = {}
        self._notification_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
        self._staged_images: List[Tuple[str, str]] = []
        self._pending_rotation: Optional[str] = None
        self._defer_initial_adoption = False
        self._turn_error_emitted = False

    def _selected_model(self) -> Optional[str]:
        cursor_model = self.session.env.get("CURSOR_MODEL")
        if isinstance(cursor_model, str) and cursor_model.strip():
            return cursor_model.strip()
        return None

    def current_model(self) -> Optional[str]:
        return self._selected_model()

    def _build_command(self) -> List[str]:
        cmd = ["agent", "--trust"]
        model = self._selected_model()
        if model:
            cmd.extend(["--model", model])
        # Plan mode enforces read-only via session/set_mode ("plan"); keep
        # --yolo enabled only when solo_mode is active in default mode.
        if self.session.solo_mode and self._current_mode == ChatMode.DEFAULT.value:
            cmd.append("--yolo")
        cmd.append("acp")
        return cmd

    def available_modes(self) -> List[StreamModeOption]:
        if self._runtime.help_confirms_plan_mode("agent", "--mode"):
            return [self._runtime.default_mode(), self._runtime.plan_mode()]
        return [self._runtime.default_mode()]

    def _configuration_fingerprint(self) -> str:
        effective_solo = (
            bool(self.session.solo_mode) and self._current_mode == ChatMode.DEFAULT.value
        )
        material = json.dumps(
            {
                "network": self._provider_network_fingerprint(),
                "model": self._selected_model(),
                "solo": effective_solo,
                "cwd": self._cwd,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _prepare_for_send(self) -> None:
        if self._turn_in_flight:
            return
        if not self._started or self._process is None:
            return
        current = self._configuration_fingerprint()
        if self._launch_fingerprint is None:
            self._launch_fingerprint = current
            return
        if current == self._launch_fingerprint:
            return
        await self._build_provider_env()
        await self.restart_for_recovery()

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

            if self._process is not None or self._reader_task is not None:
                await self._terminate_process()
            self._started = False
            self._acp_session_id = None
            self._applied_mode = None

            cmd = self._build_command()
            for attempt in range(2):
                self._notification_queue = asyncio.Queue()
                fingerprint = self._configuration_fingerprint()
                provider_env = await self._build_provider_env()
                if self._configuration_fingerprint() != fingerprint:
                    if attempt == 0:
                        continue
                    self._last_error = (
                        "provider network configuration changed repeatedly during preflight"
                    )
                    raise ProviderNetworkError(self._last_error)
                try:
                    self._process = await self._spawn_subprocess(cmd, provider_env)
                except FileNotFoundError:
                    self._last_error = f"provider binary not found: {cmd[0]}"
                    raise
                except OSError as exc:
                    self._last_error = f"failed to launch provider: {exc}"
                    raise
                self._stderr_buffer = b""
                self._exit_error = None
                self._started = True
                self._reader_task = asyncio.create_task(self._drain_stdout())
                self._stderr_task = asyncio.create_task(self._drain_stderr())
                try:
                    await self._initialize_protocol()
                except Exception as exc:
                    self._last_error = f"cursor acp initialization failed: {exc}"
                    self._started = False
                    await self._terminate_process()
                    raise RuntimeError(self._last_error) from exc
                if self._configuration_fingerprint() != fingerprint:
                    self._started = False
                    await self._terminate_process()
                    if attempt == 0:
                        continue
                    self._last_error = (
                        "provider network configuration changed repeatedly during startup"
                    )
                    raise ProviderNetworkError(self._last_error)
                try:
                    await self._initialize_session(provider_env)
                except Exception as exc:
                    self._last_error = f"cursor acp session setup failed: {exc}"
                    self._started = False
                    await self._terminate_process()
                    raise RuntimeError(self._last_error) from exc
                self._handshake_complete = True
                self._last_error = None
                self._launch_fingerprint = fingerprint
                return

    async def stop(self) -> None:
        async with self._start_lock:
            self._staged_images = []
            self._pending_rotation = None
            self._pending_questions.clear()
            self._pending_permissions.clear()
            prompt_task = self._prompt_task
            self._prompt_task = None
            if prompt_task is not None and not prompt_task.done():
                prompt_task.cancel()
            await super().stop()
            if prompt_task is not None:
                try:
                    await prompt_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._acp_session_id = None
            self._applied_mode = None
            self._suppress_active_turn_updates = False
            self._delivery_uncertain = False
            self._submission_outcome_uncertain = False

    async def _initialize_protocol(self) -> None:
        init_params = {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "claude-hub", "version": "1.0.0"},
        }
        await self._send_request("initialize", init_params, timeout=self._runtime.startup_grace_s())

    async def _initialize_session(self, provider_env: Dict[str, str]) -> None:
        cwd = os.path.realpath(self._cwd) if self._cwd else os.getcwd()
        if self._conversation_id and self._conversation_id_verified:
            sid = self._conversation_id
            await asyncio.to_thread(_migrate_legacy_cli_store_for_acp, sid, cwd, provider_env)
            self._replaying_history = True
            try:
                await self._send_request(
                    "session/load",
                    {"sessionId": sid, "cwd": cwd, "mcpServers": []},
                    timeout=_ACP_SESSION_INIT_TIMEOUT_S,
                )
                self._acp_session_id = sid
                await self._ensure_session_mode()
                return
            except RuntimeError:
                if self._required_conversation_id is not None:
                    raise
                self._runtime.logger().warning(
                    "cursor acp session/load failed for %s; starting a new session", sid
                )
            finally:
                self._replaying_history = False

        if self._required_conversation_id is not None:
            raise RuntimeError("the original provider conversation could not be resumed")

        resp = await self._send_request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            timeout=_ACP_SESSION_INIT_TIMEOUT_S,
        )
        new_sid = resp.get("sessionId") if isinstance(resp, dict) else None
        if not isinstance(new_sid, str) or not new_sid:
            raise RuntimeError("cursor acp session/new returned no sessionId")
        if self._defer_initial_adoption:
            self._pending_rotation = new_sid
            return
        self._acp_session_id = new_sid
        self._persist_conversation_id(new_sid)
        # Fresh provider conversation needs first-turn Hub runtime guidance.
        self._hub_runtime_guidance_injected = False
        await self._ensure_session_mode()

    async def _ensure_session_mode(self) -> None:
        if not self._acp_session_id:
            return
        acp_mode = "plan" if self._current_mode == ChatMode.PLAN.value else "agent"
        if self._applied_mode == acp_mode:
            return
        await self._send_request(
            "session/set_mode",
            {"sessionId": self._acp_session_id, "modeId": acp_mode},
            timeout=self._runtime.startup_grace_s(),
        )
        self._applied_mode = acp_mode

    async def set_mode(self, mode: str) -> None:
        previous_mode = self._current_mode
        await super().set_mode(mode)
        if self._started and self._process is not None and self._acp_session_id:
            # Switching into/out of plan mode when solo_mode is True changes
            # whether --yolo may be active on the underlying ACP server.
            if self.session.solo_mode and (
                (previous_mode == ChatMode.PLAN.value) != (mode == ChatMode.PLAN.value)
            ):
                await self.restart_for_recovery()
            else:
                await self._ensure_session_mode()

    async def rotate_conversation(self) -> str:
        if not self._started or self._process is None:
            defer_initial = not (self._conversation_id and self._conversation_id_verified)
            self._defer_initial_adoption = defer_initial
            try:
                await self.start()
            finally:
                self._defer_initial_adoption = False
            if defer_initial:
                if self._pending_rotation is None:
                    raise RuntimeError("cursor-native initial session/new returned no sessionId")
                return self._pending_rotation
        cwd = os.path.realpath(self._cwd) if self._cwd else os.getcwd()
        resp = await self._send_request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            timeout=_ACP_SESSION_INIT_TIMEOUT_S,
        )
        new_sid = resp.get("sessionId") if isinstance(resp, dict) else None
        if not isinstance(new_sid, str) or not new_sid:
            raise RuntimeError("cursor-native session/new returned no sessionId")
        self._pending_rotation = new_sid
        return new_sid

    def adopt_rotated_conversation(self, conversation_id: str) -> None:
        if self._pending_rotation != conversation_id:
            raise RuntimeError("cursor-native has no matching prepared rotation")
        self._adopt_rotated_conversation_id(conversation_id)
        self._acp_session_id = conversation_id
        self._applied_mode = None
        self._pending_rotation = None

    async def discard_rotated_conversation(self, conversation_id: str) -> None:
        if self._pending_rotation == conversation_id:
            self._pending_rotation = None

    def _stage_images(self, images: List[bytes]) -> None:
        if not images:
            return
        for img in images:
            media_type = self._runtime.detect_image_mime(img)
            if media_type is None:
                raise ValueError("unsupported image format or not an image")
            b64 = base64.b64encode(img).decode("ascii")
            self._staged_images.append((media_type, b64))

    def _clear_staged_images(self) -> None:
        self._staged_images = []

    async def _send_text(self, text: str) -> None:
        if not self._started or self._process is None or not self._acp_session_id:
            await self.start()
        await self._ensure_session_mode()
        self.check_delivery_admission()

        sid = self._acp_session_id
        if not sid:
            raise RuntimeError("cursor acp session is unavailable")

        prompt_text = text
        if self._dot_system_pack and not self._dot_hidden_system_pack_delivered:
            # SessionCore._with_first_turn_prefixes already prepended
            # _dot_system_pack for non-Dot or Dot turns via _dot_prompt_context.
            pass

        prompt_items: List[Dict[str, Any]] = []
        staged = self._staged_images
        self._staged_images = []
        for mime_type, b64_data in staged:
            prompt_items.append({"type": "image", "mimeType": mime_type, "data": b64_data})
        if prompt_text or not prompt_items:
            prompt_items.append({"type": "text", "text": prompt_text})

        self._suppress_active_turn_updates = False
        self._turn_error_emitted = False

        req_id, future = await self._write_request(
            "session/prompt",
            {"sessionId": sid, "prompt": prompt_items},
        )
        self._prompt_task = asyncio.create_task(self._await_prompt_completion(sid, req_id, future))

    async def _await_prompt_completion(
        self,
        sid: str,
        req_id: int,
        future: "asyncio.Future[Dict[str, Any]]",
    ) -> None:
        try:
            response = await future
        except asyncio.CancelledError:
            self._pending_requests.pop(req_id, None)
            raise
        except Exception as exc:
            if not self._suppress_active_turn_updates and self._acp_session_id == sid:
                msg = _format_acp_error_text(str(exc))
                await self._notification_queue.put(
                    {
                        "type": "result",
                        "subtype": "error",
                        "is_error": True,
                        "error": msg,
                        "session_id": sid,
                    }
                )
            return

        if self._suppress_active_turn_updates or self._acp_session_id != sid:
            return

        if "error" in response:
            err_obj = response["error"]
            if isinstance(err_obj, dict):
                detail = err_obj.get("data")
                if isinstance(detail, dict) and isinstance(detail.get("message"), str):
                    err_msg = detail["message"]
                else:
                    err_msg = str(err_obj.get("message") or err_obj)
            else:
                err_msg = str(err_obj)
            err_msg = _format_acp_error_text(err_msg)
            await self._notification_queue.put(
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "error": err_msg,
                    "session_id": sid,
                }
            )
            return

        result = response.get("result")
        stop_reason = result.get("stopReason") if isinstance(result, dict) else "end_turn"
        usage = result.get("usage") if isinstance(result, dict) else None
        if stop_reason == "cancelled":
            await self._notification_queue.put({"type": "turn_ended", "status": "aborted"})
            return
        if self._turn_error_emitted:
            payload: Dict[str, Any] = {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "error": "",
                "session_id": sid,
            }
            if isinstance(usage, dict):
                payload["usage"] = usage
            await self._notification_queue.put(payload)
            return
        payload = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": sid,
        }
        if isinstance(usage, dict):
            payload["usage"] = usage
        await self._notification_queue.put(payload)

    async def cancel_active_turn(self) -> None:
        async with self._cancel_lock:
            if not self._turn_in_flight:
                return
            self._suppress_active_turn_updates = True
            # Reject any in-flight blocking questions or permissions so the
            # server's prompt handler can unwind promptly on session/cancel.
            pending_q_ids = list(self._pending_questions.keys())
            pending_p_ids = list(self._pending_permissions.keys())
            self._pending_questions.clear()
            self._pending_permissions.clear()
            for q_req_id in pending_q_ids:
                try:
                    await self._send_jsonrpc_response(
                        q_req_id, result={"outcome": {"outcome": "cancelled"}}
                    )
                except Exception:
                    pass
            for p_req_id in pending_p_ids:
                try:
                    await self._send_jsonrpc_response(
                        p_req_id, result={"outcome": {"outcome": "cancelled"}}
                    )
                except Exception:
                    pass

            # Drain already-queued notifications from the cancelled turn so
            # stale deltas cannot leak into a subsequent turn.
            while not self._notification_queue.empty():
                item = self._notification_queue.get_nowait()
                if item is None:
                    self._notification_queue.put_nowait(None)
                    break

            prompt_task = self._prompt_task
            restart_required = False
            if (
                self._started
                and self._process is not None
                and self._process.returncode is None
                and self._acp_session_id
                and prompt_task is not None
                and not prompt_task.done()
            ):
                try:
                    await self._send_notification(
                        "session/cancel", {"sessionId": self._acp_session_id}
                    )
                    await asyncio.wait_for(
                        asyncio.shield(prompt_task), timeout=_ACP_CANCEL_CONFIRM_TIMEOUT_S
                    )
                except Exception:
                    self._runtime.logger().warning(
                        "cursor acp session/cancel did not settle within %.1fs; restarting server",
                        _ACP_CANCEL_CONFIRM_TIMEOUT_S,
                    )
                    restart_required = True
            elif prompt_task is None or prompt_task.done():
                pass
            else:
                restart_required = True

            self._prompt_task = None
            self._clear_staged_images()
            self._delivery_uncertain = False
            self._submission_outcome_uncertain = False
            if restart_required:
                try:
                    await self.restart_for_recovery()
                except Exception:
                    self._runtime.logger().exception("cursor acp recovery restart failed")
            self._end_turn()

    async def _write_request(
        self, method: str, params: Dict[str, Any]
    ) -> Tuple[int, "asyncio.Future[Dict[str, Any]]"]:
        if method == "session/prompt":
            self.check_delivery_admission()
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("cursor acp server is not running")
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
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._pending_requests.pop(req_id, None)
            self._last_error = f"cursor acp {method} write failed: {exc}"
            raise RuntimeError(self._last_error) from exc
        return req_id, future

    async def _send_request(
        self, method: str, params: Dict[str, Any], *, timeout: Optional[float] = None
    ) -> Any:
        effective_timeout = timeout if timeout is not None else self._runtime.startup_grace_s()
        req_id, future = await self._write_request(method, params)
        try:
            response = await asyncio.wait_for(future, timeout=effective_timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            self._last_error = f"cursor acp {method} timed out"
            raise RuntimeError(self._last_error) from None
        if "error" in response:
            error = response["error"]
            self._last_error = f"cursor acp {method} error: {error}"
            code = error.get("code") if isinstance(error, dict) else None
            raise ProviderRpcError(self._last_error, code=code if type(code) is int else None)
        return response.get("result")

    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("cursor acp server is not running")
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        payload = (json.dumps(notification) + "\n").encode("utf-8")
        proc.stdin.write(payload)
        await proc.stdin.drain()

    async def _send_jsonrpc_response(
        self,
        req_id: Any,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
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
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise RuntimeError("question response delivery is uncertain") from None

    async def _handle_server_request(self, record: Dict[str, Any]) -> None:
        method = record.get("method")
        req_id = record.get("id")
        params = record.get("params")
        if self._suppress_active_turn_updates or not self._turn_in_flight:
            if method == "cursor/ask_question":
                await self._send_jsonrpc_response(
                    req_id, result={"outcome": {"outcome": "cancelled"}}
                )
            elif method == "session/request_permission":
                await self._send_jsonrpc_response(
                    req_id, result={"outcome": {"outcome": "cancelled"}}
                )
            else:
                await self._send_jsonrpc_response(
                    req_id, error={"code": -32600, "message": "turn interrupted"}
                )
            return

        if (
            method == "cursor/ask_question"
            and isinstance(req_id, (int, str))
            and isinstance(params, dict)
        ):
            from ..cursor_cli_transcript import CursorCliTranscriptAdapter

            normalized_questions = CursorCliTranscriptAdapter._normalize_questions(
                params.get("questions")
            )
            if not normalized_questions:
                await self._send_jsonrpc_response(
                    req_id,
                    result={"outcome": {"outcome": "skipped", "reason": "No actionable questions"}},
                )
                return
            call_id = params.get("toolCallId")
            call_id_str = str(call_id) if isinstance(call_id, str) and call_id else f"ask_{req_id}"
            stored_params = {
                **params,
                "toolCallId": call_id_str,
                "_normalized": normalized_questions,
            }
            self._pending_questions[req_id] = stored_params
            await self._notification_queue.put(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id_str,
                    "tool_call": {
                        "askQuestionToolCall": {
                            "args": {
                                "title": params.get("title"),
                                "questions": params.get("questions"),
                            }
                        }
                    },
                }
            )
            return

        if (
            method == "cursor/create_plan"
            and isinstance(req_id, (int, str))
            and isinstance(params, dict)
        ):
            await self._send_jsonrpc_response(req_id, result={"outcome": {"outcome": "accepted"}})
            return

        if method in {"cursor/update_todos", "cursor/task", "cursor/generate_image"} and isinstance(
            req_id, (int, str)
        ):
            await self._send_jsonrpc_response(req_id, result={})
            return

        if (
            method == "session/request_permission"
            and isinstance(req_id, (int, str))
            and isinstance(params, dict)
        ):
            raw_options = params.get("options")
            choices: Dict[str, str] = {}
            card_options: List[Dict[str, str]] = []
            if isinstance(raw_options, list):
                for opt in raw_options:
                    if not isinstance(opt, dict):
                        continue
                    opt_id = opt.get("optionId")
                    name = opt.get("name")
                    if isinstance(opt_id, str) and opt_id and isinstance(name, str) and name:
                        choices[name] = opt_id
                        choices[opt_id] = opt_id
                        card_options.append({"id": opt_id, "label": name})
            if not card_options:
                await self._send_jsonrpc_response(
                    req_id, result={"outcome": {"outcome": "cancelled"}}
                )
                return
            # Under solo mode in default mode, auto-approve permission prompts.
            if self.session.solo_mode and self._current_mode == ChatMode.DEFAULT.value:
                allow_opt = next(
                    (
                        opt.get("optionId")
                        for opt in (raw_options or [])
                        if isinstance(opt, dict)
                        and opt.get("kind") in {"allow_once", "allow_always"}
                        and isinstance(opt.get("optionId"), str)
                    ),
                    card_options[0]["id"],
                )
                await self._send_jsonrpc_response(
                    req_id,
                    result={"outcome": {"outcome": "selected", "optionId": allow_opt}},
                )
                return
            question_id = f"permission:{req_id}"
            tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            title = tool_call.get("title") if isinstance(tool_call, dict) else None
            prompt_label = str(title) if isinstance(title, str) and title else "Approval required"
            self._pending_permissions[req_id] = {"choices": choices, "question_id": question_id}
            await self._notification_queue.put(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": question_id,
                    "tool_call": {
                        "askQuestionToolCall": {
                            "args": {
                                "title": "Permission",
                                "questions": [
                                    {
                                        "id": question_id,
                                        "prompt": prompt_label,
                                        "options": card_options,
                                        "allowMultiple": False,
                                    }
                                ],
                            }
                        }
                    },
                }
            )
            return

        await self._send_jsonrpc_response(
            req_id,
            error={"code": -32601, "message": f"Method not found: {method}"},
        )

    async def answer_pending_question(
        self,
        answers: List[Dict[str, Any]],
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        if not self._pending_questions and not self._pending_permissions:
            return False

        selected_by_qid: Dict[str, List[str]] = {}
        for entry in answers:
            if not isinstance(entry, dict):
                continue
            qid = entry.get("questionId")
            sel = entry.get("selected")
            if isinstance(qid, str) and isinstance(sel, list):
                selected_by_qid[qid] = [v for v in sel if isinstance(v, str)]

        responses: List[Tuple[Any, Dict[str, Any]]] = []
        completed_notifications: List[Dict[str, Any]] = []

        for req_id, perm in list(self._pending_permissions.items()):
            question_id = perm["question_id"]
            if expected_call_id is not None and question_id != expected_call_id:
                continue
            if answers and question_id not in selected_by_qid:
                continue
            self._pending_permissions.pop(req_id, None)
            vals = selected_by_qid.get(question_id, [])
            choices = perm["choices"]
            chosen_id = choices.get(vals[0]) if len(vals) == 1 else None
            if chosen_id:
                rpc_res: Dict[str, Any] = {
                    "outcome": {"outcome": "selected", "optionId": chosen_id}
                }
            else:
                rpc_res = {"outcome": {"outcome": "cancelled"}}
            responses.append((req_id, rpc_res))
            completed_notifications.append(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": question_id,
                    "tool_call": {"askQuestionToolCall": {"args": {}, "result": rpc_res}},
                }
            )

        for req_id, params in list(self._pending_questions.items()):
            call_id = str(params.get("toolCallId") or f"ask_{req_id}")
            if expected_call_id is not None and call_id != expected_call_id:
                continue
            normalized = params.get("_normalized") or []
            qids = {q["id"] for q in normalized if isinstance(q, dict) and "id" in q}
            if answers and (not qids or not qids <= selected_by_qid.keys()):
                continue
            self._pending_questions.pop(req_id, None)
            if not answers:
                rpc_res = {"outcome": {"outcome": "skipped", "reason": "Questions skipped by user"}}
            else:
                acp_answers: List[Dict[str, Any]] = []
                for q in normalized:
                    qid = q["id"]
                    raw_selected = selected_by_qid.get(qid, [])
                    # Map option labels back to option IDs when the card sent labels,
                    # preserving direct option IDs and custom "Other" text.
                    label_to_id = {
                        opt["label"]: opt["id"]
                        for opt in q.get("options", [])
                        if isinstance(opt, dict) and "id" in opt and "label" in opt
                    }
                    valid_ids = {
                        opt["id"]
                        for opt in q.get("options", [])
                        if isinstance(opt, dict) and "id" in opt
                    }
                    mapped_ids = [
                        val if val in valid_ids else label_to_id.get(val, val)
                        for val in raw_selected
                    ]
                    acp_answers.append({"questionId": qid, "selectedOptionIds": mapped_ids})
                rpc_res = {"outcome": {"outcome": "answered", "answers": acp_answers}}
            responses.append((req_id, rpc_res))
            completed_notifications.append(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "tool_call": {"askQuestionToolCall": {"args": {}, "result": {"success": True}}},
                }
            )

        for req_id, result in responses:
            await self._send_jsonrpc_response(req_id, result=result)
        for notif in completed_notifications:
            await self._notification_queue.put(notif)
        return bool(responses)

    async def _handle_notification(self, record: Dict[str, Any]) -> None:
        if self._replaying_history or self._suppress_active_turn_updates:
            return
        if record.get("method") != "session/update":
            return
        params = record.get("params")
        if not isinstance(params, dict):
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        session_id = params.get("sessionId")
        update_kind = update.get("sessionUpdate")

        # Catch inline ACP error chunks emitted by agent-session.ts catch block
        # so they surface as structured ERROR events instead of fake prose.
        if update_kind == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str) and text.startswith("\n\nError:"):
                raw_err = text.lstrip("\n")
                if raw_err.startswith("Error: "):
                    raw_err = raw_err[len("Error: ") :]
                self._turn_error_emitted = True
                await self._notification_queue.put(
                    {
                        "type": "acp_error",
                        "message": _format_acp_error_text(raw_err),
                        "session_id": session_id,
                    }
                )
                return
            if isinstance(text, str) and text in {
                "\n\nPlease sign in to continue",
                "\n\nUpgrade your plan to continue",
                "\n\nAdd a payment method to continue",
                "\n\nCheck your settings to continue",
            }:
                self._turn_error_emitted = True
                await self._notification_queue.put(
                    {
                        "type": "acp_error",
                        "message": _format_acp_error_text(text.strip()),
                        "session_id": session_id,
                    }
                )
                return

        await self._notification_queue.put(record)

    async def read_line(self) -> Optional[Dict[str, Any]]:
        record = await self._notification_queue.get()
        if record is None:
            self._client_requested_stop = False
        return record

    async def _drain_stdout(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        buffer = b""
        cancelled = False
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
                    await self._dispatch_stdout_record(record)
            if buffer:
                text = buffer.decode("utf-8", errors="ignore").strip()
                if text:
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        record = None
                    if isinstance(record, dict):
                        await self._dispatch_stdout_record(record)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            self._runtime.logger().exception("cursor acp stdout drain failed")
        finally:
            self._staged_images = []
            if not cancelled:
                try:
                    returncode = await proc.wait()
                except Exception:
                    returncode = -1
                stderr_task = self._stderr_task
                if stderr_task is not None:
                    try:
                        await stderr_task
                    except Exception:
                        pass
                if returncode != 0:
                    stderr_text = self._runtime.redact_diagnostic(
                        self._stderr_buffer.decode("utf-8", errors="ignore").strip()
                    )
                    stderr_text = _format_acp_error_text(stderr_text)
                    self._exit_error = f"provider exited with code {returncode}" + (
                        f": {stderr_text}" if stderr_text else ""
                    )
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(
                        RuntimeError(self._exit_error or "cursor acp server exited before response")
                    )
            self._pending_requests.clear()
            await self._notification_queue.put(None)

    async def _dispatch_stdout_record(self, record: Dict[str, Any]) -> None:
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

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Capture session_id for legacy stream-json records when present."""
        if record.get("type") == "system" and record.get("subtype") == "init":
            sid = record.get("session_id")
            if isinstance(sid, str) and sid:
                self._persist_conversation_id(sid)
                self._handshake_complete = True
