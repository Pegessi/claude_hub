"""Agent-stream cancellation and edit-resend controls."""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from claude_hub.api.routes.agent_stream.bindings import AgentStreamRouteBindings
from claude_hub.api.routes.agent_stream.dispatch import require_tab_input_available
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import TERMINAL_GOAL_STATUSES, GoalRunStatus, ManagedSession, User
from claude_hub.services.agent_stream import StructuredSourceUnavailable, TailerManager

CHAT_ERROR_REASON_HEADER = "X-Claude-Hub-Error-Reason"


class StreamCancelRequest(BaseModel):
    """Optional body for ``/stream/cancel``.

    ``expected_turn_id`` is a hint: when the active turn no longer matches
    (Stop raced a natural completion or a newer turn already started) the
    request is a no-op rather than cancelling the wrong turn. Omitted, Stop
    terminalizes whatever turn is active (or the latest durable orphan).
    """

    expected_turn_id: Optional[str] = None


class AgentStreamEditResendRequest(BaseModel):
    """Request body for editing a previously sent user message and rerunning."""

    text: str = Field(..., min_length=1)
    client_turn_id: str
    turn_id: str

    @model_validator(mode="after")
    def _require_fields(self) -> "AgentStreamEditResendRequest":
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if not self.client_turn_id.strip():
            raise ValueError("client_turn_id is required")
        if not self.turn_id.strip():
            raise ValueError("turn_id is required")
        return self


async def cancel_native_turn(
    session: ManagedSession,
    manager: TailerManager,
    payload: Optional[StreamCancelRequest] = None,
    *,
    bindings: AgentStreamRouteBindings,
) -> Dict[str, Any]:
    if not bindings.is_chat_native(session):
        raise HTTPException(
            status_code=400,
            detail="native turn cancel is only available for CHAT sessions",
        )
    expected_turn_id = payload.expected_turn_id if payload is not None else None
    try:
        cancelled = await manager.cancel_turn(session, expected_turn_id=expected_turn_id)
    except StructuredSourceUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "cancelled": cancelled}


def map_edit_resend_exception(exc: Exception) -> HTTPException:
    """Map edit-resend errors to explicit HTTP status codes.

    ``EditResendTurnInFlightError`` (a turn is running) → 409.
    ``ValueError`` (turn not found, bad input) → 400.
    ``TranscriptForkError`` (transcript cannot be forked) → 409.
    ``RuntimeError`` (no adapter, transport died) → 503.
    """
    from claude_hub.services.agent_stream.tailer import EditResendTurnInFlightError
    from claude_hub.services.agent_stream.transcript_fork import TranscriptForkError

    if isinstance(exc, EditResendTurnInFlightError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TranscriptForkError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@dataclass(frozen=True)
class AgentStreamControlEndpoints:
    cancel_stream_turn: Callable[..., Any]
    cancel_tab_stream_turn: Callable[..., Any]
    edit_resend_stream: Callable[..., Any]
    edit_resend_tab_stream: Callable[..., Any]


def register_control_routes(
    router: APIRouter, bindings: AgentStreamRouteBindings
) -> AgentStreamControlEndpoints:
    """Register turn-control routes in their historical order."""

    @router.post("/sessions/{managed_session_id}/stream/cancel")
    async def cancel_stream_turn(
        managed_session_id: str,
        payload: Optional[StreamCancelRequest] = None,
        current_user: User = Depends(get_current_user),
    ) -> Dict[str, Any]:
        session = bindings.session_or_404(managed_session_id)
        expected_turn_id = payload.expected_turn_id if payload is not None else None
        try:
            cancelled = await bindings.workspace_manager().cancel_chat_session(
                managed_session_id,
                expected_turn_id=expected_turn_id,
            )
            if not cancelled and expected_turn_id is not None:
                active_turn_id = await bindings.get_tailer_manager().active_turn_id(session)
                if active_turn_id is not None and active_turn_id != expected_turn_id:
                    raise HTTPException(
                        status_code=409,
                        detail="A newer managed Chat turn is active; the stale Stop was ignored",
                        headers={CHAT_ERROR_REASON_HEADER: "stale_turn"},
                    )
        except StructuredSourceUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            raise
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        outcome = "cancelled_matching" if cancelled else "already_terminal"
        return {"ok": True, "cancelled": cancelled, "outcome": outcome}

    @router.post("/tabs/{tab_id}/stream/cancel")
    async def cancel_tab_stream_turn(
        tab_id: str,
        payload: Optional[StreamCancelRequest] = None,
        current_user: User = Depends(get_current_user),
    ) -> Dict[str, Any]:
        from claude_hub.services.goal_run import (
            GoalPolicyError,
            get_goal_admission_lock,
            get_goal_manager,
        )
        from claude_hub.services.goal_run.native import NativeGoalError
        from claude_hub.services.restart_continuation import cancel_pending_continuation

        session = bindings.terminal_tab_session_or_404(tab_id)
        if not bindings.is_chat_native(session):
            return await bindings.cancel_native_turn(
                session, bindings.get_tab_tailer_manager(), payload
            )
        expected = payload.expected_turn_id if payload is not None else None
        continuation = cancel_pending_continuation(tab_id, expected)
        if continuation is not None:
            payload = StreamCancelRequest(expected_turn_id=continuation)
            expected = continuation
        async with get_goal_admission_lock(tab_id):
            try:
                manager = bindings.get_tab_tailer_manager()
                goal_manager = get_goal_manager()
                goal = await goal_manager.refresh_current(tab_id)
                active = (
                    await manager.active_turn_id(session)
                    if goal is not None and goal.execution_owner == "provider_native"
                    else None
                )
                # Native continuation may be queued before its start is read,
                # or claimed before the consumer assigns a Hub ID. Pause the
                # active Goal even if the transport guard has not risen yet.
                native_goal_without_turn = (
                    goal is not None
                    and goal.execution_owner == "provider_native"
                    and active is None
                    and (goal.status == GoalRunStatus.ACTIVE or goal.provider_stop_pending)
                )
                if (
                    goal is not None
                    and goal.execution_owner == "provider_native"
                    and (
                        (active is not None and active in goal.provider_owned_turn_ids)
                        or native_goal_without_turn
                    )
                    and (goal.status not in TERMINAL_GOAL_STATUSES or goal.provider_stop_pending)
                ):
                    if expected is not None and active != expected:
                        return {"ok": True, "cancelled": False, "outcome": "already_terminal"}
                    try:
                        paused = await goal_manager.pause(goal.id, f"stop:{uuid4()}")
                    except GoalPolicyError as exc:
                        # The provider can finish/clear its Goal between our
                        # first snapshot and pause. Stop only the originally
                        # observed turn; a replacement Goal needs a fresh action.
                        latest = await goal_manager.refresh_current(tab_id)
                        if active is None or (
                            latest is not None
                            and (
                                latest.id != goal.id or latest.status not in TERMINAL_GOAL_STATUSES
                            )
                        ):
                            raise HTTPException(status_code=409, detail=str(exc)) from exc
                        if await manager.active_turn_id(session) != active:
                            return {"ok": True, "cancelled": False, "outcome": "already_terminal"}
                        return await bindings.cancel_native_turn(
                            session, manager, StreamCancelRequest(expected_turn_id=active)
                        )
                    if paused.dispatch_state.value != "idle":
                        raise NativeGoalError(
                            paused.status_message or "Provider Goal stop is unconfirmed"
                        )
                    return {"ok": True, "cancelled": True, "outcome": "cancelled_matching"}
                return await bindings.cancel_native_turn(session, manager, payload)
            except NativeGoalError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/sessions/{managed_session_id}/stream/edit-resend")
    async def edit_resend_stream(
        managed_session_id: str,
        payload: AgentStreamEditResendRequest,
        current_user: User = Depends(get_current_user),
    ) -> Dict[str, Any]:
        """Reject history rewrites for Task-owned managed Chat sessions."""
        bindings.session_or_404(managed_session_id)
        raise HTTPException(
            status_code=409,
            detail="Managed Chat history is owned by Task execution and cannot be rewritten",
        )

    @router.post("/tabs/{tab_id}/stream/edit-resend")
    async def edit_resend_tab_stream(
        tab_id: str,
        payload: AgentStreamEditResendRequest,
        current_user: User = Depends(get_current_user),
    ) -> Dict[str, Any]:
        """Edit-resend for a direct Agent tab."""
        from claude_hub.services.goal_run import get_goal_admission_lock, get_goal_manager

        async with get_goal_admission_lock(tab_id):
            require_tab_input_available(tab_id)
            goal = get_goal_manager().current(tab_id)
            if goal is not None and (
                goal.status not in TERMINAL_GOAL_STATUSES or goal.dispatch_state.value != "idle"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Finish or clear the Goal before editing conversation history",
                )
            session = bindings.terminal_tab_session_or_404(tab_id)
            manager = bindings.get_tab_tailer_manager()
            try:
                await manager.edit_resend(
                    session,
                    payload.text,
                    payload.client_turn_id,
                    payload.turn_id,
                    turn_metadata={"origin": "web"},
                )
            except HTTPException:
                raise
            except Exception as exc:
                raise map_edit_resend_exception(exc) from exc
        return {"ok": True}

    return AgentStreamControlEndpoints(
        cancel_stream_turn=cancel_stream_turn,
        cancel_tab_stream_turn=cancel_tab_stream_turn,
        edit_resend_stream=edit_resend_stream,
        edit_resend_tab_stream=edit_resend_tab_stream,
    )
