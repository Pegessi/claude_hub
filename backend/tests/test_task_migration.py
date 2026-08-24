"""AC8/AC10: legacy state.json migration to Task Graph only."""

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


def _raw_run(
    *,
    run_id: str,
    workspace_id: str,
    parent_id: str | None,
    path: str,
    supervisor_id: str | None = None,
    executor_kind: str = "managed_task",
    context_ref: str | None = None,
    ack_sequence: int = 0,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "workspace_id": workspace_id,
        "parent_id": parent_id,
        "path": path,
        "supervisor_id": supervisor_id,
        "executor_kind": executor_kind,
        "status": "running",
        "context_ref": context_ref,
        "ack_sequence": ack_sequence,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


def _raw_agent_event(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "sequence": 1,
        "call_id": "legacy-call",
        "agent_run_id": "run-child",
        "type": "started",
        "author": "run-child",
        "recipient": "run-parent",
        "action": "spawn:started",
        "target": "run-child",
        "fingerprint": "legacy-fp",
        "payload": {},
        "created_at": "2026-01-01T00:00:00",
    }
    defaults.update(kwargs)
    return defaults


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


def _disk_has_no_legacy_agent_tree(manager: WorkspaceManager, workspace_id: str) -> None:
    disk = json.loads(manager._workspace_state_file(workspace_id).read_text(encoding="utf-8"))
    assert "agent_runs" not in disk
    assert "agent_events" not in disk
    for task in disk.get("tasks") or []:
        assert "agent_run_id" not in task
    for event in disk.get("task_events") or []:
        assert "compat_run_id" not in event


def _task_tree(manager: WorkspaceManager, workspace_id: str) -> dict[str, dict[str, Any]]:
    return {
        task.id: {
            "parent_task_id": task.parent_task_id,
            "root_task_id": task.root_task_id,
            "path": task.path,
        }
        for task in manager.tasks.values()
        if task.workspace_id == workspace_id
    }


def test_pre_unification_state_json_migrates_parent_ack_and_events(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    stamp = datetime(2026, 1, 1, 0, 0, 0)
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
            _raw_run(
                run_id="run-resident",
                workspace_id=ws_id,
                parent_id=None,
                path="run-resident",
                executor_kind="resident_root",
                ack_sequence=11,
            ),
            _raw_run(
                run_id="run-parent",
                workspace_id=ws_id,
                parent_id="run-resident",
                path="run-resident/run-parent",
                supervisor_id="run-resident",
                context_ref="task-linked-parent",
                ack_sequence=4,
            ),
            _raw_run(
                run_id="run-child",
                workspace_id=ws_id,
                parent_id="run-parent",
                path="run-resident/run-parent/run-child",
                supervisor_id="run-parent",
                context_ref="task-linked-child",
                ack_sequence=7,
            ),
        ],
        "agent_events": [
            _raw_agent_event(
                sequence=4,
                call_id="legacy-started",
                fingerprint="legacy-started-fp",
                payload={"task_id": "task-linked-child"},
            ),
            _raw_agent_event(
                sequence=5,
                call_id="report:rep-bridged",
                type="progress",
                action="report",
                fingerprint="bridged-fp",
                payload={"report_id": "rep-bridged", "task_id": "task-linked-child"},
            ),
        ],
    }
    _write_state(manager, ws_id, payload)
    _omit_index_resident_ack(manager, ws_id)

    fresh = WorkspaceManager()
    parent = fresh.tasks["task-linked-parent"]
    child = fresh.tasks["task-linked-child"]
    ordinary = fresh.tasks["task-ordinary"]
    assert parent.parent_task_id is None
    assert child.parent_task_id == parent.id
    assert child.root_task_id == parent.id
    assert child.path == f"{parent.id}/{child.id}"
    assert ordinary.parent_task_id is None
    assert parent.consumer_ack_sequence == 11
    assert child.consumer_ack_sequence == 7

    mailbox = list(fresh.task_mailbox._events[ws_id])
    assert [item.call_id for item in mailbox] == [
        "legacy-started",
        "report:rep-bridged",
        "report:rep-ordinary",
    ]
    started = mailbox[0]
    assert started.sequence == 4
    assert started.consumer_key == make_task_consumer_key(parent.id)
    assert started.fingerprint == "legacy-started-fp"
    backfilled = mailbox[2]
    assert backfilled.action == "report"
    assert backfilled.report_id == "rep-ordinary"
    assert fresh.task_mailbox._next_seq[ws_id] == 7
    _disk_has_no_legacy_agent_tree(fresh, ws_id)

    again = WorkspaceManager()
    assert again.tasks["task-linked-child"].parent_task_id == parent.id
    assert [item.call_id for item in again.task_mailbox._events[ws_id]] == [
        item.call_id for item in mailbox
    ]
    _disk_has_no_legacy_agent_tree(again, ws_id)


def test_explicit_null_parent_stays_root(manager: WorkspaceManager, tmp_path: Path) -> None:
    ws_id = _make_workspace(manager, tmp_path)
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
            "agent_runs": [
                _raw_run(
                    run_id="run-parent",
                    workspace_id=ws_id,
                    parent_id=None,
                    path="run-parent",
                    ack_sequence=2,
                ),
                _raw_run(
                    run_id="run-child",
                    workspace_id=ws_id,
                    parent_id="run-parent",
                    path="run-parent/run-child",
                    supervisor_id="run-parent",
                    ack_sequence=3,
                ),
            ],
            "agent_events": [],
        },
    )
    fresh = WorkspaceManager()
    assert fresh.tasks["task-child"].parent_task_id is None
    assert fresh.tasks["task-child"].path == "task-child"
    assert fresh.tasks["task-child"].consumer_ack_sequence == 3


def test_reverse_json_task_run_order_keeps_parent_root_path(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    runs = [
        _raw_run(
            run_id="run-resident",
            workspace_id=ws_id,
            parent_id=None,
            path="run-resident",
            executor_kind="resident_root",
        ),
        _raw_run(
            run_id="run-a",
            workspace_id=ws_id,
            parent_id="run-resident",
            path="run-resident/run-a",
            supervisor_id="run-resident",
            context_ref="task-a",
        ),
        _raw_run(
            run_id="run-b",
            workspace_id=ws_id,
            parent_id="run-a",
            path="run-resident/run-a/run-b",
            supervisor_id="run-a",
            context_ref="task-b",
        ),
        _raw_run(
            run_id="run-c",
            workspace_id=ws_id,
            parent_id="run-b",
            path="run-resident/run-a/run-b/run-c",
            supervisor_id="run-b",
            context_ref="task-c",
        ),
    ]
    tasks = [
        _raw_task(task_id="task-a", workspace_id=ws_id, title="a", agent_run_id="run-a"),
        _raw_task(task_id="task-b", workspace_id=ws_id, title="b", agent_run_id="run-b"),
        _raw_task(task_id="task-c", workspace_id=ws_id, title="c", agent_run_id="run-c"),
    ]

    def payload(
        task_items: list[dict[str, Any]], run_items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "tasks": task_items,
            "sessions": [],
            "reports": [],
            "agent_runs": run_items,
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
    runs = [
        _raw_run(
            run_id="run-a",
            workspace_id=ws_id,
            parent_id="run-b",
            path="run-b/run-a",
            supervisor_id="run-b",
            context_ref="task-a",
        ),
        _raw_run(
            run_id="run-b",
            workspace_id=ws_id,
            parent_id="run-a",
            path="run-a/run-b",
            supervisor_id="run-a",
            context_ref="task-b",
        ),
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
            "agent_runs": runs,
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
                {
                    "id": "task-a",
                    "workspace_id": ws_id,
                    "title": "a",
                    "prompt": "a",
                    "agent_type": "claude",
                    "status": "todo",
                    "parent_task_id": "task-b",
                    "root_task_id": "task-a",
                    "path": "task-a",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                },
                {
                    "id": "task-b",
                    "workspace_id": ws_id,
                    "title": "b",
                    "prompt": "b",
                    "agent_type": "claude",
                    "status": "todo",
                    "parent_task_id": "task-a",
                    "root_task_id": "task-a",
                    "path": "task-a/task-b",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                },
            ],
            "sessions": [],
            "reports": [],
        },
    )
    with pytest.raises(ValueError, match="cycle"):
        WorkspaceManager()


def test_mailbox_rejects_legacy_resident_consumer_key_after_migration(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    ws_id = _make_workspace(manager, tmp_path)
    legacy_key = legacy_resident_consumer_key(ws_id)
    _write_state(
        manager,
        ws_id,
        {
            "tasks": [_raw_task(task_id="task-root", workspace_id=ws_id, title="root")],
            "sessions": [],
            "reports": [],
            "task_events": [
                {
                    "sequence": 1,
                    "call_id": "legacy-resident-event",
                    "fingerprint": "fp",
                    "task_id": "task-root",
                    "actor_role": "worker",
                    "type": "report",
                    "action": "report",
                    "target": "task-root",
                    "consumer_key": legacy_key,
                    "payload": {},
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
        },
    )
    fresh = WorkspaceManager()
    with pytest.raises(ValueError, match="legacy resident consumer"):
        fresh.task_mailbox.wait(ws_id, legacy_key)
