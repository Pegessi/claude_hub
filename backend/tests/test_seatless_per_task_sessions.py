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
    assert first.dispatch_reason == second.dispatch_reason == "Dedicated task session"
    assert all(manager.sessions[t.session_id].ephemeral for t in (first, second))
    pinned = create(workflow)
    pinned = await manager.start_task(
        pinned.id, StartTaskRequest(target_session_id=first.session_id)
    )
    related = create(workflow, related_task_id=first.id)
    related = await manager.start_task(related.id)
    assert pinned.status == related.status == WorkspaceTaskStatus.QUEUED
    assert pinned.dispatch_reason == "User selected target agent"
    assert pinned.session_id == related.session_id == first.session_id
    await manager.dispatch_workspace(workflow.workspace_id)
    assert manager.tasks[related.id].session_id == first.session_id


def test_default_surfaces(workflow):
    manager = workflow.manager
    assert create(workflow).session_kind == SessionKind.CHAT
    term_task = create(workflow, session_kind=SessionKind.TERMINAL)
    assert term_task.session_kind == SessionKind.TERMINAL
    # Omitted session_kind on a related task must inherit the related task's surface
    # so Terminal context continuity is preserved.
    related_term = create(workflow, related_task_id=term_task.id)
    assert related_term.session_kind == SessionKind.TERMINAL
    workspace = manager.workspaces[workflow.workspace_id]
    manager.workspaces[workspace.id] = workspace.model_copy(
        update={"target": ExecutionTarget.REMOTE}
    )
    assert create(workflow).session_kind == SessionKind.TERMINAL


@pytest.mark.asyncio
async def test_retiring_session_race_provisions_fresh_ephemeral_session(workflow, monkeypatch):
    import asyncio

    manager = workflow.manager
    first = await manager.start_task(create(workflow).id)
    retiring_id = first.session_id
    assert retiring_id is not None
    retiring_tab_id = manager.sessions[retiring_id].tab_id
    workflow.native.finish(retiring_id)
    manager.tasks[first.id] = first.model_copy(
        update={"status": WorkspaceTaskStatus.REVIEW, "review_skipped_at": manager._runtime_now()}
    )

    entered_retire = asyncio.Event()
    release_retire = asyncio.Event()
    original_retire = manager._retire_session_resources

    async def slow_retire(session):
        if session.id == retiring_id:
            entered_retire.set()
            await release_retire.wait()
        await original_retire(session)

    monkeypatch.setattr(manager, "_retire_session_resources", slow_retire)

    done_coro = asyncio.create_task(
        manager.update_task(first.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.DONE))
    )
    await entered_retire.wait()
    assert retiring_id in manager._retiring_session_ids

    followup = create(workflow, related_task_id=first.id)
    start_coro = asyncio.create_task(manager.start_task(followup.id))
    await asyncio.sleep(0.02)
    release_retire.set()
    await done_coro
    started_followup = await start_coro

    assert started_followup.session_id is not None
    assert started_followup.session_id in manager.sessions
    assert manager.sessions[started_followup.session_id].tab_id != retiring_tab_id
    assert started_followup.dispatch_reason == "Dedicated task session"
    assert started_followup.status == WorkspaceTaskStatus.WORKING


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


@pytest.mark.asyncio
async def test_delete_task_endpoint_cleans_up_only_orphan_session(workflow, monkeypatch):
    import asyncio
    from importlib import import_module

    from httpx import ASGITransport, AsyncClient

    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.main import app
    from claude_hub.models import User

    manager = workflow.manager
    task = await manager.start_task(create(workflow).id)
    session_id = task.session_id
    workflow.native.finish(session_id)
    routes = import_module("claude_hub.api.workspaces")
    monkeypatch.setattr(routes, "workspace_manager", manager)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local", name="Local", email="local@localhost", avatar_url=None
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/workspaces/tasks/{task.id}")
        assert response.status_code == 204
        for _ in range(100):
            if session_id not in manager.sessions:
                break
            await asyncio.sleep(0.01)
        assert session_id not in manager.sessions
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_delete_task_preserves_session_referenced_by_another_task(workflow):
    import asyncio

    manager = workflow.manager
    first = await manager.start_task(create(workflow).id)
    second = create(workflow)
    manager.tasks[second.id] = second.model_copy(update={"session_id": first.session_id})
    manager.delete_task(first.id)
    await asyncio.sleep(0)
    assert first.session_id in manager.sessions


@pytest.mark.asyncio
async def test_delete_task_preserves_resident_session(workflow):
    import asyncio

    manager = workflow.manager
    task = await manager.start_task(create(workflow).id)
    workspace = manager.workspaces[workflow.workspace_id]
    manager.workspaces[workspace.id] = workspace.model_copy(
        update={"resident_agent_session_id": task.session_id}
    )
    manager.delete_task(task.id)
    await asyncio.sleep(0)
    assert task.session_id in manager.sessions
