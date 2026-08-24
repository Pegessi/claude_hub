"""Slice 2: Task mailbox append, wait/ack, cold-load index."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import AgentType, ExecutionTarget, WorkspaceCreate, WorkspaceTaskCreate
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.task_graph import legacy_resident_consumer_key, make_task_consumer_key
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


def _make_workspace(manager: WorkspaceManager, tmp_path: Path, name: str = "Mail WS") -> str:
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
):
    return manager.create_task(
        workspace_id,
        WorkspaceTaskCreate(
            title=title,
            prompt=f"do {title}",
            agent_type=AgentType.CLAUDE,
            parent_task_id=parent_task_id,
        ),
    )


def test_ordinary_task_append_wait_ack_survives_cold_load(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    child = _create_task(manager, ws_id, "child", parent_task_id=parent.id)
    consumer = make_task_consumer_key(parent.id)

    event, created = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=child.id,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.REPORT,
        call_id="report-1",
        action="report",
        actor_session_id="sess-worker",
        review_cycle=2,
        payload={"message": "E2E_CHILD_REPORT"},
        report_id="rep-1",
    )
    assert created is True
    assert event.sequence == 1
    assert event.task_id == child.id
    assert event.consumer_key == consumer

    waited = manager.task_mailbox.wait(ws_id, consumer, since_sequence=0)
    assert [item.call_id for item in waited] == ["report-1"]
    manager.task_mailbox.ack(ws_id, consumer, event.sequence)
    assert manager.tasks[parent.id].consumer_ack_sequence == 1
    assert manager.task_mailbox.wait(ws_id, consumer, since_sequence=0) == []

    same, again = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=child.id,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.REPORT,
        call_id="report-1",
        action="report",
        actor_session_id="sess-worker",
        review_cycle=2,
        payload={"message": "E2E_CHILD_REPORT"},
        report_id="rep-1",
    )
    assert again is False
    assert same.sequence == 1

    with pytest.raises(ValueError, match="already used"):
        manager.task_mailbox.append_event(
            workspace_id=ws_id,
            task_id=child.id,
            actor_role=TaskActorRole.WORKER,
            event_type=TaskEventType.REPORT,
            call_id="report-1",
            action="report",
            actor_session_id="sess-other",
            review_cycle=2,
            payload={"message": "different"},
            report_id="rep-1",
        )

    fresh = WorkspaceManager()
    loaded = fresh.task_mailbox._events[ws_id]
    assert len(loaded) == 1
    assert loaded[0].sequence == 1
    assert fresh.tasks[parent.id].consumer_ack_sequence == 1
    assert fresh.task_mailbox.wait(ws_id, consumer) == []


def test_append_and_ack_roll_back_on_persist_failure(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, ws_id, "ordinary")
    consumer = make_task_consumer_key(task.id)
    original_save = manager._save_state
    calls = {"n": 0}

    def flaky_save() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("mailbox save failed")
        return original_save()

    monkeypatch.setattr(manager, "_save_state", flaky_save)
    with pytest.raises(OSError, match="mailbox save failed"):
        manager.task_mailbox.append_event(
            workspace_id=ws_id,
            task_id=task.id,
            actor_role=TaskActorRole.HUMAN,
            event_type=TaskEventType.FOLLOWUP,
            call_id="follow-1",
            action="followup",
            actor_session_id="sess-human",
        )
    assert manager.task_mailbox._events.get(ws_id, []) == []
    assert manager.task_mailbox._call_record(ws_id, "follow-1") is None

    loaded = WorkspaceManager()
    assert loaded.task_mailbox._events.get(ws_id, []) == []
    assert loaded.tasks[task.id].consumer_ack_sequence == 0

    event, created = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=task.id,
        actor_role=TaskActorRole.HUMAN,
        event_type=TaskEventType.FOLLOWUP,
        call_id="follow-1",
        action="followup",
        actor_session_id="sess-human",
    )
    assert created is True
    assert event.sequence == 1

    def flaky_ack_save() -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("ack save failed")
        return original_save()

    monkeypatch.setattr(manager, "_save_state", flaky_ack_save)
    with pytest.raises(OSError, match="ack save failed"):
        manager.task_mailbox.ack(ws_id, consumer, event.sequence)
    assert manager.tasks[task.id].consumer_ack_sequence == 0

    persisted = WorkspaceManager()
    assert persisted.tasks[task.id].consumer_ack_sequence == 0
    assert persisted.task_mailbox._events[ws_id][0].call_id == "follow-1"


def test_ack_rejects_sequence_ahead_of_workspace_max(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, ws_id, "ordinary")
    consumer = make_task_consumer_key(task.id)
    event, _ = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=task.id,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.PROGRESS,
        call_id="p1",
        action="emit",
    )
    with pytest.raises(ValueError, match="ahead of workspace max"):
        manager.task_mailbox.ack(ws_id, consumer, event.sequence + 5)
    assert manager.tasks[task.id].consumer_ack_sequence == 0


def test_wait_subtree_includes_grandchild_direct_stays_directed(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    root = _create_task(manager, ws_id, "root")
    child = _create_task(manager, ws_id, "child", parent_task_id=root.id)
    grand = _create_task(manager, ws_id, "grand", parent_task_id=child.id)
    event, _ = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=grand.id,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.REPORT,
        call_id="grand-report",
        action="report",
        actor_session_id="sess-worker",
    )
    assert event.consumer_key == make_task_consumer_key(child.id)
    root_key = make_task_consumer_key(root.id)
    child_key = make_task_consumer_key(child.id)
    assert manager.task_mailbox.wait(ws_id, root_key) == []
    assert [item.call_id for item in manager.task_mailbox.wait(ws_id, child_key)] == [
        "grand-report"
    ]
    subtree = manager.task_mailbox.wait(ws_id, root_key, subtree=True)
    assert [item.call_id for item in subtree] == ["grand-report"]
    assert subtree[0].task_id == grand.id


def test_cold_load_rejects_conflicting_duplicate_call_ids(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, ws_id, "ordinary")
    first, _ = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=task.id,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.PROGRESS,
        call_id="dup-1",
        action="emit",
    )
    conflict = first.model_copy(
        update={"sequence": first.sequence + 1, "action": "report", "fingerprint": "other"}
    )
    manager.task_mailbox._events[ws_id].append(conflict)
    with pytest.raises(ValueError, match="conflicting duplicate call_id"):
        manager.task_mailbox._rebuild_call_index(ws_id)


def test_mailbox_rejects_legacy_resident_consumer_key(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    legacy_key = legacy_resident_consumer_key(ws_id)
    with pytest.raises(ValueError, match="legacy resident consumer"):
        manager.task_mailbox.wait(ws_id, legacy_key)
