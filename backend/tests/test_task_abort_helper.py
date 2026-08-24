"""3b-2: Task abort is the only work-lifecycle interrupt mutation."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    ManualTaskControlRequest,
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

    fake_tab = MagicMock(id="tab-mock", tmux_session="tmux-mock")
    monkeypatch.setattr(_wm.ttyd_manager, "update_tab", AsyncMock(return_value=fake_tab))
    monkeypatch.setattr(
        _wm.ttyd_manager, "ensure_tab_tmux_session", AsyncMock(return_value=fake_tab)
    )
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
        WorkspaceCreate(name="abort helper", path=str(repo), target=ExecutionTarget.LOCAL)
    )
    return manager, workspace.id


def _session(
    manager: WorkspaceManager,
    workspace_id: str,
    *,
    session_id: str,
    task_id: str,
    role: WorkspaceSessionRole = WorkspaceSessionRole.WORKER,
    ephemeral: bool = False,
) -> ManagedSession:
    now = datetime.utcnow()
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id=f"tab-{session_id}",
        role=role,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        title="worker",
        workspace_path="/tmp",
        tmux_session=f"tmux-{session_id}",
        target=ExecutionTarget.LOCAL,
        task_id=task_id or None,
        current_task_id=task_id or None,
        ephemeral=ephemeral,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    return session


def _working_task(
    manager: WorkspaceManager,
    workspace_id: str,
    *,
    task_id: str = "task-abort-working",
    status: WorkspaceTaskStatus = WorkspaceTaskStatus.WORKING,
    review_cycle: int = 2,
    session_id: str = "session-worker",
    system_internal: bool = False,
    internal_kind: str | None = None,
) -> WorkspaceTask:
    now = datetime.utcnow()
    task = WorkspaceTask(
        id=task_id,
        workspace_id=workspace_id,
        title="abort working",
        prompt="do the work",
        agent_type=AgentType.CLAUDE,
        status=status,
        session_id=session_id,
        review_cycle=review_cycle,
        system_internal=system_internal,
        internal_kind=internal_kind,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    if session_id:
        _session(manager, workspace_id, session_id=session_id, task_id=task.id)
    return manager.tasks[task.id]


def _mailbox_events(manager: WorkspaceManager, workspace_id: str) -> list:
    return list(manager.task_mailbox._events.get(workspace_id, []))


def _workspace_fact_snapshot(manager: WorkspaceManager, workspace_id: str) -> dict[str, object]:
    mailbox = manager.task_mailbox
    return {
        "reports": {
            key: value.model_dump(mode="json")
            for key, value in manager.reports.items()
            if value.workspace_id == workspace_id
        },
        "sessions": {
            key: value.model_dump(mode="json")
            for key, value in manager.sessions.items()
            if value.workspace_id == workspace_id
        },
        "tasks": {
            key: value.model_dump(mode="json")
            for key, value in manager.tasks.items()
            if value.workspace_id == workspace_id
        },
        "mailbox_events": [
            item.model_dump(mode="json") for item in mailbox._events.get(workspace_id, [])
        ],
        "mailbox_call_ids": sorted(mailbox._call_index.get(workspace_id, {})),
        "mailbox_next": mailbox._next_seq.get(workspace_id),
    }


def _abort_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"actor_role": TaskActorRole.HUMAN}
    payload.update(overrides)
    return payload


def _assert_single_abort_link(
    manager: WorkspaceManager,
    workspace_id: str,
    task_id: str,
    call_id: str,
) -> None:
    events = [item for item in _mailbox_events(manager, workspace_id) if item.task_id == task_id]
    assert [item.type for item in events] == [TaskEventType.ABORT]
    event = events[0]
    assert event.call_id == call_id
    assert event.report_id
    report = manager.reports.get(event.report_id)
    assert report is not None
    assert report.task_id == task_id
    assert report.id == event.report_id


def _install_side_effect_spies(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> dict[str, list[str]]:
    interrupted: list[str] = []

    async def _interrupt(session: ManagedSession) -> None:
        interrupted.append(session.id)

    monkeypatch.setattr(manager, "_interrupt_session", _interrupt)
    monkeypatch.setattr(manager, "dispatch_workspace", AsyncMock())
    return {"interrupted": interrupted}


@pytest.mark.asyncio
async def test_abort_task_working_writes_one_event_and_clears_assignment(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _working_task(manager, workspace_id)
    spies = _install_side_effect_spies(manager, monkeypatch)
    saves: list[int] = []
    real_save = manager._save_state

    def _counting_save() -> None:
        saves.append(len(_mailbox_events(manager, workspace_id)))
        real_save()

    monkeypatch.setattr(manager, "_save_state", _counting_save)

    aborted = await manager.abort_task(
        task.id,
        ManualTaskControlRequest(reason="stop the work", call_id="abort-working-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    assert aborted.status == WorkspaceTaskStatus.TODO
    assert aborted.session_id is None
    assert aborted.manual_abort_reason == "stop the work"
    assert aborted.review_cycle == 2
    events = _mailbox_events(manager, workspace_id)
    assert len(events) == 1
    event = events[0]
    assert event.type == TaskEventType.ABORT
    assert event.action == "abort"
    assert event.call_id == "abort-working-1"
    assert event.task_id == task.id
    assert event.actor_session_id is None
    assert event.actor_role == TaskActorRole.HUMAN
    assert event.review_cycle == 2
    assert event.payload.get("reason") == "stop the work"
    _assert_single_abort_link(manager, workspace_id, task.id, "abort-working-1")
    reports = [item for item in manager.reports.values() if item.task_id == task.id]
    assert len(reports) == 1
    assert reports[0].state == AgentReportState.BLOCKED
    assert reports[0].id == event.report_id
    assert event.report_id == manager._abort_report_id(workspace_id, "abort-working-1", None)
    worker = manager.sessions["session-worker"]
    assert worker.task_id is None
    assert worker.current_task_id is None
    assert worker.status == ManagedSessionStatus.IDLE
    assert worker.status != ManagedSessionStatus.STOPPED
    assert spies["interrupted"] == ["session-worker"]
    assert saves == [1]


@pytest.mark.asyncio
async def test_abort_task_same_call_id_replay_after_todo_and_cold_reload(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _working_task(manager, workspace_id)
    spies = _install_side_effect_spies(manager, monkeypatch)
    first = await manager.abort_task(
        task.id,
        ManualTaskControlRequest(reason="stop the work", call_id="abort-replay-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    first_event = _mailbox_events(manager, workspace_id)[0]
    first_report_ids = {item.id for item in manager.reports.values() if item.task_id == task.id}
    spies["interrupted"].clear()

    replay = await manager.abort_task(
        task.id,
        ManualTaskControlRequest(reason="stop the work", call_id="abort-replay-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    assert replay.status == WorkspaceTaskStatus.TODO
    assert replay.manual_aborted_at == first.manual_aborted_at
    assert [item.call_id for item in _mailbox_events(manager, workspace_id)] == ["abort-replay-1"]
    assert {item.id for item in manager.reports.values() if item.task_id == task.id} == (
        first_report_ids
    )
    assert spies["interrupted"] == []

    cold = WorkspaceManager()
    monkeypatch.setattr(cold, "_interrupt_session", AsyncMock())
    monkeypatch.setattr(cold, "dispatch_workspace", AsyncMock())
    cold_retry = await cold.abort_task(
        task.id,
        ManualTaskControlRequest(reason="stop the work", call_id="abort-replay-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    assert cold_retry.status == WorkspaceTaskStatus.TODO
    cold_events = _mailbox_events(cold, workspace_id)
    assert len(cold_events) == 1
    assert cold_events[0].sequence == first_event.sequence
    assert cold_events[0].call_id == first_event.call_id
    assert cold_events[0].report_id == first_event.report_id
    assert {item.id for item in cold.reports.values() if item.task_id == task.id} == (
        first_report_ids
    )
    _assert_single_abort_link(cold, workspace_id, task.id, "abort-replay-1")


@pytest.mark.asyncio
async def test_abort_task_conflict_different_reason_or_target(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _working_task(manager, workspace_id)
    other = _working_task(
        manager, workspace_id, task_id="task-abort-other", session_id="session-other"
    )
    spies = _install_side_effect_spies(manager, monkeypatch)
    await manager.abort_task(
        task.id,
        ManualTaskControlRequest(reason="stop the work", call_id="abort-conflict-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    before = _workspace_fact_snapshot(manager, workspace_id)
    spies["interrupted"].clear()

    with pytest.raises(ValueError, match="already used"):
        await manager.abort_task(
            task.id,
            ManualTaskControlRequest(reason="a different reason", call_id="abort-conflict-1"),
            workspace_id=workspace_id,
            **_abort_kwargs(),
        )
    with pytest.raises(ValueError, match="already used"):
        await manager.abort_task(
            other.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-conflict-1"),
            workspace_id=workspace_id,
            **_abort_kwargs(),
        )
    assert _workspace_fact_snapshot(manager, workspace_id) == before
    assert spies["interrupted"] == []
    assert manager.tasks[other.id].status == WorkspaceTaskStatus.WORKING
    assert manager.tasks[other.id].session_id == "session-other"


@pytest.mark.asyncio
async def test_abort_task_rejects_wrong_workspace_authority_and_terminal(
    manager_and_workspace: tuple[WorkspaceManager, str],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    (tmp_path / "other-repo").mkdir()
    other = manager.create_workspace(
        WorkspaceCreate(
            name="other abort workspace",
            path=str(tmp_path / "other-repo"),
            target=ExecutionTarget.LOCAL,
        )
    )
    working = _working_task(manager, workspace_id, task_id="task-abort-guards")
    done = _working_task(
        manager,
        workspace_id,
        task_id="task-abort-done",
        status=WorkspaceTaskStatus.DONE,
        session_id="session-done",
    )
    todo = _working_task(
        manager,
        workspace_id,
        task_id="task-abort-todo",
        status=WorkspaceTaskStatus.TODO,
        session_id="session-todo",
    )
    spies = _install_side_effect_spies(manager, monkeypatch)
    before = _workspace_fact_snapshot(manager, workspace_id)

    with pytest.raises(KeyError):
        await manager.abort_task(
            working.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-ws-1"),
            workspace_id=other.id,
            **_abort_kwargs(),
        )
    with pytest.raises(ValueError, match="supervisor or human"):
        await manager.abort_task(
            working.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-auth-1"),
            workspace_id=workspace_id,
            actor_role=TaskActorRole.WORKER,
        )
    with pytest.raises(ValueError, match="must name a session"):
        await manager.abort_task(
            working.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-auth-session"),
            workspace_id=workspace_id,
            actor_session_id="session-supervisor",
            actor_role=TaskActorRole.HUMAN,
        )
    with pytest.raises(RuntimeError, match="Only queued, working, or review"):
        await manager.abort_task(
            done.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-done-1"),
            workspace_id=workspace_id,
            **_abort_kwargs(),
        )
    with pytest.raises(RuntimeError, match="Only queued, working, or review"):
        await manager.abort_task(
            todo.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-todo-1"),
            workspace_id=workspace_id,
            **_abort_kwargs(),
        )
    assert _workspace_fact_snapshot(manager, workspace_id) == before
    assert spies["interrupted"] == []
    assert manager.tasks[working.id].status == WorkspaceTaskStatus.WORKING


@pytest.mark.asyncio
async def test_abort_task_persist_fault_zero_write_then_cold_retry(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _working_task(manager, workspace_id)
    spies = _install_side_effect_spies(manager, monkeypatch)
    manager._save_state()
    before = _workspace_fact_snapshot(manager, workspace_id)
    monkeypatch.setattr(
        manager,
        "_save_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        await manager.abort_task(
            task.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-persist-1"),
            workspace_id=workspace_id,
            **_abort_kwargs(),
        )
    assert _workspace_fact_snapshot(manager, workspace_id) == before
    assert spies["interrupted"] == []
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    assert manager.tasks[task.id].session_id == "session-worker"

    cold = WorkspaceManager()
    assert _mailbox_events(cold, workspace_id) == []
    assert cold.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    spies_cold = _install_side_effect_spies(cold, monkeypatch)
    aborted = await cold.abort_task(
        task.id,
        ManualTaskControlRequest(reason="stop the work", call_id="abort-persist-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    assert aborted.status == WorkspaceTaskStatus.TODO
    assert [item.call_id for item in _mailbox_events(cold, workspace_id)] == ["abort-persist-1"]
    _assert_single_abort_link(cold, workspace_id, task.id, "abort-persist-1")
    assert spies_cold["interrupted"] == ["session-worker"]


@pytest.mark.asyncio
async def test_abort_task_session_interrupt_failure_keeps_commit(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _working_task(manager, workspace_id)
    monkeypatch.setattr(manager, "dispatch_workspace", AsyncMock())

    async def _boom(session: ManagedSession) -> None:
        raise RuntimeError("tmux interrupt failed")

    monkeypatch.setattr(manager, "_interrupt_session", _boom)
    aborted = await manager.abort_task(
        task.id,
        ManualTaskControlRequest(reason="stop the work", call_id="abort-interrupt-fail"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    assert aborted.status == WorkspaceTaskStatus.TODO
    assert [item.call_id for item in _mailbox_events(manager, workspace_id)] == [
        "abort-interrupt-fail"
    ]
    worker = manager.sessions["session-worker"]
    assert worker.status == ManagedSessionStatus.IDLE
    assert worker.status != ManagedSessionStatus.STOPPED

    fresh = WorkspaceManager()
    assert fresh.tasks[task.id].status == WorkspaceTaskStatus.TODO
    _assert_single_abort_link(fresh, workspace_id, task.id, "abort-interrupt-fail")


@pytest.mark.asyncio
async def test_abort_queued_review_and_feedback_reaper(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    queued = _working_task(
        manager,
        workspace_id,
        task_id="task-abort-queued",
        status=WorkspaceTaskStatus.QUEUED,
        session_id="session-queued",
    )
    review = _working_task(
        manager,
        workspace_id,
        task_id="task-abort-review",
        status=WorkspaceTaskStatus.REVIEW,
        session_id="session-review-worker",
    )
    reviewer = _session(
        manager,
        workspace_id,
        session_id="session-reviewer",
        task_id=review.id,
        role=WorkspaceSessionRole.REVIEWER,
        ephemeral=True,
    )
    manager.tasks[review.id] = manager.tasks[review.id].model_copy(
        update={"review_session_id": reviewer.id}
    )
    reaper = _working_task(
        manager,
        workspace_id,
        task_id="task-abort-reaper",
        session_id="session-reaper",
        system_internal=True,
        internal_kind="feedback_reaper",
    )
    abandon = MagicMock()
    monkeypatch.setattr(manager, "_feedback_store", lambda: MagicMock(abandon_summary_run=abandon))
    spies = _install_side_effect_spies(manager, monkeypatch)

    queued_aborted = await manager.abort_task(
        queued.id,
        ManualTaskControlRequest(reason="drop queued", call_id="abort-queued-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    review_aborted = await manager.abort_task(
        review.id,
        ManualTaskControlRequest(reason="drop review", call_id="abort-review-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    reaper_aborted = await manager.abort_task(
        reaper.id,
        ManualTaskControlRequest(reason="drop reaper", call_id="abort-reaper-1"),
        workspace_id=workspace_id,
        **_abort_kwargs(),
    )
    assert queued_aborted.status == WorkspaceTaskStatus.TODO
    assert review_aborted.status == WorkspaceTaskStatus.TODO
    assert review_aborted.review_session_id is None
    assert reviewer.id not in manager.sessions
    assert reaper_aborted.status == WorkspaceTaskStatus.DONE
    assert reaper_aborted.completed_at is not None
    assert abandon.called
    assert [item.call_id for item in _mailbox_events(manager, workspace_id)] == [
        "abort-queued-1",
        "abort-review-1",
        "abort-reaper-1",
    ]
    assert "session-queued" in spies["interrupted"]
    assert "session-review-worker" in spies["interrupted"]
    assert "session-reviewer" in spies["interrupted"]
    _assert_single_abort_link(manager, workspace_id, queued.id, "abort-queued-1")
    _assert_single_abort_link(manager, workspace_id, review.id, "abort-review-1")
    _assert_single_abort_link(manager, workspace_id, reaper.id, "abort-reaper-1")


@pytest.mark.asyncio
async def test_abort_foreign_call_id_conflict_restores_snapshot(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _working_task(manager, workspace_id, task_id="task-abort-foreign")
    spies = _install_side_effect_spies(manager, monkeypatch)
    manager.task_mailbox._call_index[workspace_id] = {
        "abort-foreign-1": {
            "action": "followup",
            "target": "run-other",
            "fingerprint": "other-fp",
            "event": None,
        }
    }
    before = _workspace_fact_snapshot(manager, workspace_id)

    with pytest.raises(ValueError, match="already used"):
        await manager.abort_task(
            task.id,
            ManualTaskControlRequest(reason="stop the work", call_id="abort-foreign-1"),
            workspace_id=workspace_id,
            **_abort_kwargs(),
        )

    assert _workspace_fact_snapshot(manager, workspace_id) == before
    assert spies["interrupted"] == []
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    assert manager.tasks[task.id].session_id == "session-worker"
    assert not any(item.task_id == task.id for item in manager.reports.values())
