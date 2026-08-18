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

Authority: every mutating action (spawn, send, followup, interrupt) requires
the caller's session to own the ``author_id`` run. A run is owned by a session
if ``run.context_ref == session_id``. The resident root run is owned by the
resident session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request

from ..auth.dependencies import get_current_user
from ..config import settings
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


def _get_session_id(
    request: Request,
    session_id: Optional[str] = Cookie(None, alias=settings.session_cookie_name),
) -> Optional[str]:
    """Extract the session id from the request cookie.

    Returns None for local network requests (auth disabled).
    """
    from ..auth.dependencies import is_local_network_request

    if is_local_network_request(request):
        return None
    return session_id


def _assert_authority(
    manager: "AgentTreeManager", author_id: str, session_id: Optional[str]
) -> None:
    """Verify the caller's session owns the author run.

    A run is owned by a session if:
    - For ``resident_root`` runs: ``run.context_ref == session_id`` (the
      context_ref is the resident session id).
    - For ``managed_task`` runs: the workspace task referenced by
      ``run.context_ref`` has ``task.session_id == session_id``.
    - For other executor kinds: ``run.context_ref == session_id``.

    For local network requests (auth disabled), authority is not enforced.
    """
    if session_id is None:
        # Auth disabled or local network: skip authority check.
        return
    run = manager.get_run(author_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Author run {author_id} not found")

    from ..models.agent_tree import ExecutorKind

    if run.executor_kind == ExecutorKind.MANAGED_TASK:
        # For managed tasks, context_ref is the task id. Look up the task
        # via the manager's workspace manager and check that its
        # session_id matches the caller's session.
        wm = manager._wm
        task = wm.tasks.get(run.context_ref) if run.context_ref else None
        if task is None:
            raise HTTPException(
                status_code=403,
                detail=f"Run {author_id} references task {run.context_ref!r} "
                f"which does not exist",
            )
        if task.session_id != session_id:
            raise HTTPException(
                status_code=403,
                detail=f"Session {session_id} does not own run {author_id} "
                f"(task owned by session {task.session_id!r})",
            )
    else:
        if run.context_ref != session_id:
            raise HTTPException(
                status_code=403,
                detail=f"Session {session_id} does not own run {author_id} "
                f"(owned by {run.context_ref!r})",
            )


@router.post("/spawn", response_model=AgentRun)
async def spawn(
    req: SpawnRequest,
    request: Request,
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> AgentRun:
    try:
        _assert_authority(_manager(), req.parent_id, session_id)
        return await _manager().spawn(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@router.post("/send", response_model=AgentEvent)
async def send(
    req: SendRequest,
    request: Request,
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> AgentEvent:
    try:
        _assert_authority(_manager(), req.author_id, session_id)
        return await _manager().send(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/followup", response_model=AgentEvent)
async def followup(
    req: FollowupRequest,
    request: Request,
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> AgentEvent:
    try:
        _assert_authority(_manager(), req.author_id, session_id)
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
    request: Request,
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> AgentRun:
    try:
        _assert_authority(_manager(), run_id, session_id)
        return _manager().ack(workspace_id, run_id, sequence)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/interrupt", response_model=AgentRun)
async def interrupt(
    req: InterruptRequest,
    request: Request,
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> AgentRun:
    try:
        # The caller may interrupt any run in its subtree. For simplicity,
        # we check that the caller owns the run or its supervisor.
        run = _manager().get_run(req.run_id)
        if run is not None:
            if run.context_ref != session_id and run.supervisor_id:
                _assert_authority(_manager(), run.supervisor_id, session_id)
            elif run.context_ref != session_id:
                raise HTTPException(
                    status_code=403,
                    detail=f"Session {session_id} may not interrupt run {req.run_id}",
                )
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
