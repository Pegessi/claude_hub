"""Authenticated lifecycle API for direct Chat Goals."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.dependencies import get_current_user
from ..models import (
    GoalBudgetUpdate,
    GoalMutationRequest,
    GoalRun,
    GoalRunCreate,
    SessionKind,
    User,
)
from ..services import ttyd_manager
from ..services.goal_run import GoalPolicyError, get_goal_manager

router = APIRouter(tags=["goals"])


def _direct_chat_tab(tab_id: str) -> None:
    tab = ttyd_manager.get_tab(tab_id)
    if tab is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tab not found")
    if tab.session_kind != SessionKind.CHAT or tab.workspace_role is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Goals are available only for direct top-level native Chat tabs",
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
    try:
        return await get_goal_manager().create_and_start(tab_id, body)
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
        return await get_goal_manager().pause(goal_id, body.client_request_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (ValueError, GoalPolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.post("/api/goals/{goal_id}/resume", response_model=GoalRun)
async def resume_goal(
    goal_id: str, body: GoalMutationRequest, current_user: User = Depends(get_current_user)
) -> GoalRun:
    try:
        return await get_goal_manager().resume(goal_id, body.client_request_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except (ValueError, GoalPolicyError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.post("/api/goals/{goal_id}/complete", response_model=GoalRun)
async def complete_goal(
    goal_id: str, body: GoalMutationRequest, current_user: User = Depends(get_current_user)
) -> GoalRun:
    return _call(get_goal_manager().complete, goal_id, body.client_request_id)


@router.post("/api/goals/{goal_id}/clear", response_model=GoalRun)
async def clear_goal(
    goal_id: str, body: GoalMutationRequest, current_user: User = Depends(get_current_user)
) -> GoalRun:
    try:
        return await get_goal_manager().clear(goal_id, body.client_request_id)
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
