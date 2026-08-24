"""Adversarial report-intake transaction and prompt call-id tests."""

from __future__ import annotations

import asyncio
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
    ContinueTaskRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.workspace_manager import WorkspaceManager
from claude_hub.services.workspace_manager._reports import ReportCallIdConflict

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
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message", AsyncMock())
    monkeypatch.setattr(_wm.WorkspaceManager, "send_session_message", AsyncMock())
    yield root


@pytest.fixture()
def manager_and_workspace(state_root: Path, tmp_path: Path) -> tuple[WorkspaceManager, str]:
    manager = WorkspaceManager()
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = manager.create_workspace(
        WorkspaceCreate(name="report atomicity", path=str(repo), target=ExecutionTarget.LOCAL)
    )
    return manager, workspace.id


def _task_session(
    manager: WorkspaceManager,
    workspace_id: str,
    *,
    task_id: str = "task-report",
    session_id: str = "session-report",
) -> tuple[WorkspaceTask, ManagedSession]:
    now = datetime.utcnow()
    task = WorkspaceTask(
        id=task_id,
        workspace_id=workspace_id,
        title="report atomicity",
        prompt="implement safely",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=session_id,
        created_at=now,
        updated_at=now,
    )
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id=f"tab-{session_id}",
        role=WorkspaceSessionRole.WORKER,
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


@pytest.mark.asyncio
async def test_distinct_call_ids_are_serialized_and_survive_reload(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)

    async def submit(index: int) -> AgentReport:
        return await manager.create_report(
            session.id,
            AgentReportCreate(
                task_id=task.id,
                state=AgentReportState.WORKING,
                message=f"progress {index}",
                call_id=f"{task.id}-working-progress-cycle-1-{index}",
            ),
        )

    first, second = await asyncio.gather(submit(1), submit(2))
    assert first.id != second.id
    mapping = manager.sessions[session.id].report_call_ids
    assert mapping[first.call_id] == first.id
    assert mapping[second.call_id] == second.id

    fresh = WorkspaceManager()
    fresh_mapping = fresh.sessions[session.id].report_call_ids
    assert fresh_mapping[first.call_id] == first.id
    assert fresh_mapping[second.call_id] == second.id
    assert {first.id, second.id} <= set(fresh.reports)


async def _install_two_processing_followups(
    manager: WorkspaceManager,
    workspace_id: str,
    task: WorkspaceTask,
    session: ManagedSession,
) -> tuple[list[str], str]:
    call_ids = ["followup-report-intake-1", "followup-report-intake-2"]
    for call_id in call_ids:
        await manager.followup_task(
            workspace_id,
            task.id,
            f"process {call_id}",
            call_id,
            actor_session_id="session-supervisor",
            actor_role=TaskActorRole.SUPERVISOR,
        )
    pending_messages = {call_id: f"process {call_id}" for call_id in call_ids}
    manager.sessions[session.id] = manager.sessions[session.id].model_copy(
        update={
            "processing_call_ids": call_ids,
            "pending_messages": pending_messages,
        }
    )
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(
        update={"processing_call_ids": call_ids}
    )
    manager._save_state()
    return call_ids, task.id


@pytest.mark.asyncio
async def test_precommit_failure_rolls_back_both_acks_and_cold_retry_converges(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    call_ids, bound_task_id = await _install_two_processing_followups(
        manager, workspace_id, task, session
    )
    baseline_task = manager.tasks[bound_task_id].model_copy()
    state_file = manager._workspace_state_file(workspace_id)
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="ack both",
        call_id=f"{task.id}-working-progress-cycle-1-1",
        acked_call_ids=call_ids,
    )

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
            raise OSError("pre-state atomic replace failed")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_target_state_once)
    with pytest.raises(OSError, match="pre-state"):
        await manager.create_report(session.id, payload)

    rolled_back = manager.sessions[session.id]
    assert set(rolled_back.processing_call_ids) == set(call_ids)
    assert not (set(rolled_back.delivered_call_ids) & set(call_ids))
    assert payload.call_id not in rolled_back.report_call_ids
    assert staged_report_id is not None
    assert staged_report_id not in manager.reports
    assert manager.task_mailbox._call_record(workspace_id, f"report:{staged_report_id}") is None
    assert manager.tasks[bound_task_id].model_dump() == baseline_task.model_dump()
    for call_id in call_ids:
        assert manager.task_mailbox._call_record(workspace_id, call_id) is not None
        assert call_id in manager.sessions[session.id].processing_call_ids
        assert call_id not in manager.sessions[session.id].delivered_call_ids

    # Same-process retry commits exactly one report and both ACK lifecycle
    # mutations. TaskMailbox._persist is deliberately disabled to prove no
    # nested ACK persist remains inside the transaction.
    original_persist = manager.task_mailbox._persist

    def reject_nested_persist() -> None:
        if manager._report_intake_workspace.get() is not None:
            raise AssertionError("nested TaskMailbox persist")
        original_persist()

    monkeypatch.setattr(manager.task_mailbox, "_persist", reject_nested_persist)
    committed = await manager.create_report(session.id, payload)
    assert set(call_ids) <= set(manager.sessions[session.id].delivered_call_ids)
    assert manager.tasks[bound_task_id].status == WorkspaceTaskStatus.WORKING
    for call_id in call_ids:
        assert manager.task_mailbox._call_record(workspace_id, call_id) is not None

    # Cold reload + retry of the identical call_id/payload converges to the
    # same report and does not duplicate followup bridge events.
    fresh = WorkspaceManager()
    retry = await fresh.create_report(session.id, payload)
    assert retry.id == committed.id
    assert fresh.tasks[bound_task_id].status == WorkspaceTaskStatus.WORKING
    assert (
        len([report for report in fresh.reports.values() if report.call_id == payload.call_id]) == 1
    )
    for call_id in call_ids:
        assert (
            len(
                [
                    event
                    for event in fresh.task_mailbox._events.get(workspace_id, [])
                    if event.call_id == call_id
                ]
            )
            == 1
        )
        assert call_id in fresh.sessions[session.id].delivered_call_ids
    report_bridge_call_id = f"report:{committed.id}"
    assert (
        len(
            [
                event
                for event in fresh.task_mailbox._events.get(workspace_id, [])
                if event.call_id == report_bridge_call_id
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_report_rollback_preserves_concurrent_mailbox_write(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    """Reproduce: TaskMailbox persist during rename await must survive rollback."""

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    manager.sessions[session.id] = session.model_copy(update={"title": "stale title"})
    concurrent_call_id = "concurrent-mailbox-write-during-rename"
    rename_started = asyncio.Event()
    concurrent_done = asyncio.Event()
    fake_tab = MagicMock(id=session.tab_id, tmux_session=session.tmux_session)

    async def slow_update_tab(tab_id: str, name: str | None = None, **_kwargs: object):
        rename_started.set()
        await concurrent_done.wait()
        return fake_tab

    monkeypatch.setattr(_wm.ttyd_manager, "update_tab", slow_update_tab)

    original_write = manager._atomic_write_text
    failed = False
    state_file = manager._workspace_state_file(workspace_id)

    def fail_report_commit_once(path: Path, text: str) -> None:
        nonlocal failed
        if path == state_file and concurrent_done.is_set() and not failed:
            failed = True
            raise OSError("pre-commit fail after concurrent mailbox write")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_report_commit_once)

    async def concurrent_mailbox_write() -> None:
        await rename_started.wait()
        manager.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.SUPERVISOR,
            event_type=TaskEventType.MESSAGE,
            call_id=concurrent_call_id,
            action="followup",
            target=task.id,
            actor_session_id="session-supervisor",
            payload={"message": "must survive report rollback"},
            persist=True,
        )
        concurrent_done.set()

    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="rollback must not erase mailbox",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    writer = asyncio.create_task(concurrent_mailbox_write())
    with pytest.raises(OSError, match="pre-commit fail after concurrent mailbox write"):
        await manager.create_report(session.id, payload)
    await writer

    assert manager.task_mailbox._call_record(workspace_id, concurrent_call_id) is not None
    assert payload.call_id not in manager.sessions[session.id].report_call_ids
    assert not any(report.call_id == payload.call_id for report in manager.reports.values())

    fresh = WorkspaceManager()
    assert fresh.task_mailbox._call_record(workspace_id, concurrent_call_id) is not None
    assert payload.call_id not in fresh.sessions[session.id].report_call_ids


@pytest.mark.asyncio
async def test_report_rollback_serializes_concurrent_followup_task(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    """Task followup writes wait on the report workspace lock."""

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    manager.sessions[session.id] = session.model_copy(update={"title": "stale title"})
    followup_call_id = "serialized-followup-during-report-rollback"
    rename_started = asyncio.Event()
    rename_calls = 0
    fake_tab = MagicMock(id=session.tab_id, tmux_session=session.tmux_session)

    async def slow_update_tab(tab_id: str, name: str | None = None, **_kwargs: object):
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            rename_started.set()
            await asyncio.sleep(0.05)
            assert manager.task_mailbox._call_record(workspace_id, followup_call_id) is None
        return fake_tab

    monkeypatch.setattr(_wm.ttyd_manager, "update_tab", slow_update_tab)

    original_write = manager._atomic_write_text
    failed = False
    state_file = manager._workspace_state_file(workspace_id)

    def fail_report_commit_once(path: Path, text: str) -> None:
        nonlocal failed
        if path == state_file and rename_started.is_set() and not failed:
            failed = True
            raise OSError("pre-commit fail while followup waits on workspace lock")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_report_commit_once)

    async def concurrent_followup() -> object:
        await rename_started.wait()
        return await manager.followup_task(
            workspace_id,
            task.id,
            "must wait for report rollback",
            followup_call_id,
            actor_session_id="session-supervisor",
            actor_role=TaskActorRole.SUPERVISOR,
        )

    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="rollback must serialize followup",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    writer = asyncio.create_task(concurrent_followup())
    with pytest.raises(OSError, match="pre-commit fail while followup waits on workspace lock"):
        await manager.create_report(session.id, payload)
    event = await writer

    assert event.call_id == followup_call_id
    assert manager.task_mailbox._call_record(workspace_id, followup_call_id) is not None
    assert payload.call_id not in manager.sessions[session.id].report_call_ids
    assert not any(report.call_id == payload.call_id for report in manager.reports.values())

    fresh = WorkspaceManager()
    assert fresh.task_mailbox._call_record(workspace_id, followup_call_id) is not None
    assert payload.call_id not in fresh.sessions[session.id].report_call_ids


@pytest.mark.asyncio
async def test_reused_session_known_call_id_has_zero_side_effects(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    """Known call_ids must not rename or rebind a reused session."""

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="original report",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    first = await manager.create_report(session.id, payload)
    now = datetime.utcnow()
    new_task = WorkspaceTask(
        id="task-reassigned",
        workspace_id=workspace_id,
        title="reassigned task title",
        prompt="next assignment",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=session.id,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[new_task.id] = new_task
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(update={"session_id": None})
    manager.sessions[session.id] = manager.sessions[session.id].model_copy(
        update={
            "task_id": new_task.id,
            "current_task_id": new_task.id,
            "title": new_task.title,
            "updated_at": now,
        }
    )
    manager._save_state()
    rebound = manager.sessions[session.id]
    _wm.ttyd_manager.update_tab.reset_mock()

    def _assignment(sess: ManagedSession) -> tuple[str, str | None, str | None, datetime]:
        return sess.title, sess.task_id, sess.current_task_id, sess.updated_at

    with pytest.raises(ReportCallIdConflict, match="already used"):
        await manager.create_report(
            session.id,
            payload.model_copy(update={"message": "different payload"}),
        )
    assert _assignment(manager.sessions[session.id]) == _assignment(rebound)
    assert _wm.ttyd_manager.update_tab.await_count == 0

    retry = await manager.create_report(session.id, payload)
    assert retry.id == first.id
    assert _assignment(manager.sessions[session.id]) == _assignment(rebound)
    assert _wm.ttyd_manager.update_tab.await_count == 0

    fresh = WorkspaceManager()
    assert fresh.reports[first.id].id == first.id
    assert _assignment(fresh.sessions[session.id]) == _assignment(rebound)


@pytest.mark.asyncio
async def test_padded_call_id_retry_has_zero_side_effects_after_reassignment(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    """Leading/trailing call_id whitespace must alias before preflight/persist."""

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    canonical = f"{task.id}-working-progress-cycle-1-1"
    padded = f"  {canonical}\t"
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="original report",
        call_id=padded,
    )
    first = await manager.create_report(session.id, payload)
    assert first.call_id == canonical
    stored = manager.sessions[session.id]
    assert canonical in stored.report_call_ids
    assert padded not in stored.report_call_ids
    assert stored.report_call_ids[canonical] == first.id

    now = datetime.utcnow()
    new_task = WorkspaceTask(
        id="task-reassigned-padded",
        workspace_id=workspace_id,
        title="padded reassignment title",
        prompt="next assignment",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=session.id,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[new_task.id] = new_task
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(update={"session_id": None})
    manager.sessions[session.id] = manager.sessions[session.id].model_copy(
        update={
            "task_id": new_task.id,
            "current_task_id": new_task.id,
            "title": new_task.title,
            "updated_at": now,
        }
    )
    manager._save_state()
    rebound = manager.sessions[session.id]
    _wm.ttyd_manager.update_tab.reset_mock()

    def _assignment(sess: ManagedSession) -> tuple[str, str | None, str | None, datetime]:
        return sess.title, sess.task_id, sess.current_task_id, sess.updated_at

    with pytest.raises(ReportCallIdConflict, match="already used"):
        await manager.create_report(
            session.id,
            payload.model_copy(update={"message": "different payload", "call_id": padded}),
        )
    assert _assignment(manager.sessions[session.id]) == _assignment(rebound)
    assert _wm.ttyd_manager.update_tab.await_count == 0

    retry_padded = await manager.create_report(session.id, payload)
    assert retry_padded.id == first.id
    assert retry_padded.call_id == canonical
    assert _assignment(manager.sessions[session.id]) == _assignment(rebound)

    retry_canonical = await manager.create_report(
        session.id,
        payload.model_copy(update={"call_id": canonical}),
    )
    assert retry_canonical.id == first.id
    assert _assignment(manager.sessions[session.id]) == _assignment(rebound)
    assert _wm.ttyd_manager.update_tab.await_count == 0
    assert padded not in manager.sessions[session.id].report_call_ids

    fresh = WorkspaceManager()
    assert fresh.reports[first.id].call_id == canonical
    assert canonical in fresh.sessions[session.id].report_call_ids
    assert padded not in fresh.sessions[session.id].report_call_ids
    assert _assignment(fresh.sessions[session.id]) == _assignment(rebound)


@pytest.mark.asyncio
async def test_bound_replay_save_failure_rolls_back_ack_and_cold_retry_converges(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    """Matching-call replay must snapshot/restore so a failed save cannot ACK."""

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    call_ids = ["followup-report-intake-1", "followup-report-intake-2"]
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="ack both",
        call_id=f"{task.id}-working-progress-cycle-1-1",
        acked_call_ids=call_ids,
    )
    first = await manager.create_report(session.id, payload)
    installed, bound_task_id = await _install_two_processing_followups(
        manager, workspace_id, task, session
    )
    assert installed == call_ids
    live = manager.sessions[session.id]
    assert set(live.processing_call_ids) == set(call_ids)
    for call_id in call_ids:
        assert live.pending_messages[call_id] == f"process {call_id}"
    bridge_call_id = f"report:{first.id}"

    def _call_ids(mgr: WorkspaceManager) -> list[str]:
        return [event.call_id for event in mgr.task_mailbox._events.get(workspace_id, [])]

    assert _call_ids(manager).count(bridge_call_id) == 1

    state_file = manager._workspace_state_file(workspace_id)
    original_write = manager._atomic_write_text
    failed = False

    def fail_replay_save_once(path: Path, text: str) -> None:
        nonlocal failed
        if path == state_file and not failed:
            failed = True
            raise OSError("replay save failed")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_replay_save_once)
    with pytest.raises(OSError, match="replay save failed"):
        await manager.create_report(session.id, payload)

    rolled_back = manager.sessions[session.id]
    assert set(rolled_back.processing_call_ids) == set(call_ids)
    assert not (set(rolled_back.delivered_call_ids) & set(call_ids))
    for call_id in call_ids:
        assert rolled_back.pending_messages[call_id] == f"process {call_id}"
        assert call_id not in rolled_back.delivered_call_ids
    assert manager.task_mailbox._call_record(workspace_id, bridge_call_id) is not None
    assert _call_ids(manager).count(bridge_call_id) == 1

    cold = WorkspaceManager()
    cold_session = cold.sessions[session.id]
    assert set(cold_session.processing_call_ids) == set(call_ids)
    assert not (set(cold_session.delivered_call_ids) & set(call_ids))
    for call_id in call_ids:
        assert cold_session.pending_messages[call_id] == f"process {call_id}"
        assert call_id not in cold_session.delivered_call_ids
    assert cold.reports[first.id].id == first.id
    assert cold.task_mailbox._call_record(workspace_id, bridge_call_id) is not None
    assert _call_ids(cold).count(bridge_call_id) == 1

    retry = await cold.create_report(session.id, payload)
    assert retry.id == first.id
    committed = cold.sessions[session.id]
    assert set(call_ids) <= set(committed.delivered_call_ids)
    assert not (set(committed.processing_call_ids) & set(call_ids))
    for call_id in call_ids:
        assert call_id not in committed.pending_messages
        assert call_id in committed.delivered_call_ids
    assert cold.task_mailbox._call_record(workspace_id, bridge_call_id) is not None
    assert _call_ids(cold).count(bridge_call_id) == 1
    assert cold.tasks[bound_task_id].status == WorkspaceTaskStatus.WORKING

    fresh = WorkspaceManager()
    assert fresh.reports[first.id].id == first.id
    assert set(call_ids) <= set(fresh.sessions[session.id].delivered_call_ids)
    for call_id in call_ids:
        assert call_id not in fresh.sessions[session.id].pending_messages
        assert call_id in fresh.sessions[session.id].delivered_call_ids
    assert fresh.task_mailbox._call_record(workspace_id, bridge_call_id) is not None
    assert _call_ids(fresh).count(bridge_call_id) == 1


@pytest.mark.asyncio
async def test_predecessor_padded_call_id_post_save_failure_converges(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    """Legacy padded stored call_ids must use the same commit token as preflight."""

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    call_ids = ["followup-report-intake-1", "followup-report-intake-2"]
    canonical = f"{task.id}-working-progress-cycle-1-1"
    padded = f"  {canonical}\t"
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="ack both",
        call_id=canonical,
        acked_call_ids=call_ids,
    )
    first = await manager.create_report(session.id, payload)
    installed, bound_task_id = await _install_two_processing_followups(
        manager, workspace_id, task, session
    )
    assert installed == call_ids

    live = manager.sessions[session.id]
    report_call_ids = dict(live.report_call_ids)
    report_id = report_call_ids.pop(canonical)
    report_call_ids[padded] = report_id
    fingerprints = dict(live.report_call_fingerprints)
    fingerprint = fingerprints.pop(canonical)
    fingerprints[padded] = fingerprint
    manager.sessions[session.id] = live.model_copy(
        update={
            "report_call_ids": report_call_ids,
            "report_call_fingerprints": fingerprints,
        }
    )
    manager.reports[first.id] = manager.reports[first.id].model_copy(update={"call_id": padded})
    manager._save_state()

    original_wake = manager._wake_report_intake_runs
    wakes = {"n": 0}

    def fail_wake_once(targets: set[tuple[str, str]]) -> None:
        wakes["n"] += 1
        if wakes["n"] == 1:
            raise OSError("post-save wake failed")
        original_wake(targets)

    monkeypatch.setattr(manager, "_wake_report_intake_runs", fail_wake_once)
    with pytest.raises(OSError, match="post-save wake failed"):
        await manager.create_report(session.id, payload)

    live_after = manager.sessions[session.id]
    assert set(call_ids) <= set(live_after.delivered_call_ids)
    assert not (set(live_after.processing_call_ids) & set(call_ids))
    for call_id in call_ids:
        assert call_id not in live_after.pending_messages
        assert call_id in live_after.delivered_call_ids
    bridge_call_id = f"report:{first.id}"
    assert manager.task_mailbox._call_record(workspace_id, bridge_call_id) is not None

    cold = WorkspaceManager()
    cold_session = cold.sessions[session.id]
    assert set(call_ids) <= set(cold_session.delivered_call_ids)
    assert not (set(cold_session.processing_call_ids) & set(call_ids))
    for call_id in call_ids:
        assert call_id not in cold_session.pending_messages
        assert call_id in cold_session.delivered_call_ids
    assert cold.task_mailbox._call_record(workspace_id, bridge_call_id) is not None
    assert cold.reports[first.id].id == first.id

    retry = await cold.create_report(session.id, payload.model_copy(update={"call_id": padded}))
    assert retry.id == first.id
    assert cold.tasks[bound_task_id].status == WorkspaceTaskStatus.WORKING
    assert [
        event.call_id
        for event in cold.task_mailbox._events.get(workspace_id, [])
        if event.call_id == bridge_call_id
    ].count(bridge_call_id) == 1

    fresh = WorkspaceManager()
    assert fresh.reports[first.id].id == first.id
    assert set(call_ids) <= set(fresh.sessions[session.id].delivered_call_ids)
    assert fresh.task_mailbox._call_record(workspace_id, bridge_call_id) is not None


@pytest.mark.asyncio
async def test_postcommit_snapshot_failure_is_success_and_durable(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="snapshot may fail",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )

    monkeypatch.setattr(
        manager,
        "_write_snapshot",
        lambda _workspace_id: (_ for _ in ()).throw(OSError("snapshot failed")),
    )
    report = await manager.create_report(session.id, payload)
    assert manager.sessions[session.id].report_call_ids[payload.call_id] == report.id

    fresh = WorkspaceManager()
    assert fresh.sessions[session.id].report_call_ids[payload.call_id] == report.id
    assert fresh.reports[report.id].message == payload.message


def test_task_prompts_render_stable_distinct_call_ids_for_two_real_cycles(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    workspace = manager.workspaces[workspace_id]

    cycle_one_assignment = manager._build_task_assignment_prompt(
        workspace, task, session, lesson_context=[]
    )
    cycle_one_continue = manager._build_continue_prompt(task, ContinueTaskRequest(), session)
    assert f"{task.id}-goal-packet-cycle-1-attempt-1" in cycle_one_assignment
    assert f"{task.id}-started-cycle-1-attempt-1" in cycle_one_assignment
    assert f"{task.id}-assignment-progress-cycle-1-attempt-1" in cycle_one_assignment
    assert f"{task.id}-continue-progress-cycle-1-attempt-1" in cycle_one_continue
    assert cycle_one_continue == manager._build_continue_prompt(
        task, ContinueTaskRequest(), session
    )

    redispatched = task.model_copy(update={"dispatch_attempt": 2})
    manager.tasks[task.id] = redispatched
    cycle_one_redispatch = manager._build_task_assignment_prompt(
        workspace, redispatched, session, lesson_context=[]
    )
    assert f"{task.id}-assignment-progress-cycle-1-attempt-2" in cycle_one_redispatch
    assert cycle_one_redispatch != cycle_one_assignment

    task = task.model_copy(update={"review_cycle": 2})
    manager.tasks[task.id] = task
    cycle_two_assignment = manager._build_task_assignment_prompt(
        workspace, task, session, lesson_context=[]
    )
    cycle_two_continue = manager._build_continue_prompt(task, ContinueTaskRequest(), session)
    assert f"{task.id}-goal-packet-cycle-2-attempt-1" in cycle_two_assignment
    assert f"{task.id}-started-cycle-2-attempt-1" in cycle_two_assignment
    assert f"{task.id}-continue-progress-cycle-2-attempt-2" in cycle_two_continue
    assert "cycle-1" not in cycle_two_continue

    trigger = AgentReport(
        id="trigger",
        workspace_id=workspace_id,
        task_id=task.id,
        session_id=session.id,
        call_id=f"{task.id}-ready-cycle-2-1",
        state=AgentReportState.READY_FOR_REVIEW,
        message="ready",
        review_cycle=2,
        created_at=datetime.utcnow(),
    )
    review_prompt = manager._build_review_prompt(
        workspace, task, session, trigger, lesson_context=[]
    )
    assert f"{task.id}-review-started-cycle-2-attempt-1" in review_prompt
    assert f"{task.id}-review-passed-cycle-2-attempt-1" in review_prompt

    monitor_one = manager._report_endpoint_curl(
        session,
        task.id,
        purpose="monitor-reminder",
        attempt=1,
    )
    monitor_one_retry = manager._report_endpoint_curl(
        session,
        task.id,
        purpose="monitor-reminder",
        attempt=1,
    )
    monitor_two = manager._report_endpoint_curl(
        session,
        task.id,
        purpose="monitor-reminder",
        attempt=2,
    )
    assert monitor_one == monitor_one_retry
    assert monitor_one != monitor_two
    assert f"{task.id}-monitor-reminder-cycle-2-attempt-1" in monitor_one
    assert f"{task.id}-monitor-reminder-cycle-2-attempt-2" in monitor_two

    recovering_worker = session.model_copy(
        update={
            "hard_recovery_task_id": task.id,
            "hard_recovery_attempts": 1,
        }
    )
    worker_recovery = manager._build_hard_recovery_worker_prompt(
        workspace,
        task,
        recovering_worker,
        "api error",
    )
    worker_recovery_retry = manager._build_hard_recovery_worker_prompt(
        workspace,
        task,
        recovering_worker,
        "api error",
    )
    assert worker_recovery == worker_recovery_retry
    assert f"{task.id}-worker-recovery-progress-cycle-2-attempt-2" in worker_recovery

    recovering_reviewer = session.model_copy(
        update={
            "role": WorkspaceSessionRole.REVIEWER,
            "hard_recovery_task_id": task.id,
            "hard_recovery_attempts": 2,
        }
    )
    recovery_prompt = manager._build_hard_recovery_reviewer_prompt(
        workspace,
        task,
        recovering_reviewer,
        trigger,
        "api error",
    )
    for verdict in (
        "review-passed",
        "review-failed",
        "review-needs-input",
    ):
        assert f"{task.id}-{verdict}-recovery-cycle-2-attempt-3" in recovery_prompt

    supplement_report = trigger.model_copy(update={"review_cycle": 2})
    supplement_id = manager._report_prompt_call_id(
        task.id,
        "goal-packet-supplement",
        cycle=supplement_report.review_cycle + 1,
        attempt=supplement_report.id,
    )
    supplement_retry_id = manager._report_prompt_call_id(
        task.id,
        "goal-packet-supplement",
        cycle=supplement_report.review_cycle + 1,
        attempt=supplement_report.id,
    )
    later_report = supplement_report.model_copy(update={"id": "report-next-cycle"})
    later_supplement_id = manager._report_prompt_call_id(
        task.id,
        "goal-packet-supplement",
        cycle=later_report.review_cycle + 1,
        attempt=later_report.id,
    )
    assert supplement_id == supplement_retry_id
    assert supplement_id != later_supplement_id


@pytest.mark.asyncio
async def test_goal_packet_supplement_retry_reuses_cycle_and_call_id(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    task = task.model_copy(update={"review_cycle": 2})
    manager.tasks[task.id] = task
    report = AgentReport(
        id="supplement-source-report",
        workspace_id=workspace_id,
        task_id=task.id,
        session_id=session.id,
        state=AgentReportState.COMPLETED,
        message="missing packet evidence",
        review_cycle=2,
        created_at=datetime.utcnow(),
    )
    send = AsyncMock()
    monkeypatch.setattr(manager, "send_session_message", send)

    await manager._request_goal_packet_supplement(
        manager.tasks[task.id],
        manager.sessions[session.id],
        report,
        ["acceptance_check"],
    )
    first_message = send.await_args.args[1]
    assert manager.tasks[task.id].review_cycle == 3
    assert f"{task.id}-goal-packet-supplement-cycle-3-attempt-{report.id}" in first_message

    await manager._request_goal_packet_supplement(
        manager.tasks[task.id],
        manager.sessions[session.id],
        report,
        ["acceptance_check"],
    )
    second_message = send.await_args.args[1]
    assert manager.tasks[task.id].review_cycle == 3
    assert second_message == first_message


def test_resident_ack_helper_removed_from_report_intake() -> None:
    """Resident-specific ACK cursor advancement must not exist on the manager."""
    assert not hasattr(WorkspaceManager, "_advance_resident_ack_on_delivery")


@pytest.mark.asyncio
async def test_resident_root_context_ref_does_not_authorize_taskless_ack(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    """Legacy resident_root context_ref must not authorize task_id=None ACKs."""
    manager, workspace_id = manager_and_workspace
    now = datetime.utcnow()
    session_id = "resident-session"
    manager.sessions[session_id] = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id="tab-resident",
        role=WorkspaceSessionRole.RESIDENT,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Resident",
        workspace_path="/tmp",
        tmux_session="tmux-resident",
        target=ExecutionTarget.LOCAL,
        task_id=None,
        current_task_id=None,
        created_at=now,
        updated_at=now,
    )
    call_id = "resident-delivery-batch-1"
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={
            "processing_call_ids": [call_id],
            "pending_messages": {call_id: "batch"},
        }
    )
    manager.task_mailbox._call_index.setdefault(workspace_id, {})[call_id] = {
        "action": "resident_delivery",
        "target": "legacy-resident-target",
        "fingerprint": "resident-delivery-fp",
        "event": None,
    }

    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=None,
            state=AgentReportState.WORKING,
            message="attempt resident root ack",
            acked_call_ids=[call_id],
        ),
    )

    session = manager.sessions[session_id]
    assert call_id in session.processing_call_ids
    assert call_id not in session.delivered_call_ids
    assert manager._verify_call_target(call_id, None, session_id) is False
