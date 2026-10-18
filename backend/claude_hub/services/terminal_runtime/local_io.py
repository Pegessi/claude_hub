"""Local io operations with explicit runtime dependencies."""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...config import settings
from ...models import AgentStreamEvent, AgentType
from ..provider_proxy import provider_subprocess_env
from .bindings import RuntimeBindings
from .defaults import _PORT_CHECK_TIMEOUT_SECONDS
from .records import _TabStateCommitError

logger = logging.getLogger("claude_hub.services.ttyd_manager")


def _write_private_launch_file(runtime: RuntimeBindings, path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_local_port_listening(runtime: RuntimeBindings, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_CHECK_TIMEOUT_SECONDS)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _is_local_port_available(runtime: RuntimeBindings, port: int) -> bool:
    """Return whether a loopback TCP port can currently be bound.

    Unlike a connect probe this detects bound-but-not-yet-listening sockets and
    does not send traffic to unrelated local services. The check cannot reserve
    the port for ttyd, so create_tab also retries a raced bind failure.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
    return True


def _parse_optional_iso(runtime: RuntimeBindings, value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating None / empty / malformed input."""
    if not value:
        return None
    try:
        return runtime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning("Failed to parse ISO timestamp %r; treating as None", value)
        return None


def _write_tabs_snapshot(runtime: RuntimeBindings, payload: Dict[str, Any]) -> None:
    """Persist one snapshot; distinguish failure before/after replacement."""
    text = json.dumps(payload)
    # The configured runtime home may be an alias. Capture one real parent so
    # temporary creation, replacement and durability confirmation agree.
    parent = runtime.STATE_FILE.parent.resolve()
    destination = parent / runtime.STATE_FILE.name
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tabs-", suffix=".tmp", dir=parent)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        replaced = True
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise _TabStateCommitError(exc, replaced=replaced) from exc
    finally:
        if not replaced:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _event_turn_ordinals(runtime: RuntimeBindings, events: List[AgentStreamEvent]) -> List[int]:
    """Assign each event a turn ordinal by first appearance of its ``turn_id``.

    The first time a ``turn_id`` appears it opens a new turn (ordinal = number
    of distinct turns seen so far); every later event with the same
    ``turn_id`` reuses that ordinal, even when turns interleave
    (e.g. turn_ids [A, B, A] -> ordinals [0, 1, 0]).

    Events without a ``turn_id`` (legacy) are folded into the currently open
    turn (the ordinal of the most recent turn_id event); when no turn is open
    yet they open turn 0. This keeps legacy events from breaking the prefix
    boundary instead of creating artificial turns.

    The returned list is monotonic-by-turn (not by index): a fork at ordinal N
    keeps exactly the events whose ordinal is <= N, which is correct even when
    turns interleave.
    """
    ordinals: List[int] = []
    turn_index_by_id: Dict[str, int] = {}
    current_ordinal: Optional[int] = None
    for ev in events:
        if ev.turn_id:
            idx = turn_index_by_id.get(ev.turn_id)
            if idx is None:
                idx = len(turn_index_by_id)
                turn_index_by_id[ev.turn_id] = idx
            current_ordinal = idx
            ordinals.append(idx)
        else:
            if current_ordinal is None:
                current_ordinal = 0
            ordinals.append(current_ordinal)
    return ordinals


def _agent_spawn_env(runtime: RuntimeBindings) -> dict:
    """Environment for ttyd/tmux-spawned agent panes.

    Agent TUIs (Cursor/Claude/Codex) gate their styled rendering on color
    support. Two things flatten them into a colorless, low-contrast UI here:

    1. Inside tmux the inner TERM is ``tmux-256color`` with ``COLORTERM`` unset.
    2. When the backend is launched from a parent process that disables color
       (e.g. another agent CLI exports ``NO_COLOR=1`` / ``FORCE_COLOR=0``),
       those flags are inherited all the way down to the agent panes and
       suppress every escape sequence — so the input prompt, dimmed
       placeholders and accent colors all collapse to one flat shade.

    Normalize the environment so panes always render with their full palette,
    regardless of how the backend itself was started.
    """
    env = provider_subprocess_env("terminal", os.environ)
    env.pop("NO_COLOR", None)
    env["COLORTERM"] = "truecolor"
    env["FORCE_COLOR"] = "3"
    return env


def get_default_command(runtime: RuntimeBindings) -> str:
    return settings.default_command


def get_agent_command(runtime: RuntimeBindings, agent_type: AgentType) -> str:
    if agent_type == AgentType.CODEX:
        return "codex"
    if agent_type == AgentType.TRAEX:
        return "traex"
    if agent_type == AgentType.CURSOR:
        return "agent"
    return runtime.get_default_command()
