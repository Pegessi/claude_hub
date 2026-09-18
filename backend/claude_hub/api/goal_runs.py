"""Authenticated lifecycle API for direct Chat Goals."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.dependencies import get_current_user
from ..models import (
    ChatMode,
    GoalBudgetUpdate,
    GoalMutationRequest,
    GoalRun,
    GoalRunCreate,
    SessionKind,
    User,
)
from ..services import ttyd_manager
from ..services.goal_run import (
    GoalPolicyError,
    configure_goal_dispatch,
    get_goal_admission_lock,
    get_goal_manager,
)

router = APIRouter(tags=["goals"])


async def _dispatch_goal_turn(goal: GoalRun, prompt: str) -> str:
    """Send one Hub-managed Goal turn through the direct Chat transport."""
    from .agent_stream import (
        AgentStreamSendRequest,
        _get_tab_tailer_manager,
        _send_to_native,
        _terminal_tab_session_or_404,
    )

    turn_id = goal.pending_step_id
    if not turn_id:
        raise RuntimeError("Goal dispatch is missing its durable step id")
    session = _terminal_tab_session_or_404(goal.tab_id)
    await _send_to_native(
        session,
        AgentStreamSendRequest(text=prompt, client_turn_id=turn_id),
        _get_tab_tailer_manager(),
        visible_text="Continue active Goal",
        turn_metadata={"origin": "goal", "protocol": "goal-continuation-v1"},
    )
    return turn_id


async def _cancel_goal_turn(goal: GoalRun) -> None:
    """Cancel only the Goal's current direct Chat turn."""
    from .agent_stream import (
        _get_tab_tailer_manager,
        _terminal_tab_session_or_404,
    )

    session = _terminal_tab_session_or_404(goal.tab_id)
    manager = _get_tab_tailer_manager()
    cancelled = await manager.cancel_turn(session, expected_turn_id=goal.current_turn_id)
    if not cancelled and await manager.turn_in_flight(session):
        raise RuntimeError("a newer Chat turn is already in flight")


configure_goal_dispatch(_dispatch_goal_turn, cancel=_cancel_goal_turn)


def _direct_chat_tab(tab_id: str) -> None:
    tab = ttyd_manager.get_tab(tab_id)
    if tab is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tab not found")
    if tab.session_kind != SessionKind.CHAT or tab.workspace_role is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Goals are available only for direct top-level native Chat tabs",
        )


async def _ensure_goal_ready(tab_id: str, *, check_busy: bool = True) -> None:
    from .agent_stream import _get_tab_tailer_manager, _terminal_tab_session_or_404

    session = _terminal_tab_session_or_404(tab_id)
    if session.chat_mode == ChatMode.PLAN:
        raise GoalPolicyError("Switch to Agent mode before starting or resuming a Goal")
    if check_busy and await _get_tab_tailer_manager().turn_in_flight(session):
        raise GoalPolicyError(
            "Wait for the current Chat turn to finish before starting or resuming a Goal"
        )


def _call(operation: Callable[..., GoalRun], *args: object) -> GoalRun:
    try:
        return operation(*args)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (ValueError, GoalPolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.post("/api/tabs/{tab_id}/goal", response_model=GoalRun, status_code=201)
async def create_goal(
    tab_id: str, body: GoalRunCreate, current_user: User = Depends(get_current_user)
) -> GoalRun:
    _direct_chat_tab(tab_id)
    async with get_goal_admission_lock(tab_id):
        manager = get_goal_manager()
        try:
            replay = manager.replay_create(tab_id, body)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
        if replay is not None:
            return replay
        try:
            await _ensure_goal_ready(tab_id)
            return await manager.create_and_start(tab_id, body)
        except (ValueError, GoalPolicyError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.get("/api/tabs/{tab_id}/goal/current", response_model=GoalRun | None)
async def current_goal(
    tab_id: str, current_user: User = Depends(get_current_user)
) -> GoalRun | None:
    _direct_chat_tab(tab_id)
    return get_goal_manager().current(tab_id)


@router.get("/api/goals/{goal_id}", response_model=GoalRun)
async def get_goal(goal_id: str, current_user: User = Depends(get_current_user)) -> GoalRun:
    return _call(get_goal_manager().get, goal_id)


@router.post("/api/goals/{goal_id}/pause", response_model=GoalRun)
async def pause_goal(
    goal_id: str, body: GoalMutationRequest, current_user: User = Depends(get_current_user)
) -> GoalRun:
    try:
        manager = get_goal_manager()
        goal = manager.get(goal_id)
        async with get_goal_admission_lock(goal.tab_id):
            return await manager.pause(goal_id, body.client_request_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (ValueError, GoalPolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.post("/api/goals/{goal_id}/resume", response_model=GoalRun)
async def resume_goal(
    goal_id: str, body: GoalMutationRequest, current_user: User = Depends(get_current_user)
) -> GoalRun:
    try:
        manager = get_goal_manager()
        goal = manager.get(goal_id)
        async with get_goal_admission_lock(goal.tab_id):
            goal = manager.get(goal_id)
            if body.client_request_id not in goal.idempotency and goal.status.value != "active":
                await _ensure_goal_ready(
                    goal.tab_id, check_busy=goal.dispatch_state.value == "idle"
                )
            return await manager.resume(goal_id, body.client_request_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (ValueError, GoalPolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.post("/api/goals/{goal_id}/complete", response_model=GoalRun)
async def complete_goal(
    goal_id: str, body: GoalMutationRequest, current_user: User = Depends(get_current_user)
) -> GoalRun:
    try:
        manager = get_goal_manager()
        goal = manager.get(goal_id)
        async with get_goal_admission_lock(goal.tab_id):
            return await manager.complete(goal_id, body.client_request_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (ValueError, GoalPolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.post("/api/goals/{goal_id}/clear", response_model=GoalRun)
async def clear_goal(
    goal_id: str, body: GoalMutationRequest, current_user: User = Depends(get_current_user)
) -> GoalRun:
    try:
        manager = get_goal_manager()
        goal = manager.get(goal_id)
        async with get_goal_admission_lock(goal.tab_id):
            return await manager.clear(goal_id, body.client_request_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (ValueError, GoalPolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.patch("/api/goals/{goal_id}/budget", response_model=GoalRun)
async def update_goal_budget(
    goal_id: str, body: GoalBudgetUpdate, current_user: User = Depends(get_current_user)
) -> GoalRun:
    return _call(
        get_goal_manager().update_budget, goal_id, body.client_request_id, body.token_budget
    )
