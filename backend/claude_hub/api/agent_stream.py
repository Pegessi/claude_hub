"""Compatibility façade and runtime owner for structured agent-stream routes."""

from __future__ import annotations

import asyncio as asyncio
import json
import logging

# Explicit same-name aliases preserve legacy patch/import seams in this facade.
import os as os
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextlib import nullcontext as nullcontext
from datetime import datetime as datetime
from typing import Any, AsyncIterator, Callable, Dict, List, Literal, NoReturn, Optional, Tuple

from fastapi import (
    APIRouter,
)
from fastapi import Depends as Depends
from fastapi import (
    HTTPException,
)
from fastapi import Query as Query
from fastapi import Response as Response
from fastapi.responses import RedirectResponse as RedirectResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel as BaseModel
from pydantic import Field as Field
from pydantic import model_validator as model_validator

from claude_hub.api.routes.agent_stream import attachments as attachment_routes
from claude_hub.api.routes.agent_stream import controls, dispatch, media, messages, observation
from claude_hub.api.routes.agent_stream import runtime as stream_runtime
from claude_hub.api.routes.agent_stream import task_context

from ..auth.dependencies import get_current_user as get_current_user
from ..models import TERMINAL_GOAL_STATUSES as TERMINAL_GOAL_STATUSES
from ..models import AgentRuntimeStatus as AgentRuntimeStatus
from ..models import (
    AgentStreamEvent,
    AgentStreamEventPage,
    AgentStreamEventType,
    AgentType,
)
from ..models import ChatMode as ChatMode
from ..models import (
    ManagedSession,
)
from ..models import ManagedSessionStatus as ManagedSessionStatus
from ..models import (
    SessionKind,
    StreamCapabilities,
)
from ..models import User as User
from ..models import WorkspaceAttachmentCreate as WorkspaceAttachmentCreate
from ..models import WorkspaceSessionRole as WorkspaceSessionRole
from ..models.chat_image import ChatImageAttachment as AgentStreamAttachment
from ..models.schemas import TaskExecutionControl as TaskExecutionControl
from ..models.schemas import TaskExecutionReference as TaskExecutionReference
from ..services import ttyd_manager, workspace_manager
from ..services.agent_stream import ExternalTurnRetirement as ExternalTurnRetirement
from ..services.agent_stream import StructuredSourceUnavailable as StructuredSourceUnavailable
from ..services.agent_stream import (
    TailerManager,
    get_adapter_for_session,
)
from ..services.agent_stream import quoted_images as quoted_images
from ..services.agent_stream.attachments import AgentStreamAttachmentStore
from ..services.agent_stream.base import (
    NativeInputNotSubmittedError,
)
from ..services.agent_stream.base import discover_source_cached as discover_source_cached
from ..services.agent_stream.local_images import AgentImageUnavailable as AgentImageUnavailable
from ..services.agent_stream.local_images import read_cached_agent_image as read_cached_agent_image

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
_ChatLiveness = Literal["active", "dead", "terminalized", "unknown"]

_tailer_manager: Optional[TailerManager] = None
_tab_tailer_manager: Optional[TailerManager] = None
_native_input_shutting_down = False


async def _notify_goal_turn_completed(event: AgentStreamEvent) -> None:
    """Bridge persisted completion to Goal and scheduled Chat state."""
    from ..services.goal_run import get_goal_manager

    direct = event.session_id == _tab_stream_session_id(event.tab_id)
    notification = str(event.payload.get("provider_notification") or "")
    if direct and (
        event.type == AgentStreamEventType.TURN_STARTED
        or (
            event.type == AgentStreamEventType.STATUS
            and notification.startswith(("thread/goal/", "goal/"))
        )
    ):
        await get_goal_manager().refresh_current(event.tab_id)
        return
    if event.type != AgentStreamEventType.TURN_COMPLETED or not event.turn_id:
        return
    try:
        await messages.observe_completion(event)
    except Exception:
        logger.exception("Chat inbox completion persistence failed")
    if not direct:
        await workspace_manager.on_scheduled_chat_turn_completed(
            event.tab_id, event.turn_id, str(event.payload.get("status") or "failed")
        )
        return
    payload = event.payload
    try:
        await get_goal_manager().on_turn_completed(
            event.tab_id,
            event.turn_id,
            str(payload.get("status") or "failed"),
            str(
                payload.get("_goal_protocol_text")
                or payload.get("assistant_text")
                or payload.get("summary")
                or ""
            ),
            payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        )
    finally:
        # Goal and scheduled Chat turns share the transcript observer, but one
        # lifecycle must never prevent the other from releasing its queue.
        await workspace_manager.on_scheduled_chat_turn_completed(
            event.tab_id,
            event.turn_id,
            str(payload.get("status") or "failed"),
        )


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


def _persist_workspace_chat_mode(session_id: str, mode: str) -> None:
    """Persist the Task-controlled mode for a managed Chat session."""

    workspace_manager.set_managed_chat_mode(session_id, mode)


def _persist_tab_agent_session_id(stream_session_id: str, conversation_id: str) -> None:
    """Persist a provider-owned conversation id for an Agent tab."""

    ttyd_manager.set_tab_agent_session_id(
        stream_session_id.removeprefix(_TAB_STREAM_SESSION_PREFIX), conversation_id
    )


def _persist_tab_chat_mode(stream_session_id: str, mode: str) -> None:
    ttyd_manager.set_tab_chat_mode(stream_session_id.removeprefix(_TAB_STREAM_SESSION_PREFIX), mode)


def _get_tailer_manager() -> TailerManager:
    global _tailer_manager
    if _native_input_shutting_down:
        raise NativeInputNotSubmittedError(
            "service is shutting down; try again after it reconnects"
        )
    if _tailer_manager is None:
        _tailer_manager = TailerManager(
            session_getter=lambda sid: workspace_manager.sessions.get(sid),
            persist_session_id=_persist_workspace_agent_session_id,
            persist_mode=_persist_workspace_chat_mode,
            post_persist_observers=[_notify_goal_turn_completed],
            task_activity_observer=_notify_task_activity,
        )
    return _tailer_manager


def get_managed_tailer_manager() -> TailerManager:
    """Return the sole structured-stream owner for managed sessions."""

    return _get_tailer_manager()


def _tab_stream_session_id(tab_id: str) -> str:
    return f"{_TAB_STREAM_SESSION_PREFIX}{tab_id}"


def _terminal_tab_stream_session(tab_id: str) -> Optional[ManagedSession]:
    return stream_runtime.terminal_tab_stream_session(
        tab_id,
        ttyd_manager=ttyd_manager,
        stream_session_id=_tab_stream_session_id,
        stream_workspace_id=_TAB_STREAM_WORKSPACE_ID,
    )


def _terminal_tab_stream_session_by_id(
    stream_session_id: str,
) -> Optional[ManagedSession]:
    if not stream_session_id.startswith(_TAB_STREAM_SESSION_PREFIX):
        return None
    return _terminal_tab_stream_session(stream_session_id.removeprefix(_TAB_STREAM_SESSION_PREFIX))


async def _notify_dot_turn_completed(event: AgentStreamEvent) -> None:
    """Append completed Dot turns to Dot's journal (no-op for other tabs)."""
    from ..services.dot import get_dot_service

    await get_dot_service().on_stream_event(event)


def _get_tab_tailer_manager() -> TailerManager:
    global _tab_tailer_manager
    if _native_input_shutting_down:
        raise NativeInputNotSubmittedError(
            "service is shutting down; try again after it reconnects"
        )
    if _tab_tailer_manager is None:
        _tab_tailer_manager = TailerManager(
            session_getter=_terminal_tab_stream_session_by_id,
            persist_session_id=_persist_tab_agent_session_id,
            persist_mode=_persist_tab_chat_mode,
            post_persist_observers=[_notify_goal_turn_completed, _notify_dot_turn_completed],
            task_activity_observer=_notify_task_activity,
        )
    return _tab_tailer_manager


async def _scheduled_chat_turn_liveness(tab_id: str, turn_id: str) -> _ChatLiveness | None:
    return await stream_runtime.scheduled_chat_turn_liveness(
        tab_id,
        turn_id,
        terminal_session=_terminal_tab_stream_session,
        get_tab_tailer_manager=_get_tab_tailer_manager,
        logger=logger,
    )


def _feedback_chat_busy() -> bool:
    """Defer background learning during native Chat activity without waking it.

    A global quiet window avoids repository probes on every monitor tick. The
    send lock also covers startup before the provider raises its turn guard.
    """
    if _tab_tailer_manager is None:
        return False
    for tailer in _tab_tailer_manager.tailers():
        if tailer.input_busy:
            return True
    return False


workspace_manager._feedback_chat_busy = _feedback_chat_busy
workspace_manager.configure_chat_delivery(messages.get_inbox)
workspace_manager.configure_scheduled_chat_liveness(_scheduled_chat_turn_liveness)


async def _stop_all_tailer_managers() -> None:
    """Stop every initialized structured-stream owner during app shutdown."""
    managers = [
        manager for manager in (_tailer_manager, _tab_tailer_manager) if manager is not None
    ]
    for manager in managers:
        try:
            await manager.stop_all()
        except Exception:
            # One namespace failing cleanup must not strand the other one.
            logger.exception("failed to stop an agent-stream tailer manager")


def _quiesce_tailer_managers() -> None:
    global _native_input_shutting_down
    _native_input_shutting_down = True
    for manager in (_tailer_manager, _tab_tailer_manager):
        if manager is not None:
            manager.quiesce()


def _reset_tailer_manager() -> None:
    global _tailer_manager, _tab_tailer_manager, _native_input_shutting_down
    _native_input_shutting_down = False
    _tailer_manager = None
    _tab_tailer_manager = None


def _session_or_404(session_id: str) -> ManagedSession:
    session = workspace_manager.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _terminal_tab_session_or_404(tab_id: str) -> ManagedSession:
    tab = ttyd_manager.get_tab(tab_id)
    if tab is None:
        raise HTTPException(status_code=404, detail="Terminal tab not found")
    if tab.session_kind == SessionKind.CHAT and (
        tab.managed_session_id or tab.workspace_role is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="Workspace-managed tabs must use their managed session stream",
        )
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
    *,
    cache_preparation: bool = False,
    refresh: bool = False,
) -> StreamCapabilities:
    return await stream_runtime.capabilities_for(
        _get_tailer_manager,
        _is_chat_native,
        get_adapter_for_session,
        session,
        manager,
        cache_preparation=cache_preparation,
        refresh=refresh,
    )


async def _tab_capabilities_for(
    session: ManagedSession,
    manager: TailerManager,
    *,
    cache_preparation: bool = False,
    refresh: bool = False,
) -> StreamCapabilities:
    return await stream_runtime.tab_capabilities_for(
        _capabilities_for,
        get_adapter_for_session,
        session,
        manager,
        cache_preparation=cache_preparation,
        refresh=refresh,
    )


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


@asynccontextmanager
async def _managed_stream_interaction(
    session: ManagedSession,
    *,
    require_chat: bool = False,
) -> AsyncIterator[ManagedSession]:
    """Map an incarnation disappearing before admission to a stable conflict."""
    try:
        async with workspace_manager.managed_chat_interaction_guard(
            session, require_chat=require_chat
        ) as live:
            yield live
    except KeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="Managed Chat session was deleted or replaced",
            headers={CHAT_ERROR_REASON_HEADER: "structured_source_unavailable"},
        ) from exc


def _require_current_managed_session(session: ManagedSession, manager: TailerManager) -> None:
    """Reject responses that crossed deletion or friendly-id reuse."""
    if not manager.session_is_current(session):
        raise HTTPException(
            status_code=409,
            detail="Managed Chat session was deleted or replaced",
            headers={CHAT_ERROR_REASON_HEADER: "structured_source_unavailable"},
        )


def _closed_unavailable_stream(capabilities: StreamCapabilities, message: str) -> StreamingResponse:
    async def unavailable_stream() -> Any:
        yield _sse("hello", capabilities.model_dump())
        yield _sse("error", {"message": message})

    return StreamingResponse(
        unavailable_stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


AgentStreamWaitRequest = observation.AgentStreamWaitRequest
AgentStreamModeRequest = observation.AgentStreamModeRequest
_managed_default_mode_capabilities = observation.managed_default_mode_capabilities


class _AgentStreamBindings:
    def session_or_404(self, session_id: str) -> ManagedSession:
        return _session_or_404(session_id)

    def terminal_tab_session_or_404(self, tab_id: str) -> ManagedSession:
        return _terminal_tab_session_or_404(tab_id)

    def terminal_tab_stream_session(self, tab_id: str) -> Optional[ManagedSession]:
        return _terminal_tab_stream_session(tab_id)

    def get_tailer_manager(self) -> TailerManager:
        return _get_tailer_manager()

    def get_tab_tailer_manager(self) -> TailerManager:
        return _get_tab_tailer_manager()

    def managed_stream_interaction(
        self, session: ManagedSession, *, require_chat: bool = False
    ) -> AbstractAsyncContextManager[ManagedSession]:
        return _managed_stream_interaction(session, require_chat=require_chat)

    def require_current_managed_session(
        self, session: ManagedSession, manager: TailerManager
    ) -> None:
        _require_current_managed_session(session, manager)

    async def capabilities_for(
        self,
        session: ManagedSession,
        manager: Optional[TailerManager] = None,
        *,
        cache_preparation: bool = False,
        refresh: bool = False,
    ) -> StreamCapabilities:
        if cache_preparation or refresh:
            return await _capabilities_for(
                session,
                manager,
                cache_preparation=cache_preparation,
                refresh=refresh,
            )
        return await _capabilities_for(session, manager)

    async def tab_capabilities_for(
        self,
        session: ManagedSession,
        manager: TailerManager,
        *,
        cache_preparation: bool = False,
        refresh: bool = False,
    ) -> StreamCapabilities:
        if cache_preparation or refresh:
            return await _tab_capabilities_for(
                session,
                manager,
                cache_preparation=cache_preparation,
                refresh=refresh,
            )
        return await _tab_capabilities_for(session, manager)

    async def stream_events_for(self, *args: Any, **kwargs: Any) -> AgentStreamEventPage:
        return await _stream_events_for(*args, **kwargs)

    async def wait_stream_events_for(self, *args: Any, **kwargs: Any) -> AgentStreamEventPage:
        return await _wait_stream_events_for(*args, **kwargs)

    def is_chat_native(self, session: ManagedSession) -> bool:
        return _is_chat_native(session)

    def raise_structured_unavailable(
        self, session_id: str, manager: Optional[TailerManager] = None
    ) -> None:
        _raise_structured_unavailable(session_id, manager)

    def structured_failure_message(
        self, session_id: str, manager: Optional[TailerManager] = None
    ) -> str:
        return _structured_failure_message(session_id, manager)

    def closed_unavailable_stream(
        self, capabilities: StreamCapabilities, message: str
    ) -> StreamingResponse:
        return _closed_unavailable_stream(capabilities, message)

    def sse(self, event: str, data: Any) -> str:
        return _sse(event, data)

    def adapter_for_session(self, session: ManagedSession) -> Any:
        return get_adapter_for_session(session)

    def workspace_manager(self) -> Any:
        return workspace_manager

    def ttyd_manager(self) -> Any:
        return ttyd_manager

    def decode_attachments(self, attachments: Any) -> Any:
        return _decode_attachments(attachments)

    async def read_attachment_or_404(self, store: Any, attachment_id: str) -> Any:
        return await _read_attachment_or_404(store, attachment_id)

    async def send_to_native(self, *args: Any, **kwargs: Any) -> None:
        await _send_to_native(*args, **kwargs)

    async def dispatch_tab_stream_input(self, *args: Any, **kwargs: Any) -> Any:
        return await _dispatch_tab_stream_input(*args, **kwargs)

    async def yield_dot_turn(
        self, tab_id: str, session: ManagedSession, manager: TailerManager
    ) -> None:
        await _yield_dot_turn(tab_id, session, manager)

    async def cancel_native_turn(self, *args: Any, **kwargs: Any) -> Any:
        return await _cancel_native_turn(*args, **kwargs)

    def external_shutdown_cancel_timeout_seconds(self) -> float:
        return _EXTERNAL_SHUTDOWN_CANCEL_TIMEOUT_SECONDS

    def initialized_tailer_manager(self) -> Optional[TailerManager]:
        return _tailer_manager

    def initialized_tab_tailer_manager(self) -> Optional[TailerManager]:
        return _tab_tailer_manager

    def tab_stream_session_id(self, tab_id: str) -> str:
        return _tab_stream_session_id(tab_id)


_agent_stream_bindings = _AgentStreamBindings()


async def _retry_stream_for(
    session: ManagedSession, manager: TailerManager, *, direct_tab: bool = False
) -> StreamCapabilities:
    return await observation.retry_stream_for(
        session, manager, _agent_stream_bindings, direct_tab=direct_tab
    )


async def _set_stream_mode_for(
    session: ManagedSession, manager: TailerManager, mode: str, *, direct_tab: bool = False
) -> StreamCapabilities:
    return await observation.set_stream_mode_for(
        session, manager, mode, _agent_stream_bindings, direct_tab=direct_tab
    )


async def _stream_events_for(
    session: ManagedSession,
    manager: TailerManager,
    since_sequence: int,
    limit: int,
    allow_pending_source: bool = False,
    compact: bool = False,
    view: Literal["raw", "conversation"] = "raw",
    turn_id: Optional[str] = None,
    through_sequence: Optional[int] = None,
    persisted_only: bool = False,
) -> AgentStreamEventPage:
    return await observation.stream_events_for(
        session,
        manager,
        since_sequence,
        limit,
        _agent_stream_bindings,
        allow_pending_source,
        compact,
        view,
        turn_id,
        through_sequence,
        persisted_only,
    )


async def _wait_stream_events_for(
    session: ManagedSession,
    manager: TailerManager,
    session_exists: Callable[[], bool],
    payload: AgentStreamWaitRequest,
    allow_pending_source: bool = False,
    capabilities: Optional[StreamCapabilities] = None,
) -> AgentStreamEventPage:
    return await observation.wait_stream_events_for(
        session,
        manager,
        session_exists,
        payload,
        _agent_stream_bindings,
        allow_pending_source,
        capabilities,
    )


async def _stream_live_for(
    session: ManagedSession,
    manager: TailerManager,
    session_exists: Callable[[], bool],
    missing_message: str,
    since_sequence: int,
    allow_pending_source: bool = False,
) -> StreamingResponse:
    return await observation.stream_live_for(
        session,
        manager,
        session_exists,
        missing_message,
        since_sequence,
        _agent_stream_bindings,
        allow_pending_source,
    )


_observation_endpoints = observation.register_observation_routes(router, _agent_stream_bindings)
get_stream_capabilities = _observation_endpoints.get_stream_capabilities
get_tab_stream_capabilities = _observation_endpoints.get_tab_stream_capabilities
retry_stream = _observation_endpoints.retry_stream
retry_tab_stream = _observation_endpoints.retry_tab_stream
set_stream_mode = _observation_endpoints.set_stream_mode
set_tab_stream_mode = _observation_endpoints.set_tab_stream_mode
get_stream_events = _observation_endpoints.get_stream_events
get_tab_stream_events = _observation_endpoints.get_tab_stream_events
wait_stream_events = _observation_endpoints.wait_stream_events
wait_tab_stream_events = _observation_endpoints.wait_tab_stream_events
stream_live = _observation_endpoints.stream_live
stream_tab_live = _observation_endpoints.stream_tab_live
get_stream_diagnostics = _observation_endpoints.get_stream_diagnostics


# Native composer and attachment compatibility surface.
_MAX_ATTACHMENTS = 10
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_MAX_TOTAL_ATTACHMENT_BYTES = 40 * 1024 * 1024
_ATTACHMENT_CACHE_CONTROL = attachment_routes.ATTACHMENT_CACHE_CONTROL
_ATTACHMENT_NO_STORE = attachment_routes.ATTACHMENT_NO_STORE
_AGENT_IMAGE_CACHE_CONTROL = media.AGENT_IMAGE_CACHE_CONTROL
_EXTERNAL_SHUTDOWN_CANCEL_TIMEOUT_SECONDS = 10.0

AgentStreamSendRequest = dispatch.AgentStreamSendRequest
StreamCancelRequest = controls.StreamCancelRequest
AgentStreamEditResendRequest = controls.AgentStreamEditResendRequest
ExternalDispatchRetired = dispatch.ExternalDispatchRetired
ExternalInteractionRequired = dispatch.ExternalInteractionRequired
_ChatAdmissionGuard = dispatch.ChatAdmissionGuard
CHAT_ERROR_REASON_HEADER = dispatch.CHAT_ERROR_REASON_HEADER
_parse_data_url = attachment_routes.parse_data_url
_map_send_exception = dispatch.map_send_exception
_map_edit_resend_exception = controls.map_edit_resend_exception
_agent_image_not_available = media.agent_image_not_available
_same_session_incarnation = media.same_session_incarnation
_quoted_image_not_available = media.quoted_image_not_available


def _decode_attachments(
    attachments: List[AgentStreamAttachment],
) -> Tuple[List[bytes], Optional[List[Optional[bytes]]]]:
    return attachment_routes.decode_attachments(
        attachments,
        max_attachments=_MAX_ATTACHMENTS,
        max_attachment_bytes=_MAX_ATTACHMENT_BYTES,
        max_total_attachment_bytes=_MAX_TOTAL_ATTACHMENT_BYTES,
    )


async def _read_attachment_or_404(
    store: AgentStreamAttachmentStore, attachment_id: str
) -> Tuple[bytes, str]:
    return await attachment_routes.read_attachment_or_404(store, attachment_id)


async def _send_to_native(
    session: ManagedSession,
    payload: AgentStreamSendRequest,
    manager: TailerManager,
    *,
    visible_text: Optional[str] = None,
    turn_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    await dispatch.send_to_native(
        session,
        payload,
        manager,
        _agent_stream_bindings,
        visible_text=visible_text,
        turn_metadata=turn_metadata,
    )


async def _yield_dot_turn(tab_id: str, session: ManagedSession, manager: TailerManager) -> None:
    """Preserve the public patch seam for Dot's user-first dispatch policy."""
    await dispatch.yield_dot_turn(tab_id, session, manager)


async def _dispatch_tab_stream_input(
    tab_id: str,
    payload: AgentStreamSendRequest,
    *,
    visible_text: Optional[str] = None,
    turn_metadata: Optional[Dict[str, Any]] = None,
    allow_question_answer: bool = True,
    admission_guard: Optional[_ChatAdmissionGuard] = None,
) -> str:
    return await dispatch.dispatch_tab_stream_input(
        tab_id,
        payload,
        _agent_stream_bindings,
        visible_text=visible_text,
        turn_metadata=turn_metadata,
        allow_question_answer=allow_question_answer,
        admission_guard=admission_guard,
    )


async def dispatch_tab_chat_and_wait(
    tab_id: str,
    text: str,
    client_turn_id: str,
    *,
    visible_text: Optional[str] = None,
    turn_metadata: Optional[Dict[str, Any]] = None,
    attachments: Optional[List[AgentStreamAttachment]] = None,
    timeout_seconds: float | None = None,
    admission_guard: Optional[_ChatAdmissionGuard] = None,
    prefer_final_text: bool = False,
) -> str:
    return await dispatch.dispatch_tab_chat_and_wait(
        tab_id,
        text,
        client_turn_id,
        _agent_stream_bindings,
        visible_text=visible_text,
        turn_metadata=turn_metadata,
        attachments=attachments,
        timeout_seconds=timeout_seconds,
        admission_guard=admission_guard,
        prefer_final_text=prefer_final_text,
    )


async def _cancel_native_turn(
    session: ManagedSession,
    manager: TailerManager,
    payload: Optional[StreamCancelRequest] = None,
) -> Dict[str, Any]:
    return await controls.cancel_native_turn(
        session, manager, payload, bindings=_agent_stream_bindings
    )


def _managed_image_owner_is_current(expected: ManagedSession) -> bool:
    return media.managed_image_owner_is_current(expected, _agent_stream_bindings)


def _tab_image_owner_is_current(expected: ManagedSession) -> bool:
    return media.tab_image_owner_is_current(expected, _agent_stream_bindings)


_serve_agent_image = media.serve_agent_image
_serve_quoted_image = media.serve_quoted_image

_dispatch_endpoints = dispatch.register_dispatch_routes(router, _agent_stream_bindings)
send_stream_input = _dispatch_endpoints.send_stream_input
send_tab_stream_input = _dispatch_endpoints.send_tab_stream_input

_control_endpoints = controls.register_control_routes(router, _agent_stream_bindings)
cancel_stream_turn = _control_endpoints.cancel_stream_turn
cancel_tab_stream_turn = _control_endpoints.cancel_tab_stream_turn
edit_resend_stream = _control_endpoints.edit_resend_stream
edit_resend_tab_stream = _control_endpoints.edit_resend_tab_stream

_attachment_endpoints = attachment_routes.register_attachment_routes(router, _agent_stream_bindings)
get_session_attachment = _attachment_endpoints.get_session_attachment
get_tab_attachment = _attachment_endpoints.get_tab_attachment

_media_endpoints = media.register_media_routes(router, _agent_stream_bindings)
get_session_agent_image = _media_endpoints.get_session_agent_image
get_tab_agent_image = _media_endpoints.get_tab_agent_image
get_session_quoted_image = _media_endpoints.get_session_quoted_image
get_tab_quoted_image = _media_endpoints.get_tab_quoted_image
get_session_markdown = _media_endpoints.get_session_markdown
get_tab_markdown = _media_endpoints.get_tab_markdown
get_session_attachment_preview = _media_endpoints.get_session_attachment_preview
get_tab_attachment_preview = _media_endpoints.get_tab_attachment_preview
get_registered_html_preview = _media_endpoints.get_registered_html_preview


async def _notify_task_activity(event: AgentStreamEvent) -> None:
    from ..services.agent_stream.question_answers import answers

    try:
        await answers.observe(event)
    except Exception:
        logger.debug("Question projection unavailable", exc_info=False)
    await workspace_manager.record_task_stream_event(event)


TaskContextItem = task_context.TaskContextItem
TabTaskContext = task_context.TabTaskContext
_task_context_endpoints = task_context.register_task_context_routes(router, _agent_stream_bindings)
tab_task_context = _task_context_endpoints.tab_task_context

__all__ = [
    "CHAT_ERROR_REASON_HEADER",
    "ExternalDispatchRetired",
    "ExternalInteractionRequired",
    "dispatch_tab_chat_and_wait",
    "router",
    "_reset_tailer_manager",
]


# Ordinary Chat messages do not acquire Workspace Task ownership.
messages.register_message_routes(router)

from .routes.agent_stream.questions import register_question_routes

register_question_routes(router)
