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
from claude_hub.models.agent_tree import (
    AgentEventType,
    AgentRunStatus,
    ExecutorKind,
    FollowupRequest,
    SpawnRequest,
)
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
    root = manager.agent_tree.create_root_run(
        workspace_id=workspace_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=workspace_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="initial",
            call_id="spawn-report-intake",
            context_ref=task.id,
        )
    )
    # Native adapters replace context_ref with their executor handle. Bind the
    # run to this managed task so strict ACK target verification succeeds.
    child.context_ref = task.id
    call_ids = ["followup-report-intake-1", "followup-report-intake-2"]
    for call_id in call_ids:
        await manager.agent_tree.followup(
            FollowupRequest(
                workspace_id=workspace_id,
                recipient_id=child.id,
                author_id=root.id,
                message=f"process {call_id}",
                call_id=call_id,
            )
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
    return call_ids, child.id


@pytest.mark.asyncio
async def test_precommit_failure_rolls_back_both_acks_and_cold_retry_converges(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    call_ids, child_run_id = await _install_two_processing_followups(
        manager, workspace_id, task, session
    )
    baseline_run_status = manager.agent_tree.get_run(child_run_id).status
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
                manager.agent_tree._call_record(workspace_id, f"report:{staged_report_id}")
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
    assert manager.agent_tree._call_record(workspace_id, f"report:{staged_report_id}") is None
    assert manager.agent_tree.get_run(child_run_id).status == baseline_run_status
    for call_id in call_ids:
        # The followup API already committed its dispatch outcome. Report ACK
        # failure must preserve that baseline event and roll back only the new
        # delivered event / delivery-state transition.
        assert manager.agent_tree._call_record(workspace_id, f"{call_id}:outcome") is not None
        assert manager.agent_tree._call_record(workspace_id, f"{call_id}:delivered") is None

    # Same-process retry commits exactly one report and both ACK lifecycle
    # mutations. AgentTree._persist is deliberately disabled to prove no
    # nested ACK persist remains inside the transaction.
    original_persist = manager.agent_tree._persist

    def reject_nested_persist() -> None:
        if manager._report_intake_workspace.get() is not None:
            raise AssertionError("nested Agent Tree persist")
        original_persist()

    monkeypatch.setattr(manager.agent_tree, "_persist", reject_nested_persist)
    committed = await manager.create_report(session.id, payload)
    assert set(call_ids) <= set(manager.sessions[session.id].delivered_call_ids)
    assert manager.agent_tree.get_run(child_run_id).status == AgentRunStatus.RUNNING

    # Cold reload + retry of the identical call_id/payload converges to the
    # same report and does not duplicate outcome/delivered events.
    fresh = WorkspaceManager()
    retry = await fresh.create_report(session.id, payload)
    assert retry.id == committed.id
    assert fresh.agent_tree.get_run(child_run_id).status == AgentRunStatus.RUNNING
    assert (
        len([report for report in fresh.reports.values() if report.call_id == payload.call_id]) == 1
    )
    for call_id in call_ids:
        assert (
            len(
                [
                    event
                    for event in fresh.agent_tree._events[workspace_id]
                    if event.call_id == f"{call_id}:outcome"
                ]
            )
            == 1
        )
        assert (
            len(
                [
                    event
                    for event in fresh.agent_tree._events[workspace_id]
                    if event.call_id == f"{call_id}:delivered"
                ]
            )
            == 1
        )
    report_bridge_call_id = f"report:{committed.id}"
    assert (
        len(
            [
                event
                for event in fresh.agent_tree._events[workspace_id]
                if event.call_id == report_bridge_call_id
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_report_rollback_preserves_concurrent_agent_tree_write(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    """Reproduce: emit_event persist during rename await must survive rollback.

    create_report used to snapshot the workspace, then await tab rename, then
    commit. A concurrent emit_event+_persist in that await window was durable
    until rollback restored the stale snapshot and erased it. Snapshot now
    happens after rename. Public Agent Tree APIs are also serialized; this
    test keeps the raw persist path so the original race stays covered.
    """

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    manager.sessions[session.id] = session.model_copy(update={"title": "stale title"})
    root = manager.agent_tree.create_root_run(
        workspace_id=workspace_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
    )
    concurrent_call_id = "concurrent-tree-write-during-rename"
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
        # The concurrent emit persists first (before concurrent_done). The
        # next workspace save is the report commit and must fail.
        if path == state_file and concurrent_done.is_set() and not failed:
            failed = True
            raise OSError("pre-commit fail after concurrent tree write")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_report_commit_once)

    async def concurrent_tree_write() -> None:
        await rename_started.wait()
        manager.agent_tree.emit_event(
            workspace_id=workspace_id,
            agent_run_id=root.id,
            event_type=AgentEventType.PROGRESS,
            author=root.id,
            recipient=root.id,
            call_id=concurrent_call_id,
            payload={"note": "must survive report rollback"},
        )
        concurrent_done.set()

    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="rollback must not erase tree",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    writer = asyncio.create_task(concurrent_tree_write())
    with pytest.raises(OSError, match="pre-commit fail after concurrent tree write"):
        await manager.create_report(session.id, payload)
    await writer

    assert manager.agent_tree._call_record(workspace_id, concurrent_call_id) is not None
    assert payload.call_id not in manager.sessions[session.id].report_call_ids
    assert not any(report.call_id == payload.call_id for report in manager.reports.values())

    fresh = WorkspaceManager()
    assert fresh.agent_tree._call_record(workspace_id, concurrent_call_id) is not None
    assert payload.call_id not in fresh.sessions[session.id].report_call_ids


@pytest.mark.asyncio
async def test_report_rollback_serializes_agent_tree_spawn(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    """Public Agent Tree writes wait on the report workspace lock.

    spawn/send/followup/interrupt share workspace_mutation_lock with
    create_report. A concurrent spawn started during the rename await must
    not persist until report rollback releases the lock; after restore the
    spawn lands and survives.
    """

    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    manager.sessions[session.id] = session.model_copy(update={"title": "stale title"})
    root = manager.agent_tree.create_root_run(
        workspace_id=workspace_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
    )
    spawn_call_id = "serialized-spawn-during-report-rollback"
    rename_started = asyncio.Event()
    rename_calls = 0
    fake_tab = MagicMock(id=session.tab_id, tmux_session=session.tmux_session)

    async def slow_update_tab(tab_id: str, name: str | None = None, **_kwargs: object):
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            rename_started.set()
            await asyncio.sleep(0.05)
            assert manager.agent_tree._call_record(workspace_id, spawn_call_id) is None
            assert not any(
                run.parent_id == root.id
                for run in manager.agent_tree._runs.values()
                if run.workspace_id == workspace_id
            )
        return fake_tab

    monkeypatch.setattr(_wm.ttyd_manager, "update_tab", slow_update_tab)

    original_write = manager._atomic_write_text
    failed = False
    state_file = manager._workspace_state_file(workspace_id)

    def fail_report_commit_once(path: Path, text: str) -> None:
        nonlocal failed
        if path == state_file and rename_started.is_set() and not failed:
            failed = True
            raise OSError("pre-commit fail while spawn waits on workspace lock")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_report_commit_once)

    async def concurrent_spawn() -> object:
        await rename_started.wait()
        return await manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=workspace_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                title="serialized child",
                initial_message="must wait for report rollback",
                call_id=spawn_call_id,
            )
        )

    payload = AgentReportCreate(
        task_id=task.id,
        state=AgentReportState.WORKING,
        message="rollback must serialize tree spawn",
        call_id=f"{task.id}-working-progress-cycle-1-1",
    )
    writer = asyncio.create_task(concurrent_spawn())
    with pytest.raises(OSError, match="pre-commit fail while spawn waits on workspace lock"):
        await manager.create_report(session.id, payload)
    child = await writer

    assert child.parent_id == root.id
    assert manager.agent_tree._call_record(workspace_id, spawn_call_id) is not None
    assert payload.call_id not in manager.sessions[session.id].report_call_ids
    assert not any(report.call_id == payload.call_id for report in manager.reports.values())

    fresh = WorkspaceManager()
    assert fresh.agent_tree._call_record(workspace_id, spawn_call_id) is not None
    assert payload.call_id not in fresh.sessions[session.id].report_call_ids


@pytest.mark.asyncio
async def test_known_call_id_does_not_rename_reassigned_session(
    manager_and_workspace: tuple[WorkspaceManager, str],
) -> None:
    """Late retry/conflict after reassignment must not restore the old title."""

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
    manager.sessions[session.id] = manager.sessions[session.id].model_copy(
        update={
            "task_id": new_task.id,
            "current_task_id": new_task.id,
            "title": new_task.title,
        }
    )
    manager._save_state()

    with pytest.raises(ReportCallIdConflict, match="already used"):
        await manager.create_report(
            session.id,
            payload.model_copy(update={"message": "different payload"}),
        )
    assert manager.sessions[session.id].title == new_task.title
    assert manager.sessions[session.id].current_task_id == new_task.id

    retry = await manager.create_report(session.id, payload)
    assert retry.id == first.id
    assert manager.sessions[session.id].title == new_task.title
    assert manager.sessions[session.id].current_task_id == new_task.id
    assert manager.sessions[session.id].task_id == new_task.id

    fresh = WorkspaceManager()
    assert fresh.reports[first.id].id == first.id
    assert fresh.sessions[session.id].title == new_task.title
    assert fresh.sessions[session.id].current_task_id == new_task.id
    assert fresh.sessions[session.id].task_id == new_task.id


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
