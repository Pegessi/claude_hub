"""Tmux control operations with explicit runtime dependencies."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Optional

from .bindings import RuntimeBindings
from .defaults import (
    _TMUX_CONTROL_TIMEOUT_SECONDS,
    _TMUX_LAUNCH_ENV_KEYS,
    _TMUX_STALE_ENV_KEYS,
    TMUX_SESSION_PREFIX,
)

logger = logging.getLogger("claude_hub.services.ttyd_manager")


def _tmux_session_name(runtime: RuntimeBindings, tab_id: str) -> str:
    return f"{TMUX_SESSION_PREFIX}{tab_id[:8]}"


def _tmux_server_running(runtime: RuntimeBindings) -> bool:
    """Return whether the configured tmux server accepts server-scoped queries."""
    try:
        ret = subprocess.run(
            runtime.tmux_command("show-options", "-s"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TMUX_CONTROL_TIMEOUT_SECONDS,
        ).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return ret == 0


def _refresh_tmux_server_environment(runtime: RuntimeBindings) -> None:
    """Make future panes inherit this backend's stable launch environment.

    A named tmux server outlives the process that first created it. Without an
    explicit refresh, a server first started by an integration test can retain
    that test's temporary HOME/PATH and pass them to later real Agent panes.
    Session-specific credentials remain in the per-tab launch wrapper; this
    boundary only repairs stable process-launch keys and removes test markers.
    """

    for key in _TMUX_LAUNCH_ENV_KEYS:
        value = os.environ.get(key)
        command = (
            runtime.tmux_command("set-environment", "-g", key, value)
            if value is not None
            else runtime.tmux_command("set-environment", "-gu", key)
        )
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TMUX_CONTROL_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("Timed out or failed to refresh tmux global environment key %s", key)
            continue
        if result.returncode != 0:
            logger.warning("Failed to refresh tmux global environment key %s", key)

    for key in _TMUX_STALE_ENV_KEYS:
        try:
            result = subprocess.run(
                runtime.tmux_command("set-environment", "-gu", key),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TMUX_CONTROL_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("Timed out or failed to clear stale tmux global environment key %s", key)
            continue
        if result.returncode != 0:
            logger.warning("Failed to clear stale tmux global environment key %s", key)


def _ensure_tmux_server(runtime: RuntimeBindings) -> bool:
    """Ensure tmux server is running, start it if not."""
    if runtime._tmux_server_running():
        logger.debug("tmux server is already running")
        runtime._refresh_tmux_server_environment()
        return True
    try:
        # Start a dummy session to initialize tmux server, then detach
        # This ensures the tmux server stays running even with no sessions
        logger.info("tmux server not running, starting it...")
        ret = subprocess.run(
            runtime.tmux_command("new-session", "-d", "-s", "__tmux_server_keepalive__"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TMUX_CONTROL_TIMEOUT_SECONDS,
        ).returncode
        if ret == 0:
            logger.info("tmux server started successfully with keepalive session")
            runtime._refresh_tmux_server_environment()
        return True
    except Exception as e:
        logger.warning(f"Failed to start tmux server: {e}")
        return False


def _tmux_session_exists(runtime: RuntimeBindings, session_name: str) -> bool:
    """Check if a tmux session exists (sync, used during startup).

    Uses ``subprocess.run`` rather than ``os.system`` so it does not spawn a
    shell and does not block the asyncio event loop via the shell's own
    waitpid. Hot async paths should prefer :func:`_tmux_session_exists_async`
    or batch existence via :func:`_tmux_list_sessions`.
    """
    try:
        ret = subprocess.run(
            runtime.tmux_command("has-session", "-t", session_name),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except FileNotFoundError:
        return False
    return ret == 0


async def _tmux_session_exists_async(runtime: RuntimeBindings, session_name: str) -> bool:
    """Async, non-blocking check for whether a tmux session exists."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *runtime.tmux_command("has-session", "-t", session_name),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return await proc.wait() == 0


async def _tmux_list_sessions(runtime: RuntimeBindings) -> set[str]:
    """Return the set of live tmux session names in a single subprocess call.

    Batching existence into one call lets board refreshes check many tabs
    against an in-memory set instead of spawning one ``has-session`` process
    per tab (previously a blocking ``os.system`` per tab that serialized the
    event loop).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *runtime.tmux_command("list-sessions", "-F", "#{session_name}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return set()
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return set()
    return {line for line in stdout.decode("utf-8", errors="ignore").splitlines() if line}


async def _tmux_list_session_created(runtime: RuntimeBindings) -> dict[str, float]:
    """Return live tmux session names with their creation epoch."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *runtime.tmux_command("list-sessions", "-F", "#{session_name}\t#{session_created}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return {}
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {}

    sessions: dict[str, float] = {}
    for line in stdout.decode("utf-8", errors="ignore").splitlines():
        name, separator, created = line.partition("\t")
        if not separator:
            continue
        try:
            sessions[name] = float(created)
        except ValueError:
            logger.warning("Ignoring tmux session with invalid creation time: %r", line)
    return sessions


async def _finish_tmux_launcher_rollback(
    runtime: RuntimeBindings, proc: asyncio.subprocess.Process, session_name: str
) -> None:
    """Reap a cancelled tmux launcher before removing its owned session."""
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
    await runtime._tmux_kill_session(session_name)


async def _tmux_kill_session(runtime: RuntimeBindings, session_name: str) -> None:
    """Kill a tmux session and prove it is no longer present.

    ``tmux kill-session`` can return non-zero for an already-absent session,
    which is an acceptable terminal state.  A command that returns success
    while the session remains present is not acceptable for ownership
    quarantine, so always verify absence after the command completes.
    """
    proc = await asyncio.create_subprocess_exec(
        *runtime.tmux_command("kill-session", "-t", session_name),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if await runtime._tmux_session_exists_async(session_name):
        detail = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(
            f"tmux session {session_name} still exists after kill-session "
            f"(rc={proc.returncode}, stderr={detail!r})"
        )


def _tmux_session_created(runtime: RuntimeBindings, session_name: str) -> Optional[float]:
    """Return the tmux ``session_created`` epoch for a live session.

    tmux survives a backend restart, so the creation time is the deploy-moment
    anchor used to correlate a tab with the Claude conversation that started
    alongside it. Returns ``None`` when the session is not alive (e.g. after a
    reboot) or tmux is unavailable.
    """
    try:
        proc = subprocess.run(
            runtime.tmux_command(
                "display-message",
                "-p",
                "-t",
                session_name,
                "#{session_created}",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
