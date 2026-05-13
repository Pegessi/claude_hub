from typing import List

from fastapi import APIRouter, Depends, HTTPException

from ..auth.dependencies import get_current_user
from ..models import (
    AgentReport,
    AgentReportCreate,
    EnsureWorkspaceAgentRequest,
    ManagedSession,
    SendSessionMessageRequest,
    SpawnWorkerRequest,
    User,
    Workspace,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
)
from ..services import workspace_manager

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.get("", response_model=List[Workspace])
async def list_workspaces(current_user: User = Depends(get_current_user)) -> List[Workspace]:
    """List Agent Workspace configurations."""
    return workspace_manager.list_workspaces()


@router.post("", response_model=Workspace, status_code=201)
async def create_workspace(
    payload: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
) -> Workspace:
    """Create an Agent Workspace."""
    try:
        return workspace_manager.create_workspace(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{workspace_id}/board", response_model=WorkspaceBoard)
async def get_workspace_board(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
) -> WorkspaceBoard:
    """Return tasks and managed sessions for one workspace."""
    try:
        return await workspace_manager.get_board(workspace_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e


@router.post("/{workspace_id}/tasks", response_model=WorkspaceTask, status_code=201)
async def create_task(
    workspace_id: str,
    payload: WorkspaceTaskCreate,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Create a task in a workspace."""
    try:
        return workspace_manager.create_task(workspace_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e


@router.post("/{workspace_id}/agent", response_model=ManagedSession, status_code=201)
async def ensure_workspace_agent(
    workspace_id: str,
    payload: EnsureWorkspaceAgentRequest,
    current_user: User = Depends(get_current_user),
) -> ManagedSession:
    """Ensure a resident agent terminal exists for the workspace."""
    try:
        return await workspace_manager.ensure_workspace_agent(workspace_id, payload.agent_type)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e


@router.patch("/tasks/{task_id}", response_model=WorkspaceTask)
async def update_task(
    task_id: str,
    payload: WorkspaceTaskUpdate,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Update task status."""
    if payload.status is None:
        raise HTTPException(status_code=400, detail="No task update provided")
    try:
        return workspace_manager.update_task_status(task_id, payload.status)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a workspace task and its reports."""
    try:
        workspace_manager.delete_task(task_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e


@router.post("/tasks/{task_id}/spawn", response_model=ManagedSession, status_code=201)
async def spawn_worker(
    task_id: str,
    payload: SpawnWorkerRequest,
    current_user: User = Depends(get_current_user),
) -> ManagedSession:
    """Spawn a worker session for a task."""
    try:
        return await workspace_manager.spawn_worker(task_id, payload.agent_type)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/start", response_model=ManagedSession, status_code=201)
async def start_task(
    task_id: str,
    payload: SpawnWorkerRequest,
    current_user: User = Depends(get_current_user),
) -> ManagedSession:
    """Dispatch a task to the resident workspace agent."""
    try:
        return await workspace_manager.start_task(task_id, payload.agent_type)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/sessions/{managed_session_id}/send", status_code=204)
async def send_session_message(
    managed_session_id: str,
    payload: SendSessionMessageRequest,
    current_user: User = Depends(get_current_user),
) -> None:
    """Send a message to a managed session."""
    try:
        await workspace_manager.send_session_message(managed_session_id, payload.message)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Session not found") from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/sessions/{managed_session_id}/reports", response_model=AgentReport, status_code=201)
async def create_session_report(
    managed_session_id: str,
    payload: AgentReportCreate,
    current_user: User = Depends(get_current_user),
) -> AgentReport:
    """Append a progress report to a managed session."""
    try:
        return workspace_manager.create_report(managed_session_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Session not found") from e
