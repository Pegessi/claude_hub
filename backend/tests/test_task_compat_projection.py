"""AC8/AC9: ordinary Task → AgentRun projection with zero AgentRun persistence."""

from __future__ import annotations

import json
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    WorkspaceCreate,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
)
from claude_hub.models.agent_tree import (
    AgentRun,
    AgentRunStatus,
    ExecutorKind,
    ListRunsRequest,
    WaitRequest,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.task_graph import (
    compat_path_for_task,
    compat_run_id_for_task,
    task_inbox_consumer_key,
)
from claude_hub.services.workspace_manager import WorkspaceManager

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


def _make_workspace(manager: WorkspaceManager, tmp_path: Path, name: str = "Compat WS") -> str:
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


def _set_status(manager: WorkspaceManager, task_id: str, status: WorkspaceTaskStatus) -> None:
    manager.tasks[task_id] = manager.tasks[task_id].model_copy(update={"status": status})


def _tree_blob(manager: WorkspaceManager, workspace_id: str) -> tuple[str, str, int, int]:
    runs = [
        run.model_dump(mode="json")
        for run_id, run in sorted(manager.agent_tree._runs.items())
        if run.workspace_id == workspace_id
    ]
    events = [
        event.model_dump(mode="json") for event in manager.agent_tree._events.get(workspace_id, [])
    ]
    return (
        json.dumps(runs, sort_keys=True, default=str),
        json.dumps(events, sort_keys=True, default=str),
        len(runs),
        len(events),
    )


def _seed_stored_run(
    manager: WorkspaceManager,
    *,
    run_id: str,
    workspace_id: str,
    executor_kind: ExecutorKind,
    parent_id: str | None = None,
    path: str | None = None,
    ack_sequence: int = 0,
    context_ref: str | None = None,
    status: AgentRunStatus = AgentRunStatus.PENDING,
    title: str = "stored",
) -> AgentRun:
    stamp = datetime(2026, 8, 23, 6, 0, 0)
    run = AgentRun(
        id=run_id,
        workspace_id=workspace_id,
        parent_id=parent_id,
        path=path or run_id,
        supervisor_id=parent_id,
        executor_kind=executor_kind,
        status=status,
        context_ref=context_ref,
        ack_sequence=ack_sequence,
        title=title,
        created_at=stamp,
        updated_at=stamp,
    )
    manager.agent_tree._runs[run.id] = run
    return run


def test_mixed_graph_projection_unions_tasks_once_and_filters(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    ordinary_root = _create_task(manager, ws_id, "ordinary-root")
    ordinary_child = _create_task(manager, ws_id, "ordinary-child", parent_task_id=ordinary_root.id)
    linked_parent = _create_task(manager, ws_id, "linked-parent", agent_run_id="run-linked")
    linked_child = _create_task(manager, ws_id, "linked-child", parent_task_id=linked_parent.id)
    other_root = _create_task(manager, ws_id, "other-root")
    _set_status(manager, ordinary_root.id, WorkspaceTaskStatus.WORKING)
    _set_status(manager, ordinary_child.id, WorkspaceTaskStatus.WORKING)
    _set_status(manager, linked_parent.id, WorkspaceTaskStatus.REVIEW)
    _set_status(manager, other_root.id, WorkspaceTaskStatus.DONE)

    stored_linked = _seed_stored_run(
        manager,
        run_id="run-linked",
        workspace_id=ws_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        parent_id="run-stale-parent",
        path="run-stale-parent/run-linked",
        ack_sequence=9,
        context_ref=None,
        status=AgentRunStatus.PENDING,
        title="historical-linked",
    )
    resident = _seed_stored_run(
        manager,
        run_id="run-resident",
        workspace_id=ws_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        status=AgentRunStatus.RUNNING,
        title="resident",
    )
    native = _seed_stored_run(
        manager,
        run_id="run-native",
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        status=AgentRunStatus.RUNNING,
        title="native",
    )
    stored_before = stored_linked.model_dump(mode="json")
    blob_before = _tree_blob(manager, ws_id)

    assert compat_run_id_for_task(ordinary_root) == ordinary_root.id
    assert compat_run_id_for_task(linked_parent) == "run-linked"
    assert compat_path_for_task(manager.tasks, linked_child) == f"run-linked/{linked_child.id}"

    root_run = manager.agent_tree.get_run(ordinary_root.id)
    child_run = manager.agent_tree.get_run(ordinary_child.id)
    assert root_run is not None
    assert child_run is not None
    assert root_run.id == ordinary_root.id
    assert root_run.parent_id is None
    assert root_run.path == ordinary_root.id
    assert root_run.context_ref == ordinary_root.id
    assert root_run.status == AgentRunStatus.RUNNING
    assert child_run.id == ordinary_child.id
    assert child_run.parent_id == ordinary_root.id
    assert child_run.path == f"{ordinary_root.id}/{ordinary_child.id}"
    assert child_run.supervisor_id == ordinary_root.id

    linked_run = manager.agent_tree.get_run("run-linked")
    linked_child_run = manager.agent_tree.get_run(linked_child.id)
    assert linked_run is not None
    assert linked_child_run is not None
    assert linked_run.id == "run-linked"
    assert linked_run.title == "historical-linked"
    assert linked_run.context_ref == linked_parent.id
    assert linked_run.status == AgentRunStatus.WAITING
    assert linked_run.parent_id is None
    assert linked_run.path == "run-linked"
    assert linked_child_run.id == linked_child.id
    assert linked_child_run.parent_id == "run-linked"
    assert linked_child_run.path == f"run-linked/{linked_child.id}"
    assert linked_child_run.path.startswith("run-linked/")

    by_context = manager.agent_tree.get_run_by_context_ref(ws_id, ordinary_root.id)
    linked_by_context = manager.agent_tree.get_run_by_context_ref(ws_id, linked_parent.id)
    assert by_context is not None and by_context.id == ordinary_root.id
    assert linked_by_context is not None and linked_by_context.id == "run-linked"

    listed = manager.agent_tree.list_runs(ListRunsRequest(workspace_id=ws_id))
    listed_ids = [item.id for item in listed]
    assert listed_ids.count(ordinary_root.id) == 1
    assert listed_ids.count(ordinary_child.id) == 1
    assert listed_ids.count("run-linked") == 1
    assert listed_ids.count(linked_child.id) == 1
    assert listed_ids.count(other_root.id) == 1
    assert listed_ids.count(resident.id) == 0
    assert resident.id not in listed_ids
    assert listed_ids.count(native.id) == 1
    assert linked_parent.id not in listed_ids
    assert set(listed_ids) == {
        ordinary_root.id,
        ordinary_child.id,
        "run-linked",
        linked_child.id,
        other_root.id,
        native.id,
    }

    completed = manager.agent_tree.list_runs(
        ListRunsRequest(workspace_id=ws_id, status=AgentRunStatus.COMPLETED)
    )
    assert {item.id for item in completed} == {other_root.id}

    ordinary_tree = manager.agent_tree.list_runs(
        ListRunsRequest(workspace_id=ws_id, root_id=ordinary_root.id)
    )
    assert {item.id for item in ordinary_tree} == {ordinary_root.id, ordinary_child.id}

    linked_tree = manager.agent_tree.list_runs(
        ListRunsRequest(workspace_id=ws_id, root_id="run-linked")
    )
    assert {item.id for item in linked_tree} == {"run-linked", linked_child.id}

    assert manager.agent_tree._runs["run-linked"].model_dump(mode="json") == stored_before
    assert ordinary_root.id not in manager.agent_tree._runs
    assert linked_child.id not in manager.agent_tree._runs
    assert _tree_blob(manager, ws_id) == blob_before


@pytest.mark.asyncio
async def test_ordinary_direct_subtree_events_and_cursor_survive_cold_load(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "mail-parent")
    child = _create_task(manager, ws_id, "mail-child", parent_task_id=parent.id)
    blob_before = _tree_blob(manager, ws_id)
    assert blob_before[2] == 0
    assert blob_before[3] == 0

    dispatched, created_dispatched = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=child.id,
        actor_role=TaskActorRole.SUPERVISOR,
        event_type=TaskEventType.DISPATCHED,
        call_id="ordinary-dispatched-1",
        action="spawn",
        consumer_key=task_inbox_consumer_key(child),
        payload={"message": "ORDINARY_DISPATCHED"},
    )
    report, created_report = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=child.id,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.REPORT,
        call_id="ordinary-report-1",
        action="report",
        payload={"message": "ORDINARY_REPORT"},
    )
    assert created_dispatched is True
    assert created_report is True

    child_direct = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    parent_direct = manager.agent_tree.get_events(ws_id, parent.id, subtree=False)
    parent_subtree = manager.agent_tree.get_events(ws_id, parent.id, subtree=True)
    assert [item.call_id for item in child_direct] == ["ordinary-dispatched-1"]
    assert [item.call_id for item in parent_direct] == ["ordinary-report-1"]
    assert [item.call_id for item in parent_subtree] == [
        "ordinary-dispatched-1",
        "ordinary-report-1",
    ]

    waited = await manager.agent_tree.wait(
        WaitRequest(
            workspace_id=ws_id,
            recipient_id=parent.id,
            since_sequence=0,
            subtree=False,
            timeout_seconds=0.2,
        )
    )
    assert [item.call_id for item in waited] == ["ordinary-report-1"]

    acked_parent = manager.agent_tree.ack(ws_id, parent.id, report.sequence)
    assert acked_parent.id == parent.id
    assert acked_parent.ack_sequence == report.sequence
    assert manager.tasks[parent.id].consumer_ack_sequence == report.sequence
    assert manager.agent_tree.get_events(ws_id, parent.id, subtree=False) == []
    assert [
        item.call_id for item in manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    ] == ["ordinary-dispatched-1"]

    acked_child = manager.agent_tree.ack(ws_id, child.id, dispatched.sequence)
    assert acked_child.ack_sequence == dispatched.sequence
    assert manager.tasks[child.id].consumer_ack_sequence == dispatched.sequence
    assert manager.agent_tree.get_events(ws_id, child.id, subtree=False) == []
    assert manager.agent_tree.get_run(parent.id) is not None
    assert manager.agent_tree.get_run(parent.id).ack_sequence == report.sequence

    assert _tree_blob(manager, ws_id) == blob_before
    assert parent.id not in manager.agent_tree._runs
    assert child.id not in manager.agent_tree._runs

    manager._save_state()
    fresh = WorkspaceManager()
    assert set(fresh.agent_tree._runs) == set(manager.agent_tree._runs)
    assert fresh.agent_tree._events.get(ws_id, []) == []
    assert fresh.tasks[parent.id].consumer_ack_sequence == report.sequence
    assert fresh.tasks[child.id].consumer_ack_sequence == dispatched.sequence
    reloaded_parent = fresh.agent_tree.get_run(parent.id)
    reloaded_child = fresh.agent_tree.get_run(child.id)
    assert reloaded_parent is not None
    assert reloaded_child is not None
    assert reloaded_parent.id == parent.id
    assert reloaded_child.id == child.id
    assert reloaded_parent.ack_sequence == report.sequence
    assert reloaded_child.ack_sequence == dispatched.sequence
    assert fresh.agent_tree.get_events(ws_id, parent.id, subtree=False) == []
    assert fresh.agent_tree.get_events(ws_id, child.id, subtree=False) == []
    assert parent.id not in fresh.agent_tree._runs
    assert child.id not in fresh.agent_tree._runs
    assert _tree_blob(fresh, ws_id) == blob_before


@pytest.mark.asyncio
async def test_compat_projection_rejects_cross_workspace_without_mailbox_read(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_a = _make_workspace(manager, tmp_path, "Compat A")
    ws_b = _make_workspace(manager, tmp_path, "Compat B")
    task = _create_task(manager, ws_a, "owned")
    reads = {"wait": 0, "ack": 0}
    real_wait = manager.task_mailbox.wait
    real_ack = manager.task_mailbox.ack

    def _count_wait(*args: Any, **kwargs: Any):
        reads["wait"] += 1
        return real_wait(*args, **kwargs)

    def _count_ack(*args: Any, **kwargs: Any):
        reads["ack"] += 1
        return real_ack(*args, **kwargs)

    manager.task_mailbox.wait = _count_wait  # type: ignore[method-assign]
    manager.task_mailbox.ack = _count_ack  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="different workspace"):
        manager.agent_tree.get_events(ws_b, task.id)
    with pytest.raises(ValueError, match="different workspace"):
        await manager.agent_tree.wait(
            WaitRequest(
                workspace_id=ws_b,
                recipient_id=task.id,
                since_sequence=0,
                subtree=False,
                timeout_seconds=0.1,
            )
        )
    with pytest.raises(ValueError, match="different workspace"):
        manager.agent_tree.ack(ws_b, task.id, 1)
    assert reads == {"wait": 0, "ack": 0}
    assert manager.agent_tree.get_run_by_context_ref(ws_b, task.id) is None
