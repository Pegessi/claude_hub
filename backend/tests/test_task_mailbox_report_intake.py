"""Slice 3a: report/reviewer intake writes TaskMailbox as the durable fact."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentReport,
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    TerminalTab,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture()
def state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Generator[Path, None, None]:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True)
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)

    created_tabs = 0

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        nonlocal created_tabs
        created_tabs += 1
        return TerminalTab(
            id=f"tab-managed-{created_tabs}",
            name=str(kwargs.get("name") or "managed reviewer"),
            port=12380 + created_tabs,
            created_at=datetime.utcnow(),
            is_active=True,
        )

    fake_tab = MagicMock(id="tab-mock", tmux_session="tmux-mock")
    monkeypatch.setattr(_wm.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(_wm.ttyd_manager, "update_tab", AsyncMock(return_value=fake_tab))
    monkeypatch.setattr(_wm.ttyd_manager, "delete_tab", AsyncMock())
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message", AsyncMock())
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message_with_receipt", AsyncMock())
    monkeypatch.setattr(_wm.WorkspaceManager, "_ensure_session_ready_for_send", AsyncMock())
    yield root


@pytest.fixture()
def manager_and_workspace(state_root: Path, tmp_path: Path) -> tuple[WorkspaceManager, str]:
    manager = WorkspaceManager()
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = manager.create_workspace(
        WorkspaceCreate(name="mailbox report intake", path=str(repo), target=ExecutionTarget.LOCAL)
    )
    return manager, workspace.id


def _task_session(
    manager: WorkspaceManager,
    workspace_id: str,
    *,
    task_id: str = "task-report",
    session_id: str = "session-report",
    role: WorkspaceSessionRole = WorkspaceSessionRole.WORKER,
    status: WorkspaceTaskStatus = WorkspaceTaskStatus.WORKING,
    review_cycle: int = 1,
) -> tuple[WorkspaceTask, ManagedSession]:
    now = datetime.utcnow()
    task = WorkspaceTask(
        id=task_id,
        workspace_id=workspace_id,
        title="mailbox report",
        prompt="implement safely",
        agent_type=AgentType.CLAUDE,
        status=status,
        session_id=session_id if role != WorkspaceSessionRole.REVIEWER else "session-worker",
        review_session_id=session_id if role == WorkspaceSessionRole.REVIEWER else None,
        review_cycle=review_cycle,
        review_requested_at=now if role == WorkspaceSessionRole.REVIEWER else None,
        created_at=now,
        updated_at=now,
    )
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id=f"tab-{session_id}",
        role=role,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=task.title,
        workspace_path="/tmp",
        tmux_session=f"tmux-{session_id}",
        target=ExecutionTarget.LOCAL,
        task_id=task_id,
        current_task_id=task_id,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    manager.sessions[session.id] = session
    return task, session


_LEGACY_EMIT_TARGET = "legacy-run-child"


def _task_field_snapshot(task: WorkspaceTask) -> dict[str, object]:
    return {
        "status": task.status,
        "session_id": task.session_id,
        "review_cycle": task.review_cycle,
        "consumer_ack_sequence": task.consumer_ack_sequence,
        "prompt": task.prompt,
    }


def _mailbox_events(manager: WorkspaceManager, workspace_id: str) -> list:
    return list(manager.task_mailbox._events.get(workspace_id, []))


def _report_events(manager: WorkspaceManager, workspace_id: str, report_id: str) -> list:
    call_id = f"report:{report_id}"
    return [item for item in _mailbox_events(manager, workspace_id) if item.call_id == call_id]


@pytest.mark.asyncio
async def test_ordinary_worker_report_writes_task_event(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="ordinary progress",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )

    report = await manager.create_report(session.id, payload)
    events = _report_events(manager, workspace_id, report.id)
    assert len(events) == 1
    event = events[0]
    assert event.sequence == 1
    assert event.task_id == task.id
    assert event.actor_role == TaskActorRole.WORKER
    assert event.actor_session_id == session.id
    assert event.review_cycle == 1
    assert event.report_id == report.id
    assert event.type == TaskEventType.PROGRESS
    assert manager.tasks[task.id].consumer_ack_sequence == 0

    retry = await manager.create_report(session.id, payload)
    assert retry.id == report.id
    assert [item.sequence for item in _report_events(manager, workspace_id, report.id)] == [1]
    assert len(_mailbox_events(manager, workspace_id)) == 1

    fresh = WorkspaceManager()
    assert [item.sequence for item in _report_events(fresh, workspace_id, report.id)] == [1]
    cold_retry = await fresh.create_report(session.id, payload)
    assert cold_retry.id == report.id
    assert [item.sequence for item in _report_events(fresh, workspace_id, report.id)] == [1]
    assert len(_mailbox_events(fresh, workspace_id)) == 1


@pytest.mark.asyncio
async def test_linked_worker_report_writes_task_event_without_agent_run_mutation(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(
        manager, workspace_id, task_id="task-linked", session_id="session-linked"
    )
    manager.tasks[task.id] = task.model_copy(update={"parent_task_id": "task-parent-root"})
    task_before = _task_field_snapshot(manager.tasks[task.id])
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="linked progress",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )

    report = await manager.create_report(session.id, payload)
    events = _report_events(manager, workspace_id, report.id)
    assert len(events) == 1
    event = events[0]
    assert event.task_id == task.id
    assert event.actor_role == TaskActorRole.WORKER
    assert event.actor_session_id == session.id
    assert event.review_cycle == 1
    assert event.report_id == report.id
    assert _task_field_snapshot(manager.tasks[task.id]) == task_before
    assert len(_mailbox_events(manager, workspace_id)) == 1

    retry = await manager.create_report(session.id, payload)
    assert retry.id == report.id
    assert len(_report_events(manager, workspace_id, report.id)) == 1

    fresh = WorkspaceManager()
    loaded = _report_events(fresh, workspace_id, report.id)
    assert len(loaded) == 1
    assert loaded[0].sequence == event.sequence
    assert _task_field_snapshot(fresh.tasks[task.id]) == task_before


@pytest.mark.asyncio
async def test_reviewer_started_passed_failed_persist_actor_fields(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    now = datetime.utcnow()
    worker, _ = _task_session(
        manager,
        workspace_id,
        task_id="task-review",
        session_id="session-worker",
        status=WorkspaceTaskStatus.REVIEW,
        review_cycle=3,
    )
    reviewer = ManagedSession(
        id="session-reviewer",
        workspace_id=workspace_id,
        tab_id="tab-reviewer",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=worker.title,
        workspace_path="/tmp",
        tmux_session="tmux-reviewer",
        target=ExecutionTarget.LOCAL,
        task_id=worker.id,
        current_task_id=worker.id,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[reviewer.id] = reviewer
    manager.tasks[worker.id] = worker.model_copy(
        update={
            "review_session_id": reviewer.id,
            "review_requested_at": now,
            "review_completed_at": None,
            "reviewed_cycle": 0,
        }
    )

    started = await manager.create_report(
        reviewer.id,
        AgentReportCreate(
            task_id=worker.id,
            state=AgentReportState.REVIEW_STARTED,
            message="reviewer started",
            call_id=f"{worker.id}-review-started-cycle-3-1",
        ),
    )
    started_event = _report_events(manager, workspace_id, started.id)[0]
    assert started_event.type == TaskEventType.REVIEW_STARTED
    assert started_event.actor_role == TaskActorRole.REVIEWER
    assert started_event.actor_session_id == reviewer.id
    assert started_event.task_id == worker.id
    assert started_event.review_cycle == 3
    assert started_event.report_id == started.id

    passed = await manager.create_report(
        reviewer.id,
        AgentReportCreate(
            task_id=worker.id,
            state=AgentReportState.REVIEW_PASSED,
            message="reviewer passed",
            call_id=f"{worker.id}-review-passed-cycle-3-1",
        ),
    )
    passed_event = _report_events(manager, workspace_id, passed.id)[0]
    assert passed_event.type == TaskEventType.REVIEW_PASSED
    assert passed_event.actor_role == TaskActorRole.REVIEWER
    assert passed_event.actor_session_id == reviewer.id
    assert passed_event.review_cycle == 3
    assert passed_event.report_id == passed.id

    failed_task, _ = _task_session(
        manager,
        workspace_id,
        task_id="task-review-failed",
        session_id="session-worker-2",
        status=WorkspaceTaskStatus.REVIEW,
        review_cycle=2,
    )
    failed_reviewer = reviewer.model_copy(
        update={"id": "session-reviewer-2", "tab_id": "tab-reviewer-2", "task_id": failed_task.id}
    )
    manager.sessions[failed_reviewer.id] = failed_reviewer
    manager.tasks[failed_task.id] = failed_task.model_copy(
        update={
            "review_session_id": failed_reviewer.id,
            "review_requested_at": now,
            "review_completed_at": None,
            "reviewed_cycle": 0,
        }
    )
    failed = await manager.create_report(
        failed_reviewer.id,
        AgentReportCreate(
            task_id=failed_task.id,
            state=AgentReportState.REVIEW_FAILED,
            message="reviewer failed",
            call_id=f"{failed_task.id}-review-failed-cycle-2-1",
        ),
    )
    failed_event = _report_events(manager, workspace_id, failed.id)[0]
    assert failed_event.type == TaskEventType.REVIEW_FAILED
    assert failed_event.actor_role == TaskActorRole.REVIEWER
    assert failed_event.actor_session_id == failed_reviewer.id
    assert failed_event.review_cycle == 2
    assert failed_event.report_id == failed.id


@pytest.mark.asyncio
async def test_precommit_save_failure_rolls_back_mailbox_and_same_call_retry(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    manager._save_state()
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.NEEDS_INPUT,
        message="need a decision",
        call_id=f"{task.id}-needs-input-cycle-1-1",
    )
    state_file = manager._workspace_state_file(workspace_id)
    original_write = manager._atomic_write_text
    failed = False
    staged_report_id: str | None = None

    def fail_target_state_once(path: Path, text: str) -> None:
        nonlocal failed, staged_report_id
        if path == state_file and not failed:
            failed = True
            staged_reports = [
                report for report in manager.reports.values() if report.call_id == payload.call_id
            ]
            assert len(staged_reports) == 1
            staged_report_id = staged_reports[0].id
            assert (
                manager.task_mailbox._call_record(workspace_id, f"report:{staged_report_id}")
                is not None
            )
            assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
            raise OSError("pre-state atomic replace failed")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_target_state_once)
    with pytest.raises(OSError, match="pre-state"):
        await manager.create_report(session.id, payload)

    assert staged_report_id is not None
    assert staged_report_id not in manager.reports
    assert payload.call_id not in manager.sessions[session.id].report_call_ids
    assert manager.task_mailbox._call_record(workspace_id, f"report:{staged_report_id}") is None
    assert _mailbox_events(manager, workspace_id) == []
    assert manager.tasks[task.id].consumer_ack_sequence == 0

    cold = WorkspaceManager()
    assert cold.task_mailbox._events.get(workspace_id, []) == []
    assert payload.call_id not in cold.sessions[session.id].report_call_ids

    committed = await manager.create_report(session.id, payload)
    events = _report_events(manager, workspace_id, committed.id)
    assert len(events) == 1
    assert events[0].type == TaskEventType.NEEDS_INPUT
    assert events[0].sequence == 1
    assert events[0].actor_role == TaskActorRole.WORKER

    retry = await manager.create_report(session.id, payload)
    assert retry.id == committed.id
    assert [item.sequence for item in _report_events(manager, workspace_id, committed.id)] == [1]

    fresh = WorkspaceManager()
    loaded = _report_events(fresh, workspace_id, committed.id)
    assert [item.sequence for item in loaded] == [1]
    cold_retry = await fresh.create_report(session.id, payload)
    assert cold_retry.id == committed.id
    assert [item.sequence for item in _report_events(fresh, workspace_id, committed.id)] == [1]
    assert len(_mailbox_events(fresh, workspace_id)) == 1


def _append_legacy_raw_emit(
    manager: WorkspaceManager,
    *,
    workspace_id: str,
    task: WorkspaceTask,
    session: ManagedSession,
    legacy_target: str,
    call_id: str,
    payload: dict[str, object] | None = None,
    event_type: TaskEventType = TaskEventType.PROGRESS,
    report_id: str | None = None,
):
    event, _is_new = manager.task_mailbox.append_event(
        workspace_id=workspace_id,
        task_id=task.id,
        actor_role=TaskActorRole.WORKER,
        event_type=event_type,
        call_id=call_id,
        action="emit",
        target=legacy_target,
        actor_session_id=session.id,
        review_cycle=task.review_cycle,
        payload=payload or {},
        report_id=report_id,
        persist=False,
    )
    return event


def _seed_legacy_emit_report(
    manager: WorkspaceManager,
    workspace_id: str,
    task: WorkspaceTask,
    session: ManagedSession,
    *,
    report_id: str,
    producer_call_id: str,
    message: str = "legacy emit progress",
    legacy_target: str = _LEGACY_EMIT_TARGET,
) -> tuple[AgentReport, AgentReportCreate, str]:
    """Persist a pre-unification report plus raw ``report:<id>`` TaskMailbox emit blob."""

    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message=message,
        call_id=producer_call_id,
    )
    report = AgentReport(
        id=report_id,
        workspace_id=workspace_id,
        task_id=task.id,
        session_id=session.id,
        call_id=producer_call_id,
        state=payload.state,
        message=payload.message,
        review_cycle=task.review_cycle,
        created_at=datetime.utcnow(),
    )
    manager.reports[report.id] = report
    manager.sessions[session.id] = session.model_copy(
        update={
            "report_call_ids": {producer_call_id: report.id},
            "report_call_fingerprints": {
                producer_call_id: manager._compute_report_fingerprint(payload)
            },
        }
    )
    _append_legacy_raw_emit(
        manager,
        workspace_id=workspace_id,
        task=task,
        session=session,
        legacy_target=legacy_target,
        call_id=f"report:{report.id}",
        payload={
            "message": report.message,
            "report_id": report.id,
            "report_state": report.state.value,
            "task_id": report.task_id,
        },
        report_id=report.id,
    )
    return report, payload, legacy_target


@pytest.mark.asyncio
async def test_legacy_emit_report_alias_reused_on_cold_create_report_retry(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(
        manager, workspace_id, task_id="task-legacy-alias", session_id="session-legacy-alias"
    )
    report, payload, legacy_target = _seed_legacy_emit_report(
        manager,
        workspace_id,
        task,
        session,
        report_id="rep-legacy-bridge",
        producer_call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    legacy = next(
        item
        for item in _mailbox_events(manager, workspace_id)
        if item.call_id == f"report:{report.id}"
    )
    assert legacy.action == "emit"
    assert legacy.target == legacy_target
    assert legacy.fingerprint
    task_before = _task_field_snapshot(manager.tasks[task.id])
    manager._save_state()

    cold = WorkspaceManager()
    projected = _report_events(cold, workspace_id, report.id)
    assert len(projected) == 1
    assert projected[0].sequence == legacy.sequence
    assert projected[0].action == "emit"
    assert projected[0].target == legacy_target
    assert projected[0].fingerprint == legacy.fingerprint
    assert projected[0].task_id == task.id
    assert projected[0].report_id == report.id
    assert _task_field_snapshot(cold.tasks[task.id]) == task_before

    retry = await cold.create_report(session.id, payload)
    assert retry.id == report.id
    after = _report_events(cold, workspace_id, report.id)
    assert [item.sequence for item in after] == [legacy.sequence]
    assert after[0].action == "emit"
    assert after[0].target == legacy_target
    assert after[0].fingerprint == legacy.fingerprint
    assert after[0].task_id == task.id
    assert after[0].report_id == report.id
    assert len(_mailbox_events(cold, workspace_id)) == 1
    assert [item.call_id for item in _mailbox_events(cold, workspace_id)] == [f"report:{report.id}"]
    assert _task_field_snapshot(cold.tasks[task.id]) == task_before


@pytest.mark.asyncio
async def test_legacy_emit_call_id_conflicts_when_not_canonical_report_alias(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(
        manager, workspace_id, task_id="task-legacy-conflict", session_id="session-legacy-conflict"
    )
    other, _ = _task_session(
        manager,
        workspace_id,
        task_id="task-legacy-other",
        session_id="session-legacy-other",
    )
    report, _payload, legacy_target = _seed_legacy_emit_report(
        manager,
        workspace_id,
        task,
        session,
        report_id="rep-legacy-conflict",
        producer_call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    ordinary = _append_legacy_raw_emit(
        manager,
        workspace_id=workspace_id,
        task=task,
        session=session,
        legacy_target=legacy_target,
        call_id="ordinary-emit-shared",
        payload={"note": "not a report bridge", "task_id": task.id},
    )
    assert ordinary.action == "emit"
    assert ordinary.target == legacy_target
    task_before = _task_field_snapshot(manager.tasks[task.id])
    manager._save_state()

    cold = WorkspaceManager()
    with pytest.raises(ValueError, match="already used"):
        cold.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.WORKER,
            event_type=TaskEventType.PROGRESS,
            call_id="ordinary-emit-shared",
            action="report",
            actor_session_id=session.id,
            report_id=report.id,
        )
    with pytest.raises(ValueError, match="already used"):
        cold.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=other.id,
            actor_role=TaskActorRole.WORKER,
            event_type=TaskEventType.PROGRESS,
            call_id=f"report:{report.id}",
            action="report",
            actor_session_id=session.id,
            report_id=report.id,
        )
    with pytest.raises(ValueError, match="already used"):
        cold.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.WORKER,
            event_type=TaskEventType.PROGRESS,
            call_id=f"report:{report.id}",
            action="report",
            actor_session_id=session.id,
            report_id="rep-different",
        )
    assert [item.sequence for item in _report_events(cold, workspace_id, report.id)] == [
        next(
            item.sequence
            for item in _mailbox_events(cold, workspace_id)
            if item.call_id == f"report:{report.id}"
        )
    ]
    assert len(_report_events(cold, workspace_id, report.id)) == 1
    assert _task_field_snapshot(cold.tasks[task.id]) == task_before


@pytest.mark.asyncio
async def test_legacy_emit_report_alias_rejects_tampered_failed_payload(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(
        manager, workspace_id, task_id="task-legacy-tamper", session_id="session-legacy-tamper"
    )
    report, _payload, legacy_target = _seed_legacy_emit_report(
        manager,
        workspace_id,
        task,
        session,
        report_id="rep-legacy-tamper",
        producer_call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    task_before = _task_field_snapshot(manager.tasks[task.id])
    manager._save_state()

    cold = WorkspaceManager()
    projected = _report_events(cold, workspace_id, report.id)
    assert len(projected) == 1
    assert projected[0].type == TaskEventType.PROGRESS
    with pytest.raises(ValueError, match="already used"):
        cold.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.WORKER,
            event_type=TaskEventType.FAILED,
            call_id=f"report:{report.id}",
            action="report",
            actor_session_id=session.id,
            review_cycle=report.review_cycle,
            payload={
                "message": "tampered failed",
                "report_id": report.id,
                "report_state": "working",
                "task_id": task.id,
                "actor_role": TaskActorRole.WORKER.value,
                "actor_session_id": session.id,
                "review_cycle": report.review_cycle,
            },
            report_id=report.id,
        )
    after = _report_events(cold, workspace_id, report.id)
    assert len(after) == 1
    assert after[0].sequence == projected[0].sequence
    assert after[0].type == TaskEventType.PROGRESS
    assert after[0].payload.get("message") == report.message
    assert _task_field_snapshot(cold.tasks[task.id]) == task_before


def _cold_canonical_report_payload(
    manager: WorkspaceManager, report_id: str
) -> tuple[AgentReport, ManagedSession, dict[str, object]]:
    report = manager.reports[report_id]
    session = manager.sessions[report.session_id]
    assert report.task_id is not None
    task = manager.tasks[report.task_id]
    return report, session, manager._canonical_report_bridge_payload(report, session, task)


@pytest.mark.asyncio
async def test_legacy_emit_report_alias_rejects_extra_payload_key(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(
        manager, workspace_id, task_id="task-legacy-extra", session_id="session-legacy-extra"
    )
    report, _payload, legacy_target = _seed_legacy_emit_report(
        manager,
        workspace_id,
        task,
        session,
        report_id="rep-legacy-extra",
        producer_call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    task_before = _task_field_snapshot(manager.tasks[task.id])
    manager._save_state()

    cold = WorkspaceManager()
    stored, stored_session, canonical = _cold_canonical_report_payload(cold, report.id)
    extra = dict(canonical)
    extra["tampered_extra"] = True
    with pytest.raises(ValueError, match="already used"):
        reused, created = cold.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.WORKER,
            event_type=TaskEventType.PROGRESS,
            call_id=f"report:{report.id}",
            action="report",
            actor_session_id=stored_session.id,
            review_cycle=stored.review_cycle,
            payload=extra,
            report_id=report.id,
        )
        raise AssertionError(f"extra_reused {created} {reused.type.value} {reused.payload}")
    after = _report_events(cold, workspace_id, report.id)
    assert len(after) == 1
    assert after[0].type == TaskEventType.PROGRESS
    assert after[0].sequence == 1
    assert "tampered_extra" not in after[0].payload
    assert _task_field_snapshot(cold.tasks[task.id]) == task_before


@pytest.mark.asyncio
async def test_legacy_emit_report_alias_rejects_missing_payload_key(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(
        manager, workspace_id, task_id="task-legacy-missing", session_id="session-legacy-missing"
    )
    report, _payload, legacy_target = _seed_legacy_emit_report(
        manager,
        workspace_id,
        task,
        session,
        report_id="rep-legacy-missing",
        producer_call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    task_before = _task_field_snapshot(manager.tasks[task.id])
    manager._save_state()

    cold = WorkspaceManager()
    stored, stored_session, canonical = _cold_canonical_report_payload(cold, report.id)
    missing = {key: value for key, value in canonical.items() if key != "review_cycle"}
    with pytest.raises(ValueError, match="already used"):
        cold.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.WORKER,
            event_type=TaskEventType.PROGRESS,
            call_id=f"report:{report.id}",
            action="report",
            actor_session_id=stored_session.id,
            review_cycle=stored.review_cycle,
            payload=missing,
            report_id=report.id,
        )
    after = _report_events(cold, workspace_id, report.id)
    assert len(after) == 1
    assert after[0].type == TaskEventType.PROGRESS
    assert after[0].sequence == 1
    assert _task_field_snapshot(cold.tasks[task.id]) == task_before


def _report_side_effect_snapshot(
    manager: WorkspaceManager,
    workspace_id: str,
    session: ManagedSession,
    task: WorkspaceTask,
) -> dict[str, object]:
    return {
        "session_title": session.title,
        "session_task_id": session.task_id,
        "session_current_task_id": session.current_task_id,
        "session_report_call_ids": dict(session.report_call_ids),
        "task_session_id": task.session_id,
        "task_review_session_id": task.review_session_id,
        "reports": set(manager.reports),
        "mailbox": list(_mailbox_events(manager, workspace_id)),
        "call_index": dict(manager.task_mailbox._call_index.get(workspace_id, {})),
    }


def _progress_payload(task_id: str, *, call_id: str | None = None) -> AgentReportCreate:
    return AgentReportCreate(
        task_id=task_id,
        state=AgentReportState.WORKING,
        message="assignment-guard progress",
        call_id=call_id or f"{task_id}-working-progress-cycle-1-1",
    )


@pytest.mark.asyncio
async def test_unassigned_worker_report_is_rejected_without_side_effects(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id, task_id="task-unassigned")
    manager.tasks[task.id] = task.model_copy(update={"session_id": None})
    baseline = _report_side_effect_snapshot(manager, workspace_id, session, manager.tasks[task.id])
    _wm.ttyd_manager.update_tab.reset_mock()

    with pytest.raises(RuntimeError, match="no assigned worker session"):
        await manager.create_report(session.id, _progress_payload(task.id))

    assert (
        _report_side_effect_snapshot(
            manager, workspace_id, manager.sessions[session.id], manager.tasks[task.id]
        )
        == baseline
    )
    assert _wm.ttyd_manager.update_tab.await_count == 0


@pytest.mark.asyncio
async def test_wrong_worker_report_is_rejected_without_side_effects(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    assigned_id = "session-assigned"
    intruder_id = "session-intruder"
    task, assigned = _task_session(
        manager, workspace_id, task_id="task-assigned", session_id=assigned_id
    )
    now = datetime.utcnow()
    intruder = ManagedSession(
        id=intruder_id,
        workspace_id=workspace_id,
        tab_id=f"tab-{intruder_id}",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=task.title,
        workspace_path="/tmp",
        tmux_session=f"tmux-{intruder_id}",
        target=ExecutionTarget.LOCAL,
        task_id=task.id,
        current_task_id=task.id,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[intruder.id] = intruder
    baseline = _report_side_effect_snapshot(manager, workspace_id, intruder, task)
    _wm.ttyd_manager.update_tab.reset_mock()

    with pytest.raises(RuntimeError, match="not the current worker"):
        await manager.create_report(intruder.id, _progress_payload(task.id))

    assert (
        _report_side_effect_snapshot(
            manager, workspace_id, manager.sessions[intruder.id], manager.tasks[task.id]
        )
        == baseline
    )
    assert _wm.ttyd_manager.update_tab.await_count == 0


@pytest.mark.asyncio
async def test_exact_worker_report_is_allowed(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id, task_id="task-exact-worker")
    payload = _progress_payload(task.id, call_id=f"{task.id}-exact-worker-progress")

    report = await manager.create_report(session.id, payload)
    assert report.task_id == task.id
    assert len(_report_events(manager, workspace_id, report.id)) == 1


@pytest.mark.asyncio
async def test_exact_reviewer_report_is_allowed_wrong_reviewer_rejected(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    now = datetime.utcnow()
    task, worker = _task_session(
        manager,
        workspace_id,
        task_id="task-review-guard",
        session_id="session-worker-guard",
        status=WorkspaceTaskStatus.REVIEW,
        review_cycle=2,
    )
    reviewer = ManagedSession(
        id="session-reviewer-guard",
        workspace_id=workspace_id,
        tab_id="tab-reviewer-guard",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=task.title,
        workspace_path="/tmp",
        tmux_session="tmux-reviewer-guard",
        target=ExecutionTarget.LOCAL,
        task_id=task.id,
        current_task_id=task.id,
        created_at=now,
        updated_at=now,
    )
    wrong_reviewer = reviewer.model_copy(
        update={"id": "session-reviewer-wrong", "tab_id": "tab-reviewer-wrong"}
    )
    manager.sessions[reviewer.id] = reviewer
    manager.sessions[wrong_reviewer.id] = wrong_reviewer
    manager.tasks[task.id] = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.REVIEW,
            "review_session_id": reviewer.id,
            "review_requested_at": now,
        }
    )
    task = manager.tasks[task.id]
    baseline = _report_side_effect_snapshot(manager, workspace_id, wrong_reviewer, task)
    _wm.ttyd_manager.update_tab.reset_mock()

    with pytest.raises(RuntimeError, match="not the current reviewer"):
        await manager.create_report(
            wrong_reviewer.id,
            AgentReportCreate(
                task_id=task.id,
                state=AgentReportState.REVIEW_STARTED,
                message="wrong reviewer",
                call_id=f"{task.id}-review-started-cycle-2-1",
            ),
        )

    assert (
        _report_side_effect_snapshot(
            manager, workspace_id, manager.sessions[wrong_reviewer.id], manager.tasks[task.id]
        )
        == baseline
    )
    assert _wm.ttyd_manager.update_tab.await_count == 0

    passed = await manager.create_report(
        reviewer.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.REVIEW_PASSED,
            message="exact reviewer",
            call_id=f"{task.id}-review-passed-cycle-2-1",
        ),
    )
    assert len(_report_events(manager, workspace_id, passed.id)) == 1


@pytest.mark.asyncio
async def test_committed_report_idempotent_replay_survives_unassigned_task(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id, task_id="task-replay-unassigned")
    payload = _progress_payload(task.id, call_id=f"{task.id}-replay-unassigned")

    first = await manager.create_report(session.id, payload)
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(update={"session_id": None})
    rebound = manager.sessions[session.id]
    _wm.ttyd_manager.update_tab.reset_mock()

    retry = await manager.create_report(session.id, payload)
    assert retry.id == first.id
    assert len(_report_events(manager, workspace_id, first.id)) == 1
    assert _wm.ttyd_manager.update_tab.await_count == 0
    assert manager.sessions[session.id].title == rebound.title
    assert manager.sessions[session.id].task_id == rebound.task_id
