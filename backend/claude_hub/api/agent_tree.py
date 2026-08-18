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

    Returns None for local network requests (auth disabled). For non-local
    requests, fails closed with 403 if no session cookie is present.
    """
    from ..auth.dependencies import is_local_network_request

    if is_local_network_request(request):
        return None
    if not session_id:
        raise HTTPException(
            status_code=403,
            detail="Authentication required: no session cookie provided",
        )
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
    """Return True if ``session_id`` is a human LoginSession or a live
    (non-STOPPED) ManagedSession.

    A STOPPED or deleted ManagedSession is treated as unauthenticated: its
    terminal tab is gone and it can no longer act as an executor. Forged or
    stale cookies that resolve to a STOPPED session must be rejected.
    """
    from ..models.schemas import ManagedSessionStatus

    session = manager._wm.sessions.get(session_id)
    if session is not None:
        return session.status != ManagedSessionStatus.STOPPED
    return _is_human_session(session_id)


def _is_managed_session(manager: "AgentTreeManager", session_id: str) -> bool:
    """Return True if ``session_id`` is a ManagedSession (not a human session)."""
    return session_id in manager._wm.sessions


def _owned_subtree_run_ids(manager: "AgentTreeManager", session_id: str) -> set[str]:
    """Return the set of run IDs that ``session_id`` owns or supervises.

    A session owns a run if ``_session_owns_run`` returns True. It supervises
    a run if the run's path is under an owned run's path (the owned run's
    subtree).
    """
    owned_paths: list[str] = []
    for run in manager._runs.values():
        if _session_owns_run(manager, run, session_id):
            owned_paths.append(run.path)
    if not owned_paths:
        return set()
    result: set[str] = set()
    for run in manager._runs.values():
        if any(run.path == p or run.path.startswith(p + "/") for p in owned_paths):
            result.add(run.id)
    return result


def _assert_authority(
    manager: "AgentTreeManager", author_id: str, session_id: Optional[str]
) -> None:
    """Verify the caller's session owns the author run.

    Ownership rules:
    - Local network (session_id is None): authority not enforced.
    - Session must be a live authenticated ManagedSession or human
      LoginSession; forged/stale cookies get 403 before any ownership check.
    - Agent session that executes the run: allowed.
    - Human user (valid LoginSession): owns every run in the workspace.
    - Otherwise: 403.
    """
    if session_id is None:
        return
    # Reject forged or stale session cookies before checking ownership.
    if not _is_authenticated_session(manager, session_id):
        raise HTTPException(
            status_code=403,
            detail=f"Session {session_id} is not authenticated",
        )
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
) -> List[AgentEvent]:
    try:
        # Read permission: any authenticated caller (human or agent session)
        # may wait on events in their workspace. For agent sessions, the
        # recipient must be a run the session owns or supervises.
        if session_id is not None:
            # Reject forged or stale session cookies first.
            if not _is_authenticated_session(_manager(), session_id):
                raise HTTPException(
                    status_code=403,
                    detail=f"Session {session_id} is not authenticated",
                )
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
) -> AgentRun:
    try:
        # The caller may interrupt any run in its subtree. For simplicity,
        # we check that the caller owns the run or its supervisor.
        run = _manager().get_run(req.run_id)
        if run is not None and session_id is not None:
            # Reject forged or stale session cookies first.
            if not _is_authenticated_session(_manager(), session_id):
                raise HTTPException(
                    status_code=403,
                    detail=f"Session {session_id} is not authenticated",
                )
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
) -> List[AgentRun]:
    try:
        manager = _manager()
        # Read permission:
        # - Local network (session_id is None): no scoping.
        # - Human session: all runs in the workspace.
        # - ManagedSession: only runs in its own workspace that it owns or
        #   supervises (its subtree).
        if session_id is not None:
            if not _is_authenticated_session(manager, session_id):
                raise HTTPException(
                    status_code=403,
                    detail=f"Session {session_id} is not authenticated",
                )
            if _is_managed_session(manager, session_id):
                session = manager._wm.sessions[session_id]
                if session.workspace_id != workspace_id:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Session {session_id} may not read workspace {workspace_id}",
                    )
                allowed = _owned_subtree_run_ids(manager, session_id)
                runs = manager.list_runs(
                    ListRunsRequest(workspace_id=workspace_id, root_id=root_id, status=status)
                )
                return [r for r in runs if r.id in allowed]
        return manager.list_runs(
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
) -> List[AgentEvent]:
    manager = _manager()
    run = manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    # Read permission:
    # - Local network (session_id is None): no scoping.
    # - Human session: any run in the workspace.
    # - ManagedSession: only runs in its own workspace that it owns or
    #   supervises (its subtree).
    if session_id is not None:
        if not _is_authenticated_session(manager, session_id):
            raise HTTPException(
                status_code=403,
                detail=f"Session {session_id} is not authenticated",
            )
        if _is_managed_session(manager, session_id):
            session = manager._wm.sessions[session_id]
            if session.workspace_id != run.workspace_id:
                raise HTTPException(
                    status_code=403,
                    detail=f"Session {session_id} may not read workspace {run.workspace_id}",
                )
            allowed = _owned_subtree_run_ids(manager, session_id)
            if run.id not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"Session {session_id} may not read run {run_id}",
                )
    return manager.get_events(
        run.workspace_id, run_id, since_sequence=since_sequence, subtree=subtree
    )
