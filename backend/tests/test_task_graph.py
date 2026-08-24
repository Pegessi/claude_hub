"""Slice 1: Task Graph parent/root/path guards and Task-owned consumer keys."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    WorkspaceCreate,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
)
from claude_hub.services.task_graph import (
    make_task_consumer_key,
    parent_task_consumer_key,
    reparent_task,
    task_supervisor_consumer_key,
    validate_parent_task,
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


def _make_workspace(manager: WorkspaceManager, tmp_path: Path, name: str = "Graph WS") -> str:
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
    related_task_id: str | None = None,
):
    return manager.create_task(
        workspace_id,
        WorkspaceTaskCreate(
            title=title,
            prompt=f"do {title}",
            agent_type=AgentType.CLAUDE,
            parent_task_id=parent_task_id,
            related_task_id=related_task_id,
        ),
    )


def test_create_task_paths_use_real_ids(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    root = _create_task(manager, ws_id, "root")
    child = _create_task(manager, ws_id, "child", parent_task_id=root.id)
    assert root.path == root.id
    assert child.path == root.path + "/" + child.id
    assert all(part for part in child.path.split("/"))


def test_ordinary_task_is_root(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, ws_id, "ordinary")
    assert task.parent_task_id is None
    assert task.root_task_id == task.id
    assert task.path == task.id
    assert task.related_task_id is None
    assert task.consumer_ack_sequence == 0
    assert manager.list_top_level_tasks(ws_id) == [task]


def test_related_task_id_is_not_a_tree_edge(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    first = _create_task(manager, ws_id, "first")
    second = _create_task(manager, ws_id, "second", related_task_id=first.id)
    assert second.related_task_id == first.id
    assert second.parent_task_id is None
    assert second.root_task_id == second.id
    titles = {task.title for task in manager.list_top_level_tasks(ws_id)}
    assert titles == {"first", "second"}


def test_parent_child_and_subtree(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    child = _create_task(manager, ws_id, "child", parent_task_id=parent.id)
    grand = _create_task(manager, ws_id, "grand", parent_task_id=child.id)
    sibling = _create_task(manager, ws_id, "sibling", parent_task_id=parent.id)

    assert child.parent_task_id == parent.id
    assert child.root_task_id == parent.id
    assert child.path == parent.path + "/" + child.id
    assert child.path == f"{parent.id}/{child.id}"
    assert grand.path == f"{parent.id}/{child.id}/{grand.id}"
    assert sibling.path == f"{parent.id}/{sibling.id}"
    assert not sibling.path.startswith(child.path + "/")

    subtree = {task.id for task in manager.list_task_subtree(ws_id, parent.id)}
    assert subtree == {parent.id, child.id, grand.id, sibling.id}
    child_tree = {task.id for task in manager.list_task_subtree(ws_id, child.id)}
    assert child_tree == {child.id, grand.id}
    assert manager.list_top_level_tasks(ws_id) == [parent]


def test_cross_workspace_parent_rejected(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_a = _make_workspace(manager, tmp_path, "A")
    ws_b = _make_workspace(manager, tmp_path, "B")
    parent = _create_task(manager, ws_a, "parent")
    with pytest.raises(ValueError, match="different workspace"):
        _create_task(manager, ws_b, "child", parent_task_id=parent.id)


def test_missing_parent_rejected(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    with pytest.raises(KeyError):
        _create_task(manager, ws_id, "child", parent_task_id="does-not-exist")


def test_self_parent_rejected(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, ws_id, "solo")
    with pytest.raises(ValueError, match="its own parent"):
        validate_parent_task(manager.tasks, ws_id, task.id, task.id)


@pytest.mark.asyncio
async def test_cycle_rejected_on_reparent(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    child = _create_task(manager, ws_id, "child", parent_task_id=parent.id)
    with pytest.raises(ValueError, match="cycle"):
        await manager.update_task(parent.id, WorkspaceTaskUpdate(parent_task_id=child.id))
    assert manager.tasks[parent.id].parent_task_id is None
    assert manager.tasks[child.id].parent_task_id == parent.id


@pytest.mark.asyncio
async def test_reparent_rewrites_descendant_paths(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    a = _create_task(manager, ws_id, "a")
    b = _create_task(manager, ws_id, "b")
    child = _create_task(manager, ws_id, "child", parent_task_id=a.id)
    grand = _create_task(manager, ws_id, "grand", parent_task_id=child.id)
    updated = await manager.update_task(child.id, WorkspaceTaskUpdate(parent_task_id=b.id))
    assert updated.parent_task_id == b.id
    assert updated.root_task_id == b.id
    assert updated.path == f"{b.id}/{child.id}"
    assert manager.tasks[grand.id].path == f"{b.id}/{child.id}/{grand.id}"
    assert manager.tasks[grand.id].root_task_id == b.id


def test_reparent_task_staging_is_pure(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    a = _create_task(manager, ws_id, "a")
    b = _create_task(manager, ws_id, "b")
    child = _create_task(manager, ws_id, "child", parent_task_id=a.id)
    grand = _create_task(manager, ws_id, "grand", parent_task_id=child.id)
    before = {task_id: item.model_dump() for task_id, item in manager.tasks.items()}

    staged = reparent_task(manager.tasks, child, b.id)

    assert {task_id: item.model_dump() for task_id, item in manager.tasks.items()} == before
    assert staged[child.id] == {
        "parent_task_id": b.id,
        "root_task_id": b.id,
        "path": f"{b.id}/{child.id}",
    }
    assert staged[grand.id] == {
        "path": f"{b.id}/{child.id}/{grand.id}",
        "root_task_id": b.id,
    }
    assert a.id not in staged
    assert b.id not in staged


@pytest.mark.asyncio
async def test_reparent_persists_current_and_descendants_across_cold_load(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    a = _create_task(manager, ws_id, "a")
    b = _create_task(manager, ws_id, "b")
    child = _create_task(manager, ws_id, "child", parent_task_id=a.id)
    grand = _create_task(manager, ws_id, "grand", parent_task_id=child.id)
    great = _create_task(manager, ws_id, "great", parent_task_id=grand.id)

    updated = await manager.update_task(child.id, WorkspaceTaskUpdate(parent_task_id=b.id))
    assert updated.parent_task_id == b.id
    assert updated.root_task_id == b.id
    assert updated.path == f"{b.id}/{child.id}"
    assert manager.tasks[grand.id].parent_task_id == child.id
    assert manager.tasks[grand.id].root_task_id == b.id
    assert manager.tasks[grand.id].path == f"{b.id}/{child.id}/{grand.id}"
    assert manager.tasks[great.id].parent_task_id == grand.id
    assert manager.tasks[great.id].root_task_id == b.id
    assert manager.tasks[great.id].path == f"{b.id}/{child.id}/{grand.id}/{great.id}"

    fresh = WorkspaceManager()
    assert fresh.tasks[child.id].parent_task_id == b.id
    assert fresh.tasks[child.id].root_task_id == b.id
    assert fresh.tasks[child.id].path == f"{b.id}/{child.id}"
    assert fresh.tasks[grand.id].parent_task_id == child.id
    assert fresh.tasks[grand.id].root_task_id == b.id
    assert fresh.tasks[grand.id].path == f"{b.id}/{child.id}/{grand.id}"
    assert fresh.tasks[great.id].parent_task_id == grand.id
    assert fresh.tasks[great.id].root_task_id == b.id
    assert fresh.tasks[great.id].path == f"{b.id}/{child.id}/{grand.id}/{great.id}"

    again = WorkspaceManager()
    assert again.tasks[child.id].path == fresh.tasks[child.id].path
    assert again.tasks[grand.id].path == fresh.tasks[grand.id].path
    assert again.tasks[great.id].path == fresh.tasks[great.id].path


@pytest.mark.asyncio
async def test_reparent_save_failure_rolls_back_current_and_descendants(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    a = _create_task(manager, ws_id, "a")
    b = _create_task(manager, ws_id, "b")
    child = _create_task(manager, ws_id, "child", parent_task_id=a.id)
    grand = _create_task(manager, ws_id, "grand", parent_task_id=child.id)
    great = _create_task(manager, ws_id, "great", parent_task_id=grand.id)
    tracked_ids = (a.id, b.id, child.id, grand.id, great.id)
    pre = {task_id: manager.tasks[task_id].model_dump() for task_id in tracked_ids}

    calls = {"n": 0}
    original_save = manager._save_state

    def flaky_save() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("reparent save failed")
        return original_save()

    monkeypatch.setattr(manager, "_save_state", flaky_save)

    with pytest.raises(OSError, match="reparent save failed"):
        await manager.update_task(child.id, WorkspaceTaskUpdate(parent_task_id=b.id))

    for task_id, dump in pre.items():
        assert manager.tasks[task_id].model_dump() == dump

    loaded = WorkspaceManager()
    assert loaded.tasks[child.id].parent_task_id == a.id
    assert loaded.tasks[child.id].root_task_id == a.id
    assert loaded.tasks[child.id].path == f"{a.id}/{child.id}"
    assert loaded.tasks[grand.id].parent_task_id == child.id
    assert loaded.tasks[grand.id].root_task_id == a.id
    assert loaded.tasks[grand.id].path == f"{a.id}/{child.id}/{grand.id}"
    assert loaded.tasks[great.id].parent_task_id == grand.id
    assert loaded.tasks[great.id].root_task_id == a.id
    assert loaded.tasks[great.id].path == f"{a.id}/{child.id}/{grand.id}/{great.id}"

    retried = await manager.update_task(child.id, WorkspaceTaskUpdate(parent_task_id=b.id))
    assert retried.parent_task_id == b.id
    assert retried.path == f"{b.id}/{child.id}"
    assert manager.tasks[grand.id].path == f"{b.id}/{child.id}/{grand.id}"
    assert manager.tasks[great.id].path == f"{b.id}/{child.id}/{grand.id}/{great.id}"

    persisted = WorkspaceManager()
    assert persisted.tasks[child.id].parent_task_id == b.id
    assert persisted.tasks[child.id].path == f"{b.id}/{child.id}"
    assert persisted.tasks[grand.id].root_task_id == b.id
    assert persisted.tasks[grand.id].path == f"{b.id}/{child.id}/{grand.id}"
    assert persisted.tasks[great.id].root_task_id == b.id
    assert persisted.tasks[great.id].path == f"{b.id}/{child.id}/{grand.id}/{great.id}"


def test_root_task_supervisor_consumer_key_is_self(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    root = _create_task(manager, ws_id, "root")
    assert task_supervisor_consumer_key(root) == make_task_consumer_key(root.id)
    child = _create_task(manager, ws_id, "child", parent_task_id=root.id)
    assert task_supervisor_consumer_key(child) == make_task_consumer_key(root.id)


def test_parent_consumer_key_is_task_prefixed(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    key = parent_task_consumer_key(parent)
    assert key == f"task:{parent.id}"


def test_normalize_old_task_and_workspace_are_roots(manager: WorkspaceManager) -> None:
    old_task = {
        "id": "task-old",
        "workspace_id": "ws-old",
        "title": "legacy",
        "prompt": "go",
        "agent_type": "claude",
        "status": "todo",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    normalized = manager._normalize_task_item(old_task)
    assert normalized["parent_task_id"] is None
    assert normalized["root_task_id"] == "task-old"
    assert normalized["path"] == "task-old"
    assert normalized["consumer_ack_sequence"] == 0

    old_ws = {
        "id": "ws-old",
        "name": "n",
        "path": "/tmp",
        "default_branch": "main",
        "session_prefix": "n",
    }
    ws_norm = manager._normalize_workspace_item(old_ws)
    assert "resident_consumer_key" not in ws_norm
    assert "resident_ack_sequence" not in ws_norm


def test_tree_and_consumer_keys_survive_cold_load(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    child = _create_task(manager, ws_id, "child", parent_task_id=parent.id)
    parent_key = make_task_consumer_key(parent.id)
    manager.tasks[parent.id] = parent.model_copy(update={"consumer_ack_sequence": 3})
    manager._save_state()

    fresh = WorkspaceManager()
    loaded_parent = fresh.tasks[parent.id]
    loaded_child = fresh.tasks[child.id]
    loaded_ws = fresh.workspaces[ws_id]
    assert loaded_child.parent_task_id == parent.id
    assert loaded_child.root_task_id == parent.id
    assert loaded_child.path == f"{parent.id}/{child.id}"
    assert loaded_parent.consumer_ack_sequence == 3

    again = WorkspaceManager()
    assert again.tasks[parent.id].consumer_ack_sequence == 3
    assert again.task_mailbox.consumer_cursor(ws_id, parent_key) == 3
    assert again.tasks[child.id].path == loaded_child.path
