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
            provider_env = await self._build_provider_env()
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
        sid = self._conversation_id
        if sid and self._conversation_id_verified:
            return ["--conversation", sid]
        return []

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
            self._persist_conversation_id(sid)
            self._handshake_complete = True
