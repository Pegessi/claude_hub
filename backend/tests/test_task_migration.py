"""AC8/AC10: pre-unification state.json load order and deterministic migration."""

from __future__ import annotations

import json
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceCreate,
    WorkspaceSessionRole,
)
from claude_hub.models.agent_tree import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
    ExecutorCapabilities,
    ExecutorKind,
    ManagedExecutorConfig,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from claude_hub.services.task_graph import make_task_consumer_key
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


def _make_workspace(manager: WorkspaceManager, tmp_path: Path, name: str = "Migrate WS") -> str:
    repo = tmp_path / name.replace(" ", "-")
    repo.mkdir(exist_ok=True)
    return manager.create_workspace(
        WorkspaceCreate(name=name, path=str(repo), target=ExecutionTarget.LOCAL)
    ).id


def _raw_task(
    *,
    task_id: str,
    workspace_id: str,
    title: str,
    agent_run_id: str | None = None,
    parent_task_id: Any = ...,
    pending_call_ids: list[str] | None = None,
    consumer_ack_sequence: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": task_id,
        "workspace_id": workspace_id,
        "title": title,
        "prompt": f"do {title}",
        "agent_type": "claude",
        "status": "todo",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "pending_call_ids": pending_call_ids or [],
        "processing_call_ids": ["proc-1"] if title == "linked-child" else [],
        "uncertain_call_ids": ["unc-1"] if title == "linked-child" else [],
    }
    if agent_run_id is not None:
        item["agent_run_id"] = agent_run_id
    if parent_task_id is not ...:
        item["parent_task_id"] = parent_task_id
    if consumer_ack_sequence is not None:
        item["consumer_ack_sequence"] = consumer_ack_sequence
    return item


def _write_state(manager: WorkspaceManager, workspace_id: str, payload: dict[str, Any]) -> None:
    path = manager._workspace_state_file(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _omit_index_resident_ack(manager: WorkspaceManager, workspace_id: str) -> None:
    index = json.loads(_wm.INDEX_FILE.read_text(encoding="utf-8"))
    for item in index.get("workspaces", []):
        if isinstance(item, dict) and item.get("id") == workspace_id:
            item.pop("resident_ack_sequence", None)
            break
    _wm.INDEX_FILE.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _managed_contract() -> dict[str, Any]:
    return {
        "executor_capabilities": ExecutorCapabilities(available=True),
        "executor_config": ManagedExecutorConfig(agent_type=AgentType.CLAUDE),
    }


def _resident_contract() -> dict[str, Any]:
    return {"executor_capabilities": ExecutorCapabilities(available=True)}


_RUN_IDENTITY_KEYS = (
    "id",
    "parent_id",
    "path",
    "status",
    "context_ref",
    "ack_sequence",
    "executor_kind",
    "supervisor_id",
)
_EVENT_IDENTITY_KEYS = (
    "sequence",
    "call_id",
    "type",
    "author",
    "recipient",
    "payload",
    "action",
    "target",
    "fingerprint",
    "agent_run_id",
)


def _identity_blob(items: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    picked = [{key: item.get(key) for key in keys} for item in items]
    return json.dumps(picked, sort_keys=True, default=str)


def _tree_blobs(manager: WorkspaceManager, workspace_id: str) -> tuple[str, str]:
    runs = [
        run.model_dump(mode="json")
        for run_id, run in sorted(manager.agent_tree._runs.items())
        if run.workspace_id == workspace_id
    ]
    events = [
        event.model_dump(mode="json") for event in manager.agent_tree._events.get(workspace_id, [])
    ]
    return _identity_blob(runs, _RUN_IDENTITY_KEYS), _identity_blob(events, _EVENT_IDENTITY_KEYS)


def test_pre_unification_state_json_migrates_parent_ack_and_events(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 1, 0, 0, 0)
    resident = AgentRun(
        id="run-resident",
        workspace_id=ws_id,
        parent_id=None,
        path="run-resident",
        supervisor_id=None,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        status=AgentRunStatus.RUNNING,
        ack_sequence=11,
        created_at=stamp,
        updated_at=stamp,
        **_resident_contract(),
    )
    parent_run = AgentRun(
        id="run-parent",
        workspace_id=ws_id,
        parent_id="run-resident",
        path="run-resident/run-parent",
        supervisor_id="run-resident",
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        context_ref="task-linked-parent",
        ack_sequence=4,
        created_at=stamp,
        updated_at=stamp,
        **_managed_contract(),
    )
    child_run = AgentRun(
        id="run-child",
        workspace_id=ws_id,
        parent_id="run-parent",
        path="run-resident/run-parent/run-child",
        supervisor_id="run-parent",
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        context_ref="task-linked-child",
        ack_sequence=7,
        created_at=stamp,
        updated_at=stamp,
        **_managed_contract(),
    )
    legacy = AgentEvent(
        sequence=4,
        call_id="legacy-started",
        agent_run_id="run-child",
        type=AgentEventType.STARTED,
        author="run-child",
        recipient="run-resident",
        action="spawn:started",
        target="run-child",
        fingerprint="legacy-started-fp",
        payload={"task_id": "task-linked-child"},
        created_at=stamp,
    )
    bridged = AgentEvent(
        sequence=5,
        call_id="report:rep-bridged",
        agent_run_id="run-child",
        type=AgentEventType.PROGRESS,
        author="run-child",
        recipient="run-parent",
        action="report",
        target="run-child",
        fingerprint="bridged-fp",
        payload={"report_id": "rep-bridged", "task_id": "task-linked-child"},
        created_at=stamp,
    )
    session = ManagedSession(
        id="sess-worker",
        workspace_id=ws_id,
        tab_id="tab-worker",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        title="worker",
        workspace_path=str(tmp_path),
        tmux_session="tmux-worker",
        created_at=stamp,
        updated_at=stamp,
    )
    missing_session_report = AgentReport(
        id="rep-ordinary",
        workspace_id=ws_id,
        task_id="task-ordinary",
        session_id="sess-gone",
        state=AgentReportState.READY_FOR_REVIEW,
        message="ordinary report",
        review_cycle=2,
        created_at=datetime(2026, 1, 1, 1, 0, 0),
    )
    bridged_report = AgentReport(
        id="rep-bridged",
        workspace_id=ws_id,
        task_id="task-linked-child",
        session_id="sess-worker",
        state=AgentReportState.WORKING,
        message="already bridged",
        review_cycle=1,
        created_at=datetime(2026, 1, 1, 0, 30, 0),
    )
    payload = {
        "tasks": [
            _raw_task(
                task_id="task-linked-parent",
                workspace_id=ws_id,
                title="linked-parent",
                agent_run_id="run-parent",
            ),
            _raw_task(
                task_id="task-linked-child",
                workspace_id=ws_id,
                title="linked-child",
                agent_run_id="run-child",
            ),
            _raw_task(task_id="task-ordinary", workspace_id=ws_id, title="ordinary-root"),
        ],
        "sessions": [session.model_dump(mode="json")],
        "reports": [
            bridged_report.model_dump(mode="json"),
            missing_session_report.model_dump(mode="json"),
        ],
        "agent_runs": [
            resident.model_dump(mode="json"),
            parent_run.model_dump(mode="json"),
            child_run.model_dump(mode="json"),
        ],
        "agent_events": [legacy.model_dump(mode="json"), bridged.model_dump(mode="json")],
    }
    _write_state(manager, ws_id, payload)
    _omit_index_resident_ack(manager, ws_id)
    raw_runs = _identity_blob(
        sorted(payload["agent_runs"], key=lambda item: item["id"]), _RUN_IDENTITY_KEYS
    )
    raw_events = _identity_blob(payload["agent_events"], _EVENT_IDENTITY_KEYS)

    fresh = WorkspaceManager()
    parent = fresh.tasks["task-linked-parent"]
    child = fresh.tasks["task-linked-child"]
    ordinary = fresh.tasks["task-ordinary"]
    assert parent.parent_task_id is None
    assert parent.path == parent.id
    assert child.parent_task_id == parent.id
    assert child.root_task_id == parent.id
    assert child.path == f"{parent.id}/{child.id}"
    assert ordinary.parent_task_id is None
    assert ordinary.path == ordinary.id
    assert parent.consumer_ack_sequence == 11
    assert child.consumer_ack_sequence == 7
    assert child.pending_call_ids == []
    assert child.processing_call_ids == ["proc-1"]
    assert child.uncertain_call_ids == ["unc-1"]

    mailbox = list(fresh.task_mailbox._events[ws_id])
    assert [item.call_id for item in mailbox] == [
        "legacy-started",
        "report:rep-bridged",
        "report:rep-ordinary",
    ]
    started = mailbox[0]
    assert started.sequence == 4
    assert started.consumer_key == make_task_consumer_key(parent.id)
    assert started.compat_run_id == "run-child"
    assert started.fingerprint == "legacy-started-fp"
    bridged_event = mailbox[1]
    assert bridged_event.sequence == 5
    assert bridged_event.consumer_key == make_task_consumer_key(parent.id)
    assert bridged_event.report_id == "rep-bridged"
    backfilled = mailbox[2]
    assert backfilled.sequence == 6
    assert backfilled.action == "report"
    assert backfilled.target == ordinary.id
    assert backfilled.type == TaskEventType.REPORT
    assert backfilled.actor_session_id == "sess-gone"
    assert backfilled.actor_role == TaskActorRole.WORKER
    assert backfilled.review_cycle == 2
    assert backfilled.consumer_key == make_task_consumer_key(ordinary.id)
    assert backfilled.report_id == "rep-ordinary"
    assert fresh.task_mailbox._next_seq[ws_id] == 7
    disk = json.loads(fresh._workspace_state_file(ws_id).read_text(encoding="utf-8"))
    assert disk["agent_runs"] == payload["agent_runs"]
    assert disk["agent_events"] == payload["agent_events"]
    live_child = fresh.agent_tree._runs["run-child"]
    assert live_child.parent_id == "run-parent"
    assert live_child.path == child_run.path
    assert live_child.status == AgentRunStatus.RUNNING
    assert live_child.context_ref == "task-linked-child"
    assert live_child.ack_sequence == 7
    assert live_child.executor_capabilities == child_run.executor_capabilities
    assert live_child.executor_config == child_run.executor_config
    assert [item.payload for item in fresh.agent_tree._events[ws_id]] == [
        legacy.payload,
        bridged.payload,
    ]
    assert [item.sequence for item in fresh.agent_tree._events[ws_id]] == [4, 5]
    assert _tree_blobs(fresh, ws_id) == (raw_runs, raw_events)

    again = WorkspaceManager()
    assert again.tasks["task-linked-child"].parent_task_id == parent.id
    assert again.tasks["task-linked-child"].path == f"{parent.id}/{child.id}"
    assert again.tasks["task-linked-parent"].consumer_ack_sequence == 11
    assert again.tasks["task-linked-child"].consumer_ack_sequence == 7
    assert [item.call_id for item in again.task_mailbox._events[ws_id]] == [
        item.call_id for item in mailbox
    ]
    assert [item.sequence for item in again.task_mailbox._events[ws_id]] == [4, 5, 6]
    assert again.task_mailbox._next_seq[ws_id] == 7
    assert again.tasks["task-linked-child"].processing_call_ids == ["proc-1"]
    assert again.tasks["task-linked-child"].uncertain_call_ids == ["unc-1"]
    assert _tree_blobs(again, ws_id) == (raw_runs, raw_events)
    assert "run-child" in again.agent_tree._runs
    assert again.agent_tree._runs["run-child"].ack_sequence == 7


def test_explicit_null_parent_stays_root(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 2)
    parent_run = AgentRun(
        id="run-parent",
        workspace_id=ws_id,
        parent_id=None,
        path="run-parent",
        supervisor_id=None,
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        ack_sequence=2,
        created_at=stamp,
        updated_at=stamp,
    )
    child_run = AgentRun(
        id="run-child",
        workspace_id=ws_id,
        parent_id="run-parent",
        path="run-parent/run-child",
        supervisor_id="run-parent",
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        ack_sequence=3,
        created_at=stamp,
        updated_at=stamp,
    )
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [
                _raw_task(
                    task_id="task-parent",
                    workspace_id=ws_id,
                    title="parent",
                    agent_run_id="run-parent",
                    parent_task_id=None,
                ),
                _raw_task(
                    task_id="task-child",
                    workspace_id=ws_id,
                    title="child",
                    agent_run_id="run-child",
                    parent_task_id=None,
                ),
            ],
            "sessions": [],
            "reports": [],
            "agent_runs": [parent_run.model_dump(mode="json"), child_run.model_dump(mode="json")],
            "agent_events": [],
        },
    )
    fresh = WorkspaceManager()
    assert fresh.tasks["task-child"].parent_task_id is None
    assert fresh.tasks["task-child"].path == "task-child"
    assert fresh.tasks["task-child"].consumer_ack_sequence == 3


def test_triple_source_report_does_not_duplicate_or_advance_seq(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 3)
    existing = TaskEvent(
        sequence=3,
        call_id="report:rep-triple",
        fingerprint="task-fp",
        task_id="task-ordinary",
        actor_role=TaskActorRole.WORKER,
        type=TaskEventType.REPORT,
        action="report",
        target="task-ordinary",
        consumer_key=legacy_resident_consumer_key(ws_id),
        payload={"report_id": "rep-triple"},
        created_at=stamp,
        report_id="rep-triple",
    )
    agent = AgentEvent(
        sequence=3,
        call_id="legacy-triple",
        agent_run_id="run-missing",
        type=AgentEventType.PROGRESS,
        author="run-missing",
        recipient="task-ordinary",
        action="report",
        target="task-ordinary",
        fingerprint="agent-fp",
        payload={"report_id": "rep-triple"},
        created_at=stamp,
    )
    report = AgentReport(
        id="rep-triple",
        workspace_id=ws_id,
        task_id="task-ordinary",
        session_id="sess-worker",
        state=AgentReportState.WORKING,
        message="triple",
        created_at=stamp,
    )
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [_raw_task(task_id="task-ordinary", workspace_id=ws_id, title="ordinary")],
            "sessions": [],
            "reports": [report.model_dump(mode="json")],
            "task_events": [existing.model_dump(mode="json")],
            "agent_runs": [],
            "agent_events": [agent.model_dump(mode="json")],
        },
    )
    fresh = WorkspaceManager()
    mailbox = list(fresh.task_mailbox._events[ws_id])
    assert [item.call_id for item in mailbox] == ["report:rep-triple"]
    assert mailbox[0].sequence == 3
    assert fresh.task_mailbox._next_seq[ws_id] == 4
    again = WorkspaceManager()
    assert [item.call_id for item in again.task_mailbox._events[ws_id]] == ["report:rep-triple"]
    assert again.task_mailbox._next_seq[ws_id] == 4


def test_legacy_call_id_conflict_fails_closed(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 4)
    existing = TaskEvent(
        sequence=1,
        call_id="shared-call",
        fingerprint="task-fp",
        task_id="task-ordinary",
        actor_role=TaskActorRole.WORKER,
        type=TaskEventType.PROGRESS,
        action="emit",
        target="task-ordinary",
        consumer_key=make_task_consumer_key("task-ordinary"),
        created_at=stamp,
    )
    agent = AgentEvent(
        sequence=2,
        call_id="shared-call",
        agent_run_id="task-ordinary",
        type=AgentEventType.PROGRESS,
        author="task-ordinary",
        recipient="task-ordinary",
        action="report",
        target="other",
        fingerprint="agent-fp",
        created_at=stamp,
    )
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [_raw_task(task_id="task-ordinary", workspace_id=ws_id, title="ordinary")],
            "sessions": [],
            "reports": [],
            "task_events": [existing.model_dump(mode="json")],
            "agent_runs": [],
            "agent_events": [agent.model_dump(mode="json")],
        },
    )
    with pytest.raises(ValueError, match="conflicting duplicate call_id"):
        WorkspaceManager()


def test_same_report_call_id_alias_ignores_action_fingerprint(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 5)
    existing = TaskEvent(
        sequence=3,
        call_id="report:rep-alias",
        fingerprint="task-fp",
        task_id="task-ordinary",
        actor_role=TaskActorRole.WORKER,
        type=TaskEventType.REPORT,
        action="report",
        target="task-ordinary",
        consumer_key=legacy_resident_consumer_key(ws_id),
        payload={"report_id": "rep-alias"},
        created_at=stamp,
        report_id="rep-alias",
    )
    agent = AgentEvent(
        sequence=3,
        call_id="report:rep-alias",
        agent_run_id="run-missing",
        type=AgentEventType.PROGRESS,
        author="run-missing",
        recipient="task-ordinary",
        action="emit",
        target="run-missing",
        fingerprint="agent-fp",
        payload={"report_id": "rep-alias", "task_id": "task-ordinary"},
        created_at=stamp,
    )
    report = AgentReport(
        id="rep-alias",
        workspace_id=ws_id,
        task_id="task-ordinary",
        session_id="sess-worker",
        state=AgentReportState.WORKING,
        message="alias",
        created_at=stamp,
    )
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [_raw_task(task_id="task-ordinary", workspace_id=ws_id, title="ordinary")],
            "sessions": [],
            "reports": [report.model_dump(mode="json")],
            "task_events": [existing.model_dump(mode="json")],
            "agent_runs": [],
            "agent_events": [agent.model_dump(mode="json")],
        },
    )
    fresh = WorkspaceManager()
    mailbox = list(fresh.task_mailbox._events[ws_id])
    assert [item.call_id for item in mailbox] == ["report:rep-alias"]
    assert mailbox[0].action == "report"
    assert mailbox[0].sequence == 3
    assert fresh.task_mailbox._next_seq[ws_id] == 4


def test_same_report_call_id_mismatched_task_fails_closed(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 6)
    existing = TaskEvent(
        sequence=1,
        call_id="report:rep-mismatch",
        fingerprint="task-fp",
        task_id="task-ordinary",
        actor_role=TaskActorRole.WORKER,
        type=TaskEventType.REPORT,
        action="report",
        target="task-ordinary",
        consumer_key=legacy_resident_consumer_key(ws_id),
        payload={"report_id": "rep-mismatch"},
        created_at=stamp,
        report_id="rep-mismatch",
    )
    agent = AgentEvent(
        sequence=2,
        call_id="report:rep-mismatch",
        agent_run_id="task-other",
        type=AgentEventType.PROGRESS,
        author="task-other",
        recipient="task-other",
        action="emit",
        target="task-other",
        fingerprint="agent-fp",
        payload={"report_id": "rep-mismatch", "task_id": "task-other"},
        created_at=stamp,
    )
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [
                _raw_task(task_id="task-ordinary", workspace_id=ws_id, title="ordinary"),
                _raw_task(task_id="task-other", workspace_id=ws_id, title="other"),
            ],
            "sessions": [],
            "reports": [],
            "task_events": [existing.model_dump(mode="json")],
            "agent_runs": [],
            "agent_events": [agent.model_dump(mode="json")],
        },
    )
    with pytest.raises(ValueError, match="conflicting duplicate call_id"):
        WorkspaceManager()


def test_legacy_reviewer_verdict_uses_stored_report_fields(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 7)
    child_run = AgentRun(
        id="run-child",
        workspace_id=ws_id,
        parent_id="run-root",
        path="run-root/run-child",
        supervisor_id="run-root",
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        context_ref="task-linked",
        ack_sequence=8,
        created_at=stamp,
        updated_at=stamp,
        **_managed_contract(),
    )
    session = ManagedSession(
        id="sess-reviewer",
        workspace_id=ws_id,
        tab_id="tab-reviewer",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        title="reviewer",
        workspace_path=str(tmp_path),
        tmux_session="tmux-reviewer",
        created_at=stamp,
        updated_at=stamp,
    )
    report = AgentReport(
        id="rep-verdict",
        workspace_id=ws_id,
        task_id="task-linked",
        session_id="sess-reviewer",
        state=AgentReportState.REVIEW_PASSED,
        message="passed",
        review_cycle=5,
        created_at=stamp,
    )
    legacy = AgentEvent(
        sequence=8,
        call_id="report:rep-verdict",
        agent_run_id="run-child",
        type=AgentEventType.PROGRESS,
        author="run-child",
        recipient="run-root",
        action="emit",
        target="run-child",
        fingerprint="crude-fp",
        payload={"report_id": "rep-verdict"},
        created_at=stamp,
    )
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [
                _raw_task(
                    task_id="task-linked",
                    workspace_id=ws_id,
                    title="linked",
                    agent_run_id="run-child",
                )
            ],
            "sessions": [session.model_dump(mode="json")],
            "reports": [report.model_dump(mode="json")],
            "agent_runs": [child_run.model_dump(mode="json")],
            "agent_events": [legacy.model_dump(mode="json")],
        },
    )
    fresh = WorkspaceManager()
    mailbox = list(fresh.task_mailbox._events[ws_id])
    assert [item.call_id for item in mailbox] == ["report:rep-verdict"]
    event = mailbox[0]
    assert event.sequence == 8
    assert event.action == "emit"
    assert event.actor_role == TaskActorRole.REVIEWER
    assert event.actor_session_id == "sess-reviewer"
    assert event.review_cycle == 5
    assert event.type == TaskEventType.REVIEW_PASSED
    assert event.report_id == "rep-verdict"
    assert fresh.task_mailbox._next_seq[ws_id] == 9
    assert fresh.agent_tree._events[ws_id][0].type == AgentEventType.PROGRESS
    assert fresh.agent_tree._events[ws_id][0].action == "emit"


def _managed_run(
    workspace_id: str,
    run_id: str,
    *,
    parent_id: str | None,
    context_ref: str,
    stamp: datetime,
) -> AgentRun:
    path = run_id if parent_id is None else f"{parent_id}/{run_id}"
    return AgentRun(
        id=run_id,
        workspace_id=workspace_id,
        parent_id=parent_id,
        path=path,
        supervisor_id=parent_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        status=AgentRunStatus.RUNNING,
        context_ref=context_ref,
        ack_sequence=1,
        created_at=stamp,
        updated_at=stamp,
        **_managed_contract(),
    )


def _task_tree(manager: WorkspaceManager, workspace_id: str) -> dict[str, dict[str, str | None]]:
    return {
        task_id: {
            "parent_task_id": task.parent_task_id,
            "root_task_id": task.root_task_id,
            "path": task.path,
        }
        for task_id, task in sorted(manager.tasks.items())
        if task.workspace_id == workspace_id
    }


def test_reverse_json_task_run_order_keeps_parent_root_path(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 8)
    resident = AgentRun(
        id="run-resident",
        workspace_id=ws_id,
        parent_id=None,
        path="run-resident",
        supervisor_id=None,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        status=AgentRunStatus.RUNNING,
        ack_sequence=0,
        created_at=stamp,
        updated_at=stamp,
        **_resident_contract(),
    )
    runs = [
        resident,
        _managed_run(ws_id, "run-a", parent_id="run-resident", context_ref="task-a", stamp=stamp),
        _managed_run(ws_id, "run-b", parent_id="run-a", context_ref="task-b", stamp=stamp),
        _managed_run(ws_id, "run-c", parent_id="run-b", context_ref="task-c", stamp=stamp),
    ]
    tasks = [
        _raw_task(task_id="task-a", workspace_id=ws_id, title="a", agent_run_id="run-a"),
        _raw_task(task_id="task-b", workspace_id=ws_id, title="b", agent_run_id="run-b"),
        _raw_task(task_id="task-c", workspace_id=ws_id, title="c", agent_run_id="run-c"),
    ]

    def payload(task_items: list[dict[str, Any]], run_items: list[AgentRun]) -> dict[str, Any]:
        return {
            "tasks": task_items,
            "sessions": [],
            "reports": [],
            "agent_runs": [item.model_dump(mode="json") for item in run_items],
            "agent_events": [],
        }

    _write_state(manager, ws_id, payload(tasks, runs))
    forward = _task_tree(WorkspaceManager(), ws_id)
    _write_state(manager, ws_id, payload(list(reversed(tasks)), list(reversed(runs))))
    reverse = _task_tree(WorkspaceManager(), ws_id)
    expected = {
        "task-a": {"parent_task_id": None, "root_task_id": "task-a", "path": "task-a"},
        "task-b": {
            "parent_task_id": "task-a",
            "root_task_id": "task-a",
            "path": "task-a/task-b",
        },
        "task-c": {
            "parent_task_id": "task-b",
            "root_task_id": "task-a",
            "path": "task-a/task-b/task-c",
        },
    }
    assert forward == expected
    assert reverse == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_run_parent_cycle_fails_closed(
    manager: WorkspaceManager, tmp_path: Path, reverse: bool
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 9)
    runs = [
        _managed_run(ws_id, "run-a", parent_id="run-b", context_ref="task-a", stamp=stamp),
        _managed_run(ws_id, "run-b", parent_id="run-a", context_ref="task-b", stamp=stamp),
    ]
    tasks = [
        _raw_task(task_id="task-a", workspace_id=ws_id, title="a", agent_run_id="run-a"),
        _raw_task(task_id="task-b", workspace_id=ws_id, title="b", agent_run_id="run-b"),
    ]
    if reverse:
        tasks = list(reversed(tasks))
        runs = list(reversed(runs))
    _write_state(
        manager,
        ws_id,
        {
            "tasks": tasks,
            "sessions": [],
            "reports": [],
            "agent_runs": [item.model_dump(mode="json") for item in runs],
            "agent_events": [],
        },
    )
    with pytest.raises(ValueError, match="cycle"):
        WorkspaceManager()


def test_persisted_task_parent_cycle_fails_closed(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [
                _raw_task(
                    task_id="task-a",
                    workspace_id=ws_id,
                    title="a",
                    parent_task_id="task-b",
                ),
                _raw_task(
                    task_id="task-b",
                    workspace_id=ws_id,
                    title="b",
                    parent_task_id="task-a",
                ),
            ],
            "sessions": [],
            "reports": [],
            "agent_runs": [],
            "agent_events": [],
        },
    )
    with pytest.raises(ValueError, match="cycle"):
        WorkspaceManager()
