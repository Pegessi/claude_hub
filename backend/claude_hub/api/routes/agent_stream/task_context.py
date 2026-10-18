"""Task execution context projection for terminal tabs."""

from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from claude_hub.api.routes.agent_stream.bindings import AgentStreamRouteBindings
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import User
from claude_hub.models.schemas import TaskExecutionControl, TaskExecutionReference


class TaskContextItem(BaseModel):
    id: str
    workspace_id: str
    execution_control: TaskExecutionControl
    execution_epoch: int
    progress_revision: int


class TabTaskContext(BaseModel):
    tab_id: str
    execution_ref: Optional[TaskExecutionReference] = None
    tasks: list[TaskContextItem] = Field(default_factory=list)
    workspace_ids: list[str] = Field(default_factory=list)
    reason: Literal["active", "not_observed", "inactive", "ambiguous"]


@dataclass(frozen=True)
class AgentStreamTaskContextEndpoints:
    tab_task_context: Callable[..., Any]


def register_task_context_routes(
    router: APIRouter, bindings: AgentStreamRouteBindings
) -> AgentStreamTaskContextEndpoints:
    """Register the terminal task-context route."""

    @router.get("/tabs/{tab_id}/task-context", response_model=TabTaskContext)
    async def tab_task_context(
        tab_id: str, current_user: User = Depends(get_current_user)
    ) -> TabTaskContext:
        if bindings.ttyd_manager().get_tab(tab_id) is None:
            raise HTTPException(status_code=404, detail="Tab not found")
        sessions = [
            item for item in bindings.workspace_manager().sessions.values() if item.tab_id == tab_id
        ]
        tailers = []
        tab_manager = bindings.initialized_tab_tailer_manager()
        if tab_manager is not None:
            existing = tab_manager._tailers.get(bindings.tab_stream_session_id(tab_id))
            if existing is not None:
                tailers.append(existing)
        managed_manager = bindings.initialized_tailer_manager()
        if managed_manager is not None:
            for session in sessions:
                existing = managed_manager._tailers.get(session.id)
                if existing is not None and existing not in tailers:
                    tailers.append(existing)
        refs = [
            peeked
            for tailer in tailers
            if (peeked := tailer.peek_task_execution_ref(tab_id)) is not None
        ]
        if len(refs) > 1:
            return TabTaskContext(tab_id=tab_id, reason="ambiguous")
        ref = TaskExecutionReference(**refs[0]) if refs else None
        workspace_ids = {
            session.workspace_id
            for session in sessions
            if session.workspace_id in bindings.workspace_manager().workspaces
        }
        linked = []
        for task in bindings.workspace_manager().tasks.values():
            assigned = any(
                task.execution_control == TaskExecutionControl.WORKSPACE
                and task.id in {session.task_id, session.current_task_id}
                and session.id in {task.session_id, task.review_session_id}
                for session in sessions
            )
            expected = task.execution_ref
            observed = bool(
                ref is not None
                and expected is not None
                and task.execution_control == TaskExecutionControl.INITIATOR
                and not task.execution_released
                and expected.session_id == ref.session_id
                and expected.run_epoch == ref.run_epoch
                and expected.turn_id == ref.turn_id
                and (expected.thread_id is None or expected.thread_id == ref.thread_id)
                and (expected.provider is None or expected.provider == ref.provider)
            )
            if assigned or observed:
                linked.append(
                    TaskContextItem(
                        id=task.id,
                        workspace_id=task.workspace_id,
                        execution_control=task.execution_control,
                        execution_epoch=task.execution_epoch,
                        progress_revision=task.progress_revision,
                    )
                )
                workspace_ids.add(task.workspace_id)
        return TabTaskContext(
            tab_id=tab_id,
            execution_ref=ref,
            tasks=linked,
            workspace_ids=sorted(workspace_ids),
            reason=("active" if ref is not None else ("inactive" if tailers else "not_observed")),
        )

    return AgentStreamTaskContextEndpoints(tab_task_context=tab_task_context)
