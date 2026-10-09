"""Slice 2: Task mailbox append, wait/ack, cold-load index."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from typing import Any, Generator, cast

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
    original_save = manager._save_workspace_state
    calls = {"n": 0}

    def flaky_save(target_workspace_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("mailbox save failed")
        return original_save(target_workspace_id)

    monkeypatch.setattr(manager, "_save_workspace_state", flaky_save)
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

    def flaky_ack_save(target_workspace_id: str) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("ack save failed")
        return original_save(target_workspace_id)

    monkeypatch.setattr(manager, "_save_workspace_state", flaky_ack_save)
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


@pytest.mark.parametrize("present", [False, True])
def test_mailbox_snapshot_preserves_missing_and_empty_caches(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch, present: bool
) -> None:
    workspace_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, workspace_id, "snapshot")
    mailbox = manager.task_mailbox
    if present:
        mailbox._events[workspace_id] = []
        mailbox._call_index[workspace_id] = {}
        mailbox._next_seq[workspace_id] = 17
    else:
        mailbox._events.pop(workspace_id, None)
        mailbox._call_index.pop(workspace_id, None)
        mailbox._next_seq.pop(workspace_id, None)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("snapshot/restore must not load, rebuild, persist, or wake")

    for name in ("load_from_dict", "to_dict", "_rebuild_call_index", "_persist"):
        monkeypatch.setattr(mailbox, name, forbidden)
    monkeypatch.setattr(manager, "_save_workspace_state", forbidden)
    monkeypatch.setattr(mailbox._waiters, "wake", forbidden)
    monkeypatch.setattr(mailbox._waiters, "wake_if_subtree", forbidden)
    snapshot = mailbox.snapshot_workspace(workspace_id)
    mailbox.append_event(
        workspace_id=workspace_id,
        task_id=task.id,
        actor_role=TaskActorRole.HUMAN,
        event_type=TaskEventType.FOLLOWUP,
        call_id="staged-after-snapshot",
        action="followup",
        persist=False,
        wake=False,
    )
    mailbox.restore_workspace(workspace_id, snapshot)
    assert (workspace_id in mailbox._events) is present
    assert (workspace_id in mailbox._call_index) is present
    assert (workspace_id in mailbox._next_seq) is present
    assert mailbox._events.get(workspace_id, []) == []
    assert mailbox._call_index.get(workspace_id, {}) == {}
    assert mailbox._next_seq.get(workspace_id) == (17 if present else None)


def test_mailbox_snapshot_restores_internal_aliases_without_touching_other_workspace(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace_a = _make_workspace(manager, tmp_path, "Snapshot A")
    workspace_b = _make_workspace(manager, tmp_path, "Snapshot B")
    task_a = _create_task(manager, workspace_a, "a")
    task_b = _create_task(manager, workspace_b, "b")
    mailbox = manager.task_mailbox
    event, _ = mailbox.append_event(
        workspace_id=workspace_a,
        task_id=task_a.id,
        actor_role=TaskActorRole.HUMAN,
        event_type=TaskEventType.FOLLOWUP,
        call_id="source-event",
        action="followup",
        payload={"nested": {"values": ["before"]}},
        persist=False,
        wake=False,
    )
    alias = dict(mailbox._call_index[workspace_a][event.call_id])
    alias.update(action="cached-action", target="cached-target", fingerprint="cached-fingerprint")
    mailbox._call_index[workspace_a]["cached-alias"] = alias
    snapshot = mailbox.snapshot_workspace(workspace_a)
    assert snapshot.events is not None and snapshot.call_index is not None
    assert snapshot.call_index["cached-alias"]["event"] is snapshot.events[0]
    event.payload["nested"]["values"].append("changed-after-snapshot")
    mailbox._call_index[workspace_a].clear()
    mailbox._next_seq[workspace_a] = 99
    other, _ = mailbox.append_event(
        workspace_id=workspace_b,
        task_id=task_b.id,
        actor_role=TaskActorRole.HUMAN,
        event_type=TaskEventType.FOLLOWUP,
        call_id="other-workspace-advance",
        action="followup",
        persist=False,
        wake=False,
    )
    other_snapshot = mailbox.snapshot_workspace(workspace_b)
    mailbox.restore_workspace(workspace_a, snapshot)
    restored = mailbox._events[workspace_a][0]
    restored_alias = mailbox._call_index[workspace_a]["cached-alias"]
    assert restored is not event and restored is not snapshot.events[0]
    assert restored is mailbox._call_index[workspace_a][event.call_id]["event"]
    assert restored_alias["event"] is restored
    assert restored_alias["action"] == "cached-action"
    assert restored_alias["target"] == "cached-target"
    assert restored_alias["fingerprint"] == "cached-fingerprint"
    assert restored.payload["nested"]["values"] == ["before"]
    assert mailbox._next_seq[workspace_a] == snapshot.next_sequence
    assert mailbox.snapshot_workspace(workspace_b) == other_snapshot
    assert mailbox._events[workspace_b][0] is other
    restored.payload["nested"]["values"].append("changed-after-restore")
    mailbox.restore_workspace(workspace_a, snapshot)
    assert mailbox._events[workspace_a][0].payload["nested"]["values"] == ["before"]
    assert snapshot.events[0].payload["nested"]["values"] == ["before"]
    with pytest.raises(ValueError, match="another workspace"):
        mailbox.restore_workspace(workspace_b, snapshot)
    assert mailbox.snapshot_workspace(workspace_b) == other_snapshot
    assert mailbox._events[workspace_b][0] is other


def test_lookup_call_preserves_cached_fields_and_is_only_shallow_read_only(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, workspace_id, "lookup")
    mailbox = manager.task_mailbox
    event, _ = mailbox.append_event(
        workspace_id=workspace_id,
        task_id=task.id,
        actor_role=TaskActorRole.HUMAN,
        event_type=TaskEventType.FOLLOWUP,
        call_id="lookup-call",
        action="followup",
        payload={"values": []},
        persist=False,
        wake=False,
    )
    record = mailbox._call_index[workspace_id][event.call_id]
    record.update(target="index-target", action="index-action", fingerprint="index-fingerprint")
    view = mailbox.lookup_call(workspace_id, event.call_id)
    assert view is not None
    assert (view["target"], view["action"], view["fingerprint"]) == (
        "index-target",
        "index-action",
        "index-fingerprint",
    )
    assert view["event"] is event
    with pytest.raises(TypeError):
        cast(Any, view)["target"] = "must-not-change"
    assert record["target"] == "index-target"
    view["event"].payload["values"].append("nested-values-are-not-frozen")
    assert event.payload["values"] == ["nested-values-are-not-frozen"]
    assert mailbox.lookup_call(workspace_id, "missing") is None


@pytest.mark.parametrize("operation", ["append", "replay", "ack"])
def test_mailbox_commit_uses_explicit_workspace_and_leaves_other_files_unchanged(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch, operation: str
) -> None:
    workspace_a = _make_workspace(manager, tmp_path, "Commit A")
    workspace_b = _make_workspace(manager, tmp_path, "Commit B")
    task_a = _create_task(manager, workspace_a, "a")
    task_b = _create_task(manager, workspace_b, "b")
    mailbox = manager.task_mailbox
    arguments: dict[str, Any] = {
        "workspace_id": workspace_a,
        "task_id": task_a.id,
        "actor_role": TaskActorRole.HUMAN,
        "event_type": TaskEventType.FOLLOWUP,
        "call_id": "existing-call",
        "action": "followup",
        "wake": False,
    }
    event, _ = mailbox.append_event(**arguments)
    state_a = manager._workspace_state_file(workspace_a)
    state_b = manager._workspace_state_file(workspace_b)
    snapshot_a = manager.snapshot_path(workspace_a)
    snapshot_b = manager.snapshot_path(workspace_b)
    untouched = {path: path.read_bytes() for path in (_wm.INDEX_FILE, state_b, snapshot_b)}
    manager.tasks[task_b.id] = task_b.model_copy(update={"title": "uncommitted other task"})
    manager.workspaces[workspace_b] = manager.workspaces[workspace_b].model_copy(
        update={"name": "uncommitted other workspace"}
    )
    original_save = manager._save_workspace_state
    original_write = manager._atomic_write_text
    saves: list[str] = []
    writes: list[Path] = []

    def save_one(workspace_id: str) -> None:
        saves.append(workspace_id)
        original_save(workspace_id)

    def record_write(path: Path, text: str) -> None:
        writes.append(path)
        original_write(path, text)

    def reject_bulk() -> None:
        pytest.fail("Mailbox commit must not use the full-state entry")

    monkeypatch.setattr(manager, "_save_workspace_state", save_one)
    monkeypatch.setattr(manager, "_atomic_write_text", record_write)
    monkeypatch.setattr(manager, "_save_state", reject_bulk)
    if operation == "append":
        _new, created = mailbox.append_event(**{**arguments, "call_id": "new-call"})
        assert created is True
        assert len(mailbox._events[workspace_a]) == 2
    elif operation == "replay":
        replay, created = mailbox.append_event(**arguments)
        assert replay is event and created is False
        assert len(mailbox._events[workspace_a]) == 1
    else:
        mailbox.ack(workspace_a, make_task_consumer_key(task_a.id), event.sequence)
        assert manager.tasks[task_a.id].consumer_ack_sequence == event.sequence
    assert saves == [workspace_a]
    assert writes == [state_a, snapshot_a]
    for path, before in untouched.items():
        assert path.read_bytes() == before
    assert manager.tasks[task_b.id].title == "uncommitted other task"
    saved = json.loads(state_a.read_text())
    assert len(saved["task_events"]) == (2 if operation == "append" else 1)
    if operation == "ack":
        assert (
            next(item for item in saved["tasks"] if item["id"] == task_a.id)[
                "consumer_ack_sequence"
            ]
            == event.sequence
        )


def test_full_state_entry_still_writes_index_and_each_workspace(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace_a = _make_workspace(manager, tmp_path, "Full A")
    workspace_b = _make_workspace(manager, tmp_path, "Full B")
    task_a = _create_task(manager, workspace_a, "a")
    task_b = _create_task(manager, workspace_b, "b")
    manager.workspaces[workspace_a] = manager.workspaces[workspace_a].model_copy(
        update={"name": "saved workspace a"}
    )
    manager.workspaces[workspace_b] = manager.workspaces[workspace_b].model_copy(
        update={"name": "saved workspace b"}
    )
    manager.tasks[task_a.id] = task_a.model_copy(update={"title": "saved task a"})
    manager.tasks[task_b.id] = task_b.model_copy(update={"title": "saved task b"})
    writes: list[Path] = []
    original_write = manager._atomic_write_text

    def record_write(path: Path, text: str) -> None:
        writes.append(path)
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", record_write)
    assert not hasattr(manager, "_report_intake_workspace")
    manager._save_state()
    assert writes == [
        _wm.INDEX_FILE,
        manager._workspace_state_file(workspace_a),
        manager.snapshot_path(workspace_a),
        manager._workspace_state_file(workspace_b),
        manager.snapshot_path(workspace_b),
    ]
    index = json.loads(_wm.INDEX_FILE.read_text())
    names = {item["id"]: item["name"] for item in index["workspaces"]}
    assert names[workspace_a] == "saved workspace a"
    assert names[workspace_b] == "saved workspace b"
    for workspace_id, task_id, title in (
        (workspace_a, task_a.id, "saved task a"),
        (workspace_b, task_b.id, "saved task b"),
    ):
        payload = json.loads(manager._workspace_state_file(workspace_id).read_text())
        assert next(item for item in payload["tasks"] if item["id"] == task_id)["title"] == title


def test_report_wakes_preserve_task_consumer_filter(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(manager.task_mailbox._waiters, "wake", seen.append)
    manager._wake_report_intake_runs({("task:consumer", "author"), ("legacy-consumer", "author")})
    assert seen == ["task:consumer"]
