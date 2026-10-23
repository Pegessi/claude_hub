"""Hub subagents: one-call cross-provider help (``claude-hub agent run``).

Not a Workspace Task: no board, Goal Packet, review or acceptance. See
``docs/working-logs/2026-10-24-agent-run-subagents.md``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth.dependencies import get_current_user
from ..models import User
from ..models.subagent import (
    SUBAGENT_MAX_WAIT_SECONDS,
    Subagent,
    SubagentCreate,
    SubagentSummary,
    SubagentTurnCreate,
    SubagentTurnResult,
)
from ..services.subagents import SubagentError, get_subagent_service
from ..services.ttyd_manager import TabLimitExceededError, TabStartupTimeoutError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/subagents", tags=["subagents"])


def _http(exc: SubagentError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.post("", response_model=SubagentTurnResult, status_code=201)
async def create_subagent(
    body: SubagentCreate, current_user: User = Depends(get_current_user)
) -> SubagentTurnResult:
    """Create a subagent Chat tab and start its first turn in the background.

    Poll ``GET /api/subagents/{id}/turns/{turn_id}?wait=…`` for the result.
    """
    from ..services.agent_stream.maso_http import MasoServiceUnavailable

    try:
        record, turn = await get_subagent_service().create(body, created_by=current_user.email)
    except SubagentError as exc:
        raise _http(exc) from None
    except TabLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except TabStartupTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from None
    except MasoServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return SubagentTurnResult.of(record, turn)


@router.get("", response_model=List[SubagentSummary])
async def list_subagents(
    parent_tab_id: Optional[str] = Query(default=None),
    include_closed: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
) -> List[SubagentSummary]:
    rows = get_subagent_service().list_subagents(
        parent_tab_id=parent_tab_id, include_closed=include_closed
    )
    return [SubagentSummary.of(row) for row in rows]


@router.get("/{subagent_id}", response_model=Subagent)
async def get_subagent(
    subagent_id: str, current_user: User = Depends(get_current_user)
) -> Subagent:
    try:
        return get_subagent_service().get(subagent_id)
    except SubagentError as exc:
        raise _http(exc) from None


@router.post("/{subagent_id}/turns", response_model=SubagentTurnResult, status_code=201)
async def send_subagent_turn(
    subagent_id: str,
    body: SubagentTurnCreate,
    current_user: User = Depends(get_current_user),
) -> SubagentTurnResult:
    """Start a follow-up turn on a kept subagent (same conversation)."""
    try:
        record, turn = await get_subagent_service().send(subagent_id, body)
    except SubagentError as exc:
        raise _http(exc) from None
    return SubagentTurnResult.of(record, turn)


async def _turn_result(subagent_id: str, turn_id: Optional[str], wait: float) -> SubagentTurnResult:
    try:
        record, turn = await get_subagent_service().wait_turn(subagent_id, turn_id, wait)
    except SubagentError as exc:
        raise _http(exc) from None
    return SubagentTurnResult.of(record, turn)


@router.get("/{subagent_id}/turns/latest", response_model=SubagentTurnResult)
async def get_latest_subagent_turn(
    subagent_id: str,
    wait: float = Query(default=0.0, ge=0, le=SUBAGENT_MAX_WAIT_SECONDS),
    current_user: User = Depends(get_current_user),
) -> SubagentTurnResult:
    return await _turn_result(subagent_id, None, wait)


@router.get("/{subagent_id}/turns/{turn_id}", response_model=SubagentTurnResult)
async def get_subagent_turn(
    subagent_id: str,
    turn_id: str,
    wait: float = Query(default=0.0, ge=0, le=SUBAGENT_MAX_WAIT_SECONDS),
    current_user: User = Depends(get_current_user),
) -> SubagentTurnResult:
    """Long-poll one turn: returns when it finishes or after ``wait`` seconds."""
    return await _turn_result(subagent_id, turn_id, wait)


@router.post("/{subagent_id}/cancel", response_model=Dict[str, Any])
async def cancel_subagent(
    subagent_id: str,
    turn_id: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Cancel the running turn (only ``turn_id`` when given; a finished turn is
    left as is). A non-kept subagent is then archived."""
    try:
        record, turn = await get_subagent_service().cancel(subagent_id, turn_id)
    except SubagentError as exc:
        raise _http(exc) from None
    return {
        "subagent": SubagentSummary.of(record).model_dump(mode="json"),
        "turn": SubagentTurnResult.of(record, turn).model_dump(mode="json") if turn else None,
    }


@router.post("/{subagent_id}/close", response_model=SubagentSummary)
async def close_subagent(
    subagent_id: str, current_user: User = Depends(get_current_user)
) -> SubagentSummary:
    """Cancel any running turn and archive the subagent's Chat tab (history kept)."""
    try:
        record = await get_subagent_service().close(subagent_id)
    except SubagentError as exc:
        raise _http(exc) from None
    return SubagentSummary.of(record)
