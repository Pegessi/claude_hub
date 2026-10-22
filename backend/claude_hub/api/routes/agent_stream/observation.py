"""Capabilities, replay, long-poll, and live agent-stream routes."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from claude_hub.api.routes.agent_stream.bindings import AgentStreamRouteBindings
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventPage,
    ChatMode,
    ManagedSession,
    StreamCapabilities,
    User,
)
from claude_hub.services.agent_stream import StructuredSourceUnavailable, TailerManager
from claude_hub.services.agent_stream.base import discover_source_cached

_DEFAULT_WAIT_TIMEOUT_S = 30.0
_MAX_WAIT_TIMEOUT_S = 60.0
_MAX_HISTORY_PAGE_SIZE = 5_000
_SSE_HEALTH_POLL_S = 1.0
_SSE_HEARTBEAT_S = 15.0
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class AgentStreamWaitRequest(BaseModel):
    # Store sequences start at zero and the cursor is exclusive, so -1 is the
    # only value that can request the first persisted event.
    since_sequence: int = Field(-1, ge=-1)
    timeout_seconds: Optional[float] = Field(None, ge=0)


class AgentStreamModeRequest(BaseModel):
    mode: str = Field(..., min_length=1, max_length=32)


def managed_default_mode_capabilities(capabilities: StreamCapabilities) -> StreamCapabilities:
    """Project provider capabilities onto managed Task execution policy."""
    return capabilities.model_copy(
        update={
            "available_modes": [
                option
                for option in capabilities.available_modes
                if option.id == ChatMode.DEFAULT.value
            ],
            "current_mode": ChatMode.DEFAULT.value,
            "supports_dynamic_modes": False,
        }
    )


async def retry_stream_for(
    session: ManagedSession,
    manager: TailerManager,
    bindings: AgentStreamRouteBindings,
    *,
    direct_tab: bool = False,
) -> StreamCapabilities:
    try:
        await manager.retry(session)
    except (ValueError, StructuredSourceUnavailable, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if direct_tab:
        return await bindings.tab_capabilities_for(session, manager)
    return managed_default_mode_capabilities(await bindings.capabilities_for(session, manager))


async def set_stream_mode_for(
    session: ManagedSession,
    manager: TailerManager,
    mode: str,
    bindings: AgentStreamRouteBindings,
    *,
    direct_tab: bool = False,
) -> StreamCapabilities:
    if not bindings.is_chat_native(session):
        raise HTTPException(
            status_code=400, detail="mode is only available for native Chat sessions"
        )
    if not direct_tab and mode != ChatMode.DEFAULT.value:
        raise HTTPException(
            status_code=409,
            detail="Managed Chat mode is controlled by task execution",
        )
    from claude_hub.services.goal_run import get_goal_admission_lock, get_goal_manager

    tab_id = session.tab_id
    admission = get_goal_admission_lock(tab_id) if direct_tab else asyncio.Lock()
    try:
        async with admission:
            if direct_tab and mode == ChatMode.PLAN.value:
                goal = get_goal_manager().current(tab_id)
                if goal is not None and (
                    goal.status.value == "active" or goal.dispatch_state.value != "idle"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Pause or finish the active Goal before switching to Plan mode",
                    )
            await manager.set_mode(session, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        status_code = 409 if "in flight" in str(exc) else 503
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    if direct_tab:
        return await bindings.tab_capabilities_for(session, manager)
    return managed_default_mode_capabilities(await bindings.capabilities_for(session, manager))


async def stream_events_for(
    session: ManagedSession,
    manager: TailerManager,
    since_sequence: int,
    limit: int,
    bindings: AgentStreamRouteBindings,
    allow_pending_source: bool = False,
    compact: bool = False,
    view: Literal["raw", "conversation"] = "raw",
    turn_id: Optional[str] = None,
    through_sequence: Optional[int] = None,
    persisted_only: bool = False,
) -> AgentStreamEventPage:
    if (turn_id is None) != (through_sequence is None):
        raise HTTPException(
            status_code=400, detail="turn_id and through_sequence must be provided together"
        )
    if view == "conversation" and turn_id is not None:
        raise HTTPException(status_code=400, detail="conversation view cannot filter one turn")
    if persisted_only or session.archived:
        store = manager.get_store(session.workspace_id, session.id)
        if view == "conversation":
            return await store.read_conversation(since_sequence, limit, compact=compact)
        if turn_id is not None and through_sequence is not None:
            return await store.read_turn_since(
                turn_id, since_sequence, through_sequence, limit, compact=compact
            )
        if compact:
            return await store.read_since(since_sequence, limit, compact=True)
        return await store.read_since(since_sequence, limit)
    caps = await bindings.capabilities_for(session, manager, cache_preparation=True)
    if not caps.structured and not allow_pending_source:
        return AgentStreamEventPage(events=[], next_sequence=since_sequence, has_more=False)
    try:
        await manager.ensure_started(session)
    except ValueError:
        return AgentStreamEventPage(events=[], next_sequence=since_sequence, has_more=False)
    except StructuredSourceUnavailable:
        bindings.raise_structured_unavailable(session.id, manager)
    if manager.hard_failed(session.id):
        bindings.raise_structured_unavailable(session.id, manager)
    store = manager.get_store(session.workspace_id, session.id)
    if view == "conversation":
        return await store.read_conversation(since_sequence, limit, compact=compact)
    if turn_id is not None and through_sequence is not None:
        return await store.read_turn_since(
            turn_id, since_sequence, through_sequence, limit, compact=compact
        )
    if compact:
        return await store.read_since(since_sequence, limit, compact=True)
    return await store.read_since(since_sequence, limit)


async def wait_stream_events_for(
    session: ManagedSession,
    manager: TailerManager,
    session_exists: Callable[[], bool],
    payload: AgentStreamWaitRequest,
    bindings: AgentStreamRouteBindings,
    allow_pending_source: bool = False,
    capabilities: Optional[StreamCapabilities] = None,
) -> AgentStreamEventPage:
    since = payload.since_sequence
    caps = capabilities or await bindings.capabilities_for(
        session,
        manager,
        cache_preparation=True,
    )
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
        bindings.raise_structured_unavailable(session.id, manager)
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
                bindings.raise_structured_unavailable(session.id, manager)
            if manager.hard_failed(session.id):
                bindings.raise_structured_unavailable(session.id, manager)
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


async def stream_live_for(
    session: ManagedSession,
    manager: TailerManager,
    session_exists: Callable[[], bool],
    missing_message: str,
    since_sequence: int,
    bindings: AgentStreamRouteBindings,
    allow_pending_source: bool = False,
    cache_preparation: bool = False,
) -> StreamingResponse:
    caps = await bindings.capabilities_for(
        session,
        manager,
        cache_preparation=cache_preparation,
    )
    if not session_exists():
        return bindings.closed_unavailable_stream(caps, missing_message)
    if not caps.structured and not allow_pending_source:
        return bindings.closed_unavailable_stream(
            caps,
            "structured observation unavailable for this session",
        )

    if manager.hard_failed(session.id):
        return bindings.closed_unavailable_stream(
            caps, bindings.structured_failure_message(session.id, manager)
        )
    try:
        queue = await manager.subscribe(session)
    except StructuredSourceUnavailable:
        return bindings.closed_unavailable_stream(
            caps, bindings.structured_failure_message(session.id, manager)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not session_exists():
        manager.unsubscribe(session.id, queue)
        return bindings.closed_unavailable_stream(caps, missing_message)
    if allow_pending_source:
        caps = await bindings.tab_capabilities_for(
            session,
            manager,
            cache_preparation=cache_preparation,
        )
    store = manager.get_store(session.workspace_id, session.id)

    async def event_stream() -> Any:
        try:
            if not session_exists():
                yield bindings.sse("error", {"message": missing_message})
                return
            yield bindings.sse("hello", caps.model_dump())
            delivered_sequence = since_sequence

            while True:
                page = await store.read_since(delivered_sequence, limit=200)
                if not session_exists():
                    yield bindings.sse("error", {"message": missing_message})
                    return
                for replay_event in page.events:
                    if replay_event.stream_sequence <= delivered_sequence:
                        continue
                    delivered_sequence = replay_event.stream_sequence
                    yield bindings.sse(
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
                    yield bindings.sse(
                        "error",
                        {"message": missing_message},
                    )
                    return
                if manager.hard_failed(session.id):
                    yield bindings.sse(
                        "error",
                        {"message": bindings.structured_failure_message(session.id, manager)},
                    )
                    return
                try:
                    event: AgentStreamEvent = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEALTH_POLL_S
                    )
                except asyncio.TimeoutError:
                    if not session_exists():
                        yield bindings.sse("error", {"message": missing_message})
                        return
                    if loop.time() - last_heartbeat >= _SSE_HEARTBEAT_S:
                        yield ": ping\n\n"
                        last_heartbeat = loop.time()
                    continue
                if not session_exists():
                    yield bindings.sse("error", {"message": missing_message})
                    return
                if event.stream_sequence <= delivered_sequence:
                    continue
                delivered_sequence = event.stream_sequence
                yield bindings.sse("agent-stream", json.loads(event.model_dump_json()))
        except asyncio.CancelledError:
            raise
        finally:
            manager.unsubscribe(session.id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@dataclass(frozen=True)
class AgentStreamObservationEndpoints:
    get_stream_capabilities: Callable[..., Coroutine[Any, Any, StreamCapabilities]]
    get_tab_stream_capabilities: Callable[..., Coroutine[Any, Any, StreamCapabilities]]
    retry_stream: Callable[..., Coroutine[Any, Any, StreamCapabilities]]
    retry_tab_stream: Callable[..., Coroutine[Any, Any, StreamCapabilities]]
    set_stream_mode: Callable[..., Coroutine[Any, Any, StreamCapabilities]]
    set_tab_stream_mode: Callable[..., Coroutine[Any, Any, StreamCapabilities]]
    get_stream_events: Callable[..., Coroutine[Any, Any, AgentStreamEventPage]]
    get_tab_stream_events: Callable[..., Coroutine[Any, Any, AgentStreamEventPage]]
    wait_stream_events: Callable[..., Coroutine[Any, Any, AgentStreamEventPage]]
    wait_tab_stream_events: Callable[..., Coroutine[Any, Any, AgentStreamEventPage]]
    stream_live: Callable[..., Coroutine[Any, Any, StreamingResponse]]
    stream_tab_live: Callable[..., Coroutine[Any, Any, StreamingResponse]]
    get_stream_diagnostics: Callable[..., Coroutine[Any, Any, Dict[str, Any]]]


def register_observation_routes(
    router: APIRouter, bindings: AgentStreamRouteBindings
) -> AgentStreamObservationEndpoints:
    """Register observation routes in their historical order."""

    @router.get(
        "/sessions/{managed_session_id}/stream/capabilities",
        response_model=StreamCapabilities,
    )
    async def get_stream_capabilities(
        managed_session_id: str,
        current_user: User = Depends(get_current_user),
        refresh: bool = False,
    ) -> StreamCapabilities:
        session = bindings.session_or_404(managed_session_id)
        manager = bindings.get_tailer_manager()
        async with bindings.managed_stream_interaction(session) as live:
            capabilities = await bindings.capabilities_for(
                live,
                manager,
                cache_preparation=True,
                refresh=refresh,
            )
        bindings.require_current_managed_session(session, manager)
        return managed_default_mode_capabilities(capabilities)

    @router.get(
        "/tabs/{tab_id}/stream/capabilities",
        response_model=StreamCapabilities,
    )
    async def get_tab_stream_capabilities(
        tab_id: str,
        current_user: User = Depends(get_current_user),
        refresh: bool = False,
    ) -> StreamCapabilities:
        """Return structured-stream capability for a direct Chat tab."""

        session = bindings.terminal_tab_session_or_404(tab_id)
        return await bindings.tab_capabilities_for(
            session,
            bindings.get_tab_tailer_manager(),
            cache_preparation=True,
            refresh=refresh,
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

        session = bindings.session_or_404(managed_session_id)
        manager = bindings.get_tailer_manager()
        async with bindings.managed_stream_interaction(session) as live:
            capabilities = await retry_stream_for(live, manager, bindings)
        bindings.require_current_managed_session(session, manager)
        return capabilities

    @router.post(
        "/tabs/{tab_id}/stream/retry",
        response_model=StreamCapabilities,
    )
    async def retry_tab_stream(
        tab_id: str,
        current_user: User = Depends(get_current_user),
    ) -> StreamCapabilities:
        """Retry a direct Agent tab without changing its persisted conversation."""

        session = bindings.terminal_tab_session_or_404(tab_id)
        return await retry_stream_for(
            session,
            bindings.get_tab_tailer_manager(),
            bindings,
            direct_tab=True,
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
        session = bindings.session_or_404(managed_session_id)
        manager = bindings.get_tailer_manager()
        async with bindings.managed_stream_interaction(session) as live:
            capabilities = await set_stream_mode_for(live, manager, payload.mode, bindings)
        bindings.require_current_managed_session(session, manager)
        return capabilities

    @router.put(
        "/tabs/{tab_id}/stream/mode",
        response_model=StreamCapabilities,
    )
    async def set_tab_stream_mode(
        tab_id: str,
        payload: AgentStreamModeRequest,
        current_user: User = Depends(get_current_user),
    ) -> StreamCapabilities:
        session = bindings.terminal_tab_session_or_404(tab_id)
        return await set_stream_mode_for(
            session,
            bindings.get_tab_tailer_manager(),
            payload.mode,
            bindings,
            direct_tab=True,
        )

    @router.get(
        "/sessions/{managed_session_id}/stream/events",
        response_model=AgentStreamEventPage,
    )
    async def get_stream_events(
        managed_session_id: str,
        since_sequence: int = Query(-1, ge=-1),
        limit: int = Query(200, ge=1, le=_MAX_HISTORY_PAGE_SIZE),
        compact: bool = Query(False),
        view: Literal["raw", "conversation"] = Query("raw"),
        turn_id: Optional[str] = Query(None, min_length=1),
        through_sequence: Optional[int] = Query(None, ge=0),
        current_user: User = Depends(get_current_user),
        persisted_only: bool = False,
    ) -> AgentStreamEventPage:
        session = bindings.session_or_404(managed_session_id)
        manager = bindings.get_tailer_manager()
        if persisted_only or session.archived:
            bindings.require_current_managed_session(session, manager)
            page = await bindings.stream_events_for(
                session,
                manager,
                since_sequence,
                limit,
                compact=compact,
                view=view,
                turn_id=turn_id,
                through_sequence=through_sequence,
                persisted_only=True,
            )
            bindings.require_current_managed_session(session, manager)
            return page
        async with bindings.managed_stream_interaction(session) as live:
            page = await bindings.stream_events_for(
                live,
                manager,
                since_sequence,
                limit,
                compact=compact,
                view=view,
                turn_id=turn_id,
                through_sequence=through_sequence,
                persisted_only=False,
            )
        bindings.require_current_managed_session(session, manager)
        return page

    @router.get(
        "/tabs/{tab_id}/stream/events",
        response_model=AgentStreamEventPage,
    )
    async def get_tab_stream_events(
        tab_id: str,
        since_sequence: int = Query(-1, ge=-1),
        limit: int = Query(200, ge=1, le=_MAX_HISTORY_PAGE_SIZE),
        compact: bool = Query(False),
        view: Literal["raw", "conversation"] = Query("raw"),
        turn_id: Optional[str] = Query(None, min_length=1),
        through_sequence: Optional[int] = Query(None, ge=0),
        current_user: User = Depends(get_current_user),
        persisted_only: bool = False,
    ) -> AgentStreamEventPage:
        session = bindings.terminal_tab_session_or_404(tab_id)
        return await bindings.stream_events_for(
            session,
            bindings.get_tab_tailer_manager(),
            since_sequence,
            limit,
            allow_pending_source=True,
            compact=compact,
            view=view,
            turn_id=turn_id,
            through_sequence=through_sequence,
            persisted_only=persisted_only,
        )

    @router.post(
        "/sessions/{managed_session_id}/stream/wait",
        response_model=AgentStreamEventPage,
    )
    async def wait_stream_events(
        managed_session_id: str,
        payload: AgentStreamWaitRequest,
        current_user: User = Depends(get_current_user),
    ) -> AgentStreamEventPage:
        session = bindings.session_or_404(managed_session_id)
        manager = bindings.get_tailer_manager()
        async with bindings.managed_stream_interaction(session) as live:
            capabilities = await bindings.capabilities_for(
                live,
                manager,
                cache_preparation=True,
            )
        page = await bindings.wait_stream_events_for(
            session,
            manager,
            lambda: manager.session_is_current(session),
            payload,
            capabilities=capabilities,
        )
        bindings.require_current_managed_session(session, manager)
        return page

    @router.post(
        "/tabs/{tab_id}/stream/wait",
        response_model=AgentStreamEventPage,
    )
    async def wait_tab_stream_events(
        tab_id: str,
        payload: AgentStreamWaitRequest,
        current_user: User = Depends(get_current_user),
    ) -> AgentStreamEventPage:
        session = bindings.terminal_tab_session_or_404(tab_id)
        manager = bindings.get_tab_tailer_manager()
        capabilities = await bindings.tab_capabilities_for(
            session,
            manager,
            cache_preparation=True,
        )
        return await bindings.wait_stream_events_for(
            session,
            manager,
            lambda: bindings.terminal_tab_stream_session(tab_id) is not None,
            payload,
            allow_pending_source=True,
            capabilities=capabilities,
        )

    @router.get("/sessions/{managed_session_id}/stream/live")
    async def stream_live(
        managed_session_id: str,
        since_sequence: int = Query(-1, ge=-1),
        current_user: User = Depends(get_current_user),
    ) -> StreamingResponse:
        session = bindings.session_or_404(managed_session_id)
        manager = bindings.get_tailer_manager()
        async with bindings.managed_stream_interaction(session) as live:
            response = await stream_live_for(
                live,
                manager,
                lambda: manager.session_is_current(session),
                "session was deleted or replaced",
                since_sequence,
                bindings,
                cache_preparation=True,
            )
        bindings.require_current_managed_session(session, manager)
        return response

    @router.get("/tabs/{tab_id}/stream/live")
    async def stream_tab_live(
        tab_id: str,
        since_sequence: int = Query(-1, ge=-1),
        current_user: User = Depends(get_current_user),
    ) -> StreamingResponse:
        session = bindings.terminal_tab_session_or_404(tab_id)
        return await stream_live_for(
            session,
            bindings.get_tab_tailer_manager(),
            lambda: bindings.terminal_tab_stream_session(tab_id) is not None,
            "terminal tab was deleted",
            since_sequence,
            bindings,
            allow_pending_source=True,
            cache_preparation=True,
        )

    @router.get("/sessions/{managed_session_id}/stream/diagnostics")
    async def get_stream_diagnostics(
        managed_session_id: str,
        current_user: User = Depends(get_current_user),
    ) -> Dict[str, Any]:
        session = bindings.session_or_404(managed_session_id)
        manager = bindings.get_tailer_manager()
        async with bindings.managed_stream_interaction(session) as live:
            store = manager.get_store(live.workspace_id, live.id)
            adapter = bindings.adapter_for_session(live)
            if adapter is None:
                result = {
                    "adapter_id": "none",
                    "structured": False,
                    "schema_version": 0,
                    "event_count": await store.count(),
                    "last_event_at": await store.last_event_at(),
                }
            else:
                structured = (await bindings.capabilities_for(live, manager)).structured
                tailer = manager.get_tailer(live.id)
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
                        source = discover_source_cached(adapter, live)
                    except Exception:
                        source = None
                    if source is not None:
                        tail_path = str(source)

                result = {
                    "adapter_id": adapter.adapter_id,
                    "structured": structured,
                    "schema_version": adapter.schema_version,
                    "tail_path": tail_path,
                    "last_error": last_error,
                    "event_count": await store.count(),
                    "last_event_at": await store.last_event_at(),
                }
        bindings.require_current_managed_session(session, manager)
        return result

    return AgentStreamObservationEndpoints(
        get_stream_capabilities=get_stream_capabilities,
        get_tab_stream_capabilities=get_tab_stream_capabilities,
        retry_stream=retry_stream,
        retry_tab_stream=retry_tab_stream,
        set_stream_mode=set_stream_mode,
        set_tab_stream_mode=set_tab_stream_mode,
        get_stream_events=get_stream_events,
        get_tab_stream_events=get_tab_stream_events,
        wait_stream_events=wait_stream_events,
        wait_tab_stream_events=wait_tab_stream_events,
        stream_live=stream_live,
        stream_tab_live=stream_tab_live,
        get_stream_diagnostics=get_stream_diagnostics,
    )
