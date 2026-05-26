from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..auth.dependencies import get_current_user
from ..models import (
    AgentReport,
    AgentReportCreate,
    ContinueTaskRequest,
    DispatchDecisionRequest,
    EnsureWorkspaceAgentRequest,
    ManagedSession,
    RequestTaskReviewRequest,
    SendSessionMessageRequest,
    SpawnWorkerRequest,
    StartTaskRequest,
    User,
    Workspace,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
    WorkspaceUpdate,
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


@router.patch("/{workspace_id}", response_model=Workspace)
async def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    current_user: User = Depends(get_current_user),
) -> Workspace:
    """Update an Agent Workspace's editable fields."""
    try:
        return workspace_manager.update_workspace(workspace_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/attachments/{attachment_id}")
async def get_attachment(
    attachment_id: str,
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    """Return a persisted workspace task attachment."""
    try:
        attachment = workspace_manager.get_attachment(attachment_id)
        return FileResponse(
            attachment.path,
            media_type=attachment.mime_type,
            filename=attachment.filename,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Attachment not found") from e


@router.post("/{workspace_id}/agent", response_model=ManagedSession, status_code=201)
async def ensure_workspace_agent(
    workspace_id: str,
    payload: EnsureWorkspaceAgentRequest,
    current_user: User = Depends(get_current_user),
) -> ManagedSession:
    """Ensure a resident agent terminal exists for the workspace."""
    try:
        return await workspace_manager.ensure_workspace_agent(workspace_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.patch("/tasks/{task_id}", response_model=WorkspaceTask)
async def update_task(
    task_id: str,
    payload: WorkspaceTaskUpdate,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Update task metadata or status."""
    if (
        payload.status is None
        and payload.goal_packet is None
        and payload.task_mode is None
        and payload.review_profiles is None
        and payload.autonomy_policy is None
        and payload.autonomous_run is None
    ):
        raise HTTPException(status_code=400, detail="No task update provided")
    try:
        return await workspace_manager.update_task(task_id, payload)
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


@router.post("/tasks/{task_id}/start", response_model=WorkspaceTask, status_code=201)
async def start_task(
    task_id: str,
    payload: StartTaskRequest,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Queue a task and dispatch it when its target agent is available."""
    try:
        return await workspace_manager.start_task(task_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/continue", response_model=WorkspaceTask)
async def continue_task(
    task_id: str,
    payload: ContinueTaskRequest,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Move a review task back to working on its original agent."""
    try:
        return await workspace_manager.continue_task(task_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/request-review", response_model=WorkspaceTask)
async def request_task_review(
    task_id: str,
    payload: RequestTaskReviewRequest | None = None,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Manually request reviewer checks for a task."""
    try:
        return await workspace_manager.request_task_review(task_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/tasks/{task_id}/dispatch-decision", response_model=WorkspaceTask)
async def apply_dispatch_decision(
    task_id: str,
    payload: DispatchDecisionRequest,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Apply a structured dispatch decision from the dispatcher agent."""
    try:
        return await workspace_manager.apply_dispatch_decision(task_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task or agent not found") from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{workspace_id}/dispatch", status_code=204)
async def dispatch_workspace(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Manually trigger dispatch for queued tasks in one workspace."""
    try:
        await workspace_manager.dispatch_workspace(workspace_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e


@router.delete("/sessions/{managed_session_id}", status_code=204)
async def delete_session(
    managed_session_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete an idle managed agent session and its terminal tab."""
    try:
        await workspace_manager.delete_session(managed_session_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Session not found") from e
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
        await workspace_manager.send_session_message(
            managed_session_id,
            payload.message,
            payload.attachments,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Session not found") from e
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/sessions/{managed_session_id}/reports", response_model=AgentReport, status_code=201)
async def create_session_report(
    managed_session_id: str,
    payload: AgentReportCreate,
    current_user: User = Depends(get_current_user),
) -> AgentReport:
    """Append a progress report to a managed session."""
    try:
        return await workspace_manager.create_report(managed_session_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Session not found") from e
