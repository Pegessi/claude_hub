"""Runtime algorithms shared by the agent-stream compatibility facade."""

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Optional

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ChatMode,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    StreamCapabilities,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream import StructuredSourceUnavailable, TailerManager
from claude_hub.services.agent_stream.base import AgentStreamAdapter

ChatLiveness = Literal["active", "dead", "terminalized", "unknown"]


def terminal_tab_stream_session(
    tab_id: str,
    *,
    ttyd_manager: Any,
    stream_session_id: Callable[[str], str],
    stream_workspace_id: str,
) -> Optional[ManagedSession]:
    """Build the minimal stream descriptor for a live terminal AI tab.

    This is deliberately ephemeral: normal tabs remain normal tabs and do not
    appear in the workspace board.  ``SessionTailer`` needs only the provider
    source metadata, while its event store is namespaced under
    ``terminal-tabs``.
    """

    tab = ttyd_manager.get_tab(tab_id)
    if tab is None:
        return None

    now = datetime.now()
    return ManagedSession(
        id=stream_session_id(tab_id),
        workspace_id=stream_workspace_id,
        tab_id=tab.id,
        role=WorkspaceSessionRole.WORKER,
        agent_type=tab.agent_type,
        session_kind=tab.session_kind,
        chat_mode=getattr(tab, "chat_mode", ChatMode.DEFAULT),
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=tab.name,
        # A local tab without an explicit cwd inherits the backend process cwd
        # (the same directory its launcher shell uses).  Preserve that effective
        # source location so the first Claude transcript can be discovered.
        workspace_path=tab.cwd
        or (os.getcwd() if tab.target.value == "local" else tab.remote_cwd or ""),
        tmux_session=f"claude-hub-{tab.id[:8]}",
        target=tab.target,
        remote_profile_id=tab.remote_profile_id,
        remote_cwd=tab.remote_cwd,
        remote_reconnect=tab.remote_reconnect,
        solo_mode=tab.solo_mode,
        env=dict(tab.env),
        agent_session_id=tab.agent_session_id,
        agent_session_id_verified=getattr(tab, "agent_session_id_verified", False),
        cursor_transport=tab.cursor_transport,
        cursor_data_dir=tab.cursor_data_dir,
        cursor_cli_version=tab.cursor_cli_version,
        cursor_transcript_path=tab.cursor_transcript_path,
        cursor_transcript_schema=tab.cursor_transcript_schema,
        created_at=tab.created_at,
        updated_at=now,
    )


async def scheduled_chat_turn_liveness(
    tab_id: str,
    turn_id: str,
    *,
    terminal_session: Callable[[str], Optional[ManagedSession]],
    get_tab_tailer_manager: Callable[[], TailerManager],
    logger: logging.Logger,
) -> ChatLiveness | None:
    """Live liveness probe for the scheduler's stale-run reaper.

    Returns ``"active"`` when the provider is busy (this turn OR a newer
    unrelated turn, e.g. a manual composer message — a raised guard is never
    "dead", regardless of which turn owns it), ``"terminalized"`` when a dead
    durable orphan for this turn was just closed (its completion observer is in
    flight), ``"dead"`` when the provider is idle and no unfinished turn
    remains, and ``"unknown"`` when no tailer exists or a *different* orphan is
    still open (the run must not be redelivered into someone else's turn).
    """
    session = terminal_session(tab_id)
    if session is None:
        # The target tab is gone; the drain's deleted-target rule owns this.
        return "dead"
    tailer = get_tab_tailer_manager().get_tailer(session.id)
    if tailer is None:
        return "unknown"
    transport = tailer.native_transport
    if transport is not None and transport.turn_in_flight:
        # The human composer bypasses the scheduler and can own the guard with
        # a newer turn while a scheduled run is stale. That is not evidence the
        # scheduled run is dead: stay conservative and re-probe after the
        # guard clears (next reaper pass terminalizes the real orphan then).
        return "active"
    # Provider idle while the run is still marked in flight: the durable
    # transcript holds an orphan. Terminalize our own (Stop-equivalent) so the
    # persisted completion edge releases the FIFO via its observers.
    try:
        repaired = await tailer.cancel_turn(expected_turn_id=turn_id)
    except Exception:
        logger.exception(
            "Scheduled run liveness: orphan terminalization failed for tab=%s turn=%s",
            tab_id,
            turn_id,
        )
        return "unknown"
    if repaired:
        return "terminalized"
    # No terminalization happened. If a *different* unfinished turn is open,
    # redelivering this run would collide with it (and busy-park forever);
    # leave the run alone for the next pass rather than call it dead.
    try:
        other_orphan = await tailer.store.latest_unfinished_turn()
    except Exception:
        logger.exception(
            "Scheduled run liveness: orphan lookup failed for tab=%s turn=%s",
            tab_id,
            turn_id,
        )
        return "unknown"
    if other_orphan is not None and other_orphan.turn_id != turn_id:
        return "unknown"
    return "dead"


async def capabilities_for(
    get_default_manager: Callable[[], TailerManager],
    is_chat_native: Callable[[ManagedSession], bool],
    adapter_for_session: Callable[[ManagedSession], Optional[AgentStreamAdapter]],
    session: ManagedSession,
    manager: Optional[TailerManager] = None,
) -> StreamCapabilities:
    """Return structured-stream capabilities for ``session``.

    For CHAT sessions the native provider transport is the sole owner of the
    real-time stream, so capabilities (including ``supports_images``) come
    from the transport. Transcript discovery is only used for TERMINAL
    sessions and Cursor's ``terminal_transcript`` fallback.

    Fail-closed: if the native transport could not be created, return
    ``structured=False`` rather than silently falling back to the transcript.
    """

    manager = manager or get_default_manager()

    if is_chat_native(session):
        # Ensure the tailer (and its native transport) exist. ``ensure_started``
        # creates the transport synchronously; it does not require a turn to
        # have been sent.
        try:
            tailer = await manager.ensure_started(session)
        except (ValueError, StructuredSourceUnavailable):
            return StreamCapabilities()
        if tailer.native_error is not None:
            # Native transport creation failed — fail closed.
            return StreamCapabilities(structured=False)
        transport = tailer.native_transport
        if transport is None:
            return StreamCapabilities(structured=False)
        await transport.prepare_capabilities()
        caps = transport.capabilities()
        if manager.hard_failed(session.id):
            caps = caps.model_copy(update={"structured": False})
        return caps

    # TERMINAL sessions (and Cursor terminal_transcript fallback): use the
    # adapter's transcript-based discovery.
    adapter = adapter_for_session(session)
    if adapter is None:
        return StreamCapabilities()
    caps = adapter.capabilities(session)
    if manager.hard_failed(session.id):
        caps = caps.model_copy(update={"structured": False})
    return caps


async def tab_capabilities_for(
    capabilities: Callable[..., Awaitable[StreamCapabilities]],
    adapter_for_session: Callable[[ManagedSession], Optional[AgentStreamAdapter]],
    session: ManagedSession,
    manager: TailerManager,
) -> StreamCapabilities:
    """Advertise an empty, ready-to-compose view before its first transcript.

    Claude and Codex create their transcript lazily, often only after the first
    submitted prompt.  A Terminal tab is already a concrete agent source at
    that point, so hiding Paseo until a file exists would make its composer
    unusable for the first turn.  Unsupported transports (notably raw Cursor)
    remain fail-closed because they have no adapter at all.
    """

    caps = await capabilities(session, manager)
    adapter = adapter_for_session(session)
    if (
        not caps.structured
        and adapter is not None
        and not manager.hard_failed(session.id)
        and adapter.supports_transcript_discovery
    ):
        # Providers with a wired on-disk transcript (Claude/Codex) are promoted
        # to structured lazily before their first rollout exists. Adapters that
        # disable transcript discovery (TraeX this wave) fail closed to the raw
        # terminal; Chat sessions returned native caps above and never land here.
        return caps.model_copy(update={"structured": True})
    return caps
