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
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.models.agent_tree import ExecutorKind, FollowupRequest, SpawnRequest
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
) -> list[str]:
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
    return call_ids


@pytest.mark.asyncio
async def test_precommit_failure_rolls_back_both_acks_and_cold_retry_converges(
    manager_and_workspace: tuple[WorkspaceManager, str], monkeypatch: MonkeyPatch
) -> None:
    manager, workspace_id = manager_and_workspace
    task, session = _task_session(manager, workspace_id)
    call_ids = await _install_two_processing_followups(manager, workspace_id, task, session)
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

    def fail_target_state_once(path: Path, text: str) -> None:
        nonlocal failed
        if path == state_file and not failed:
            failed = True
            raise OSError("pre-state atomic replace failed")
        original_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", fail_target_state_once)
    with pytest.raises(OSError, match="pre-state"):
        await manager.create_report(session.id, payload)

    rolled_back = manager.sessions[session.id]
    assert set(rolled_back.processing_call_ids) == set(call_ids)
    assert not (set(rolled_back.delivered_call_ids) & set(call_ids))
    assert payload.call_id not in rolled_back.report_call_ids
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

    # Cold reload + retry of the identical call_id/payload converges to the
    # same report and does not duplicate outcome/delivered events.
    fresh = WorkspaceManager()
    retry = await fresh.create_report(session.id, payload)
    assert retry.id == committed.id
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
    cycle_one_continue = manager._report_endpoint_curl(session, task.id)
    assert f"{task.id}-goal-packet-cycle-1-1" in cycle_one_assignment
    assert f"{task.id}-started-cycle-1-1" in cycle_one_assignment
    assert f"{task.id}-working-progress-cycle-1-1" in cycle_one_continue
    assert cycle_one_continue == manager._report_endpoint_curl(session, task.id)

    task = task.model_copy(update={"review_cycle": 2})
    manager.tasks[task.id] = task
    cycle_two_assignment = manager._build_task_assignment_prompt(
        workspace, task, session, lesson_context=[]
    )
    cycle_two_continue = manager._report_endpoint_curl(session, task.id)
    assert f"{task.id}-goal-packet-cycle-2-1" in cycle_two_assignment
    assert f"{task.id}-started-cycle-2-1" in cycle_two_assignment
    assert f"{task.id}-working-progress-cycle-2-1" in cycle_two_continue
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
    assert f"{task.id}-review-started-cycle-2-1" in review_prompt
    assert f"{task.id}-review-passed-cycle-2-1" in review_prompt
