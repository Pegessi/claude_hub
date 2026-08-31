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
from typing import Any, Callable, Dict, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentStreamEventPage,
    ManagedSession,
    ManagedSessionStatus,
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


def _get_tailer_manager() -> TailerManager:
    global _tailer_manager
    if _tailer_manager is None:
        _tailer_manager = TailerManager(
            session_getter=lambda sid: workspace_manager.sessions.get(sid)
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
        _tab_tailer_manager = TailerManager(session_getter=_terminal_tab_stream_session_by_id)
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


def _capabilities_for(
    session: ManagedSession,
    manager: Optional[TailerManager] = None,
) -> StreamCapabilities:
    adapter = get_adapter_for_session(session)
    if adapter is None:
        return StreamCapabilities()
    caps = adapter.capabilities(session)
    if (manager or _get_tailer_manager()).hard_failed(session.id):
        caps = caps.model_copy(update={"structured": False})
    return caps


def _tab_capabilities_for(session: ManagedSession, manager: TailerManager) -> StreamCapabilities:
    """Advertise an empty, ready-to-compose view before its first transcript.

    Claude and Codex create their transcript lazily, often only after the first
    submitted prompt.  A Terminal tab is already a concrete agent source at
    that point, so hiding Paseo until a file exists would make its composer
    unusable for the first turn.  Unsupported transports (notably raw Cursor)
    remain fail-closed because they have no adapter at all.
    """

    caps = _capabilities_for(session, manager)
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
    return _capabilities_for(session)


@router.get(
    "/tabs/{tab_id}/stream/capabilities",
    response_model=StreamCapabilities,
)
async def get_tab_stream_capabilities(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamCapabilities:
    """Return structured-stream capability for a Terminal-created agent tab."""

    session = _terminal_tab_session_or_404(tab_id)
    return _tab_capabilities_for(session, _get_tab_tailer_manager())


async def _stream_events_for(
    session: ManagedSession,
    manager: TailerManager,
    since_sequence: int,
    limit: int,
    allow_pending_source: bool = False,
) -> AgentStreamEventPage:
    adapter = get_adapter_for_session(session)
    if adapter is None or (
        not allow_pending_source and not adapter.capabilities(session).structured
    ):
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
    limit: int = Query(200, ge=1, le=1000),
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
    limit: int = Query(200, ge=1, le=1000),
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
    adapter = get_adapter_for_session(session)
    if adapter is None or (
        not allow_pending_source and not adapter.capabilities(session).structured
    ):
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
        while True:
            page = await store.read_since(since, limit=200)
            if page.events:
                return page
            if not session_exists():
                _raise_structured_unavailable(session.id, manager)
            if manager.hard_failed(session.id):
                _raise_structured_unavailable(session.id, manager)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return page
            try:
                await asyncio.wait_for(queue.get(), timeout=min(remaining, _SSE_HEALTH_POLL_S))
            except asyncio.TimeoutError:
                pass
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
    adapter = get_adapter_for_session(session)
    if adapter is None or (
        not allow_pending_source and not adapter.capabilities(session).structured
    ):
        return _closed_unavailable_stream(
            StreamCapabilities(),
            "structured observation unavailable for this session",
        )

    if manager.hard_failed(session.id):
        return _closed_unavailable_stream(
            _capabilities_for(session, manager), _structured_failure_message(session.id, manager)
        )
    try:
        queue = await manager.subscribe(session)
    except StructuredSourceUnavailable:
        return _closed_unavailable_stream(
            _capabilities_for(session, manager), _structured_failure_message(session.id, manager)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    caps = (
        _tab_capabilities_for(session, manager)
        if allow_pending_source
        else _capabilities_for(session, manager)
    )
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
    structured = _capabilities_for(session).structured
    tail_path: Optional[str] = None
    last_error: Optional[str] = None
    if tailer is not None:
        source = tailer.current_source
        if source is not None:
            tail_path = str(source)
        last_error = tailer.last_error
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


__all__ = ["router", "_reset_tailer_manager"]
