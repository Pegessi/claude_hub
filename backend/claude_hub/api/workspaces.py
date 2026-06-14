import hashlib
import json
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from ..auth.dependencies import get_current_user
from ..models import (
    AgentReport,
    AgentReportCreate,
    ContinueTaskRequest,
    DispatchDecisionRequest,
    EnsureWorkspaceAgentRequest,
    FeedbackLesson,
    FeedbackLessonCreate,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    FeedbackSummaryRequest,
    FeedbackSummaryRun,
    ManagedSession,
    ManualTaskControlRequest,
    RequestTaskReviewRequest,
    SendSessionMessageRequest,
    SpawnWorkerRequest,
    StartTaskRequest,
    User,
    Workspace,
    WorkspaceArtifactPreview,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
    WorkspaceUpdate,
)
from ..services import workspace_manager

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

# Per-session timestamps that tick on every status refresh without reflecting any
# content the board UI renders. Excluding them from the ETag lets idle 2.5s polls
# resolve to a 304 instead of re-shipping the whole (gzipped) board body.
_VOLATILE_SESSION_FIELDS = ("updated_at", "last_activity_at")
_VOLATILE_MARKDOWN_FIELDS = ("updated_at",)


def _board_etag(board: WorkspaceBoard) -> str:
    """Compute a stable, order-independent ETag over board *content*.

    Volatile per-session/markdown timestamps are stripped so that an idle board
    (heavy content byte-stable, only cosmetic timestamps churning) keeps the same
    ETag across polls. Lists are sorted by id so server-side ordering jitter does
    not change the hash.
    """
    payload: dict[str, Any] = json.loads(board.model_dump_json())

    for session in payload.get("sessions") or []:
        for field in _VOLATILE_SESSION_FIELDS:
            session.pop(field, None)

    for document in payload.get("markdown_documents") or []:
        for field in _VOLATILE_MARKDOWN_FIELDS:
            document.pop(field, None)

    for key in ("tasks", "sessions", "reports", "markdown_documents"):
        items = payload.get(key)
        if isinstance(items, list):
            items.sort(key=lambda item: item.get("id") or "")

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


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
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> Any:
    """Return tasks and managed sessions for one workspace.

    Sends a content-based ETag; an idle 2.5s poll whose ``If-None-Match`` still
    matches gets a bodyless 304 instead of the (gzipped) board payload.
    """
    try:
        board = await workspace_manager.get_board(workspace_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e

    etag = _board_etag(board)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    return board


@router.get(
    "/{workspace_id}/tasks/{task_id}/reports",
    response_model=List[AgentReport],
)
async def get_task_reports(
    workspace_id: str,
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> List[AgentReport]:
    """Full report history for a single task (detail panel, on-demand).

    The board response only carries the latest report per task; the detail
    panel fetches the complete history here when a task is opened.
    """
    try:
        return workspace_manager.reports_for_task(workspace_id, task_id)
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


@router.get("/{workspace_id}/lessons", response_model=List[FeedbackLesson])
async def list_feedback_lessons(
    workspace_id: str,
    query: str = "",
    limit: int = Query(20, ge=1, le=50),
    include_inactive: bool = False,
    current_user: User = Depends(get_current_user),
) -> List[FeedbackLesson]:
    """List or keyword-search active feedback lessons for a workspace."""
    try:
        return workspace_manager.feedback_lessons(
            workspace_id,
            query=query,
            limit=limit,
            include_inactive=include_inactive,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e


@router.post("/{workspace_id}/lessons", response_model=FeedbackLesson, status_code=201)
async def create_feedback_lesson(
    workspace_id: str,
    payload: FeedbackLessonCreate,
    current_user: User = Depends(get_current_user),
) -> FeedbackLesson:
    """Manually create or promote an active feedback lesson for a workspace."""
    try:
        return workspace_manager.create_feedback_lesson(workspace_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{workspace_id}/lessons/{lesson_id}", response_model=FeedbackLesson)
async def get_feedback_lesson(
    workspace_id: str,
    lesson_id: str,
    current_user: User = Depends(get_current_user),
) -> FeedbackLesson:
    """Fetch a single feedback lesson by ID. Records a "take" (hit) for usage tracking."""
    try:
        return workspace_manager.get_feedback_lesson(workspace_id, lesson_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Feedback lesson not found") from e


@router.delete("/{workspace_id}/lessons/{lesson_id}", response_model=FeedbackLesson)
async def delete_feedback_lesson(
    workspace_id: str,
    lesson_id: str,
    current_user: User = Depends(get_current_user),
) -> FeedbackLesson:
    """Archive an active feedback lesson so it no longer participates in retrieval."""
    try:
        return workspace_manager.delete_feedback_lesson(workspace_id, lesson_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Feedback lesson not found") from e


@router.post(
    "/{workspace_id}/lessons/summarize",
    response_model=FeedbackSummaryRun,
    status_code=201,
)
async def summarize_feedback_lessons(
    workspace_id: str,
    payload: FeedbackSummaryRequest | None = None,
    current_user: User = Depends(get_current_user),
) -> FeedbackSummaryRun:
    """Queue a system-internal Feedback Reaper task for workspace lesson summarization."""
    try:
        return await workspace_manager.summarize_workspace_feedback(
            workspace_id,
            payload or FeedbackSummaryRequest(),
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
    except RuntimeError as e:
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


@router.get("/{workspace_id}/artifacts/preview", response_model=WorkspaceArtifactPreview)
async def preview_workspace_artifact(
    workspace_id: str,
    path: str,
    report_id: str | None = None,
    current_user: User = Depends(get_current_user),
) -> WorkspaceArtifactPreview:
    """Return previewable Markdown content for a report artifact."""
    try:
        return workspace_manager.preview_artifact(workspace_id, path, report_id=report_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Artifact not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
        payload.title is None
        and payload.prompt is None
        and payload.status is None
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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


@router.post("/tasks/{task_id}/feedback/reap", response_model=FeedbackReaperRun)
async def reap_task_feedback(
    task_id: str,
    payload: FeedbackReaperRequest,
    current_user: User = Depends(get_current_user),
) -> FeedbackReaperRun:
    """Manually collect task feedback evidence and optional lesson drafts."""
    try:
        return workspace_manager.reap_task_feedback(task_id, payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Task not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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


@router.post("/tasks/{task_id}/abort", response_model=WorkspaceTask)
async def abort_task(
    task_id: str,
    payload: ManualTaskControlRequest,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Manually abort an active task and return it to the todo column."""
    try:
        return await workspace_manager.abort_task(task_id, payload)
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
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
