"""Task, session, and execution-control workspace routes."""

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from claude_hub.api.routes.workspaces.bindings import WorkspaceRouteBindings
from claude_hub.api.routes.workspaces.common import task_public_http
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    AgentReport,
    AgentReportCreate,
    ContinueTaskRequest,
    DispatchDecisionRequest,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    ManagedSessionPublic,
    ManualTaskControlRequest,
    RequestTaskReviewRequest,
    RetryUncertainDeliveryRequest,
    SendSessionMessageRequest,
    SessionKind,
    SpawnWorkerRequest,
    StartTaskRequest,
    TaskCleanupResult,
    User,
    WorkspaceTask,
    WorkspaceTaskUpdate,
)
from claude_hub.models.schemas import (
    TaskExecutionCapabilities,
    TaskExecutionHandoffRequest,
    TaskExecutionMutationResult,
    TaskManualProgressRequest,
    TaskProgressRequest,
)
from claude_hub.services.task_dependencies import TaskHasDependentsError
from claude_hub.services.task_graph import TaskHasDescendantsError
from claude_hub.services.workspace_manager._constants import (
    DeliveryUncertain,
    WorkspaceAgentInitializationError,
)
from claude_hub.services.workspace_manager._reports import ReportCallIdConflict
from claude_hub.services.workspace_manager._task_execution import (
    TaskExecutionConflict,
    TaskReporterForbidden,
)


@dataclass(frozen=True)
class WorkspaceExecutionEndpoints:
    update_task: Callable[..., Any]
    delete_task: Callable[..., Any]
    cleanup_task_session: Callable[..., Any]
    reap_task_feedback: Callable[..., Any]
    spawn_worker: Callable[..., Any]
    start_task: Callable[..., Any]
    continue_task: Callable[..., Any]
    request_task_review: Callable[..., Any]
    abort_task: Callable[..., Any]
    apply_dispatch_decision: Callable[..., Any]
    dispatch_workspace: Callable[..., Any]
    archive_session: Callable[..., Any]
    unarchive_session: Callable[..., Any]
    delete_session: Callable[..., Any]
    send_session_message: Callable[..., Any]
    retry_uncertain_delivery: Callable[..., Any]
    create_session_report: Callable[..., Any]
    task_execution_capabilities: Callable[..., Any]
    task_progress: Callable[..., Any]
    manual_task_progress: Callable[..., Any]
    handoff_task_execution: Callable[..., Any]


def register_execution_routes(
    router: APIRouter, bindings: WorkspaceRouteBindings
) -> WorkspaceExecutionEndpoints:
    """Register execution routes in their historical order."""

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
            and payload.depends_on_task_ids is None
            and "agent_tag" not in payload.model_fields_set
        ):
            raise HTTPException(status_code=400, detail="No task update provided")
        try:
            return await bindings.workspace_manager().update_task(task_id, payload)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Task not found") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/tasks/{task_id}", status_code=204)
    async def delete_task(
        task_id: str,
        current_user: User = Depends(get_current_user),
    ) -> None:
        """Delete a leaf workspace task and its durable reports/events."""
        try:
            bindings.workspace_manager().delete_task(task_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Task not found") from e
        except (TaskHasDescendantsError, TaskHasDependentsError, TaskExecutionConflict) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @router.post("/tasks/{task_id}/cleanup", response_model=TaskCleanupResult)
    async def cleanup_task_session(
        task_id: str,
        current_user: User = Depends(get_current_user),
    ) -> TaskCleanupResult:
        """Safely delete a caller-owned ephemeral session after a terminal task."""
        try:
            return await bindings.workspace_manager().cleanup_task_session(task_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Task not found") from e
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/tasks/{task_id}/feedback/reap", response_model=FeedbackReaperRun)
    async def reap_task_feedback(
        task_id: str,
        payload: FeedbackReaperRequest,
        current_user: User = Depends(get_current_user),
    ) -> FeedbackReaperRun:
        """Manually collect task feedback evidence and optional lesson drafts."""
        try:
            return bindings.workspace_manager().reap_task_feedback(task_id, payload)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Task not found") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/tasks/{task_id}/spawn", response_model=ManagedSessionPublic, status_code=201)
    async def spawn_worker(
        task_id: str,
        payload: SpawnWorkerRequest,
        current_user: User = Depends(get_current_user),
    ) -> ManagedSessionPublic:
        """Spawn a worker session for a task."""
        try:
            session = await bindings.workspace_manager().spawn_worker(task_id, payload.agent_type)
            return ManagedSessionPublic.from_managed_session(session)
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
            return await bindings.workspace_manager().start_task(task_id, payload)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Task not found") from e
        except WorkspaceAgentInitializationError as e:
            # Starting may provision the Task's dedicated agent; keep the same
            # contract as direct agent creation.
            raise HTTPException(status_code=502, detail=str(e)) from e
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
            return await bindings.workspace_manager().continue_task(task_id, payload)
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
            return await bindings.workspace_manager().request_task_review(task_id, payload)
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
            return await bindings.workspace_manager().abort_task(task_id, payload)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Task not found") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
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
            return await bindings.workspace_manager().apply_dispatch_decision(task_id, payload)
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
            await bindings.workspace_manager().dispatch_workspace(workspace_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Workspace not found") from e

    @router.delete("/sessions/{managed_session_id}", status_code=204)
    async def delete_session(
        managed_session_id: str,
        current_user: User = Depends(get_current_user),
    ) -> None:
        """Delete an idle managed agent session and its terminal tab."""
        try:
            await bindings.workspace_manager().delete_session(managed_session_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Session not found") from e
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post(
        "/{workspace_id}/sessions/{managed_session_id}/archive", response_model=ManagedSessionPublic
    )
    async def archive_session(
        workspace_id: str,
        managed_session_id: str,
        current_user: User = Depends(get_current_user),
    ) -> ManagedSessionPublic:
        """Release a managed Chat runtime while retaining resumable history."""
        try:
            session = await bindings.workspace_manager().archive_session(
                workspace_id, managed_session_id
            )
            return ManagedSessionPublic.from_managed_session(session)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found in workspace") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/{workspace_id}/sessions/{managed_session_id}/unarchive",
        response_model=ManagedSessionPublic,
    )
    async def unarchive_session(
        workspace_id: str,
        managed_session_id: str,
        current_user: User = Depends(get_current_user),
    ) -> ManagedSessionPublic:
        """Restore a managed Chat and best-effort restart its provider runtime."""
        try:
            session = await bindings.workspace_manager().unarchive_session(
                workspace_id, managed_session_id
            )
            return ManagedSessionPublic.from_managed_session(session)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found in workspace") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sessions/{managed_session_id}/send", status_code=204)
    async def send_session_message(
        managed_session_id: str,
        payload: SendSessionMessageRequest,
        current_user: User = Depends(get_current_user),
    ) -> None:
        """Send a message to a managed session."""
        try:
            session = bindings.workspace_manager().sessions.get(managed_session_id)
            if session is not None and session.session_kind == SessionKind.CHAT:
                await bindings.workspace_manager().send_chat_session_message(
                    managed_session_id, payload.message, payload.attachments, payload.call_id
                )
            else:
                await bindings.workspace_manager().send_session_message(
                    managed_session_id,
                    payload.message,
                    payload.attachments,
                )
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Session not found") from e
        except (RuntimeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/sessions/{managed_session_id}/retry-uncertain", status_code=204)
    async def retry_uncertain_delivery(
        managed_session_id: str,
        payload: RetryUncertainDeliveryRequest,
        current_user: User = Depends(get_current_user),
    ) -> None:
        """Operator retry of an uncertain delivery.

        Moves ``call_id`` from ``uncertain_call_ids`` back to
        ``pending_call_ids`` so the normal pump path can re-deliver it. The
        original payload is preserved. Rejects unknown, delivered, processing,
        and cross-session call_ids.

        The actor identity is derived from the authenticated ``current_user``
        (``open_id``) so the audit trail cannot be forged by the client.
        """
        try:
            await bindings.workspace_manager().retry_uncertain_delivery(
                managed_session_id,
                payload.call_id,
                reason=payload.reason,
                actor=current_user.open_id,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except DeliveryUncertain as e:
            # The retry hit another ambiguous tmux failure; the delivery is
            # still uncertain. Surface a visible 400 (not a false 204) so the
            # operator can decide whether to retry again.
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post(
        "/sessions/{managed_session_id}/reports", response_model=AgentReport, status_code=201
    )
    async def create_session_report(
        managed_session_id: str,
        payload: AgentReportCreate,
        current_user: User = Depends(get_current_user),
    ) -> AgentReport:
        """Append a progress report to a managed session."""
        try:
            return await bindings.workspace_manager().create_report(managed_session_id, payload)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="Session not found") from e
        except ReportCallIdConflict as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/{workspace_id}/task-capabilities", response_model=TaskExecutionCapabilities)
    async def task_execution_capabilities(
        workspace_id: str,
        current_user: User = Depends(get_current_user),
    ) -> TaskExecutionCapabilities:
        if workspace_id not in bindings.workspace_manager().workspaces:
            raise HTTPException(status_code=404, detail="Workspace not found")
        return TaskExecutionCapabilities()

    @router.post(
        "/{workspace_id}/tasks/{task_id}/progress", response_model=TaskExecutionMutationResult
    )
    async def task_progress(
        workspace_id: str,
        task_id: str,
        payload: TaskProgressRequest,
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> TaskExecutionMutationResult:
        try:
            return await bindings.workspace_manager().record_task_progress(
                workspace_id, task_id, payload, request.headers.get("X-Task-Reporter-Key")
            )
        except (KeyError, ValueError, RuntimeError, TaskReporterForbidden) as exc:
            raise task_public_http(exc) from None

    @router.post(
        "/{workspace_id}/tasks/{task_id}/progress/manual",
        response_model=TaskExecutionMutationResult,
    )
    async def manual_task_progress(
        workspace_id: str,
        task_id: str,
        payload: TaskManualProgressRequest,
        current_user: User = Depends(get_current_user),
    ) -> TaskExecutionMutationResult:
        try:
            return await bindings.workspace_manager().record_manual_task_progress(
                workspace_id, task_id, payload
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise task_public_http(exc) from None

    @router.post(
        "/{workspace_id}/tasks/{task_id}/handoff", response_model=TaskExecutionMutationResult
    )
    async def handoff_task_execution(
        workspace_id: str,
        task_id: str,
        payload: TaskExecutionHandoffRequest,
        current_user: User = Depends(get_current_user),
    ) -> TaskExecutionMutationResult:
        try:
            return await bindings.workspace_manager().handoff_task_execution(
                workspace_id, task_id, payload
            )
        except TaskExecutionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="Task not found") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_task_handoff") from None

    return WorkspaceExecutionEndpoints(
        update_task=update_task,
        delete_task=delete_task,
        cleanup_task_session=cleanup_task_session,
        reap_task_feedback=reap_task_feedback,
        spawn_worker=spawn_worker,
        start_task=start_task,
        continue_task=continue_task,
        request_task_review=request_task_review,
        abort_task=abort_task,
        apply_dispatch_decision=apply_dispatch_decision,
        dispatch_workspace=dispatch_workspace,
        archive_session=archive_session,
        unarchive_session=unarchive_session,
        delete_session=delete_session,
        send_session_message=send_session_message,
        retry_uncertain_delivery=retry_uncertain_delivery,
        create_session_report=create_session_report,
        task_execution_capabilities=task_execution_capabilities,
        task_progress=task_progress,
        manual_task_progress=manual_task_progress,
        handoff_task_execution=handoff_task_execution,
    )
