"""Structured agent-stream HTTP surface (Layer B observation plane).

Implements the routes under the existing workspace session resource::

    GET  /api/workspaces/sessions/{id}/stream/capabilities
    GET  /api/workspaces/sessions/{id}/stream/events?since_sequence&limit
    POST /api/workspaces/sessions/{id}/stream/wait
    GET  /api/workspaces/sessions/{id}/stream/live          (SSE)
    GET  /api/workspaces/sessions/{id}/stream/diagnostics

Design points:

- **Additive** — these routes only read the new per-session event log and drive
  the new tailer; they never touch ``/api/tabs``, ``/api/terminal/*``, or the
  task/report/mailbox APIs.
- **Fail-closed to raw** — ``capabilities`` reports ``structured=False`` whenever
  no adapter exists for the exact session transport OR the tailer has hard-failed.
- **History first** — ``GET /events`` ensures the session's tailer is started;
  on first touch the tailer runs one inline backfill poll before its live loop.
- **Sequence-safe SSE handoff** — the client hydrates via ``/events`` first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, NoReturn, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ..auth.dependencies import get_current_user
from ..models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentStreamEventPage,
    AgentType,
    ChatMode,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    StreamCapabilities,
    User,
    WorkspaceSessionRole,
)
from ..services import ttyd_manager, workspace_manager
from ..services.agent_stream import (
    StructuredSourceUnavailable,
    TailerManager,
    get_adapter_for_session,
)
from ..services.agent_stream.attachments import AgentStreamAttachmentStore
from ..services.agent_stream.base import discover_source_cached

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspaces", tags=["agent-stream"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_DEFAULT_WAIT_TIMEOUT_S = 30.0
_MAX_WAIT_TIMEOUT_S = 60.0
_MAX_HISTORY_PAGE_SIZE = 5_000
_SSE_HEALTH_POLL_S = 1.0
_SSE_HEARTBEAT_S = 15.0

_tailer_manager: Optional[TailerManager] = None
_tab_tailer_manager: Optional[TailerManager] = None

# Terminal-created AI tabs do not have an Agent Workspace record, but their
# transcript and pinned provider conversation id are just as real as a managed
# agent's.  Give them an isolated stream namespace rather than inventing a
# visible Workspace or (worse) treating a provider conversation UUID as a
# managed-session UUID.
_TAB_STREAM_WORKSPACE_ID = "terminal-tabs"
_TAB_STREAM_SESSION_PREFIX = "terminal-tab-"


def _persist_workspace_agent_session_id(session_id: str, conversation_id: str) -> None:
    """Persist a provider-owned conversation id for a managed Agent session."""

    workspace_manager.set_session_agent_session_id(session_id, conversation_id)


def _persist_tab_agent_session_id(stream_session_id: str, conversation_id: str) -> None:
    """Persist a provider-owned conversation id for an Agent tab."""

    ttyd_manager.set_tab_agent_session_id(
        stream_session_id.removeprefix(_TAB_STREAM_SESSION_PREFIX), conversation_id
    )


def _persist_tab_chat_mode(stream_session_id: str, mode: str) -> None:
    ttyd_manager.set_tab_chat_mode(stream_session_id.removeprefix(_TAB_STREAM_SESSION_PREFIX), mode)


def _get_tailer_manager() -> TailerManager:
    global _tailer_manager
    if _tailer_manager is None:
        _tailer_manager = TailerManager(
            session_getter=lambda sid: workspace_manager.sessions.get(sid),
            persist_session_id=_persist_workspace_agent_session_id,
        )
    return _tailer_manager


def _tab_stream_session_id(tab_id: str) -> str:
    return f"{_TAB_STREAM_SESSION_PREFIX}{tab_id}"


def _terminal_tab_stream_session(tab_id: str) -> Optional[ManagedSession]:
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
        id=_tab_stream_session_id(tab_id),
        workspace_id=_TAB_STREAM_WORKSPACE_ID,
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
        created_at=now,
        updated_at=now,
    )


def _terminal_tab_stream_session_by_id(stream_session_id: str) -> Optional[ManagedSession]:
    if not stream_session_id.startswith(_TAB_STREAM_SESSION_PREFIX):
        return None
    return _terminal_tab_stream_session(stream_session_id.removeprefix(_TAB_STREAM_SESSION_PREFIX))


def _get_tab_tailer_manager() -> TailerManager:
    global _tab_tailer_manager
    if _tab_tailer_manager is None:
        _tab_tailer_manager = TailerManager(
            session_getter=_terminal_tab_stream_session_by_id,
            persist_session_id=_persist_tab_agent_session_id,
            persist_mode=_persist_tab_chat_mode,
        )
    return _tab_tailer_manager


def _reset_tailer_manager() -> None:
    global _tailer_manager, _tab_tailer_manager
    _tailer_manager = None
    _tab_tailer_manager = None


def _session_or_404(session_id: str) -> ManagedSession:
    session = workspace_manager.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _terminal_tab_session_or_404(tab_id: str) -> ManagedSession:
    session = _terminal_tab_stream_session(tab_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Terminal tab not found")
    return session


def _is_chat_native(session: ManagedSession) -> bool:
    """True when the session must use its native provider transport.

    CHAT sessions own a single native ``ProviderSession`` as their real-time
    source. The only exception is Cursor's explicit ``terminal_transcript``
    compatibility mode, which keeps the transcript-file tailer.
    """

    if session.session_kind != SessionKind.CHAT:
        return False
    if session.agent_type == AgentType.CURSOR and session.cursor_transport == "terminal_transcript":
        return False
    return True


async def _capabilities_for(
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

    manager = manager or _get_tailer_manager()

    if _is_chat_native(session):
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
    adapter = get_adapter_for_session(session)
    if adapter is None:
        return StreamCapabilities()
    caps = adapter.capabilities(session)
    if manager.hard_failed(session.id):
        caps = caps.model_copy(update={"structured": False})
    return caps


async def _tab_capabilities_for(
    session: ManagedSession, manager: TailerManager
) -> StreamCapabilities:
    """Advertise an empty, ready-to-compose view before its first transcript.

    Claude and Codex create their transcript lazily, often only after the first
    submitted prompt.  A Terminal tab is already a concrete agent source at
    that point, so hiding Paseo until a file exists would make its composer
    unusable for the first turn.  Unsupported transports (notably raw Cursor)
    remain fail-closed because they have no adapter at all.
    """

    caps = await _capabilities_for(session, manager)
    if (
        not caps.structured
        and get_adapter_for_session(session) is not None
        and not manager.hard_failed(session.id)
    ):
        return caps.model_copy(update={"structured": True})
    return caps


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _structured_failure_message(session_id: str, manager: Optional[TailerManager] = None) -> str:
    tailer = (manager or _get_tailer_manager()).get_tailer(session_id)
    if tailer is not None and tailer.last_error:
        return tailer.last_error
    return "structured transcript source unavailable"


def _raise_structured_unavailable(
    session_id: str,
    manager: Optional[TailerManager] = None,
) -> NoReturn:
    raise HTTPException(status_code=409, detail=_structured_failure_message(session_id, manager))


def _closed_unavailable_stream(capabilities: StreamCapabilities, message: str) -> StreamingResponse:
    async def unavailable_stream() -> Any:
        yield _sse("hello", capabilities.model_dump())
        yield _sse("error", {"message": message})

    return StreamingResponse(
        unavailable_stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


class AgentStreamWaitRequest(BaseModel):
    # Store sequences start at zero and the cursor is exclusive, so -1 is the
    # only value that can request the first persisted event.
    since_sequence: int = Field(-1, ge=-1)
    timeout_seconds: Optional[float] = Field(None, ge=0)


class AgentStreamModeRequest(BaseModel):
    mode: str = Field(..., min_length=1, max_length=32)


# ── capabilities ─────────────────────────────────────────────────────────────


@router.get(
    "/sessions/{managed_session_id}/stream/capabilities",
    response_model=StreamCapabilities,
)
async def get_stream_capabilities(
    managed_session_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamCapabilities:
    session = _session_or_404(managed_session_id)
    return await _capabilities_for(session)


@router.get(
    "/tabs/{tab_id}/stream/capabilities",
    response_model=StreamCapabilities,
)
async def get_tab_stream_capabilities(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamCapabilities:
    """Return structured-stream capability for a direct Chat tab."""

    session = _terminal_tab_session_or_404(tab_id)
    return await _tab_capabilities_for(session, _get_tab_tailer_manager())


async def _retry_stream_for(
    session: ManagedSession,
    manager: TailerManager,
    *,
    direct_tab: bool = False,
) -> StreamCapabilities:
    try:
        await manager.retry(session)
    except (ValueError, StructuredSourceUnavailable, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return (
        await _tab_capabilities_for(session, manager)
        if direct_tab
        else await _capabilities_for(session, manager)
    )


@router.post(
    "/sessions/{managed_session_id}/stream/retry",
    response_model=StreamCapabilities,
)
async def retry_stream(
    managed_session_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamCapabilities:
    """Explicitly replace a failed native provider transport."""

    session = _session_or_404(managed_session_id)
    return await _retry_stream_for(session, _get_tailer_manager())


@router.post(
    "/tabs/{tab_id}/stream/retry",
    response_model=StreamCapabilities,
)
async def retry_tab_stream(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamCapabilities:
    """Retry a direct Agent tab without changing its persisted conversation."""

    session = _terminal_tab_session_or_404(tab_id)
    return await _retry_stream_for(
        session,
        _get_tab_tailer_manager(),
        direct_tab=True,
    )


async def _set_stream_mode_for(
    session: ManagedSession,
    manager: TailerManager,
    mode: str,
    *,
    direct_tab: bool = False,
) -> StreamCapabilities:
    if not _is_chat_native(session):
        raise HTTPException(
            status_code=400, detail="mode is only available for native Chat sessions"
        )
    try:
        await manager.set_mode(session, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        status_code = 409 if "in flight" in str(exc) else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return (
        await _tab_capabilities_for(session, manager)
        if direct_tab
        else await _capabilities_for(session, manager)
    )


@router.put(
    "/sessions/{managed_session_id}/stream/mode",
    response_model=StreamCapabilities,
)
async def set_stream_mode(
    managed_session_id: str,
    payload: AgentStreamModeRequest,
    current_user: User = Depends(get_current_user),
) -> StreamCapabilities:
    session = _session_or_404(managed_session_id)
    return await _set_stream_mode_for(session, _get_tailer_manager(), payload.mode)


@router.put(
    "/tabs/{tab_id}/stream/mode",
    response_model=StreamCapabilities,
)
async def set_tab_stream_mode(
    tab_id: str,
    payload: AgentStreamModeRequest,
    current_user: User = Depends(get_current_user),
) -> StreamCapabilities:
    session = _terminal_tab_session_or_404(tab_id)
    return await _set_stream_mode_for(
        session,
        _get_tab_tailer_manager(),
        payload.mode,
        direct_tab=True,
    )


async def _stream_events_for(
    session: ManagedSession,
    manager: TailerManager,
    since_sequence: int,
    limit: int,
    allow_pending_source: bool = False,
) -> AgentStreamEventPage:
    caps = await _capabilities_for(session, manager)
    if not caps.structured and not allow_pending_source:
        return AgentStreamEventPage(events=[], next_sequence=since_sequence, has_more=False)
    try:
        await manager.ensure_started(session)
    except ValueError:
        return AgentStreamEventPage(events=[], next_sequence=since_sequence, has_more=False)
    except StructuredSourceUnavailable:
        _raise_structured_unavailable(session.id, manager)
    if manager.hard_failed(session.id):
        _raise_structured_unavailable(session.id, manager)
    store = manager.get_store(session.workspace_id, session.id)
    return await store.read_since(since_sequence, limit)


# ── history / replay ─────────────────────────────────────────────────────────


@router.get(
    "/sessions/{managed_session_id}/stream/events",
    response_model=AgentStreamEventPage,
)
async def get_stream_events(
    managed_session_id: str,
    since_sequence: int = Query(-1, ge=-1),
    limit: int = Query(200, ge=1, le=_MAX_HISTORY_PAGE_SIZE),
    current_user: User = Depends(get_current_user),
) -> AgentStreamEventPage:
    session = _session_or_404(managed_session_id)
    return await _stream_events_for(session, _get_tailer_manager(), since_sequence, limit)


@router.get(
    "/tabs/{tab_id}/stream/events",
    response_model=AgentStreamEventPage,
)
async def get_tab_stream_events(
    tab_id: str,
    since_sequence: int = Query(-1, ge=-1),
    limit: int = Query(200, ge=1, le=_MAX_HISTORY_PAGE_SIZE),
    current_user: User = Depends(get_current_user),
) -> AgentStreamEventPage:
    session = _terminal_tab_session_or_404(tab_id)
    return await _stream_events_for(
        session,
        _get_tab_tailer_manager(),
        since_sequence,
        limit,
        allow_pending_source=True,
    )


# ── long-poll ────────────────────────────────────────────────────────────────


async def _wait_stream_events_for(
    session: ManagedSession,
    manager: TailerManager,
    session_exists: Callable[[], bool],
    payload: AgentStreamWaitRequest,
    allow_pending_source: bool = False,
) -> AgentStreamEventPage:
    since = payload.since_sequence
    caps = await _capabilities_for(session, manager)
    if not caps.structured and not allow_pending_source:
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    timeout = payload.timeout_seconds
    if timeout is None:
        timeout = _DEFAULT_WAIT_TIMEOUT_S
    timeout = min(max(timeout, 0.0), _MAX_WAIT_TIMEOUT_S)

    store = manager.get_store(session.workspace_id, session.id)
    try:
        queue = await manager.subscribe(session)
    except StructuredSourceUnavailable:
        _raise_structured_unavailable(session.id, manager)
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        # Initial/reconnect catch-up: read_since ONCE after subscribe. This
        # returns every event persisted before we subscribed. ``_publish``
        # persists before fanout, so any event published after this point is
        # already in our queue — we never need to re-scan the JSONL to find
        # it.
        page = await store.read_since(since, limit=200)
        if page.events:
            return page

        while True:
            if not session_exists():
                _raise_structured_unavailable(session.id, manager)
            if manager.hard_failed(session.id):
                _raise_structured_unavailable(session.id, manager)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=min(remaining, _SSE_HEALTH_POLL_S)
                )
            except asyncio.TimeoutError:
                # Health tick: only existence / hard_failure / deadline are
                # checked above. We deliberately do NOT call read_since here —
                # that would re-scan the full JSONL from byte 0 on every 1s
                # tick, which dominates CPU for long sessions.
                continue

            # Consume the queue event directly. ``_publish`` persists before
            # fanout, so the event is already durable.
            if event.stream_sequence <= since:
                # Already delivered via the initial read_since or a prior
                # batch; skip without touching the store.
                continue

            if event.stream_sequence > since + 1:
                # Gap: the queue skipped one or more sequences (e.g. the
                # subscriber queue overflowed and dropped events). Reconcile
                # by reading the store once. This is the only path besides
                # the initial catch-up that calls read_since.
                return await store.read_since(since, limit=200)

            # event.stream_sequence == since + 1: contiguous with our cursor.
            # Drain as many contiguous events as are already queued so the
            # frontend gets a full batch instead of one event per round-trip.
            events: List[AgentStreamEvent] = [event]
            next_seq = event.stream_sequence
            while len(events) < 200:
                try:
                    next_event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_event.stream_sequence <= next_seq:
                    # Stale (seq <= since) or duplicate of an already-collected
                    # event. ``_fanout`` puts each event once per subscriber,
                    # so duplicates should not occur under normal operation,
                    # but we skip defensively rather than treating them as a
                    # gap.
                    continue
                if next_event.stream_sequence != next_seq + 1:
                    # Mid-drain gap: stop. The events we've collected are
                    # contiguous and safe to return; the gap will be filled
                    # by the next request's read_since(since=next_seq).
                    break
                events.append(next_event)
                next_seq = next_event.stream_sequence

            # has_more: we filled the batch to its limit and the subscriber
            # queue still holds events. In a single-threaded asyncio context
            # no producer can run between the last get_nowait and this check,
            # so qsize() is a stable snapshot. The long-poll client ignores
            # has_more (it always re-requests), but the hydration loop and
            # any other consumer rely on it to know whether more events are
            # immediately available.
            has_more = len(events) == 200 and queue.qsize() > 0
            return AgentStreamEventPage(events=events, next_sequence=next_seq, has_more=has_more)
    finally:
        manager.unsubscribe(session.id, queue)


@router.post(
    "/sessions/{managed_session_id}/stream/wait",
    response_model=AgentStreamEventPage,
)
async def wait_stream_events(
    managed_session_id: str,
    payload: AgentStreamWaitRequest,
    current_user: User = Depends(get_current_user),
) -> AgentStreamEventPage:
    session = _session_or_404(managed_session_id)
    return await _wait_stream_events_for(
        session,
        _get_tailer_manager(),
        lambda: workspace_manager.sessions.get(session.id) is not None,
        payload,
    )


@router.post(
    "/tabs/{tab_id}/stream/wait",
    response_model=AgentStreamEventPage,
)
async def wait_tab_stream_events(
    tab_id: str,
    payload: AgentStreamWaitRequest,
    current_user: User = Depends(get_current_user),
) -> AgentStreamEventPage:
    session = _terminal_tab_session_or_404(tab_id)
    return await _wait_stream_events_for(
        session,
        _get_tab_tailer_manager(),
        lambda: _terminal_tab_stream_session(tab_id) is not None,
        payload,
        allow_pending_source=True,
    )


# ── live SSE ─────────────────────────────────────────────────────────────────


async def _stream_live_for(
    session: ManagedSession,
    manager: TailerManager,
    session_exists: Callable[[], bool],
    missing_message: str,
    since_sequence: int,
    allow_pending_source: bool = False,
) -> StreamingResponse:
    caps = await _capabilities_for(session, manager)
    if not caps.structured and not allow_pending_source:
        return _closed_unavailable_stream(
            caps,
            "structured observation unavailable for this session",
        )

    if manager.hard_failed(session.id):
        return _closed_unavailable_stream(caps, _structured_failure_message(session.id, manager))
    try:
        queue = await manager.subscribe(session)
    except StructuredSourceUnavailable:
        return _closed_unavailable_stream(caps, _structured_failure_message(session.id, manager))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if allow_pending_source:
        caps = await _tab_capabilities_for(session, manager)
    store = manager.get_store(session.workspace_id, session.id)

    async def event_stream() -> Any:
        try:
            yield _sse("hello", caps.model_dump())
            delivered_sequence = since_sequence

            while True:
                page = await store.read_since(delivered_sequence, limit=200)
                for replay_event in page.events:
                    if replay_event.stream_sequence <= delivered_sequence:
                        continue
                    delivered_sequence = replay_event.stream_sequence
                    yield _sse(
                        "agent-stream",
                        json.loads(replay_event.model_dump_json()),
                    )
                if not page.has_more:
                    break

            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            while True:
                # If the owning resource disappears, terminate instead of
                # heartbeating forever. ``hard_failed`` returns False once the
                # tailer is gone, so existence must be checked independently.
                if not session_exists():
                    yield _sse(
                        "error",
                        {"message": missing_message},
                    )
                    return
                if manager.hard_failed(session.id):
                    yield _sse(
                        "error",
                        {"message": _structured_failure_message(session.id, manager)},
                    )
                    return
                try:
                    event: AgentStreamEvent = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEALTH_POLL_S
                    )
                except asyncio.TimeoutError:
                    if loop.time() - last_heartbeat >= _SSE_HEARTBEAT_S:
                        yield ": ping\n\n"
                        last_heartbeat = loop.time()
                    continue
                if event.stream_sequence <= delivered_sequence:
                    continue
                delivered_sequence = event.stream_sequence
                yield _sse("agent-stream", json.loads(event.model_dump_json()))
        except asyncio.CancelledError:
            raise
        finally:
            manager.unsubscribe(session.id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/sessions/{managed_session_id}/stream/live")
async def stream_live(
    managed_session_id: str,
    since_sequence: int = Query(-1, ge=-1),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    session = _session_or_404(managed_session_id)
    return await _stream_live_for(
        session,
        _get_tailer_manager(),
        lambda: workspace_manager.sessions.get(session.id) is not None,
        "session was deleted",
        since_sequence,
    )


@router.get("/tabs/{tab_id}/stream/live")
async def stream_tab_live(
    tab_id: str,
    since_sequence: int = Query(-1, ge=-1),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    session = _terminal_tab_session_or_404(tab_id)
    return await _stream_live_for(
        session,
        _get_tab_tailer_manager(),
        lambda: _terminal_tab_stream_session(tab_id) is not None,
        "terminal tab was deleted",
        since_sequence,
        allow_pending_source=True,
    )


# ── diagnostics ──────────────────────────────────────────────────────────────


@router.get("/sessions/{managed_session_id}/stream/diagnostics")
async def get_stream_diagnostics(
    managed_session_id: str,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    session = _session_or_404(managed_session_id)
    manager = _get_tailer_manager()
    store = manager.get_store(session.workspace_id, session.id)

    adapter = get_adapter_for_session(session)
    if adapter is None:
        return {
            "adapter_id": "none",
            "structured": False,
            "schema_version": 0,
            "event_count": await store.count(),
            "last_event_at": await store.last_event_at(),
        }

    tailer = manager.get_tailer(session.id)
    structured = (await _capabilities_for(session, manager)).structured
    tail_path: Optional[str] = None
    last_error: Optional[str] = None
    if tailer is not None:
        source = tailer.current_source
        if source is not None:
            tail_path = str(source)
        last_error = tailer.last_error
        # For CHAT sessions the native transport is the source; surface its
        # error if the transport creation failed.
        if tailer.native_error is not None and not last_error:
            last_error = tailer.native_error
    else:
        try:
            source = discover_source_cached(adapter, session)
        except Exception:
            source = None
        if source is not None:
            tail_path = str(source)

    return {
        "adapter_id": adapter.adapter_id,
        "structured": structured,
        "schema_version": adapter.schema_version,
        "tail_path": tail_path,
        "last_error": last_error,
        "event_count": await store.count(),
        "last_event_at": await store.last_event_at(),
    }


# ── Native composer input (send) ────────────────────────────────────────────

# Hard limits on composer attachments. These protect the backend from
# pathological payloads before any provider subprocess is involved.
_MAX_ATTACHMENTS = 10
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MiB decoded per file
_MAX_TOTAL_ATTACHMENT_BYTES = 40 * 1024 * 1024  # 40 MiB total decoded per request


class AgentStreamAttachment(BaseModel):
    filename: str
    mime_type: str
    data_url: str
    # Bounded preview (max edge 1024px) generated by the frontend for display.
    # Only the preview is persisted to the attachment cache; the original
    # ``data_url`` bytes are sent to the provider and never stored.
    preview_data_url: Optional[str] = None


class AgentStreamSendRequest(BaseModel):
    # Text may be empty for image-only turns, but at least one of text or
    # attachments must be present (validated in the model validator).
    text: str = ""
    attachments: List[AgentStreamAttachment] = Field(default_factory=list)
    # Stable turn id generated by the frontend. The tailer echoes it back on
    # every event of the turn (turn_started, deltas, turn_completed) so the
    # frontend can upsert by identity instead of text matching. Two identical
    # user messages therefore remain two distinct turns.
    client_turn_id: str
    # ``normal``: fail with 409 when a turn is already in flight.
    # ``steer``: cancel the active turn, then deliver this message.
    delivery: str = Field(default="normal", pattern="^(normal|steer)$")

    @model_validator(mode="after")
    def _require_text_or_attachments(self) -> "AgentStreamSendRequest":
        if not self.text.strip() and not self.attachments:
            raise ValueError("send request must include text or at least one attachment")
        if not self.client_turn_id.strip():
            raise ValueError("client_turn_id is required")
        return self


def _parse_data_url(data_url: str) -> Tuple[str, bytes]:
    """Parse a ``data:`` URL into ``(mime_type, bytes)``.

    Only ``data:<mime>;base64,<payload>`` form is accepted. The comma and
    ``;base64`` marker are required; malformed URLs (e.g. ``data:image/png``
    with no payload) are rejected with 400 rather than silently returning
    empty bytes.
    """
    import base64

    if not data_url.startswith("data:"):
        raise HTTPException(status_code=400, detail="attachment is not a data URL")
    rest = data_url[len("data:") :]
    comma_idx = rest.find(",")
    if comma_idx < 0:
        raise HTTPException(
            status_code=400,
            detail="attachment data URL is missing the payload comma",
        )
    header = rest[:comma_idx]
    b64 = rest[comma_idx + 1 :]
    # header is "<mime>[;base64]"
    parts = header.split(";")
    mime = parts[0] or "application/octet-stream"
    if "base64" not in parts[1:]:
        raise HTTPException(
            status_code=400,
            detail="attachment data URL must use base64 encoding",
        )
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"attachment is not valid base64: {exc}",
        )
    return mime, data


def _decode_attachments(
    attachments: List[AgentStreamAttachment],
) -> Tuple[List[bytes], Optional[List[bytes]]]:
    """Decode data-URL attachments into ``(originals, previews)``.

    Returns a list of original bytes and either a list of preview bytes (one
    per original) or ``None`` if no previews were supplied.

    Previews are all-or-nothing: either every attachment has a
    ``preview_data_url`` or none do. Mixed presence is rejected with 400.
    When previews are absent, ``previews`` is ``None`` so the tailer emits
    placeholder metadata and never persists original bytes.

    For originals, the declared ``mime_type``, the data URL media type, and
    the magic bytes must agree; unsupported MIME types (non-PNG/JPEG/GIF/WebP)
    are rejected.

    For previews, the data URL media type and the preview's own magic bytes
    must agree, and the preview MIME must be in the whitelist. The preview
    MIME is *not* required to match the original MIME — browsers commonly
    transcode PNG/GIF originals to bounded WebP/JPEG previews to meet the
    size cap. The store persists the preview under its detected MIME.

    Fail-closed: any malformed data URL, oversized file, MIME mismatch, or
    unsupported type raises ``HTTPException(400)``.
    """
    from claude_hub.services.agent_stream.attachments import (
        _ALLOWED_MIME_TYPES,
        _magic_mime,
    )

    if len(attachments) > _MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"too many attachments: {len(attachments)} > {_MAX_ATTACHMENTS}",
        )

    originals: List[bytes] = []
    previews: List[bytes] = []
    has_previews = [a.preview_data_url is not None for a in attachments]
    if any(has_previews) and not all(has_previews):
        raise HTTPException(
            status_code=400,
            detail="attachments must either all include previews or none",
        )
    any_preview = any(has_previews)

    total_bytes = 0
    for att in attachments:
        # Decode and validate the original.
        orig_mime, original = _parse_data_url(att.data_url)
        if len(original) > _MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r} exceeds size limit: "
                    f"{len(original)} > {_MAX_ATTACHMENT_BYTES} bytes"
                ),
            )
        total_bytes += len(original)
        if total_bytes > _MAX_TOTAL_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"total attachment size exceeds limit: "
                    f"{total_bytes} > {_MAX_TOTAL_ATTACHMENT_BYTES} bytes"
                ),
            )
        if att.mime_type not in _ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r} has unsupported mime type " f"{att.mime_type!r}"
                ),
            )
        if orig_mime != att.mime_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r}: data URL media type "
                    f"{orig_mime!r} does not match declared mime_type "
                    f"{att.mime_type!r}"
                ),
            )
        detected = _magic_mime(original)
        if detected != att.mime_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r}: magic bytes indicate "
                    f"{detected!r} but declared mime_type is {att.mime_type!r}"
                ),
            )
        originals.append(original)

        # Decode and validate the preview (if supplied). The preview MIME is
        # independent of the original MIME — browsers may transcode to a
        # different format to meet the size cap.
        if any_preview:
            prev_mime, preview = _parse_data_url(att.preview_data_url)  # type: ignore[arg-type]
            if len(preview) > _MAX_ATTACHMENT_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"attachment {att.filename!r} preview exceeds size limit: "
                        f"{len(preview)} > {_MAX_ATTACHMENT_BYTES} bytes"
                    ),
                )
            total_bytes += len(preview)
            if total_bytes > _MAX_TOTAL_ATTACHMENT_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"total attachment size exceeds limit: "
                        f"{total_bytes} > {_MAX_TOTAL_ATTACHMENT_BYTES} bytes"
                    ),
                )
            if prev_mime not in _ALLOWED_MIME_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"attachment {att.filename!r}: preview has unsupported "
                        f"mime type {prev_mime!r}"
                    ),
                )
            detected_prev = _magic_mime(preview)
            if detected_prev != prev_mime:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"attachment {att.filename!r}: preview magic bytes "
                        f"indicate {detected_prev!r} but data URL media type "
                        f"is {prev_mime!r}"
                    ),
                )
            previews.append(preview)

    return originals, (previews if any_preview else None)


# Attachment preview responses are short-lived and user-specific. The preview
# cache may evict entries at any time, so successful responses are privately
# cached for a bounded window; missing/evicted responses are never cached.
_ATTACHMENT_CACHE_CONTROL = "private, max-age=300"
_ATTACHMENT_NO_STORE = "no-store"


async def _read_attachment_or_404(
    store: "AgentStreamAttachmentStore", attachment_id: str
) -> Tuple[bytes, str]:
    """Return ``(preview_bytes, mime_type)`` for an existing attachment.

    Raises ``HTTPException(404)`` for an invalid attachment id format and
    ``HTTPException(410)`` for a valid id whose preview has been evicted or
    never existed. Both responses carry ``X-Content-Type-Options: nosniff``
    and ``Cache-Control: no-store`` so the frontend can render a visible
    "Preview expired" placeholder instead of a silently broken image.
    """
    import uuid

    # Distinguish invalid id format (404) from valid-but-evicted (410).
    try:
        uuid.UUID(attachment_id)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail="invalid attachment id",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": _ATTACHMENT_NO_STORE,
            },
        )
    try:
        return await store.read(attachment_id)
    except KeyError:
        raise HTTPException(
            status_code=410,
            detail="attachment preview not available",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": _ATTACHMENT_NO_STORE,
            },
        )


async def _send_to_native(
    session: ManagedSession,
    payload: AgentStreamSendRequest,
    manager: TailerManager,
) -> None:
    """Deliver composer input to the native provider transport atomically.

    CHAT sessions own a single ``ProviderSession`` that is both the stream
    source and the input sink. Text and images are delivered together via
    ``send_message`` so a failed send never leaves staged images that could
    pollute a later turn. This is the only input path for CHAT sessions —
    it must never fall back to tmux or the workspace outbox.
    """
    if not _is_chat_native(session):
        raise HTTPException(
            status_code=400,
            detail="native composer input is only available for CHAT sessions",
        )

    images, previews = _decode_attachments(payload.attachments)
    if images:
        # Verify the transport supports images before staging them.
        caps = await _capabilities_for(session, manager)
        if not caps.supports_images:
            raise HTTPException(
                status_code=400,
                detail=f"{session.agent_type.value} does not support image attachments",
            )
    await manager.send_message(
        session,
        payload.text,
        images,
        payload.client_turn_id,
        previews=previews,
        delivery=payload.delivery,
    )


def _map_send_exception(exc: Exception) -> HTTPException:
    """Map provider send errors to explicit HTTP status codes.

    ``ValueError`` (invalid image, bad input) → 400.
    ``NotImplementedError`` (provider lacks image support) → 400.
    ``RuntimeError`` for an in-flight turn → 409 Conflict.
    Other ``RuntimeError`` (no native transport, transport died) → 503.
    """
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, NotImplementedError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        if "turn is already in flight" in msg:
            return HTTPException(status_code=409, detail=msg)
        return HTTPException(status_code=503, detail=msg)
    # Unknown errors still fail closed, never as a bare 500.
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/sessions/{managed_session_id}/stream/send")
async def send_stream_input(
    managed_session_id: str,
    payload: AgentStreamSendRequest,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    session = _session_or_404(managed_session_id)
    manager = _get_tailer_manager()
    try:
        await _send_to_native(session, payload, manager)
    except StructuredSourceUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_send_exception(exc)
    return {"ok": True}


@router.post("/tabs/{tab_id}/stream/send")
async def send_tab_stream_input(
    tab_id: str,
    payload: AgentStreamSendRequest,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    session = _terminal_tab_session_or_404(tab_id)
    manager = _get_tab_tailer_manager()
    try:
        await _send_to_native(session, payload, manager)
    except StructuredSourceUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_send_exception(exc)
    return {"ok": True}


async def _cancel_native_turn(
    session: ManagedSession,
    manager: TailerManager,
) -> Dict[str, Any]:
    if not _is_chat_native(session):
        raise HTTPException(
            status_code=400,
            detail="native turn cancel is only available for CHAT sessions",
        )
    try:
        cancelled = await manager.cancel_turn(session)
    except StructuredSourceUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "cancelled": cancelled}


@router.post("/sessions/{managed_session_id}/stream/cancel")
async def cancel_stream_turn(
    managed_session_id: str,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    session = _session_or_404(managed_session_id)
    return await _cancel_native_turn(session, _get_tailer_manager())


@router.post("/tabs/{tab_id}/stream/cancel")
async def cancel_tab_stream_turn(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    session = _terminal_tab_session_or_404(tab_id)
    return await _cancel_native_turn(session, _get_tab_tailer_manager())


@router.get("/sessions/{managed_session_id}/stream/attachments/{attachment_id}")
async def get_session_attachment(
    managed_session_id: str,
    attachment_id: str,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Serve a bounded preview image for a managed session turn.

    Successful responses carry the whitelisted ``Content-Type``,
    ``X-Content-Type-Options: nosniff``, and a private cache policy. Missing
    or evicted previews return ``410 Gone`` (or ``404`` for an invalid id)
    with ``no-store`` so the frontend can render a visible "Preview expired"
    placeholder instead of a silently blank image.
    """
    session = _session_or_404(managed_session_id)
    store = AgentStreamAttachmentStore(session.workspace_id, session.id)
    data, mime = await _read_attachment_or_404(store, attachment_id)
    return Response(
        content=data,
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": _ATTACHMENT_CACHE_CONTROL,
        },
    )


@router.get("/tabs/{tab_id}/stream/attachments/{attachment_id}")
async def get_tab_attachment(
    tab_id: str,
    attachment_id: str,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Serve a bounded preview image for a terminal AI tab turn."""
    session = _terminal_tab_session_or_404(tab_id)
    store = AgentStreamAttachmentStore(_TAB_STREAM_WORKSPACE_ID, session.id)
    data, mime = await _read_attachment_or_404(store, attachment_id)
    return Response(
        content=data,
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": _ATTACHMENT_CACHE_CONTROL,
        },
    )


__all__ = ["router", "_reset_tailer_manager"]
