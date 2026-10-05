"""Prerequisite contracts at manager, dispatch, REST and CLI boundaries."""

from __future__ import annotations

import json
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from claude_hub.auth.dependencies import get_current_user
from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.main import cli
from claude_hub.main import app
from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    DispatchDecisionRequest,
    ManagedSession,
    ManagedSessionStatus,
    ScheduledTask,
    ScheduledTaskKind,
    User,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
)
from claude_hub.services.task_dependencies import TaskHasDependentsError, task_dependency_blockers
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")
_api = import_module("claude_hub.api.workspaces")


@pytest.fixture()
def manager(monkeypatch, tmp_path):
    root = tmp_path / "state"
    for module in (_wm, _wm._state, _wm._persistence):
        monkeypatch.setattr(module, "INDEX_FILE", root / "index.json")
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    result = WorkspaceManager()
    monkeypatch.setattr(result, "dispatch_workspace", AsyncMock())
    return result


@pytest.fixture()
def workspace(manager, tmp_path):
    return manager.create_workspace(WorkspaceCreate(name="Dependencies", path=str(tmp_path)))


def create(manager, workspace, title="task", **kwargs):
    return manager.create_task(
        workspace.id, WorkspaceTaskCreate(title=title, prompt="Implement and validate", **kwargs)
    )


def session_for(manager, workspace):
    now = _wm._now()
    session = ManagedSession(
        id="worker",
        workspace_id=workspace.id,
        tab_id="tab",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="worker",
        workspace_path=workspace.path,
        tmux_session="unused",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    return session


@pytest.mark.asyncio
async def test_graph_roundtrip_rejects_cycle_and_keeps_tree_independent(manager, workspace):
    root = create(manager, workspace, "supervisor")
    first = create(manager, workspace, "first", parent_task_id=root.id)
    second = create(manager, workspace, "second", depends_on_task_ids=[first.id, first.id])
    assert second.depends_on_task_ids == [first.id]
    assert second.parent_task_id is None
    with pytest.raises(ValueError, match="cycle"):
        await manager.update_task(first.id, WorkspaceTaskUpdate(depends_on_task_ids=[second.id]))
    assert manager.tasks[first.id].depends_on_task_ids == []
    fresh = WorkspaceManager()
    assert fresh.tasks[second.id].depends_on_task_ids == [first.id]
    assert fresh.tasks[first.id].parent_task_id == root.id
    # Pre-feature records load unchanged without a migration or new controller.
    path = manager._workspace_state_file(workspace.id)
    payload = json.loads(path.read_text())
    for item in payload["tasks"]:
        item.pop("depends_on_task_ids", None)
    path.write_text(json.dumps(payload))
    assert all(not task.depends_on_task_ids for task in WorkspaceManager().tasks.values())


@pytest.mark.asyncio
async def test_invalid_edges_have_no_mutation_and_active_edges_are_frozen(
    manager, workspace, tmp_path
):
    first = create(manager, workspace)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_ws = manager.create_workspace(WorkspaceCreate(name="Other", path=str(other_dir)))
    other = create(manager, other_ws)
    original_state = manager._workspace_state_file(workspace.id).read_bytes()
    for ids, message in [
        ([first.id], "cycle"),
        ([other.id], "different workspace"),
        (["missing"], "not found"),
    ]:
        with pytest.raises(ValueError, match=message):
            await manager.update_task(first.id, WorkspaceTaskUpdate(depends_on_task_ids=ids))
    assert manager._workspace_state_file(workspace.id).read_bytes() == original_state
    manager.tasks[first.id] = first.model_copy(update={"status": WorkspaceTaskStatus.WORKING})
    with pytest.raises(ValueError, match="Only todo"):
        await manager.update_task(first.id, WorkspaceTaskUpdate(depends_on_task_ids=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [WorkspaceTaskStatus.TODO, WorkspaceTaskStatus.REVIEW, WorkspaceTaskStatus.FAILED]
)
async def test_start_gate_precedes_session_creation_and_requires_done(
    manager, workspace, monkeypatch, status
):
    prerequisite = create(manager, workspace, "prerequisite")
    manager.tasks[prerequisite.id] = prerequisite.model_copy(update={"status": status})
    dependent = create(manager, workspace, "dependent", depends_on_task_ids=[prerequisite.id])
    ensure = AsyncMock()
    refresh = AsyncMock()
    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure)
    monkeypatch.setattr(manager, "_refresh_session_statuses", refresh)
    with pytest.raises(ValueError, match=prerequisite.id):
        await manager.start_task(dependent.id)
    ensure.assert_not_called()
    refresh.assert_not_called()
    assert manager.tasks[dependent.id].status == WorkspaceTaskStatus.TODO
    session_for(manager, workspace)
    manager.tasks[prerequisite.id] = prerequisite.model_copy(
        update={"status": WorkspaceTaskStatus.DONE}
    )
    started = await manager.start_task(dependent.id)
    assert started.status == WorkspaceTaskStatus.QUEUED


@pytest.mark.asyncio
async def test_queue_continue_and_crash_recovery_cannot_bypass_dependencies(
    manager, workspace, monkeypatch
):
    prerequisite = create(manager, workspace, "prerequisite")
    blocked = create(manager, workspace, "blocked", depends_on_task_ids=[prerequisite.id])
    ready = create(manager, workspace, "ready")
    session = session_for(manager, workspace)
    for task in (blocked, ready):
        manager.tasks[task.id] = task.model_copy(
            update={"status": WorkspaceTaskStatus.QUEUED, "session_id": session.id}
        )
    assert manager._next_queued_task(session.id).id == ready.id
    send = AsyncMock()
    rename = AsyncMock(return_value=session)
    monkeypatch.setattr(manager, "_send_dispatch_message", send)
    monkeypatch.setattr(manager, "_rename_session_for_task", rename)
    with pytest.raises(ValueError, match="dependencies"):
        await manager._dispatch_task_to_session(manager.tasks[blocked.id], session)
    rename.assert_not_called()
    manager.sessions[session.id] = session.model_copy(update={"task_id": blocked.id})
    await manager._recover_queued_task_ownership(workspace.id)
    send.assert_not_called()
    with pytest.raises(ValueError, match="dependencies"):
        await manager.apply_dispatch_decision(
            blocked.id, DispatchDecisionRequest(target_session_id=session.id)
        )
    manager.tasks[blocked.id] = manager.tasks[blocked.id].model_copy(
        update={"status": WorkspaceTaskStatus.FAILED}
    )
    with pytest.raises(ValueError, match="dependencies"):
        await manager.continue_task(blocked.id)
    with pytest.raises(ValueError, match="dependencies"):
        await manager.update_task(
            blocked.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.WORKING)
        )
    # Existing work is not cancelled when a prerequisite is reopened.
    manager.tasks[blocked.id] = manager.tasks[blocked.id].model_copy(
        update={"status": WorkspaceTaskStatus.WORKING}
    )
    assert (await manager.start_task(blocked.id)).status == WorkspaceTaskStatus.WORKING


@pytest.mark.asyncio
async def test_delete_prerequisite_requires_explicit_edge_removal(manager, workspace):
    prerequisite = create(manager, workspace)
    dependent = create(manager, workspace, depends_on_task_ids=[prerequisite.id])
    with pytest.raises(TaskHasDependentsError, match=dependent.id):
        manager.delete_task(prerequisite.id)
    await manager.update_task(dependent.id, WorkspaceTaskUpdate(depends_on_task_ids=[]))
    manager.delete_task(prerequisite.id)
    assert prerequisite.id not in manager.tasks


def test_corrupt_persisted_dependency_fails_closed(manager, workspace):
    task = create(manager, workspace)
    broken = task.model_copy(update={"depends_on_task_ids": ["missing"]})
    assert "not found" in task_dependency_blockers(manager.tasks, broken)[0]


def test_rest_create_patch_start_delete_dependency_contract(manager, workspace, monkeypatch):
    monkeypatch.setattr(_api, "workspace_manager", manager)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="tester", name="tester", email="tester@example.invalid"
    )
    try:
        client = TestClient(app, raise_server_exceptions=True)
        prerequisite = create(manager, workspace)
        response = client.post(
            f"/api/workspaces/{workspace.id}/tasks",
            json={"title": "child", "prompt": "work", "depends_on_task_ids": [prerequisite.id]},
        )
        assert response.status_code == 201, response.text
        dependent_id = response.json()["id"]
        assert response.json()["depends_on_task_ids"] == [prerequisite.id]
        response = client.post(f"/api/workspaces/tasks/{dependent_id}/start", json={})
        assert response.status_code == 400
        assert prerequisite.id in response.json()["detail"]
        assert client.delete(f"/api/workspaces/tasks/{prerequisite.id}").status_code == 409
        response = client.patch(
            f"/api/workspaces/tasks/{dependent_id}", json={"depends_on_task_ids": []}
        )
        assert response.status_code == 200, response.text
        assert response.json()["depends_on_task_ids"] == []
        response = client.patch(
            f"/api/workspaces/tasks/{dependent_id}", json={"depends_on_task_ids": ["missing"]}
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_cli_dependency_options_send_clear_and_replace_without_ambiguity(monkeypatch):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "child", **captured[-1]})

    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient(base_url="http://testserver", transport=httpx.MockTransport(handler)),
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "task",
            "create",
            "ws",
            "--title",
            "t",
            "--prompt",
            "p",
            "--depends-on",
            "a",
            "--depends-on",
            "b",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured[-1]["depends_on_task_ids"] == ["a", "b"]
    result = runner.invoke(cli, ["task", "update", "child", "--depends-on", "c"])
    assert result.exit_code == 0, result.output
    assert captured[-1]["depends_on_task_ids"] == ["c"]
    result = runner.invoke(cli, ["task", "update", "child", "--clear-dependencies"])
    assert result.exit_code == 0, result.output
    assert captured[-1]["depends_on_task_ids"] == []
    count = len(captured)
    result = runner.invoke(
        cli, ["task", "update", "child", "--depends-on", "c", "--clear-dependencies"]
    )
    assert result.exit_code != 0
    assert len(captured) == count


@pytest.mark.asyncio
async def test_continue_rechecks_after_async_session_preparation(manager, workspace, monkeypatch):
    prerequisite = create(manager, workspace, "prerequisite")
    manager.tasks[prerequisite.id] = prerequisite.model_copy(
        update={"status": WorkspaceTaskStatus.DONE}
    )
    dependent = create(manager, workspace, "dependent", depends_on_task_ids=[prerequisite.id])
    session = session_for(manager, workspace)
    manager.tasks[dependent.id] = dependent.model_copy(
        update={"status": WorkspaceTaskStatus.FAILED, "session_id": session.id}
    )

    async def rename_and_reopen(*args, **kwargs):
        manager.tasks[prerequisite.id] = prerequisite
        return session

    monkeypatch.setattr(manager, "_rename_session_for_task", rename_and_reopen)
    send = AsyncMock()
    monkeypatch.setattr(manager, "send_session_message", send)
    with pytest.raises(ValueError, match="dependencies"):
        await manager.continue_task(dependent.id)
    send.assert_not_called()
    assert manager.tasks[dependent.id].status == WorkspaceTaskStatus.FAILED


@pytest.mark.asyncio
async def test_start_preserves_dependency_edit_during_session_preparation(
    manager, workspace, monkeypatch
):
    prerequisite = create(manager, workspace, "prerequisite")
    dependent = create(manager, workspace, "dependent")
    session_for(manager, workspace)

    async def refresh_and_edit(*args, **kwargs):
        await manager.update_task(
            dependent.id, WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id])
        )

    monkeypatch.setattr(manager, "_refresh_session_statuses", refresh_and_edit)
    with pytest.raises(ValueError, match="dependencies"):
        await manager.start_task(dependent.id)
    assert manager.tasks[dependent.id].depends_on_task_ids == [prerequisite.id]
    assert manager.tasks[dependent.id].status == WorkspaceTaskStatus.TODO


def scheduled_task(workspace):
    now = _wm._now()
    return ScheduledTask(
        id="schedule",
        name="schedule",
        workspace_id=workspace.id,
        kind=ScheduledTaskKind.HUB_TASK,
        task_title="scheduled child",
        message="work",
        agent_type=AgentType.CLAUDE,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prerequisite_done", [False, True])
async def test_scheduled_fire_preserves_todo_edits_during_worker_creation(
    manager, workspace, monkeypatch, prerequisite_done
):
    prerequisite = create(manager, workspace, "prerequisite")
    if prerequisite_done:
        manager.tasks[prerequisite.id] = prerequisite.model_copy(
            update={"status": WorkspaceTaskStatus.DONE}
        )
    session = session_for(manager, workspace)

    async def ensure_and_edit(*args, **kwargs):
        task = next(t for t in manager.tasks.values() if t.internal_kind == "scheduled")
        await manager.update_task(
            task.id,
            WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id], prompt="new contract"),
        )
        return session

    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure_and_edit)
    monkeypatch.setattr(manager, "_rename_session_for_task", AsyncMock(return_value=session))
    monkeypatch.setattr(manager, "_lesson_context_payload", lambda *args: [])
    send = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(manager, "_send_dispatch_message", send)
    monkeypatch.setattr(manager, "_best_effort_delete_session", cleanup)
    if prerequisite_done:
        await manager._fire_hub_task(scheduled_task(workspace))
        send.assert_awaited_once()
        assert "new contract" in send.call_args.args[2]
        cleanup.assert_not_called()
    else:
        with pytest.raises(ValueError, match="dependencies"):
            await manager._fire_hub_task(scheduled_task(workspace))
        send.assert_not_called()
        cleanup.assert_awaited_once_with(session.id)
    task = next(t for t in manager.tasks.values() if t.internal_kind == "scheduled")
    assert task.depends_on_task_ids == [prerequisite.id]
    assert task.prompt == "new contract"
    assert task.status == (
        WorkspaceTaskStatus.WORKING if prerequisite_done else WorkspaceTaskStatus.FAILED
    )
    fresh = WorkspaceManager()
    assert fresh.tasks[task.id].depends_on_task_ids == [prerequisite.id]
    assert fresh.tasks[task.id].prompt == "new contract"


@pytest.mark.asyncio
@pytest.mark.parametrize("prerequisite_done", [False, True])
@pytest.mark.parametrize("edit_during", ["rename", "clear"])
async def test_final_dispatch_uses_fresh_contract_after_async_preparation(
    manager, workspace, monkeypatch, prerequisite_done, edit_during
):
    prerequisite = create(manager, workspace, "prerequisite")
    if prerequisite_done:
        manager.tasks[prerequisite.id] = prerequisite.model_copy(
            update={"status": WorkspaceTaskStatus.DONE}
        )
    dependent = create(manager, workspace, "dependent", clear_context=edit_during == "clear")
    session = session_for(manager, workspace)

    async def edit():
        await manager.update_task(
            dependent.id,
            WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id], prompt="new contract"),
        )

    async def rename(*args, **kwargs):
        if edit_during == "rename":
            await edit()
        return session

    async def clear(*args, **kwargs):
        assert args[1] == "/clear"
        await edit()

    monkeypatch.setattr(manager, "_rename_session_for_task", rename)
    monkeypatch.setattr(manager, "send_session_message", clear)
    monkeypatch.setattr(manager, "_lesson_context_payload", lambda *args: [])
    send = AsyncMock()
    monkeypatch.setattr(manager, "_send_dispatch_message", send)
    if prerequisite_done:
        await manager._dispatch_task_to_session(dependent, session)
        send.assert_awaited_once()
        assert "new contract" in send.call_args.args[2]
    else:
        with pytest.raises(ValueError, match="dependencies"):
            await manager._dispatch_task_to_session(dependent, session)
        send.assert_not_called()
    assert manager.tasks[dependent.id].depends_on_task_ids == [prerequisite.id]
    assert manager.tasks[dependent.id].prompt == "new contract"


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn_fails", [False, True])
async def test_orphan_recovery_preserves_operator_todo_edit_while_spawning(
    manager, workspace, monkeypatch, spawn_fails
):
    prerequisite = create(manager, workspace, "prerequisite")
    dependent = create(manager, workspace, "orphan")
    dead = session_for(manager, workspace).model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    manager.sessions[dead.id] = dead
    now = _wm._now()
    orphan = dependent.model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "system_internal": True,
            "internal_kind": "scheduled",
            "session_id": dead.id,
            "updated_at": now - timedelta(seconds=_wm.HUBTASK_ORPHAN_GRACE_SECONDS + 1),
        }
    )
    manager.tasks[orphan.id] = orphan
    replacement = dead.model_copy(update={"id": "replacement", "status": ManagedSessionStatus.IDLE})

    async def ensure_and_edit(*args, **kwargs):
        await manager.update_task(orphan.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.TODO))
        await manager.update_task(
            orphan.id,
            WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id], prompt="operator contract"),
        )
        if spawn_fails:
            raise RuntimeError("spawn failed after operator update")
        manager.sessions[replacement.id] = replacement
        return replacement

    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure_and_edit)
    send = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(manager, "_send_dispatch_message", send)
    monkeypatch.setattr(manager, "_best_effort_delete_session", cleanup)
    await manager._converge_one_orphaned_hub_task(orphan, now)
    current = manager.tasks[orphan.id]
    assert current.status == WorkspaceTaskStatus.TODO
    assert current.depends_on_task_ids == [prerequisite.id]
    assert current.prompt == "operator contract"
    send.assert_not_called()
    if spawn_fails:
        cleanup.assert_not_called()
    else:
        cleanup.assert_awaited_once_with(replacement.id)
    assert WorkspaceManager().tasks[orphan.id].depends_on_task_ids == [prerequisite.id]


@pytest.mark.asyncio
async def test_scheduled_failure_cleanup_survives_state_write_failure(
    manager, workspace, monkeypatch
):
    prerequisite = create(manager, workspace, "prerequisite")
    session = session_for(manager, workspace)

    async def ensure_and_edit(*args, **kwargs):
        task = next(t for t in manager.tasks.values() if t.internal_kind == "scheduled")
        await manager.update_task(
            task.id, WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id])
        )

        def fail_save():
            raise OSError("failure state not committed")

        monkeypatch.setattr(manager, "_save_state", fail_save)
        return session

    cleanup = AsyncMock()
    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure_and_edit)
    monkeypatch.setattr(manager, "_best_effort_delete_session", cleanup)
    with pytest.raises(OSError, match="not committed") as error:
        await manager._fire_hub_task(scheduled_task(workspace))
    assert isinstance(error.value.__context__, ValueError)
    cleanup.assert_awaited_once_with(session.id)
    task = next(t for t in manager.tasks.values() if t.internal_kind == "scheduled")
    assert task.depends_on_task_ids == [prerequisite.id]


@pytest.mark.asyncio
async def test_scheduled_failure_does_not_delete_worker_reassigned_to_another_task(
    manager, workspace, monkeypatch
):
    prerequisite = create(manager, workspace, "prerequisite")
    other = create(manager, workspace, "other owner")
    session = session_for(manager, workspace)

    async def ensure_and_edit(*args, **kwargs):
        task = next(t for t in manager.tasks.values() if t.internal_kind == "scheduled")
        await manager.update_task(
            task.id, WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id])
        )
        manager.tasks[other.id] = other.model_copy(
            update={"status": WorkspaceTaskStatus.QUEUED, "session_id": session.id}
        )
        manager.sessions[session.id] = session.model_copy(
            update={"task_id": other.id, "current_task_id": other.id}
        )
        return session

    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure_and_edit)
    with pytest.raises(ValueError, match="dependencies"):
        await manager._fire_hub_task(scheduled_task(workspace))
    # Exercise real best-effort deletion and its existing reference guard.
    assert manager.sessions[session.id].current_task_id == other.id
    assert manager.tasks[other.id].status == WorkspaceTaskStatus.QUEUED


@pytest.mark.asyncio
async def test_dispatch_does_not_clobber_dependency_edit_while_delivery_is_pending(
    manager, workspace, monkeypatch
):
    prerequisite = create(manager, workspace, "prerequisite")
    dependent = create(manager, workspace, "dependent")
    queued = dependent.model_copy(update={"status": WorkspaceTaskStatus.QUEUED})
    manager.tasks[queued.id] = queued
    session = session_for(manager, workspace)
    monkeypatch.setattr(manager, "_rename_session_for_task", AsyncMock(return_value=session))
    monkeypatch.setattr(manager, "_lesson_context_payload", lambda *args: [])

    async def send_and_reopen(*args, **kwargs):
        await manager.update_task(queued.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.TODO))
        await manager.update_task(
            queued.id, WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id])
        )

    monkeypatch.setattr(manager, "_send_dispatch_message", send_and_reopen)
    await manager._dispatch_task_to_session(queued, session)
    assert manager.tasks[queued.id].status == WorkspaceTaskStatus.TODO
    assert manager.tasks[queued.id].depends_on_task_ids == [prerequisite.id]


@pytest.mark.asyncio
async def test_continue_does_not_clobber_operator_reopen_during_rename(
    manager, workspace, monkeypatch
):
    prerequisite = create(manager, workspace, "prerequisite")
    dependent = create(manager, workspace, "dependent")
    session = session_for(manager, workspace)
    manager.tasks[dependent.id] = dependent.model_copy(
        update={"status": WorkspaceTaskStatus.FAILED, "session_id": session.id}
    )

    async def rename_and_reopen(*args, **kwargs):
        await manager.update_task(
            dependent.id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.TODO)
        )
        await manager.update_task(
            dependent.id, WorkspaceTaskUpdate(depends_on_task_ids=[prerequisite.id])
        )
        return session

    monkeypatch.setattr(manager, "_rename_session_for_task", rename_and_reopen)
    with pytest.raises(RuntimeError, match="Task changed"):
        await manager.continue_task(dependent.id)
    assert manager.tasks[dependent.id].status == WorkspaceTaskStatus.TODO
    assert manager.tasks[dependent.id].depends_on_task_ids == [prerequisite.id]
