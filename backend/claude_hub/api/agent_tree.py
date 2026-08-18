"""Agent Tree + Durable Mailbox API endpoints.

Exposes the unified agent-to-agent coordination layer:
- ``POST /agent-tree/spawn`` — create a child run and dispatch its task.
- ``POST /agent-tree/send`` — append a message to a run's mailbox.
- ``POST /agent-tree/followup`` — append a message and resume the run's turn.
- ``POST /agent-tree/wait`` — block until directed events arrive (cursor).
- ``POST /agent-tree/ack`` — advance a run's acknowledged sequence cursor.
- ``POST /agent-tree/interrupt`` — interrupt a run, preserving context.
- ``GET  /agent-tree/runs`` — list runs (optionally scoped to a subtree).
- ``GET  /agent-tree/runs/{run_id}/events`` — replay a run's event stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth.dependencies import get_current_user
from ..models.agent_tree import (
    AgentEvent,
    AgentRun,
    AgentRunStatus,
    FollowupRequest,
    InterruptRequest,
    ListRunsRequest,
    SendRequest,
    SpawnRequest,
    WaitRequest,
)
from ..models.schemas import User
from ..services import workspace_manager

if TYPE_CHECKING:
    from ..services.agent_tree import AgentTreeManager

router = APIRouter(prefix="/api/agent-tree", tags=["agent-tree"])


def _manager() -> "AgentTreeManager":
    return workspace_manager.agent_tree


@router.post("/spawn", response_model=AgentRun)
async def spawn(req: SpawnRequest, current_user: User = Depends(get_current_user)) -> AgentRun:
    try:
        return await _manager().spawn(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@router.post("/send", response_model=AgentEvent)
async def send(req: SendRequest, current_user: User = Depends(get_current_user)) -> AgentEvent:
    try:
        return await _manager().send(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/followup", response_model=AgentEvent)
async def followup(
    req: FollowupRequest, current_user: User = Depends(get_current_user)
) -> AgentEvent:
    try:
        return await _manager().followup(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/wait", response_model=List[AgentEvent])
async def wait(
    req: WaitRequest, current_user: User = Depends(get_current_user)
) -> List[AgentEvent]:
    try:
        return await _manager().wait(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/ack", response_model=AgentRun)
async def ack(
    workspace_id: str,
    run_id: str,
    sequence: int,
    current_user: User = Depends(get_current_user),
) -> AgentRun:
    try:
        return _manager().ack(workspace_id, run_id, sequence)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/interrupt", response_model=AgentRun)
async def interrupt(
    req: InterruptRequest, current_user: User = Depends(get_current_user)
) -> AgentRun:
    try:
        return await _manager().interrupt(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/runs", response_model=List[AgentRun])
def list_runs(
    workspace_id: str = Query(...),
    root_id: Optional[str] = Query(None),
    status: Optional[AgentRunStatus] = Query(None),
    current_user: User = Depends(get_current_user),
) -> List[AgentRun]:
    try:
        return _manager().list_runs(
            ListRunsRequest(workspace_id=workspace_id, root_id=root_id, status=status)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/runs/{run_id}/events", response_model=List[AgentEvent])
def get_run_events(
    run_id: str,
    since_sequence: int = Query(0),
    subtree: bool = Query(True),
    current_user: User = Depends(get_current_user),
) -> List[AgentEvent]:
    run = _manager().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _manager().get_events(
        run.workspace_id, run_id, since_sequence=since_sequence, subtree=subtree
    )
