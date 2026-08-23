"""Slice 2: Task mailbox append, wait/ack, cold-load index, legacy projection."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import AgentType, ExecutionTarget, WorkspaceCreate, WorkspaceTaskCreate
from claude_hub.models.agent_tree import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
    ExecutorKind,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.agent_tree import _request_fingerprint
from claude_hub.services.task_graph import make_task_consumer_key
from claude_hub.services.task_mailbox import compat_run_id_for_task
from claude_hub.services.task_migration import legacy_resident_consumer_key
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


def test_ordinary_task_append_wait_ack_survives_cold_load(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    parent = _create_task(manager, ws_id, "parent")
    child = _create_task(manager, ws_id, "child", parent_task_id=parent.id)
    consumer = make_task_consumer_key(parent.id)
    assert compat_run_id_for_task(child) == child.id

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
    assert event.actor_session_id == "sess-worker"
    assert event.actor_role == TaskActorRole.WORKER
    assert event.review_cycle == 2
    assert event.consumer_key == consumer
    assert event.compat_run_id == child.id

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
    assert loaded[0].task_id == child.id
    assert loaded[0].actor_role == TaskActorRole.WORKER
    assert loaded[0].review_cycle == 2
    assert loaded[0].compat_run_id == child.id
    assert fresh.tasks[parent.id].consumer_ack_sequence == 1
    assert fresh.task_mailbox.wait(ws_id, consumer) == []
    record = fresh.task_mailbox._call_record(ws_id, "report-1")
    assert record is not None
    assert record["fingerprint"] == loaded[0].fingerprint
    assert fresh.agent_tree._runs == {}


def test_legacy_agent_event_projects_without_mutating_agent_run(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    linked = _create_task(manager, ws_id, "linked", agent_run_id="run-child")
    created_at = datetime(2026, 1, 1)
    root = AgentRun(
        id="run-root",
        workspace_id=ws_id,
        parent_id=None,
        path="run-root",
        supervisor_id=None,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        status=AgentRunStatus.RUNNING,
        context_ref=None,
        ack_sequence=1,
    )
    child_run = AgentRun(
        id="run-child",
        workspace_id=ws_id,
        parent_id="run-root",
        path="run-root/run-child",
        supervisor_id="run-root",
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        context_ref=linked.id,
        ack_sequence=1,
    )
    legacy = AgentEvent(
        sequence=4,
        call_id="legacy-call",
        agent_run_id="run-child",
        type=AgentEventType.PROGRESS,
        author="run-child",
        recipient="run-root",
        action="emit",
        target="run-child",
        fingerprint="abc123",
        payload={"report_id": "old-report", "review_cycle": 3},
        created_at=created_at,
    )
    tree = manager.agent_tree
    tree._runs[root.id] = root
    tree._runs[child_run.id] = child_run
    tree._events[ws_id] = [legacy]
    tree._next_seq[ws_id] = 5
    tree._call_index[ws_id] = {
        "legacy-call": {
            "action": "emit",
            "target": "run-child",
            "fingerprint": "abc123",
            "event": legacy,
        }
    }
    manager._save_state()

    fresh = WorkspaceManager()
    loaded_run = fresh.agent_tree._runs[child_run.id]
    assert loaded_run.status == AgentRunStatus.RUNNING
    assert loaded_run.ack_sequence == 1
    assert loaded_run.context_ref == linked.id
    assert loaded_run.id == "run-child"

    projected = [
        item for item in fresh.task_mailbox._events[ws_id] if item.call_id == "legacy-call"
    ]
    assert len(projected) == 1
    event = projected[0]
    assert event.sequence == 4
    assert event.task_id == linked.id
    assert event.review_cycle == 3
    assert event.compat_run_id == "run-child"
    assert event.consumer_key == make_task_consumer_key(linked.id)
    assert event.report_id == "old-report"

    again = WorkspaceManager()
    again_events = [
        item for item in again.task_mailbox._events[ws_id] if item.call_id == "legacy-call"
    ]
    assert len(again_events) == 1
    assert again_events[0].sequence == 4
    assert again.agent_tree._runs[child_run.id].ack_sequence == 1
    assert again.agent_tree._runs[child_run.id].context_ref == linked.id

    task_key = make_task_consumer_key(linked.id)
    follow, created = fresh.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=linked.id,
        actor_role=TaskActorRole.REVIEWER,
        event_type=TaskEventType.REVIEW_PASSED,
        call_id="review-1",
        action="review",
        consumer_key=task_key,
        actor_session_id="sess-reviewer",
        review_cycle=3,
    )
    assert created is True
    assert follow.sequence == 5
    visible = fresh.task_mailbox.wait(ws_id, task_key, subtree=True)
    assert [item.call_id for item in visible] == ["legacy-call", "review-1"]
    fresh.task_mailbox.ack(ws_id, task_key, visible[-1].sequence)
    assert fresh.tasks[linked.id].consumer_ack_sequence == 5
    assert fresh.agent_tree._runs[child_run.id].ack_sequence == 1


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
    assert persisted.agent_tree._runs == {}


def _seed_agent_event(
    manager: WorkspaceManager,
    workspace_id: str,
    event: AgentEvent,
    *,
    run: AgentRun | None = None,
) -> None:
    tree = manager.agent_tree
    if run is not None:
        tree._runs[run.id] = run
    tree._events.setdefault(workspace_id, []).append(event)
    tree._events[workspace_id].sort(key=lambda item: item.sequence)
    tree._next_seq[workspace_id] = max(event.sequence + 1, tree._next_seq.get(workspace_id, 1))
    tree._call_index.setdefault(workspace_id, {})[event.call_id] = {
        "action": event.action or "emit",
        "target": event.target or event.agent_run_id,
        "fingerprint": event.fingerprint or "fp",
        "event": event,
    }


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


def test_unlinked_legacy_agent_event_is_not_projected_with_empty_task_id(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    orphan = AgentEvent(
        sequence=2,
        call_id="orphan-call",
        agent_run_id="run-orphan",
        type=AgentEventType.PROGRESS,
        author="run-orphan",
        recipient="run-root",
        action="emit",
        target="run-orphan",
        fingerprint="orphan-fp",
        payload={"note": "unlinked"},
        created_at=datetime(2026, 1, 1),
    )
    _seed_agent_event(
        manager,
        ws_id,
        orphan,
        run=AgentRun(
            id="run-orphan",
            workspace_id=ws_id,
            parent_id="run-root",
            path="run-root/run-orphan",
            supervisor_id="run-root",
            executor_kind=ExecutorKind.MANAGED_TASK,
            status=AgentRunStatus.RUNNING,
            context_ref=None,
            ack_sequence=2,
        ),
    )
    manager._save_state()

    fresh = WorkspaceManager()
    assert all(item.task_id for item in fresh.task_mailbox._events.get(ws_id, []))
    assert not any(
        item.call_id == "orphan-call" for item in fresh.task_mailbox._events.get(ws_id, [])
    )
    compat = next(item for item in fresh.agent_tree._events[ws_id] if item.call_id == "orphan-call")
    assert compat.sequence == 2
    assert fresh.agent_tree._runs["run-orphan"].ack_sequence == 2
    assert fresh.agent_tree._runs["run-orphan"].context_ref is None


def test_legacy_same_report_id_different_call_ids_dedupes(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    linked = _create_task(manager, ws_id, "linked", agent_run_id="run-child")
    first = AgentEvent(
        sequence=4,
        call_id="legacy-a",
        agent_run_id="run-child",
        type=AgentEventType.PROGRESS,
        author="run-child",
        recipient="run-root",
        action="emit",
        target="run-child",
        fingerprint="fp-a",
        payload={"report_id": "shared-report"},
        created_at=datetime(2026, 1, 1),
    )
    second = AgentEvent(
        sequence=5,
        call_id="legacy-b",
        agent_run_id="run-child",
        type=AgentEventType.PROGRESS,
        author="run-child",
        recipient="run-root",
        action="emit",
        target="run-child",
        fingerprint="fp-b",
        payload={"report_id": "shared-report"},
        created_at=datetime(2026, 1, 2),
    )
    run = AgentRun(
        id="run-child",
        workspace_id=ws_id,
        parent_id="run-root",
        path="run-root/run-child",
        supervisor_id="run-root",
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        context_ref=linked.id,
        ack_sequence=4,
    )
    _seed_agent_event(manager, ws_id, first, run=run)
    _seed_agent_event(manager, ws_id, second)
    manager._save_state()

    fresh = WorkspaceManager()
    projected = [
        item for item in fresh.task_mailbox._events[ws_id] if item.report_id == "shared-report"
    ]
    assert [item.call_id for item in projected] == ["legacy-a"]
    assert len(fresh.agent_tree._events[ws_id]) == 2
    assert fresh.agent_tree._runs["run-child"].ack_sequence == 4


def test_legacy_actor_fields_honor_payload_reviewer(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    linked = _create_task(manager, ws_id, "linked", agent_run_id="run-child")
    legacy = AgentEvent(
        sequence=3,
        call_id="review-legacy",
        agent_run_id="run-child",
        type=AgentEventType.PROGRESS,
        author="run-child",
        recipient="run-root",
        action="emit",
        target="run-child",
        fingerprint="fp-review",
        payload={
            "actor_role": "reviewer",
            "session_id": "sess-reviewer",
            "review_cycle": 4,
        },
        created_at=datetime(2026, 1, 1),
    )
    _seed_agent_event(
        manager,
        ws_id,
        legacy,
        run=AgentRun(
            id="run-child",
            workspace_id=ws_id,
            parent_id="run-root",
            path="run-root/run-child",
            supervisor_id="run-root",
            executor_kind=ExecutorKind.MANAGED_TASK,
            status=AgentRunStatus.RUNNING,
            context_ref=linked.id,
            ack_sequence=3,
        ),
    )
    manager._save_state()

    fresh = WorkspaceManager()
    event = next(
        item for item in fresh.task_mailbox._events[ws_id] if item.call_id == "review-legacy"
    )
    assert event.actor_role == TaskActorRole.REVIEWER
    assert event.actor_session_id == "sess-reviewer"
    assert event.review_cycle == 4
    assert fresh.agent_tree._runs["run-child"].status == AgentRunStatus.RUNNING


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


def test_append_does_not_duplicate_agent_tree_call_id(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    task = _create_task(manager, ws_id, "ordinary")
    consumer = make_task_consumer_key(task.id)
    body = {
        "task_id": task.id,
        "actor_session_id": "sess-worker",
        "actor_role": TaskActorRole.WORKER.value,
        "review_cycle": None,
        "event_type": TaskEventType.REPORT.value,
        "target": task.id,
        "consumer_key": consumer,
        "payload": {},
        "report_id": None,
    }
    fingerprint = _request_fingerprint("report", body)
    legacy = AgentEvent(
        sequence=7,
        call_id="shared-1",
        agent_run_id="run-ordinary",
        type=AgentEventType.PROGRESS,
        author="run-ordinary",
        recipient="run-root",
        action="report",
        target=task.id,
        fingerprint=fingerprint,
        payload={"task_id": task.id},
        created_at=datetime(2026, 1, 1),
    )
    _seed_agent_event(
        manager,
        ws_id,
        legacy,
        run=AgentRun(
            id="run-ordinary",
            workspace_id=ws_id,
            parent_id="run-root",
            path="run-root/run-ordinary",
            supervisor_id="run-root",
            executor_kind=ExecutorKind.MANAGED_TASK,
            status=AgentRunStatus.RUNNING,
            context_ref=task.id,
            ack_sequence=7,
        ),
    )
    reused, created = manager.task_mailbox.append_event(
        workspace_id=ws_id,
        task_id=task.id,
        actor_role=TaskActorRole.WORKER,
        event_type=TaskEventType.REPORT,
        call_id="shared-1",
        action="report",
        actor_session_id="sess-worker",
    )
    assert created is False
    assert reused.call_id == "shared-1"
    assert manager.task_mailbox._events.get(ws_id, []) == []
    assert manager.task_mailbox._next_seq.get(ws_id, 1) == 1
    assert manager.agent_tree._runs["run-ordinary"].ack_sequence == 7


def test_mailbox_rejects_legacy_resident_consumer_key(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    resident_key = legacy_resident_consumer_key(ws_id)
    with pytest.raises(ValueError, match="task:<task_id>"):
        manager.task_mailbox.consumer_cursor(ws_id, resident_key)
    with pytest.raises(ValueError, match="task:<task_id>"):
        manager.task_mailbox.wait(ws_id, resident_key)
    with pytest.raises(ValueError, match="task:<task_id>"):
        manager.task_mailbox.ack(ws_id, resident_key, 1)
