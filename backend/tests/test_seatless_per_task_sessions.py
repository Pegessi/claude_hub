"""Per-task sessions and explicit session affinity."""

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    SessionKind,
    StartTaskRequest,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
)
from tests.test_managed_chat_workflow import workflow  # noqa: F401


def create(workflow, **kwargs):
    return workflow.manager.create_task(
        workflow.workspace_id,
        WorkspaceTaskCreate(
            title="Task", prompt="Do the work", agent_type=AgentType.CLAUDE, **kwargs
        ),
    )


@pytest.mark.asyncio
async def test_independent_sessions_and_explicit_affinity(workflow):
    manager = workflow.manager
    first, second = create(workflow), create(workflow)
    assert first.session_kind == second.session_kind == SessionKind.CHAT
    first = await manager.start_task(first.id)
    second = await manager.start_task(second.id)
    assert first.status == second.status == WorkspaceTaskStatus.WORKING
    assert first.session_id != second.session_id
    assert all(manager.sessions[t.session_id].ephemeral for t in (first, second))
    pinned = create(workflow)
    pinned = await manager.start_task(
        pinned.id, StartTaskRequest(target_session_id=first.session_id)
    )
    related = create(workflow, related_task_id=first.id)
    related = await manager.start_task(related.id)
    assert pinned.status == related.status == WorkspaceTaskStatus.QUEUED
    assert pinned.session_id == related.session_id == first.session_id
    await manager.dispatch_workspace(workflow.workspace_id)
    assert manager.tasks[related.id].session_id == first.session_id


def test_default_surfaces(workflow):
    manager = workflow.manager
    assert create(workflow).session_kind == SessionKind.CHAT
    assert create(workflow, session_kind=SessionKind.TERMINAL).session_kind == SessionKind.TERMINAL
    workspace = manager.workspaces[workflow.workspace_id]
    manager.workspaces[workspace.id] = workspace.model_copy(
        update={"target": ExecutionTarget.REMOTE}
    )
    assert create(workflow).session_kind == SessionKind.TERMINAL


@pytest.mark.asyncio
async def test_standard_done_removes_session(workflow):
    manager = workflow.manager
    task = await manager.start_task(create(workflow).id)
    session_id = task.session_id
    workflow.native.finish(session_id)
    # Human acceptance applies to a task ready for acceptance.
    manager.tasks[task.id] = task.model_copy(
        update={"status": WorkspaceTaskStatus.REVIEW, "review_skipped_at": manager._runtime_now()}
    )
    await manager.update_task(task.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.DONE))
    assert session_id not in manager.sessions
