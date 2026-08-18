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


def _session_owns_run(manager: "AgentTreeManager", run: "AgentRun", session_id: str) -> bool:
    """Return True if ``session_id`` owns ``run`` as its executor.

    - ``managed_task`` runs: the referenced task's ``session_id`` matches.
    - ``resident_root`` / ``native_subagent`` / ``external_job``:
      ``run.context_ref`` matches.
    """
    from ..models.agent_tree import ExecutorKind

    if run.executor_kind == ExecutorKind.MANAGED_TASK:
        wm = manager._wm
        task = wm.tasks.get(run.context_ref) if run.context_ref else None
        return task is not None and task.session_id == session_id
    return run.context_ref == session_id


def _is_human_session(session_id: str) -> bool:
    """Return True if ``session_id`` resolves to a valid human LoginSession."""
    from ..auth.session import get_session

    return get_session(session_id) is not None


def _is_authenticated_session(manager: "AgentTreeManager", session_id: str) -> bool:
    """Return True if ``session_id`` is a human LoginSession or a ManagedSession."""
    if session_id in manager._wm.sessions:
        return True
    return _is_human_session(session_id)


def _assert_authority(
    manager: "AgentTreeManager", author_id: str, session_id: Optional[str]
) -> None:
    """Verify the caller's session owns the author run.

    Ownership rules:
    - Local network (session_id is None): authority not enforced.
    - Agent session that executes the run: allowed.
    - Human user (valid LoginSession): owns every run in the workspace.
    - Otherwise: 403.
    """
    if session_id is None:
        return
    run = manager.get_run(author_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Author run {author_id} not found")
    if _session_owns_run(manager, run, session_id):
        return
    if _is_human_session(session_id):
        return
    raise HTTPException(
        status_code=403,
        detail=f"Session {session_id} does not own run {author_id}",
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
    req: WaitRequest,
    request: Request,
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> List[AgentEvent]:
    try:
        # Read permission: any authenticated caller (human or agent session)
        # may wait on events in their workspace. For agent sessions, the
        # recipient must be a run the session owns or supervises.
        if session_id is not None:
            run = _manager().get_run(req.recipient_id)
            if run is not None:
                if not _session_owns_run(_manager(), run, session_id):
                    if run.supervisor_id:
                        _assert_authority(_manager(), run.supervisor_id, session_id)
                    elif not _is_human_session(session_id):
                        raise HTTPException(
                            status_code=403,
                            detail=f"Session {session_id} may not wait on run {req.recipient_id}",
                        )
            elif not _is_human_session(session_id):
                raise HTTPException(
                    status_code=403,
                    detail=f"Session {session_id} is not authenticated",
                )
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
        if run is not None and session_id is not None:
            if not _session_owns_run(_manager(), run, session_id):
                if run.supervisor_id:
                    _assert_authority(_manager(), run.supervisor_id, session_id)
                elif not _is_human_session(session_id):
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
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> List[AgentRun]:
    try:
        # Read permission: any authenticated caller (human or agent session)
        # may list runs in their workspace.
        if session_id is not None and not _is_authenticated_session(_manager(), session_id):
            raise HTTPException(
                status_code=403,
                detail=f"Session {session_id} is not authenticated",
            )
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
    session_id: Optional[str] = Depends(_get_session_id),
    current_user: User = Depends(get_current_user),
) -> List[AgentEvent]:
    run = _manager().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    # Read permission: any authenticated caller may replay events.
    if session_id is not None and not _is_authenticated_session(_manager(), session_id):
        raise HTTPException(
            status_code=403,
            detail=f"Session {session_id} is not authenticated",
        )
    return _manager().get_events(
        run.workspace_id, run_id, since_sequence=since_sequence, subtree=subtree
    )
