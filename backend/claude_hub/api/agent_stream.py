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
from typing import Any, Dict, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..models import (
    AgentStreamEvent,
    AgentStreamEventPage,
    ManagedSession,
    StreamCapabilities,
    User,
)
from ..services import workspace_manager
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


def _get_tailer_manager() -> TailerManager:
    global _tailer_manager
    if _tailer_manager is None:
        _tailer_manager = TailerManager(
            session_getter=lambda sid: workspace_manager.sessions.get(sid)
        )
    return _tailer_manager


def _reset_tailer_manager() -> None:
    global _tailer_manager
    _tailer_manager = None


def _session_or_404(session_id: str) -> ManagedSession:
    session = workspace_manager.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _capabilities_for(session: ManagedSession) -> StreamCapabilities:
    adapter = get_adapter_for_session(session)
    if adapter is None:
        return StreamCapabilities()
    caps = adapter.capabilities(session)
    if _get_tailer_manager().hard_failed(session.id):
        caps = caps.model_copy(update={"structured": False})
    return caps


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _structured_failure_message(session_id: str) -> str:
    tailer = _get_tailer_manager().get_tailer(session_id)
    if tailer is not None and tailer.last_error:
        return tailer.last_error
    return "structured transcript source unavailable"


def _raise_structured_unavailable(session_id: str) -> NoReturn:
    raise HTTPException(status_code=409, detail=_structured_failure_message(session_id))


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
    since_sequence: int = Field(0, ge=0)
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


# ── history / replay ─────────────────────────────────────────────────────────


@router.get(
    "/sessions/{managed_session_id}/stream/events",
    response_model=AgentStreamEventPage,
)
async def get_stream_events(
    managed_session_id: str,
    since_sequence: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
) -> AgentStreamEventPage:
    session = _session_or_404(managed_session_id)
    adapter = get_adapter_for_session(session)
    if adapter is None or not adapter.capabilities(session).structured:
        return AgentStreamEventPage(events=[], next_sequence=since_sequence, has_more=False)
    manager = _get_tailer_manager()
    try:
        await manager.ensure_started(session)
    except ValueError:
        return AgentStreamEventPage(events=[], next_sequence=since_sequence, has_more=False)
    except StructuredSourceUnavailable:
        _raise_structured_unavailable(session.id)
    if manager.hard_failed(session.id):
        _raise_structured_unavailable(session.id)
    store = manager.get_store(session.workspace_id, session.id)
    return await store.read_since(since_sequence, limit)


# ── long-poll ────────────────────────────────────────────────────────────────


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
    since = payload.since_sequence
    adapter = get_adapter_for_session(session)
    if adapter is None or not adapter.capabilities(session).structured:
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    timeout = payload.timeout_seconds
    if timeout is None:
        timeout = _DEFAULT_WAIT_TIMEOUT_S
    timeout = min(max(timeout, 0.0), _MAX_WAIT_TIMEOUT_S)

    manager = _get_tailer_manager()
    store = manager.get_store(session.workspace_id, session.id)
    try:
        queue = await manager.subscribe(session)
    except StructuredSourceUnavailable:
        _raise_structured_unavailable(session.id)
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            page = await store.read_since(since, limit=200)
            if page.events:
                return page
            if manager.hard_failed(session.id):
                _raise_structured_unavailable(session.id)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return page
            try:
                await asyncio.wait_for(queue.get(), timeout=min(remaining, _SSE_HEALTH_POLL_S))
            except asyncio.TimeoutError:
                pass
    finally:
        manager.unsubscribe(session.id, queue)


# ── live SSE ─────────────────────────────────────────────────────────────────


@router.get("/sessions/{managed_session_id}/stream/live")
async def stream_live(
    managed_session_id: str,
    since_sequence: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    session = _session_or_404(managed_session_id)

    adapter = get_adapter_for_session(session)
    if adapter is None or not adapter.capabilities(session).structured:
        return _closed_unavailable_stream(
            StreamCapabilities(),
            "structured observation unavailable for this session",
        )

    manager = _get_tailer_manager()
    if manager.hard_failed(session.id):
        return _closed_unavailable_stream(
            _capabilities_for(session), _structured_failure_message(session.id)
        )
    try:
        queue = await manager.subscribe(session)
    except StructuredSourceUnavailable:
        return _closed_unavailable_stream(
            _capabilities_for(session), _structured_failure_message(session.id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    caps = _capabilities_for(session)
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
                if manager.hard_failed(session.id):
                    yield _sse(
                        "error",
                        {"message": _structured_failure_message(session.id)},
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
