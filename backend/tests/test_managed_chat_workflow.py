"""End-to-end managed Chat workflow using real Workspace state and a fake provider."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from importlib import import_module
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSessionStatus,
    ReviewDecision,
    SessionKind,
    StartTaskRequest,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
)
from claude_hub.services import managed_chat
from claude_hub.services.ttyd_manager import TTYDProcess
from claude_hub.services.workspace_manager import DeliveryUncertain, WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


@dataclass
class FakeNative:
    manager: WorkspaceManager
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    receipts: dict[tuple[str, str], str] = field(default_factory=dict)
    active: dict[str, str] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    reset_sessions: list[str] = field(default_factory=list)
    uncertain_next: bool = False

    async def send(self, session, text, attachments, call_id):
        assert session.session_kind == SessionKind.CHAT
        assert session.tmux_session == ""
        current = self.manager.sessions[session.id]
        assert call_id in current.processing_call_ids
        assert call_id in current.pending_messages
        assert call_id in current.call_payload_fingerprints
        assert session.id not in self.active, "native turns overlapped"
        assert (
            session.id,
            call_id,
        ) not in self.receipts, "accepted or uncertain assignment replayed"
        assert not attachments
        self.calls.append((session.id, text, call_id))
        self.active[session.id] = call_id
        if self.uncertain_next:
            self.uncertain_next = False
            self.receipts[session.id, call_id] = "uncertain"
            raise RuntimeError("fake native acceptance unknown")
        self.receipts[session.id, call_id] = "accepted"

    def state(self, session, call_id):
        if call_id in session.delivered_call_ids:
            return "accepted"
        return self.receipts.get((session.id, call_id), "not_sent")

    def status(self, session):
        return AgentRuntimeStatus.WORKING if session.id in self.active else AgentRuntimeStatus.IDLE

    async def cancel(self, session, *, expected_turn_id=None):
        current = self.active.get(session.id)
        if current is None or (expected_turn_id is not None and current != expected_turn_id):
            return False
        self.active.pop(session.id)
        self.cancelled.append(session.id)
        return True

    async def reset(self, session):
        assert session.id not in self.active
        self.reset_sessions.append(session.id)

    def finish(self, session_id):
        self.active.pop(session_id, None)

    def last_call(self, session_id):
        return next(
            call_id for candidate, _text, call_id in reversed(self.calls) if candidate == session_id
        )


@dataclass
class Workflow:
    manager: WorkspaceManager
    native: FakeNative
    workspace_id: str
    worker_id: str
    forbidden: AsyncMock


@pytest.fixture()
async def workflow(monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    index = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index)

    # Keep real headless create/update and seat validation, but replace the
    # process registry and its disk persistence with fixture-owned state.
    tabs = _wm.ttyd_manager
    monkeypatch.setattr(tabs, "processes", {})
    monkeypatch.setattr(tabs, "_tab_order", [])
    monkeypatch.setattr(tabs, "_status_snapshots", {})
    monkeypatch.setattr(tabs, "_status_cache", {})
    monkeypatch.setattr(tabs, "_start_locks", {})
    monkeypatch.setattr(tabs, "_save_state", lambda: None)
    monkeypatch.setattr(tabs, "_save_order", lambda: None)
    monkeypatch.setattr(tabs, "_get_next_port", lambda: 23000 + len(tabs.processes))
    forbidden = AsyncMock(
        side_effect=AssertionError("managed Chat invoked terminal infrastructure")
    )
    monkeypatch.setattr(TTYDProcess, "start", forbidden)
    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", forbidden)
    for name in (
        "_send_tmux_message",
        "_send_tmux_message_with_receipt",
        "_query_tmux_receipt",
        "_ensure_submitted_without_repaste",
        "_capture_tmux_output",
        "_run_tmux",
        "_interrupt_tmux_session",
    ):
        if hasattr(WorkspaceManager, name):
            monkeypatch.setattr(WorkspaceManager, name, forbidden)
    monkeypatch.setattr(tabs, "list_tab_agent_statuses", forbidden)

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    manager = WorkspaceManager()
    native = FakeNative(manager)
    monkeypatch.setattr(managed_chat, "send_message", native.send)
    monkeypatch.setattr(managed_chat, "delivery_state", native.state)
    monkeypatch.setattr(
        managed_chat, "is_turn_running", lambda session: session.id in native.active
    )
    monkeypatch.setattr(managed_chat, "runtime_status", native.status)
    monkeypatch.setattr(managed_chat, "cancel", native.cancel)
    monkeypatch.setattr(managed_chat, "reset_context", native.reset)

    workspace = manager.create_workspace(
        WorkspaceCreate(name="Managed Chat workflow", path=str(repo), target=ExecutionTarget.LOCAL)
    )
    worker = await manager.ensure_workspace_agent(
        workspace.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.ORCHESTRATOR,
            cwd=str(repo),
            env={"ANTHROPIC_MODEL": "fake-workflow-model"},
            reuse_existing=False,
        ),
    )
    assert worker.session_kind == SessionKind.CHAT
    assert tabs.processes[worker.tab_id].is_managed_chat
    assert tabs.processes[worker.tab_id].process is None
    await manager._refresh_session_statuses(workspace.id, run_auto_continue=False)
    if worker.id in native.active:
        bootstrap_id = native.last_call(worker.id)
        await manager.create_report(
            worker.id,
            AgentReportCreate(
                state=AgentReportState.WORKING,
                message="Bootstrap instructions received.",
                call_id="bootstrap-ack",
                acked_call_ids=[bootstrap_id],
            ),
        )
        native.finish(worker.id)
        await manager._refresh_session_statuses(workspace.id, run_auto_continue=False)
        native.calls.clear()  # Task assertions below start after the completed bootstrap.
    yield Workflow(manager, native, workspace.id, worker.id, forbidden)
    forbidden.assert_not_awaited()


def _create_task(workflow):
    return workflow.manager.create_task(
        workflow.workspace_id,
        WorkspaceTaskCreate(
            title="Small local edit",
            prompt="Make one bounded edit and report checks.",
            agent_type=AgentType.CLAUDE,
            task_mode=WorkspaceTaskMode.REVIEWED,
            execution_complexity="simple",
            session_kind=SessionKind.CHAT,
            session_id=workflow.worker_id,
        ),
    )


async def _start_task(workflow):
    task = _create_task(workflow)
    started = await workflow.manager.start_task(
        task.id, StartTaskRequest(target_session_id=workflow.worker_id)
    )
    assert started.status == WorkspaceTaskStatus.WORKING
    assert started.session_id == workflow.worker_id
    return task.id


async def _report(workflow, session_id, task_id, state, call_id, *, ack=None, message=None):
    return await workflow.manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task_id,
            state=state,
            message=message or f"Evidence for {state.value}",
            call_id=call_id,
            acked_call_ids=ack or [],
            validation="Focused deterministic checks passed.",
            risks="No known risks in this bounded change.",
            review_decision=ReviewDecision.REQUEST,
        ),
    )


async def test_chat_worker_review_fix_re_review_and_human_acceptance(workflow):
    manager, native = workflow.manager, workflow.native
    task_id = await _start_task(workflow)
    dispatch_id = native.last_call(workflow.worker_id)
    original_dispatch = manager.sessions[workflow.worker_id].pending_messages[dispatch_id]
    assert dispatch_id.startswith("dispatch:")

    working = await _report(
        workflow,
        workflow.worker_id,
        task_id,
        AgentReportState.WORKING,
        "worker-working-1",
        ack=[dispatch_id],
    )
    assert dispatch_id in manager.sessions[workflow.worker_id].delivered_call_ids
    assert dispatch_id not in manager.sessions[workflow.worker_id].pending_messages
    duplicate = await _report(
        workflow,
        workflow.worker_id,
        task_id,
        AgentReportState.WORKING,
        "worker-working-1",
        ack=[dispatch_id],
    )
    assert duplicate.id == working.id
    await manager.send_session_message(workflow.worker_id, original_dispatch, call_id=dispatch_id)
    assert len(native.calls) == 1
    with pytest.raises(ValueError, match="different payload"):
        await manager.send_session_message(
            workflow.worker_id, "changed assignment", call_id=dispatch_id
        )

    native.finish(workflow.worker_id)
    await _report(
        workflow, workflow.worker_id, task_id, AgentReportState.COMPLETED, "worker-completed-1"
    )
    task = manager.tasks[task_id]
    reviewer_id = task.review_session_id
    assert reviewer_id and reviewer_id != workflow.worker_id
    reviewer = manager.sessions[reviewer_id]
    assert reviewer.role == WorkspaceSessionRole.REVIEWER
    assert reviewer.session_kind == SessionKind.CHAT
    assert reviewer.agent_type == manager.sessions[workflow.worker_id].agent_type
    assert reviewer.env == manager.sessions[workflow.worker_id].env
    assert reviewer.workspace_path == manager.sessions[workflow.worker_id].workspace_path
    assert reviewer.tmux_session == ""
    review_call = native.last_call(reviewer_id)
    assert task.status == WorkspaceTaskStatus.REVIEW
    assert task.human_accepted_at is None

    await _report(
        workflow,
        reviewer_id,
        task_id,
        AgentReportState.REVIEW_STARTED,
        "review-started-1",
        ack=[review_call],
    )
    native.finish(reviewer_id)
    failed_cycle = manager.tasks[task_id].review_cycle
    failed_report = await _report(
        workflow,
        reviewer_id,
        task_id,
        AgentReportState.REVIEW_FAILED,
        "review-failed-1",
        message="Fix the missing edge case.",
    )
    # The existing reviewed-task policy automatically continues the original
    # worker on failure; Chat must use that same policy and a new durable call.
    assert manager.tasks[task_id].human_accepted_at is None
    assert manager.tasks[task_id].session_id == workflow.worker_id
    assert manager.tasks[task_id].review_cycle == failed_cycle + 1
    assert manager.tasks[task_id].status == WorkspaceTaskStatus.WORKING
    continue_call = native.last_call(workflow.worker_id)
    assert continue_call != dispatch_id
    call_count = len(native.calls)
    duplicate_failure = await _report(
        workflow,
        reviewer_id,
        task_id,
        AgentReportState.REVIEW_FAILED,
        "review-failed-1",
        message="Fix the missing edge case.",
    )
    assert duplicate_failure.id == failed_report.id
    assert len(native.calls) == call_count
    await _report(
        workflow,
        workflow.worker_id,
        task_id,
        AgentReportState.WORKING,
        "worker-working-2",
        ack=[continue_call],
    )
    native.finish(workflow.worker_id)
    await _report(
        workflow, workflow.worker_id, task_id, AgentReportState.COMPLETED, "worker-completed-2"
    )
    assert manager.tasks[task_id].review_session_id == reviewer_id
    assert native.reset_sessions == []
    second_review_call = native.last_call(reviewer_id)
    assert second_review_call != review_call
    await _report(
        workflow,
        reviewer_id,
        task_id,
        AgentReportState.REVIEW_STARTED,
        "review-started-2",
        ack=[second_review_call],
    )
    native.finish(reviewer_id)
    await _report(workflow, reviewer_id, task_id, AgentReportState.REVIEW_PASSED, "review-passed-2")
    task = manager.tasks[task_id]
    assert task.status == WorkspaceTaskStatus.REVIEW
    assert task.human_acceptance_requested_at is not None
    assert task.human_accepted_at is None
    assert task.completed_at is None
    assert task.reviewed_cycle == task.review_cycle

    accepted = await manager.update_task(
        task_id, WorkspaceTaskUpdate(status=WorkspaceTaskStatus.DONE)
    )
    assert accepted.status == WorkspaceTaskStatus.DONE
    assert accepted.human_accepted_at is not None
    assert accepted.completed_at is not None
    workflow.forbidden.assert_not_awaited()


async def test_chat_stop_keeps_task_followup_pending_across_monitor_and_restart(workflow):
    manager, native = workflow.manager, workflow.native
    task_id = await _start_task(workflow)
    dispatch_id = native.last_call(workflow.worker_id)
    await manager.followup_task(
        workflow.workspace_id, task_id, "Do not forget the edge case.", "followup-1"
    )
    assert "followup-1" in manager.sessions[workflow.worker_id].pending_call_ids
    assert len(native.calls) == 1
    assert await manager.cancel_chat_session(workflow.worker_id, expected_turn_id=dispatch_id)
    assert native.cancelled == [workflow.worker_id]
    assert manager.sessions[workflow.worker_id].chat_paused
    await manager._refresh_session_statuses(workflow.workspace_id, run_auto_continue=True)
    await manager._pump_session_messages(workflow.worker_id)
    assert len(native.calls) == 1
    assert "followup-1" in manager.sessions[workflow.worker_id].pending_call_ids
    assert manager.tasks[task_id].status == WorkspaceTaskStatus.WORKING

    manager._save_state()
    fresh = WorkspaceManager()
    native.manager = fresh
    assert fresh.sessions[workflow.worker_id].session_kind == SessionKind.CHAT
    assert fresh.sessions[workflow.worker_id].chat_paused
    await fresh._recover_processing_via_receipt(workflow.worker_id)
    await fresh._refresh_session_statuses(workflow.workspace_id, run_auto_continue=True)
    await fresh._pump_session_messages(workflow.worker_id)
    assert len(native.calls) == 1
    assert dispatch_id in fresh.sessions[workflow.worker_id].processing_call_ids
    assert "followup-1" in fresh.sessions[workflow.worker_id].pending_call_ids
    assert fresh.tasks[task_id].human_accepted_at is None
    workflow.forbidden.assert_not_awaited()


async def test_chat_restart_does_not_replay_uncertain_assignment(workflow):
    manager, native = workflow.manager, workflow.native
    native.uncertain_next = True
    task_id = _create_task(workflow).id
    with pytest.raises(DeliveryUncertain):
        await manager.start_task(task_id, StartTaskRequest(target_session_id=workflow.worker_id))
    call_id = native.last_call(workflow.worker_id)
    assert call_id in manager.sessions[workflow.worker_id].uncertain_call_ids
    manager._save_state()
    native.finish(workflow.worker_id)
    fresh = WorkspaceManager()
    native.manager = fresh
    await fresh._recover_processing_via_receipt(workflow.worker_id)
    await fresh._refresh_session_statuses(workflow.workspace_id, run_auto_continue=True)
    await fresh._pump_session_messages(workflow.worker_id)
    assert len(native.calls) == 1
    assert call_id in fresh.sessions[workflow.worker_id].uncertain_call_ids
    with pytest.raises(DeliveryUncertain):
        await fresh.resume_existing_call(workflow.worker_id, call_id)
    assert len(native.calls) == 1
    assert fresh.tasks[task_id].human_accepted_at is None


async def test_chat_review_does_not_reuse_existing_terminal_reviewer(workflow, monkeypatch):
    manager, native = workflow.manager, workflow.native
    task_id = await _start_task(workflow)
    dispatch_id = native.last_call(workflow.worker_id)
    await _report(
        workflow,
        workflow.worker_id,
        task_id,
        AgentReportState.WORKING,
        "worker-ack",
        ack=[dispatch_id],
    )
    native.finish(workflow.worker_id)

    # Represent a previously configured idle Terminal Reviewer without
    # starting its runtime. It must not be selected for the Chat task.
    worker = manager.sessions[workflow.worker_id]
    terminal = worker.model_copy(
        update={
            "id": "existing-terminal-reviewer",
            "tab_id": "existing-terminal-tab",
            "role": WorkspaceSessionRole.REVIEWER,
            "session_kind": SessionKind.TERMINAL,
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "task_id": None,
            "current_task_id": None,
            "tmux_session": "claude-hub-existing-terminal",
            "title": "Terminal Reviewer",
        }
    )
    manager.sessions[terminal.id] = terminal

    async def terminal_status_only(*, tab_ids):
        assert tab_ids == [terminal.tab_id]
        return []

    monkeypatch.setattr(_wm.ttyd_manager, "list_tab_agent_statuses", terminal_status_only)
    await _report(workflow, workflow.worker_id, task_id, AgentReportState.COMPLETED, "worker-done")
    reviewer_id = manager.tasks[task_id].review_session_id
    assert reviewer_id != terminal.id
    assert reviewer_id != workflow.worker_id
    assert manager.sessions[reviewer_id].session_kind == SessionKind.CHAT
    assert native.last_call(reviewer_id)
    assert all(session_id != terminal.id for session_id, _text, _call_id in native.calls)


async def test_chat_review_reuses_independently_configured_chat_provider(workflow):
    manager, native = workflow.manager, workflow.native
    existing = await manager.ensure_workspace_agent(
        workflow.workspace_id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CODEX,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.REVIEWER,
            cwd=manager.sessions[workflow.worker_id].workspace_path,
            env={"CODEX_MODEL": "independent-review-model"},
            reuse_existing=False,
        ),
    )
    await manager._refresh_session_statuses(workflow.workspace_id, run_auto_continue=False)
    if existing.id in native.active:
        await _report(
            workflow,
            existing.id,
            None,
            AgentReportState.WORKING,
            "reviewer-bootstrap-ack",
            ack=[native.last_call(existing.id)],
        )
        native.finish(existing.id)
        await manager._refresh_session_statuses(workflow.workspace_id, run_auto_continue=False)
    independent_env = dict(manager.sessions[existing.id].env)
    task_id = await _start_task(workflow)
    dispatch_id = native.last_call(workflow.worker_id)
    await _report(
        workflow,
        workflow.worker_id,
        task_id,
        AgentReportState.WORKING,
        "worker-ack",
        ack=[dispatch_id],
    )
    native.finish(workflow.worker_id)
    await _report(workflow, workflow.worker_id, task_id, AgentReportState.COMPLETED, "worker-done")
    assert manager.tasks[task_id].review_session_id == existing.id
    reviewer = manager.sessions[existing.id]
    assert reviewer.session_kind == SessionKind.CHAT
    assert reviewer.agent_type == AgentType.CODEX
    assert reviewer.env == independent_env
    assert reviewer.env["CODEX_MODEL"] == "independent-review-model"
    assert manager.sessions[workflow.worker_id].agent_type == AgentType.CLAUDE
    assert native.last_call(existing.id)


async def test_terminal_review_does_not_reuse_only_chat_reviewer(workflow, monkeypatch):
    manager = workflow.manager
    worker = manager.sessions[workflow.worker_id]
    terminal_worker = worker.model_copy(
        update={
            "session_kind": SessionKind.TERMINAL,
            "tmux_session": "terminal-worker-not-started",
        }
    )
    manager.sessions[worker.id] = terminal_worker
    existing_chat = worker.model_copy(
        update={
            "id": "existing-chat-reviewer",
            "tab_id": "existing-chat-reviewer-tab",
            "role": WorkspaceSessionRole.REVIEWER,
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "task_id": None,
            "current_task_id": None,
            "pending_call_ids": [],
            "processing_call_ids": [],
            "delivered_call_ids": [],
            "title": "Existing Chat Reviewer",
        }
    )
    manager.sessions[existing_chat.id] = existing_chat
    task = manager.create_task(
        workflow.workspace_id,
        WorkspaceTaskCreate(
            title="Terminal task",
            prompt="Retain the existing Terminal execution contract.",
            agent_type=terminal_worker.agent_type,
            session_kind=SessionKind.TERMINAL,
            task_mode=WorkspaceTaskMode.REVIEWED,
            session_id=terminal_worker.id,
        ),
    )
    terminal_reviewer = terminal_worker.model_copy(
        update={
            "id": "new-terminal-reviewer",
            "tab_id": "new-terminal-reviewer-tab",
            "role": WorkspaceSessionRole.REVIEWER,
        }
    )
    create = AsyncMock(return_value=terminal_reviewer)
    monkeypatch.setattr(manager, "ensure_workspace_agent", create)

    selected = await manager._select_or_create_reviewer(
        manager.workspaces[workflow.workspace_id], task
    )

    assert selected.id == terminal_reviewer.id
    assert selected.id != existing_chat.id
    assert selected.session_kind == SessionKind.TERMINAL
    create.assert_awaited_once()
    request = create.await_args.args[1]
    assert request.session_kind == SessionKind.TERMINAL
    assert request.role == WorkspaceSessionRole.REVIEWER
    workflow.forbidden.assert_not_awaited()
