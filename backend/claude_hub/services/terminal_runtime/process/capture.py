"""Process capture responsibilities on the single terminal owner."""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Optional

import claude_hub.services.pty_exec as pty_exec

from ....models import ExecutionTarget
from ..defaults import _REMOTE_CAPTURE_TIMEOUT_SECONDS
from ..records import CursorPosition
from .host import ProcessHost

logger = logging.getLogger("claude_hub.services.ttyd_manager")


class ProcessCaptureMixin(ProcessHost):
    async def _log_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            async for line in self.process.stderr:
                logger.warning(f"ttyd[{self.tab_id}] stderr: {line.decode().strip()}")
        except Exception as e:
            logger.debug(f"Error reading stderr for tab {self.tab_id}: {e}")

    async def _configure_tmux(self) -> None:
        """Configure tmux options for better mouse scrolling and user experience."""
        try:
            # Wait a bit for tmux session to be ready
            await asyncio.sleep(0.5)

            # Set tmux options - these will apply to all sessions
            tmux_commands = [
                ["set", "-g", "mouse", "off"],
                ["set", "-g", "history-limit", "100000"],
                ["set", "-g", "terminal-overrides", "xterm*:smcup@:rmcup@"],
                # Advertise 24-bit color so agent TUIs render their full palette
                # instead of degrading to a flat, colorless UI under tmux. The
                # tmux server inherits its global environment from whatever
                # launched it; if that parent disabled color (NO_COLOR=1 /
                # FORCE_COLOR=0, common when started from another agent CLI),
                # tmux forces those onto every new pane and overrides the env we
                # pass to new-session. Scrub them from the global environment so
                # panes can emit color.
                ["set", "-as", "terminal-features", ",xterm-256color:RGB"],
                ["setenv", "-g", "-u", "NO_COLOR"],
                ["setenv", "-g", "FORCE_COLOR", "3"],
                ["setenv", "-g", "COLORTERM", "truecolor"],
                # Allow scrolling without entering copy mode explicitly
                ["set", "-g", "mode-keys", "vi"],
                ["set", "-g", "status", "off"],
            ]

            for cmd in tmux_commands:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *self._runtime.tmux_command(*cmd),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass

            # Set options on our specific session too
            for cmd in tmux_commands:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *self._runtime.tmux_command("-t", self.tmux_session, *cmd),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass

            logger.info(f"Configured tmux options for tab {self.tab_id}")
        except Exception as e:
            logger.warning(f"Failed to configure tmux for tab {self.tab_id}: {e}")

    async def _run_remote_capture_command(self, remote_command: str) -> str:
        if self._remote_is_pty_gateway():
            # A gateway ignores the argv channel; type the one-shot into a driven
            # PTY and read the guarded stdout back.
            try:
                return await pty_exec.pty_exec(self._remote_profile(), remote_command)
            except pty_exec.PtyExecError as exc:
                raise RuntimeError(f"remote PTY exec failed: {exc}") from exc
        proc = await asyncio.create_subprocess_exec(
            *self._build_remote_ssh_command(remote_command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_REMOTE_CAPTURE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError("remote tmux capture timed out") from exc

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"remote tmux capture failed with code {proc.returncode}")

        return stdout.decode("utf-8", errors="ignore")

    async def _capture_remote_history(self, lines: int = 100000) -> str:
        safe_lines = max(100, min(lines, 100000))
        start = f"-{safe_lines}"
        script = "; ".join(
            [
                self._remote_path_bootstrap(),
                "command -v tmux >/dev/null 2>&1",
                (
                    "tmux capture-pane -p -e "
                    f"-S {shlex.quote(start)} "
                    f"-t {shlex.quote(self.tmux_session)}"
                ),
            ]
        )
        return await self._run_remote_capture_command(self._remote_shell_command(script))

    async def _capture_local_history(self, lines: int = 100000) -> str:
        """Capture full terminal history from tmux for replay on reconnect.

        Captures the entire terminal content (scrollback + visible screen)
        because the client-side replay must clear and rewrite the full
        buffer when ttyd has already rendered the visible screen before
        our script can intercept.

        Returns an empty string if the tmux session does not exist yet
        (ttyd creates sessions lazily on first WebSocket connection).
        """
        if not await self._runtime._tmux_session_exists_async(self.tmux_session):
            return ""
        safe_lines = max(100, min(lines, 100000))
        start = f"-{safe_lines}"

        proc = await asyncio.create_subprocess_exec(
            *self._runtime.tmux_command(
                "capture-pane",
                "-p",
                "-e",
                "-S",
                start,
                "-t",
                self.tmux_session,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux capture-pane failed with code {proc.returncode}")

        return stdout.decode("utf-8", errors="ignore")

    async def capture_history(self, lines: int = 100000, prefer_remote: bool = False) -> str:
        if prefer_remote and self.target == ExecutionTarget.REMOTE:
            try:
                remote_history = await self._capture_remote_history(lines)
                if remote_history:
                    return remote_history
            except Exception as e:
                logger.debug(
                    "Falling back to local history for remote tab %s after remote capture failed: %s",
                    self.tab_id,
                    e,
                )
        return await self._capture_local_history(lines)

    async def _capture_local_cursor_position(self) -> Optional[CursorPosition]:
        """Capture tmux pane cursor position as zero-based x/y coordinates."""
        if not await self._runtime._tmux_session_exists_async(self.tmux_session):
            return None

        proc = await asyncio.create_subprocess_exec(
            *self._runtime.tmux_command(
                "display-message",
                "-p",
                "-t",
                self.tmux_session,
                "#{cursor_x} #{cursor_y}",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux display-message failed with code {proc.returncode}")

        parts = stdout.decode("utf-8", errors="ignore").strip().split()
        if len(parts) != 2:
            return None

        try:
            return {"cursor_x": int(parts[0]), "cursor_y": int(parts[1])}
        except ValueError:
            return None

    async def _capture_remote_cursor_position(self) -> Optional[CursorPosition]:
        script = "; ".join(
            [
                self._remote_path_bootstrap(),
                "command -v tmux >/dev/null 2>&1",
                (
                    "tmux display-message -p "
                    f"-t {shlex.quote(self.tmux_session)} "
                    f"{shlex.quote('#{cursor_x} #{cursor_y}')}"
                ),
            ]
        )
        stdout = await self._run_remote_capture_command(self._remote_shell_command(script))
        parts = stdout.strip().split()
        if len(parts) != 2:
            return None
        try:
            return {"cursor_x": int(parts[0]), "cursor_y": int(parts[1])}
        except ValueError:
            return None

    async def capture_cursor_position(
        self,
        prefer_remote: bool = False,
    ) -> Optional[CursorPosition]:
        if prefer_remote and self.target == ExecutionTarget.REMOTE:
            try:
                remote_cursor = await self._capture_remote_cursor_position()
                if remote_cursor:
                    return remote_cursor
            except Exception as e:
                logger.debug(
                    "Falling back to local cursor for remote tab %s after remote capture failed: %s",
                    self.tab_id,
                    e,
                )
        return await self._capture_local_cursor_position()

    async def capture_foreground_command(self) -> Optional[str]:
        """Capture the foreground command currently running in the tmux pane."""
        if not await self._runtime._tmux_session_exists_async(self.tmux_session):
            return None

        proc = await asyncio.create_subprocess_exec(
            *self._runtime.tmux_command(
                "display-message",
                "-p",
                "-t",
                self.tmux_session,
                "#{pane_current_command}",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        command = stdout.decode("utf-8", errors="ignore").strip()
        return command or None
