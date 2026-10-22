import hashlib
import json
import logging
import uuid
from typing import Any, Callable, Coroutine, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute

from claude_hub.api.routes.workspaces.common import (
    TaskSecretSafeRoute,
    board_etag,
    task_public_http,
)
from claude_hub.api.routes.workspaces.execution import register_execution_routes

from ..auth.dependencies import get_current_user
from ..config import settings
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
    ManagedSessionPublic,
    ManualTaskControlRequest,
    RequestTaskReviewRequest,
    RetryUncertainDeliveryRequest,
    SendSessionMessageRequest,
    SessionKind,
    SpawnWorkerRequest,
    StartTaskRequest,
    TaskCleanupResult,
    TaskFollowupRequest,
    TaskMailboxAckRequest,
    User,
    Workspace,
    WorkspaceArtifactPreview,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceEnsure,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
    WorkspaceUpdate,
    redact_workspace_board_for_public,
)
from ..models.schemas import (
    TaskExecutionCapabilities,
    TaskExecutionHandoffRequest,
    TaskExecutionMutationResult,
    TaskManualProgressRequest,
    TaskProgressRequest,
)
from ..models.task_mailbox import TaskEvent
from ..services import WorkspaceManager, workspace_manager
from ..services.task_dependencies import TaskHasDependentsError
from ..services.task_graph import TaskHasDescendantsError
from ..services.task_mailbox import TaskCallIdConflict
from ..services.workspace_identity import (
    AmbiguousWorkspaceError,
    DuplicateWorkspaceError,
    WorkspaceIdentityError,
)
from ..services.workspace_manager._constants import (
    DeliveryUncertain,
    WorkspaceAgentInitializationError,
)
from ..services.workspace_manager._reports import ReportCallIdConflict
from ..services.workspace_manager._task_execution import (
    TaskExecutionConflict,
    TaskReporterForbidden,
)

_TaskSecretSafeRoute = TaskSecretSafeRoute
_board_etag = board_etag
_task_public_http = task_public_http


router = APIRouter(prefix="/api/workspaces", tags=["workspaces"], route_class=_TaskSecretSafeRoute)
logger = logging.getLogger(__name__)


@router.get("", response_model=List[Workspace])
async def list_workspaces(current_user: User = Depends(get_current_user)) -> List[Workspace]:
    """List Agent Workspace configurations."""
    return workspace_manager.list_workspaces()


@router.get("/sessions", response_model=List[ManagedSessionPublic])
async def list_managed_sessions(
    current_user: User = Depends(get_current_user),
) -> List[ManagedSessionPublic]:
    """List managed sessions across workspaces for terminal-tab discovery.

    The terminal shell is independent of the currently selected Agent
    Workspace, so it cannot use the active board as its session identity
    index. Returns the public (env-redacted) projection of every managed
    session so the frontend can map a terminal tab to its managed session.
    """
    sessions = list(workspace_manager.sessions.values())
    return [ManagedSessionPublic.from_managed_session(s) for s in sessions]


@router.post("", response_model=Workspace, status_code=201)
async def create_workspace(
    payload: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
) -> Workspace:
    """Create an Agent Workspace."""
    try:
        return workspace_manager.create_workspace(payload)
    except (DuplicateWorkspaceError, AmbiguousWorkspaceError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/ensure", response_model=Workspace)
async def ensure_workspace(
    payload: WorkspaceEnsure,
    current_user: User = Depends(get_current_user),
) -> Workspace:
    """Ensure a workspace exists for a repo identity (reuse or create)."""
    try:
        return workspace_manager.ensure_workspace(payload)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (DuplicateWorkspaceError, AmbiguousWorkspaceError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
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
    except DuplicateWorkspaceError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except WorkspaceIdentityError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{workspace_id}/resident/run", response_model=Workspace)
async def run_resident_now(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
) -> Workspace:
    """Force the resident agent to run on the next monitor tick ("run now").

    Applies the currently-saved directive and periodic tasks. Returns the
    updated workspace (with ``resident_agent_run_requested_at`` stamped). The
    cycle fires within one monitor interval and respects the existing
    WORKING-skip guard (a busy resident defers rather than double-drives).
    """
    try:
        return workspace_manager.request_resident_run(workspace_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{workspace_id}/resident/events", include_in_schema=False)
@router.post("/{workspace_id}/resident/wait", include_in_schema=False)
@router.post("/{workspace_id}/resident/ack", include_in_schema=False)
async def removed_resident_mailbox_routes(workspace_id: str) -> None:
    """Preserve the removed mailbox surface as an explicit 404 tombstone."""
    raise HTTPException(status_code=404, detail="Resident mailbox route removed")


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a workspace, its sessions/tasks/reports, and its terminal tabs."""
    try:
        await workspace_manager.delete_workspace(workspace_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e


@router.get("/{workspace_id}/board", response_model=WorkspaceBoard)
async def get_workspace_board(
    workspace_id: str,
    request: Request,
    response: Response,
    include_archived: bool = Query(False, description="Include archived managed sessions."),
    tasks_limit: int | None = Query(
        default=None,
        ge=1,
        le=100,
        description=("When set, return all non-Done tasks plus this many most-recent Done tasks."),
    ),
    tasks_cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a prior paginated board response (requires tasks_limit).",
    ),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Return tasks and managed sessions for one workspace.

    Sends a content-based ETag; an idle 2.5s poll whose ``If-None-Match`` still
    matches gets a bodyless 304 instead of the (gzipped) board payload.

    Without ``tasks_limit`` the full task history is returned (CLI default).
    """
    if tasks_cursor is not None and tasks_limit is None:
        raise HTTPException(
            status_code=400,
            detail="tasks_cursor requires tasks_limit",
        )
    try:
        board = await workspace_manager.get_board(
            workspace_id,
            include_archived=include_archived,
            tasks_limit=tasks_limit,
            tasks_cursor=tasks_cursor,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    etag = _board_etag(board)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    return redact_workspace_board_for_public(board)


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


@router.post(
    "/{workspace_id}/tasks",
    response_model=WorkspaceTask,
    status_code=201,
    responses={200: {"model": WorkspaceTask, "description": "Existing registration replayed"}},
)
async def create_task(
    workspace_id: str,
    payload: WorkspaceTaskCreate,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Register one Task; actor identity comes only from Hub authentication."""
    actor_key = hashlib.sha256(
        json.dumps(
            {"user": current_user.open_id, "oauth_app": settings.feishu_app_id or ""},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    try:
        task, replayed = await workspace_manager.register_task(workspace_id, payload, actor_key)
    except TaskExecutionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace not found") from None
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_task_create") from None
    response.status_code = 200 if replayed else 201
    response.headers["X-Task-Replayed"] = "true" if replayed else "false"
    return task


@router.get("/{workspace_id}/tasks/tree", response_model=List[WorkspaceTask])
async def list_top_level_task_tree(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
) -> List[WorkspaceTask]:
    """Top-level Tasks in the workspace Task graph."""
    try:
        return workspace_manager.list_top_level_tasks(workspace_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _task_public_http(exc) from exc


@router.get("/{workspace_id}/tasks/{task_id}/tree", response_model=List[WorkspaceTask])
async def list_task_subtree(
    workspace_id: str,
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> List[WorkspaceTask]:
    """Subtree of Tasks including ``task_id``. Validates workspace membership."""
    try:
        return workspace_manager.list_task_subtree(workspace_id, task_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _task_public_http(exc) from exc


@router.get("/{workspace_id}/tasks/{task_id}/events", response_model=List[TaskEvent])
async def list_task_mailbox_events(
    workspace_id: str,
    task_id: str,
    since_sequence: int = Query(0, ge=0),
    subtree: bool = Query(False),
    current_user: User = Depends(get_current_user),
) -> List[TaskEvent]:
    """TaskMailbox events for ``task:<task_id>``. Optional subtree replay."""
    try:
        return workspace_manager.list_task_mailbox_events(
            workspace_id,
            task_id,
            since_sequence=since_sequence,
            subtree=subtree,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _task_public_http(exc) from exc


@router.post("/{workspace_id}/tasks/{task_id}/wait", response_model=List[TaskEvent])
async def wait_task_mailbox_events(
    workspace_id: str,
    task_id: str,
    since_sequence: int = Query(0, ge=0),
    subtree: bool = Query(False),
    timeout_seconds: float = Query(30.0, ge=0),
    current_user: User = Depends(get_current_user),
) -> List[TaskEvent]:
    """Directed long-poll for ``task:<task_id>`` TaskMailbox events."""
    try:
        return await workspace_manager.wait_task_mailbox_events(
            workspace_id,
            task_id,
            since_sequence=since_sequence,
            subtree=subtree,
            timeout_seconds=timeout_seconds,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _task_public_http(exc) from exc


@router.post("/{workspace_id}/tasks/{task_id}/ack", response_model=WorkspaceTask)
async def ack_task_mailbox(
    workspace_id: str,
    task_id: str,
    payload: TaskMailboxAckRequest,
    current_user: User = Depends(get_current_user),
) -> WorkspaceTask:
    """Advance ``Task.consumer_ack_sequence`` for a TaskMailbox consumer."""
    try:
        result = workspace_manager.ack_task_mailbox(
            workspace_id,
            payload.sequence,
            task_id=task_id,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _task_public_http(exc) from exc
    assert isinstance(result, WorkspaceTask)
    return result


@router.post("/{workspace_id}/tasks/{task_id}/followup", response_model=TaskEvent)
async def followup_workspace_task(
    workspace_id: str,
    task_id: str,
    payload: TaskFollowupRequest,
    current_user: User = Depends(get_current_user),
) -> TaskEvent:
    """Write a TaskMailbox followup on the Task Graph REST surface."""
    call_id = payload.call_id or str(uuid.uuid4())
    try:
        return await workspace_manager.followup_task(
            workspace_id,
            task_id,
            payload.message,
            call_id,
        )
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _task_public_http(exc) from exc


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
    """Queue or return the visible managed Feedback Reaper task for this workspace."""
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


@router.post("/{workspace_id}/agent", response_model=ManagedSessionPublic, status_code=201)
async def ensure_workspace_agent(
    workspace_id: str,
    payload: EnsureWorkspaceAgentRequest,
    current_user: User = Depends(get_current_user),
) -> ManagedSessionPublic:
    """Ensure a resident agent terminal exists for the workspace."""
    try:
        session = await workspace_manager.ensure_workspace_agent(workspace_id, payload)
        return ManagedSessionPublic.from_managed_session(session)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Workspace not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except WorkspaceAgentInitializationError as e:
        logger.exception(
            "Failed to initialize workspace agent workspace_id=%s",
            workspace_id,
        )
        raise HTTPException(status_code=502, detail=str(e)) from e


class _WorkspaceBindings:
    def workspace_manager(self) -> WorkspaceManager:
        return workspace_manager


_execution_endpoints = register_execution_routes(router, _WorkspaceBindings())
update_task = _execution_endpoints.update_task
delete_task = _execution_endpoints.delete_task
cleanup_task_session = _execution_endpoints.cleanup_task_session
reap_task_feedback = _execution_endpoints.reap_task_feedback
spawn_worker = _execution_endpoints.spawn_worker
start_task = _execution_endpoints.start_task
continue_task = _execution_endpoints.continue_task
request_task_review = _execution_endpoints.request_task_review
abort_task = _execution_endpoints.abort_task
apply_dispatch_decision = _execution_endpoints.apply_dispatch_decision
dispatch_workspace = _execution_endpoints.dispatch_workspace
delete_session = _execution_endpoints.delete_session
send_session_message = _execution_endpoints.send_session_message
retry_uncertain_delivery = _execution_endpoints.retry_uncertain_delivery
create_session_report = _execution_endpoints.create_session_report
task_execution_capabilities = _execution_endpoints.task_execution_capabilities
task_progress = _execution_endpoints.task_progress
manual_task_progress = _execution_endpoints.manual_task_progress
handoff_task_execution = _execution_endpoints.handoff_task_execution
