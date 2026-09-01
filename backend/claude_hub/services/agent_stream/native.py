"""Native provider transport sessions for the Paseo Agent surface.

A :class:`ProviderSession` owns the provider subprocess (or persistent
JSON-RPC connection) and exposes a uniform ``send_message`` / ``read_line``
interface to the tailer. ``send_message`` is the single input entry point;
text and images are delivered atomically under one turn guard.

Provider-specific lifecycle:

* **Claude / Cursor** — ``--print`` invocations are one-shot turns. Each
  ``send_message`` spawns a fresh streaming subprocess with the prompt on
  stdin; the provider's conversation id (from the first ``message_start``
  event) is captured so the next turn can ``--resume`` it. No persistent
  process sits idle between turns.

* **Codex** — ``codex app-server --stdio`` is a persistent JSON-RPC server.
  ``start`` launches it and runs the ``initialize`` handshake (with
  ``clientInfo``) followed by the ``initialized`` notification, then creates
  or resumes a thread. ``send_message`` issues a ``turn/start`` request with
  ``input: [{type: "text", text: ...}]``. Responses are dispatched to
  per-request Futures; notifications (``AgentMessageDelta``,
  ``ReasoningTextDelta``, ``TurnStarted``, ``TurnCompleted``, ...) are
  forwarded to the notification queue consumed by :meth:`read_line`.

Fail-closed contract:

* If the binary is missing, the handshake times out, or the process exits
  before a recognized event, the session reports ``structured=False`` and
  the API surfaces the last error.
* Image staging raises ``NotImplementedError`` only when the provider does
  not support image input; otherwise images are injected into the turn.
* ``structured`` is only ``True`` after the provider has emitted a
  recognized event (handshake / first delta), never merely because the
  process launched.
* A send lock prevents concurrent turns; a second ``send_message`` while a
  turn is in flight raises rather than cancelling the active turn.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ...models import AgentType, ManagedSession, StreamCapabilities

logger = logging.getLogger(__name__)

# How long to wait for the provider's first recognized event before declaring
# the transport unavailable.
_STARTUP_GRACE_S = 10.0

# Maximum stderr bytes buffered per session (sanitized, bounded).
_STDERR_BUFFER_MAX = 64 * 1024

# Maximum image size accepted (20 MiB). Larger payloads are rejected before
# being base64-encoded into the user message envelope.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _detect_image_mime(data: bytes) -> Optional[str]:
    """Return the MIME type of an image from its magic bytes, or ``None``.

    Supports PNG, JPEG, GIF, WebP, and BMP. Anything else (including non-image
    bytes) returns ``None`` so the caller can fail closed.
    """
    if not data:
        return None
    if len(data) > _MAX_IMAGE_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def _mime_extension(mime: str) -> str:
    """Return a file extension (with dot) for a known image MIME type."""
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(mime, ".bin")


# Codex ``turn/start`` accepts ``localImage`` input items that reference a
# local file path. The original image bytes are staged to temp files for the
# duration of one turn. These files live under an app-owned 0700 directory
# inside the runtime home (NOT the persistent workspace STATE_ROOT) so that:
#
# * the backend controls the lifecycle and permissions,
# * original images never end up in durable/backup state,
# * a crashed process leaves orphans that the startup cleanup can remove,
# * no other user on the host can read the staged images.
#
# ``BackendInstanceLock`` guarantees a single owning process per runtime
# home, so at startup we can safely remove any prior-process temp files.
_CODEX_IMAGE_TEMP_DIR_NAME = "codex-images"


def _runtime_home() -> Path:
    """Return the runtime home directory (isolated per worktree)."""
    from ...services.runtime_isolation import resolve_runtime_home

    return resolve_runtime_home()


def _codex_image_temp_dir() -> Path:
    """Return the app-owned Codex image temp directory, creating it mode 0700.

    The directory lives under ``runtime_home/tmp`` so it is scoped to the
    runtime instance and never persisted as part of workspace state.
    """
    path = _runtime_home() / "tmp" / _CODEX_IMAGE_TEMP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    # mkdir is subject to umask; force 0700 on every component up to runtime
    # home so the whole tmp tree is private.
    current = path
    root = _runtime_home()
    while current != root and current != current.parent:
        try:
            os.chmod(current, 0o700)
        except OSError:
            pass
        current = current.parent
    return path


def cleanup_codex_temp_dir(max_age_seconds: Optional[float] = None) -> int:
    """Remove Codex image temp files.

    At startup (before any turn can stage new files) ``BackendInstanceLock``
    guarantees we are the sole owner of this runtime, so the default
    (``max_age_seconds=None``) removes *all* leftover files from a prior
    crashed process — including fresh ones. Pass a bounded
    ``max_age_seconds`` to only remove files older than the threshold (a
    safety net for mid-run cleanup, though the normal lifecycle already
    removes files on turn completion / stop).

    Returns the number of files removed.
    """
    import time

    temp_dir = _codex_image_temp_dir()
    now = time.time()
    removed = 0
    for entry in temp_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            if max_age_seconds is None or (now - entry.stat().st_mtime) > max_age_seconds:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed


class ProviderSession(ABC):
    """Base contract for a native provider transport.

    Subclasses set :attr:`adapter_id`, :attr:`schema_version`, and the
    capability flags; implement :meth:`_build_command` and
    :meth:`_send_text`; and may override :meth:`start` / :meth:`stop`.
    """

    adapter_id: str = "native"
    schema_version: int = 1
    supports_approval_ui: bool = False
    supports_tool_timeline: bool = False
    supports_images: bool = False

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.session = session
        # Optional callback to persist the provider conversation id so a cold
        # restart can resume the same conversation.
        self._conversation_id_persist = conversation_id_persist
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._stderr_buffer: bytes = b""
        self._started = False
        self._handshake_complete = False
        self._last_error: Optional[str] = None
        # Per-turn exit error: set by ``_drain_stdout`` when the one-shot
        # provider subprocess exits nonzero (or before emitting a recognized
        # completion record). Surfaced to the tailer via ``exit_error`` so it
        # can emit an ``error`` event and a failed ``turn_completed``.
        self._exit_error: Optional[str] = None
        self._cwd: str = session.workspace_path or ""
        # Provider conversation id. For a fresh tab this is a constructive
        # placeholder (a UUID generated by the tab manager) that may NOT
        # correspond to an existing provider conversation. We must NOT pass it
        # to ``--resume`` until it has been verified (i.e. the provider has
        # emitted it in a system/init record). Until then we use ``--session-id``
        # (which creates the conversation) or no flag at all.
        #
        # ``_conversation_id_verified`` is seeded from the persisted
        # ``agent_session_id_verified`` flag, never inferred from the mere
        # presence of a UUID. A constructive id that has never been confirmed
        # by the provider must not be treated as resumable.
        self._conversation_id: Optional[str] = session.agent_session_id
        self._conversation_id_verified: bool = bool(
            getattr(session, "agent_session_id_verified", False)
        )
        # Turn-in-flight guard. A second ``send_message`` while a turn is still
        # generating must raise rather than cancel the active turn (which the
        # old ``_send_lock``-only approach did, because the lock was released
        # the moment ``_spawn_oneshot`` returned).
        self._turn_in_flight: bool = False
        self._turn_completion: Optional[asyncio.Future[None]] = None
        # Serialize sends so a second prompt never cancels an active turn.
        self._send_lock = asyncio.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────────

    @abstractmethod
    def _build_command(self) -> List[str]:
        """Return the argv used to launch the provider in streaming mode."""

    async def start(self) -> None:
        """Launch any persistent provider process (Codex app-server).

        For one-shot providers (Claude / Cursor) this is a no-op; the
        subprocess is spawned per turn in :meth:`send_message`.
        """
        self._started = True

    async def stop(self) -> None:
        """Terminate the provider subprocess and stop the readers."""
        self._started = False
        await self._terminate_process()
        # Resolve any pending turn completion so a caller awaiting the turn
        # does not hang after the transport is stopped.
        self._end_turn()

    async def _terminate_process(self) -> None:
        """Kill the current subprocess and its reader tasks.

        This does NOT call ``_end_turn`` — it is used both for full transport
        shutdown (where ``stop`` calls ``_end_turn`` separately) and for
        rolling over from one one-shot turn to the next (where the new turn's
        completion is resolved by the new process's EOF, not by the old
        process's death).
        """
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    # ── turn-in-flight guard ────────────────────────────────────────────────

    def _begin_turn(self) -> None:
        """Mark a turn as in flight.

        Raises ``RuntimeError`` if a turn is already active. The caller must
        hold ``_send_lock``.
        """
        if self._turn_in_flight:
            raise RuntimeError(
                "a turn is already in flight; wait for it to complete before "
                "sending another message"
            )
        self._turn_in_flight = True
        loop = asyncio.get_running_loop()
        self._turn_completion = loop.create_future()

    def _end_turn(self) -> None:
        """Mark the active turn as complete and resolve its completion future."""
        self._turn_in_flight = False
        fut = self._turn_completion
        self._turn_completion = None
        if fut is not None and not fut.done():
            fut.set_result(None)

    def acknowledge_turn_complete(self) -> None:
        """Acknowledge that the tailer has consumed the turn-end signal.

        The tailer calls this after it has processed the provider's turn-end
        record (EOF for one-shot providers, ``turn/completed`` for Codex) and
        released the active turn id. Only then is ``_turn_in_flight`` cleared,
        so a concurrent ``send_message`` can never overwrite ``_active_turn_id``
        while the previous turn's records are still queued.

        This is the normal-completion counterpart to ``_end_turn`` (which is
        reserved for error/shutdown paths where no turn-end record arrives).
        """
        self._end_turn()

    @property
    def turn_in_flight(self) -> bool:
        return self._turn_in_flight

    @property
    def eof_is_fatal(self) -> bool:
        """Whether an EOF on the transport's stdout is a fatal error.

        For one-shot providers (Claude/Cursor) EOF means the turn completed
        normally; the push consumer should keep waiting for the next turn.
        For persistent providers (Codex app-server) EOF means the server
        died and the session must fail closed.
        """
        return False

    # ── input ───────────────────────────────────────────────────────────────

    async def send_message(self, text: str, images: List[bytes]) -> None:
        """Atomically deliver a user turn (text + images) to the provider.

        This is the single input entry point for the composer. It acquires the
        send lock once, checks the turn-in-flight guard, stages images, and
        sends the text prompt. If any step fails, staged images are cleared
        and the turn guard is released so they cannot leak into a later turn.

        Subclasses implement :meth:`_send_text` and :meth:`_stage_images`
        (both lock-free); this method owns the lock and the turn guard.
        """
        async with self._send_lock:
            self._begin_turn()
            try:
                if images:
                    self._stage_images(images)
                await self._send_text(text)
            except Exception:
                self._clear_staged_images()
                self._end_turn()
                raise

    @abstractmethod
    async def _send_text(self, text: str) -> None:
        """Provider-specific turn submission (lock-free).

        The caller holds ``_send_lock`` and has already called ``_begin_turn``.
        This method must consume any staged images (clearing ``_staged_images``
        or moving them to ``_inflight_images``) so a successful turn does not
        leak attachments.
        """

    def _stage_images(self, images: List[bytes]) -> None:
        """Stage image bytes for the next turn (lock-free).

        The base implementation raises ``NotImplementedError``; subclasses
        that support images override this to validate and store the bytes.
        """
        if not self.supports_images:
            raise NotImplementedError(f"{self.adapter_id} does not support image input")

    def _clear_staged_images(self) -> None:
        """Discard any staged images (lock-free).

        Called on send failure so attachments never leak across turns.
        Subclasses that stage to temp files override this to delete them.
        """

    # ── output ──────────────────────────────────────────────────────────────

    async def read_line(self) -> Optional[Dict[str, Any]]:
        """Await one parsed JSON record from stdout, or ``None`` on EOF."""
        return await self._stdout_queue.get()

    async def _drain_stdout(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        cancelled = False
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    await self._stdout_queue.put(record)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("native transport stdout drain failed")
        finally:
            if cancelled:
                # The turn was rolled over or the transport is shutting down.
                # ``_terminate_process`` will kill the subprocess after this
                # task returns, so we must NOT await ``proc.wait()`` here (it
                # could deadlock). Just signal EOF so the tailer's
                # ``read_line`` does not block forever.
                await self._stdout_queue.put(None)
                return
            # Natural EOF: the provider's stdout has closed. Wait for the
            # process to fully exit so we can inspect its return code. A
            # nonzero exit (or an exit before any recognized completion
            # record) is a turn failure: capture the bounded stderr and
            # expose it as ``exit_error`` so the tailer can emit an ``error``
            # event and a failed ``turn_completed``.
            try:
                returncode = await proc.wait()
            except Exception:
                returncode = -1
            # Ensure the stderr drain has finished capturing all output before
            # we read the bounded buffer.
            stderr_task = self._stderr_task
            if stderr_task is not None:
                try:
                    await stderr_task
                except Exception:
                    pass
            if returncode != 0:
                stderr_text = self._stderr_buffer.decode("utf-8", errors="ignore").strip()
                self._exit_error = f"provider exited with code {returncode}" + (
                    f": {stderr_text}" if stderr_text else ""
                )
            # Signal EOF to consumers. The tailer will process all queued
            # records, then this EOF sentinel, and then call
            # ``acknowledge_turn_complete`` to release the turn guard. We must
            # NOT call ``_end_turn`` here: doing so would clear
            # ``_turn_in_flight`` before the tailer has consumed the EOF,
            # allowing a concurrent ``send_message`` to overwrite
            # ``_active_turn_id`` while this turn's records are still queued.
            await self._stdout_queue.put(None)

    async def _drain_stderr(self) -> None:
        """Bounded stderr capture; surfaced as ``last_error`` on non-zero exit."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_buffer = (self._stderr_buffer + chunk)[-_STDERR_BUFFER_MAX:]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("native transport stderr drain failed")

    # ── capabilities ────────────────────────────────────────────────────────

    def capabilities(self) -> StreamCapabilities:
        # For AGENT sessions the native transport is the sole real-time source.
        # ``structured`` is True only when (a) the provider binary is
        # discoverable on PATH and (b) the transport has not recorded a fatal
        # error. We must not depend on ``_handshake_complete``, which stays
        # False for one-shot transports (Claude/Cursor) until the first turn
        # produces a system/init record — but the transport *can* deliver
        # structured events as soon as the binary exists.
        binary = self._build_command()[0]
        binary_available = shutil.which(binary) is not None
        if not binary_available:
            self._last_error = f"provider binary not found on PATH: {binary}"
        return StreamCapabilities(
            structured=binary_available and self._last_error is None,
            adapter_id=self.adapter_id,
            schema_version=self.schema_version,
            sources=[],
            supports_approval_ui=self.supports_approval_ui,
            supports_tool_timeline=self.supports_tool_timeline,
            supports_images=self.supports_images,
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def exit_error(self) -> Optional[str]:
        """Per-turn exit error from a nonzero provider subprocess exit.

        ``None`` when the last turn exited cleanly (or no turn has run yet).
        Set by ``_drain_stdout`` after the process exits; consumed by the
        tailer's EOF handler to decide whether to emit a failed
        ``turn_completed``.
        """
        return self._exit_error

    # ── helpers ──────────────────────────────────────────────────────────────

    def _build_env(self) -> Dict[str, str]:
        """Merge the inherited environment with session.env overrides.

        PATH is always preserved from the parent environment so the provider
        binary and its toolchain remain discoverable.
        """
        env = dict(os.environ)
        for key, value in self.session.env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        # Never let the session override PATH away; keep the inherited PATH.
        if "PATH" not in env:
            env["PATH"] = os.environ.get("PATH", "")
        return env

    def _resume_arg(self) -> List[str]:
        """Return the session-id argument for the next turn.

        Base behavior: only pass ``--resume`` when the conversation id has
        been verified (the provider emitted it in a system/init record). For
        an unverified constructive id, pass nothing — subclasses override to
        use a provider-specific constructive flag (Claude: ``--session-id``;
        Cursor: ``--resume`` is itself constructive).
        """
        sid = self._conversation_id
        if sid and self._conversation_id_verified:
            return ["--resume", sid]
        return []

    def _persist_conversation_id(self, cid: str) -> None:
        """Capture and persist the provider conversation id for resume."""
        if not cid:
            return
        if cid == self._conversation_id and self._conversation_id_verified:
            return
        self._conversation_id = cid
        self._conversation_id_verified = True
        if self._conversation_id_persist is not None:
            try:
                self._conversation_id_persist(cid)
            except Exception:
                logger.exception("failed to persist conversation id %s", cid)

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Provider-specific hook to extract the conversation id from a record.

        The base implementation is a no-op. Claude/Cursor override to capture
        the id from ``message_start``; Codex captures it during thread/start.
        """

    async def _spawn_oneshot(self, cmd: List[str], stdin_text: str) -> None:
        """Spawn a one-shot streaming subprocess for Claude/Cursor turns.

        The stdout queue is NOT replaced between turns: the previous turn's
        reader puts an EOF sentinel (``None``) on the shared queue, and the
        push consumer reads through it before consuming the next turn's
        records. Replacing the queue would strand a consumer awaiting on the
        old queue object.
        """
        # Stop any previous turn's process before starting a new one. This
        # awaits the old reader so its EOF sentinel is on the queue before we
        # begin writing new records. We use ``_terminate_process`` (not
        # ``stop``) so we do NOT call ``_end_turn`` — the new turn's
        # completion is resolved by the new process's EOF, not by the old
        # process's death.
        await self._terminate_process()
        self._stderr_buffer = b""
        self._exit_error = None
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._cwd or None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )
        except FileNotFoundError:
            self._last_error = f"provider binary not found: {cmd[0]}"
            self._end_turn()
            raise
        except OSError as exc:
            self._last_error = f"failed to launch provider: {exc}"
            self._end_turn()
            raise
        self._started = True
        self._reader_task = asyncio.create_task(self._drain_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        # Feed the prompt to the provider's stdin.
        if self._process.stdin is not None:
            self._process.stdin.write(stdin_text.encode("utf-8"))
            await self._process.stdin.drain()
            self._process.stdin.close()


# ── Claude ───────────────────────────────────────────────────────────────────


class ClaudeNativeSession(ProviderSession):
    """Claude Code native transport via ``stream-json`` stdout.

    Each turn spawns ``claude --print --input-format stream-json
    --output-format stream-json --include-partial-messages`` and writes a
    single SDKUserMessage JSONL record on stdin, then closes stdin for the
    one-shot turn. The conversation id from the system init record is
    captured so the next turn resumes via ``--resume``.

    Images are sent as ``image`` content blocks inside the user message
    envelope (``source.type=base64``). The ``--attach`` flag does not exist
    in Claude Code 2.1.x and the ``[IMAGE:base64]`` inline token was never
    verified; the SDKUserMessage envelope is the documented mechanism.
    """

    adapter_id = "claude-native"
    schema_version = 1
    supports_tool_timeline = True
    # Image support uses the SDKUserMessage image content block. Marked True
    # only after the envelope was validated against the installed CLI.
    supports_images = True

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(session, conversation_id_persist=conversation_id_persist)
        # Staged image bytes for the next turn's user message content blocks.
        self._staged_images: List[bytes] = []

    def _build_command(self) -> List[str]:
        cmd = [
            "claude",
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        if self.session.solo_mode:
            cmd.append("--dangerously-skip-permissions")
        cmd.extend(self._resume_arg())
        return cmd

    def _resume_arg(self) -> List[str]:
        """Claude: ``--session-id`` creates the conversation; ``--resume``
        requires it to exist. Use ``--session-id`` for the constructive
        (unverified) first turn, then ``--resume`` once the id is confirmed."""
        sid = self._conversation_id
        if not sid:
            return []
        if self._conversation_id_verified:
            return ["--resume", sid]
        return ["--session-id", sid]

    def _build_stdin(self, text: str) -> str:
        """Build the SDKUserMessage JSONL record for stdin.

        Envelope shape (verified against Claude Code's SDK input format):

            {"type":"user","message":{"role":"user","content":[
              {"type":"image","source":{"type":"base64","media_type":"image/png","data":"<b64>"}},
              {"type":"text","text":"<prompt>"}
            ]},"parent_tool_use_id":null,"session_id":null}

        For text-only turns the content may be a single text block or a plain
        string; we always emit a list of blocks for consistency.
        """
        content: List[Dict[str, Any]] = []
        for img in self._staged_images:
            media_type = _detect_image_mime(img)
            if media_type is None:
                # Should have been rejected in _stage_images, but fail closed.
                raise ValueError("unsupported image format")
            b64 = base64.b64encode(img).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                }
            )
        content.append({"type": "text", "text": text})
        envelope = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": None,
        }
        return json.dumps(envelope)

    async def _send_text(self, text: str) -> None:
        cmd = self._build_command()
        stdin_text = self._build_stdin(text)
        # Clear staged images after they've been incorporated into the
        # envelope for this turn.
        self._staged_images = []
        await self._spawn_oneshot(cmd, stdin_text)

    def _stage_images(self, images: List[bytes]) -> None:
        """Stage images for the next turn's user message content blocks.

        Images are validated by magic bytes and stored as raw bytes; they are
        base64-encoded into the SDKUserMessage envelope on the next
        ``_send_text`` call.
        """
        if not images:
            return
        for img in images:
            media_type = _detect_image_mime(img)
            if media_type is None:
                raise ValueError("unsupported image format or not an image")
            self._staged_images.append(img)

    def _clear_staged_images(self) -> None:
        self._staged_images = []

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Extract the conversation id from Claude's system init record.

        Claude emits a top-level ``{type:"system", subtype:"init",
        session_id:"..."}`` record at the start of each ``--print`` run. That
        ``session_id`` is the value to pass to ``--resume`` on the next turn.
        ``message_start.message.id`` is a per-message id, not the conversation
        id, so it must NOT be used for resume.
        """
        if record.get("type") != "system":
            return
        if record.get("subtype") != "init":
            return
        sid = record.get("session_id")
        if isinstance(sid, str) and sid:
            self._persist_conversation_id(sid)
            self._handshake_complete = True


# ── Codex ────────────────────────────────────────────────────────────────────


class CodexNativeSession(ProviderSession):
    """Codex native transport via the app-server JSON-RPC stdio protocol.

    ``start`` launches ``codex app-server --stdio`` and runs the
    ``initialize`` handshake (with ``clientInfo``) followed by the
    ``initialized`` notification, then creates or resumes a thread.
    ``send_message`` issues a ``turn/start`` request with ``input`` items.

    Responses (records with an ``id``) are dispatched to per-request
    Futures; notifications (no ``id``) are forwarded to the notification
    queue consumed by :meth:`read_line`. This avoids the spin-loop hazard
    of re-queueing notifications ahead of a pending response.
    """

    adapter_id = "codex-native"
    schema_version = 1
    supports_tool_timeline = True
    supports_images = True

    @property
    def eof_is_fatal(self) -> bool:
        # Codex runs a persistent app-server; EOF means it died.
        return True

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(session, conversation_id_persist=conversation_id_persist)
        self._jsonrpc_id = 0
        self._thread_id: Optional[str] = None
        # Per-request response futures keyed by JSON-RPC id.
        self._pending_requests: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        # Notifications (no ``id``) are placed here for the tailer to consume.
        self._notification_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
        # Staged image temp files for the next turn/start.
        self._staged_images: List[Path] = []
        # Image temp files owned by the in-flight turn; cleaned up on
        # turn/completed or stop.
        self._inflight_images: List[Path] = []
        # ``SessionTailer._run_native_push`` and the first composer send can
        # both discover an unstarted transport at the same time.  Codex owns
        # one persistent app-server, so initialization must be single-flight:
        # two readers attached to the same overwritten ``self._process``
        # would otherwise concurrently read one stdout stream and tear down
        # structured delivery with asyncio's "another coroutine is already
        # waiting" error.
        self._start_lock = asyncio.Lock()

    def _build_command(self) -> List[str]:
        return ["codex", "app-server", "--stdio"]

    async def stop(self) -> None:
        """Terminate the app-server and clean up any staged image temp files."""
        async with self._start_lock:
            staged = self._staged_images
            inflight = self._inflight_images
            self._staged_images = []
            self._inflight_images = []
            await super().stop()
            self._cleanup_images(staged)
            self._cleanup_images(inflight)

    async def start(self) -> None:
        async with self._start_lock:
            # A second caller that waited for the in-progress handshake can
            # reuse the one healthy app-server.  Re-enter initialization only
            # when the reader already terminated.
            if (
                self._started
                and self._process is not None
                and self._reader_task is not None
                and not self._reader_task.done()
            ):
                return

            # Clear a half-started/dead process before retrying.  This is also
            # important after an initialize or thread/resume failure: leaving
            # ``_started`` true would make every later send target a broken
            # pipe instead of getting one clean retry.
            if self._process is not None or self._reader_task is not None:
                await self._terminate_process()
            self._started = False

            # ``stop()`` cancels the previous stdout reader, whose ``finally``
            # block deliberately publishes an EOF sentinel so an active
            # consumer cannot hang.  After an idle reap there is no active
            # consumer, so that sentinel remains queued.  Reusing the queue
            # for a new app-server would make the fresh process look dead on
            # its first read.  Each persistent process generation therefore
            # owns a fresh notification queue.
            self._notification_queue = asyncio.Queue()

            cmd = self._build_command()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=self._cwd or None,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._build_env(),
                )
            except FileNotFoundError:
                self._last_error = f"provider binary not found: {cmd[0]}"
                raise
            except OSError as exc:
                self._last_error = f"failed to launch provider: {exc}"
                raise
            self._started = True
            self._reader_task = asyncio.create_task(self._drain_stdout())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            try:
                await self._initialize()
            except Exception as exc:
                self._last_error = f"codex app-server initialization failed: {exc}"
                self._started = False
                await self._terminate_process()
                raise RuntimeError(self._last_error) from exc
            self._last_error = None

    async def _initialize(self) -> None:
        """Run the JSON-RPC ``initialize`` handshake and create/resume a thread."""
        init_params = {
            "clientInfo": {"name": "claude-hub", "version": "1.0.0"},
            "capabilities": {},
        }
        await self._send_request("initialize", init_params)
        # The protocol expects an ``initialized`` notification after the
        # initialize response.
        await self._send_notification("initialized", {})
        # Resume an existing thread when the session pins one; otherwise start
        # a fresh thread.
        if self._conversation_id:
            try:
                resume_resp = await self._send_request(
                    "thread/resume", {"threadId": self._conversation_id}
                )
                if isinstance(resume_resp, dict):
                    thread = resume_resp.get("thread")
                    if isinstance(thread, dict) and thread.get("id"):
                        self._thread_id = thread["id"]
                        return
            except RuntimeError:
                # Resume failed (e.g. thread not found); fall through to start.
                logger.warning("codex thread/resume failed; starting a new thread")
        thread_resp = await self._send_request("thread/start", {})
        if isinstance(thread_resp, dict):
            thread = thread_resp.get("thread")
            if isinstance(thread, dict) and thread.get("id"):
                self._thread_id = thread["id"]
                self._persist_conversation_id(thread["id"])

    async def _send_text(self, text: str) -> None:
        if not self._started or self._process is None:
            await self.start()
        input_items: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        # Append any staged images as localImage inputs.
        image_paths = self._staged_images
        self._staged_images = []
        for img_path in image_paths:
            input_items.append({"type": "localImage", "path": str(img_path)})
        params: Dict[str, Any] = {
            "threadId": self._thread_id,
            "input": input_items,
        }
        # Transfer ownership of the image temp files to the in-flight turn
        # BEFORE issuing turn/start. The app-server may emit turn/completed
        # (or any notification) before the turn/start response arrives; if we
        # waited for the response to set _inflight_images, the completed
        # handler would see an empty list and the temp files would leak.
        self._inflight_images = image_paths
        try:
            await self._send_request("turn/start", params)
        except Exception:
            # The turn never started (or the server rejected it). The image
            # temp files are still ours to clean up; the turn/completed
            # handler will not run for a turn that never started.
            inflight = self._inflight_images
            self._inflight_images = []
            self._cleanup_images(inflight)
            raise

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
        temp_dir = _codex_image_temp_dir()
        for img in images:
            media_type = _detect_image_mime(img)
            if media_type is None:
                raise ValueError("unsupported image format or not an image")
            # Use a unique name inside the app-owned temp dir. ``mkstemp``
            # would create the file in the system temp dir; we want it under
            # our 0700 directory so the backend owns the lifecycle.
            fd, path = tempfile.mkstemp(
                prefix="codex-img-",
                suffix=_mime_extension(media_type),
                dir=str(temp_dir),
            )
            f = None
            try:
                f = os.fdopen(fd, "wb")
                f.write(img)
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

    # ── JSON-RPC dispatch ───────────────────────────────────────────────────

    async def _send_request(self, method: str, params: Dict[str, Any]) -> Any:
        """Send a JSON-RPC request and await its matching response.

        The stdout reader dispatches the response (matched by ``id``) to the
        per-request Future. Notifications that arrive before the response are
        placed on the notification queue and never re-queued ahead of the
        response.
        """
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
            response = await asyncio.wait_for(future, timeout=_STARTUP_GRACE_S)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            self._last_error = f"codex {method} timed out"
            raise RuntimeError(self._last_error)
        if "error" in response:
            self._last_error = f"codex {method} error: {response['error']}"
            raise RuntimeError(self._last_error)
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

    # ── output override ─────────────────────────────────────────────────────

    async def read_line(self) -> Optional[Dict[str, Any]]:
        """Await one server notification, or ``None`` on EOF."""
        return await self._notification_queue.get()

    async def _drain_stdout(self) -> None:
        """Parse stdout lines and dispatch responses vs notifications.

        Records with an ``id`` are JSON-RPC responses: resolve the matching
        pending Future. Records without an ``id`` are notifications: place
        them on the notification queue for the tailer.
        """
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
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
                    # Response: dispatch to the matching pending request.
                    req_id = record.get("id")
                    if isinstance(req_id, int):
                        future = self._pending_requests.pop(req_id, None)
                        if future is not None and not future.done():
                            future.set_result(record)
                else:
                    # Notification: forward to the tailer.
                    self._handshake_complete = True
                    method = record.get("method")
                    if method == "turn/completed":
                        # The turn has finished. Clean up any image temp files
                        # that were owned by this turn. The turn guard is
                        # released by the tailer via
                        # ``acknowledge_turn_complete`` after it processes this
                        # notification — NOT here, so a concurrent send cannot
                        # overwrite ``_active_turn_id`` while this
                        # ``turn/completed`` record is still queued.
                        inflight = self._inflight_images
                        self._inflight_images = []
                        self._cleanup_images(inflight)
                    await self._notification_queue.put(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex native transport stdout drain failed")
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


# ── Cursor ───────────────────────────────────────────────────────────────────


class CursorNativeSession(ProviderSession):
    """Cursor Agent native transport via ``stream-json`` stdout.

    Each turn spawns ``agent --trust --print --output-format stream-json
    --stream-partial-output`` with the user prompt on stdin. The
    conversation id is captured for ``--resume`` on the next turn.
    """

    adapter_id = "cursor-native"
    schema_version = 1
    supports_tool_timeline = True
    supports_images = False

    def _build_command(self) -> List[str]:
        cmd = [
            "agent",
            "--trust",
            "--print",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
        ]
        if self.session.solo_mode:
            cmd.append("--yolo")
        cmd.extend(self._resume_arg())
        return cmd

    def _resume_arg(self) -> List[str]:
        """Cursor Agent's ``--resume`` is constructive: if the session does
        not exist it is created. We can therefore always pass ``--resume``
        with the pinned id, even on the first turn."""
        sid = self._conversation_id
        if sid:
            return ["--resume", sid]
        return []

    async def _send_text(self, text: str) -> None:
        cmd = self._build_command()
        await self._spawn_oneshot(cmd, text)

    def maybe_capture_conversation_id(self, record: Dict[str, Any]) -> None:
        """Extract the conversation id from Cursor's system init record.

        Cursor's stream-json stdout begins with
        ``{type:"system", subtype:"init", session_id:"..."}``. That
        ``session_id`` is the value to pass to ``--resume`` on the next turn.
        """
        if record.get("type") != "system":
            return
        if record.get("subtype") != "init":
            return
        sid = record.get("session_id")
        if isinstance(sid, str) and sid:
            self._persist_conversation_id(sid)
            self._handshake_complete = True


# ── factory ──────────────────────────────────────────────────────────────────


def create_native_session(
    session: ManagedSession,
    conversation_id_persist: Optional[Callable[[str], None]] = None,
) -> ProviderSession:
    """Return the native transport for ``session``'s agent type.

    Raises ``ValueError`` for unsupported agent types (fail-closed).
    """
    if session.agent_type == AgentType.CLAUDE:
        return ClaudeNativeSession(session, conversation_id_persist=conversation_id_persist)
    if session.agent_type == AgentType.CODEX:
        return CodexNativeSession(session, conversation_id_persist=conversation_id_persist)
    if session.agent_type == AgentType.CURSOR:
        return CursorNativeSession(session, conversation_id_persist=conversation_id_persist)
    raise ValueError(f"no native transport for agent_type={session.agent_type}")
