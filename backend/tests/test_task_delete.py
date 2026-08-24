"""P1-1: Task delete fail-closed for non-leaf nodes and atomic leaf cleanup."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentType,
    ExecutionTarget,
    ReviewDecision,
    User,
    WorkspaceCreate,
    WorkspaceTaskCreate,
)
from claude_hub.models.agent_tree import AgentRun, AgentRunStatus, ExecutorKind
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.task_graph import TaskHasDescendantsError, make_task_consumer_key
from claude_hub.services.task_mailbox import compat_run_id_for_task
from claude_hub.services.workspace_manager import WorkspaceManager, workspace_manager

_wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture()
def state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Generator[Path, None, None]:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    yield root


@pytest.fixture()
def manager(state_root: Path) -> WorkspaceManager:
    return WorkspaceManager()


def _make_workspace(manager: WorkspaceManager, tmp_path: Path, name: str = "Delete WS") -> str:
    repo = tmp_path / name.replace(" ", "-")
    repo.mkdir(exist_ok=True)
    return manager.create_workspace(
        WorkspaceCreate(name=name, path=str(repo), target=ExecutionTarget.LOCAL)
    ).id


def _create_task(
    manager: WorkspaceManager,
    workspace_id: str,
    title: str,
    *,
    parent_task_id: str | None = None,
    agent_run_id: str | None = None,
):
    return manager.create_task(
        workspace_id,
        WorkspaceTaskCreate(
            title=title,
            prompt=f"do {title}",
            agent_type=AgentType.CLAUDE,
            parent_task_id=parent_task_id,
            agent_run_id=agent_run_id,
        ),
    )


def _append_task_event(manager: WorkspaceManager, task, *, call_id: str) -> None:
    manager.task_mailbox.append_event(
        workspace_id=task.workspace_id,
        task_id=task.id,
        actor_session_id=None,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.PROGRESS,
        action="progress",
        target=task.id,
        consumer_key=make_task_consumer_key(task.id),
        call_id=call_id,
        payload={"note": call_id},
        persist=True,
    )


def _make_report(*, workspace_id: str, task_id: str, report_id: str) -> AgentReport:
    return AgentReport(
        id=report_id,
        workspace_id=workspace_id,
        task_id=task_id,
        session_id="session-delete-test",
        state=AgentReportState.WORKING,
        message="report",
        message_en="report",
        message_zh="report",
        changed_files=[],
        validation=None,
        risks=None,
        review_decision=ReviewDecision.AUTO,
        review_reason=None,
        risk_level=None,
        review_cycle=1,
        created_at=_wm._now(),
    )


def test_delete_task_with_descendants_raises_and_leaves_state_unchanged(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    child = _create_task(manager, ws_id, "child", parent_task_id=parent.id)
    before_tasks = {key: value.model_dump() for key, value in manager.tasks.items()}
    before_reports = {key: value.model_dump() for key, value in manager.reports.items()}

    with pytest.raises(TaskHasDescendantsError):
        manager.delete_task(parent.id)

    assert child.id in manager.tasks
    assert parent.id in manager.tasks
    assert {key: value.model_dump() for key, value in manager.tasks.items()} == before_tasks
    assert {key: value.model_dump() for key, value in manager.reports.items()} == before_reports


def test_delete_leaf_task_removes_reports_events_call_index_and_compat_runs(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    sibling = _create_task(manager, ws_id, "sibling", parent_task_id=parent.id)
    leaf = _create_task(
        manager,
        ws_id,
        "leaf",
        parent_task_id=parent.id,
        agent_run_id="run-linked-leaf",
    )
    run_id = compat_run_id_for_task(leaf)
    manager.agent_tree._runs[run_id] = AgentRun(
        id=run_id,
        workspace_id=ws_id,
        parent_id=None,
        path=run_id,
        supervisor_id=None,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title=leaf.title,
        context_ref=leaf.id,
        status=AgentRunStatus.RUNNING,
    )
    manager.agent_tree._runs["run-linked-leaf"] = AgentRun(
        id="run-linked-leaf",
        workspace_id=ws_id,
        parent_id=None,
        path="run-linked-leaf",
        supervisor_id=None,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title=leaf.title,
        context_ref=leaf.id,
        status=AgentRunStatus.RUNNING,
    )
    _append_task_event(manager, leaf, call_id="leaf-progress-1")
    _append_task_event(manager, sibling, call_id="sibling-progress-1")
    report = _make_report(workspace_id=ws_id, task_id=leaf.id, report_id="report-leaf-1")
    manager.reports[report.id] = report
    manager._save_state()

    manager.delete_task(leaf.id)

    assert leaf.id not in manager.tasks
    assert report.id not in manager.reports
    assert run_id not in manager.agent_tree._runs
    assert "run-linked-leaf" not in manager.agent_tree._runs
    assert parent.id in manager.tasks
    assert sibling.id in manager.tasks
    assert "leaf-progress-1" not in manager.task_mailbox._call_index.get(ws_id, {})
    assert "sibling-progress-1" in manager.task_mailbox._call_index.get(ws_id, {})
    assert all(event.task_id != leaf.id for event in manager.task_mailbox._events.get(ws_id, []))


def test_delete_leaf_task_survives_cold_reload(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    leaf = _create_task(manager, ws_id, "leaf", parent_task_id=parent.id)
    _append_task_event(manager, leaf, call_id="leaf-cold-1")
    manager._save_state()

    manager.delete_task(leaf.id)

    fresh = WorkspaceManager()
    assert leaf.id not in fresh.tasks
    assert parent.id in fresh.tasks
    assert all(event.task_id != leaf.id for event in fresh.task_mailbox._events.get(ws_id, []))
    assert "leaf-cold-1" not in fresh.task_mailbox._call_index.get(ws_id, {})


def test_delete_leaf_task_save_failure_rolls_back(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    leaf = _create_task(manager, ws_id, "leaf")
    _append_task_event(manager, leaf, call_id="leaf-rollback-1")
    report = _make_report(workspace_id=ws_id, task_id=leaf.id, report_id="report-rollback-1")
    manager.reports[report.id] = report
    manager._save_state()
    before = manager._snapshot_report_intake_workspace(ws_id)

    original_save = manager._save_state
    calls = {"n": 0}

    def flaky_save() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("delete save failed")
        return original_save()

    monkeypatch.setattr(manager, "_save_state", flaky_save)

    with pytest.raises(OSError, match="delete save failed"):
        manager.delete_task(leaf.id)

    assert leaf.id in manager.tasks
    assert report.id in manager.reports
    assert "leaf-rollback-1" in manager.task_mailbox._call_index.get(ws_id, {})
    after = manager._snapshot_report_intake_workspace(ws_id)
    assert after["tasks"] == before["tasks"]
    assert after["reports"] == before["reports"]
    assert after["task_events"] == before["task_events"]
    assert after["task_call_index"] == before["task_call_index"]


def test_delete_leaf_task_does_not_affect_other_workspace(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_a = _make_workspace(manager, tmp_path, "WS A")
    ws_b = _make_workspace(manager, tmp_path, "WS B")
    leaf_a = _create_task(manager, ws_a, "leaf-a")
    keep_b = _create_task(manager, ws_b, "keep-b")
    _append_task_event(manager, keep_b, call_id="keep-b-event")
    manager._save_state()

    manager.delete_task(leaf_a.id)

    assert keep_b.id in manager.tasks
    assert "keep-b-event" in manager.task_mailbox._call_index.get(ws_b, {})


def test_delete_task_api_returns_409_for_parent_with_child(
    state_root: Path,
    tmp_path: Path,
) -> None:
    ws_id = _make_workspace(workspace_manager, tmp_path, "API WS")
    parent = _create_task(workspace_manager, ws_id, "parent")
    _create_task(workspace_manager, ws_id, "child", parent_task_id=parent.id)
    workspace_manager._save_state()

    async def fake_current_user() -> User:
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    app.dependency_overrides[get_current_user] = fake_current_user
    try:
        client = TestClient(app)
        response = client.delete(f"/api/workspaces/tasks/{parent.id}")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 409
    assert "child tasks" in response.json()["detail"].lower()
    assert parent.id in workspace_manager.tasks


@pytest.mark.asyncio
async def test_recover_pending_runs_skips_legacy_resident_root(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path, "Resident Skip")
    run_id = "legacy-resident-pending"
    manager.agent_tree._runs[run_id] = AgentRun(
        id=run_id,
        workspace_id=ws_id,
        parent_id=None,
        path=run_id,
        supervisor_id=None,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        title="legacy",
        context_ref="resident-session",
        status=AgentRunStatus.PENDING,
    )
    manager._save_state()

    await manager.agent_tree.recover_pending_runs(ws_id)

    assert manager.agent_tree._runs[run_id].status == AgentRunStatus.PENDING
    assert not manager.agent_tree._events.get(ws_id, [])
