"""Agent Tree + Durable Mailbox API endpoints.

Exposes the unified agent-to-agent coordination layer:
- ``POST /agent-tree/spawn`` — create a child run and dispatch its task.
- ``POST /agent-tree/send`` — append a message to a run's mailbox.
- ``POST /agent-tree/followup`` — append a message and resume the run's turn.
- ``POST /agent-tree/wait`` — block until directed events arrive (cursor).
- ``POST /agent-tree/interrupt`` — interrupt a run, preserving context.
- ``GET  /agent-tree/runs`` — list runs (optionally scoped to a subtree).
- ``GET  /agent-tree/runs/{run_id}/events`` — replay a run's event stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from fastapi import APIRouter, HTTPException, Query

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
from ..services import workspace_manager

if TYPE_CHECKING:
    from ..services.agent_tree import AgentTreeManager

router = APIRouter(prefix="/api/agent-tree", tags=["agent-tree"])


def _manager() -> "AgentTreeManager":
    return workspace_manager.agent_tree


@router.post("/spawn", response_model=AgentRun)
async def spawn(req: SpawnRequest) -> AgentRun:
    try:
        return await _manager().spawn(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/send", response_model=AgentEvent)
async def send(req: SendRequest) -> AgentEvent:
    try:
        return await _manager().send(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/followup", response_model=AgentEvent)
async def followup(req: FollowupRequest) -> AgentEvent:
    try:
        return await _manager().followup(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/wait", response_model=List[AgentEvent])
async def wait(req: WaitRequest) -> List[AgentEvent]:
    try:
        return await _manager().wait(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/interrupt", response_model=AgentRun)
async def interrupt(req: InterruptRequest) -> AgentRun:
    try:
        return await _manager().interrupt(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/runs", response_model=List[AgentRun])
def list_runs(
    workspace_id: str = Query(...),
    root_id: Optional[str] = Query(None),
    status: Optional[AgentRunStatus] = Query(None),
) -> List[AgentRun]:
    return _manager().list_runs(
        ListRunsRequest(workspace_id=workspace_id, root_id=root_id, status=status)
    )


@router.get("/runs/{run_id}/events", response_model=List[AgentEvent])
def get_run_events(
    run_id: str,
    since_sequence: int = Query(0),
    subtree: bool = Query(True),
) -> List[AgentEvent]:
    run = _manager().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _manager().get_events(
        run.workspace_id, run_id, since_sequence=since_sequence, subtree=subtree
    )
