"""Tests for agent error hard-recovery mechanism.

Tests:
- Schema fields (agent_session_id on TerminalTab, hard recovery fields on ManagedSession)
- Constants for escalation thresholds
- Session normalization defaults
- Threshold logic for soft-to-hard escalation
- Recovery prompt builder helpers
"""

from datetime import datetime, timedelta

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    TerminalTab,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import (
    CLEAR_CONTEXT_SETTLE_SECONDS,
    workspace_manager,
)


def test_terminal_tab_has_agent_session_id_field() -> None:
    """TerminalTab schema exposes agent_session_id for recovery logging."""
    tab = TerminalTab(
        id="tab-1",
        name="test",
        shell="bash",
        cwd="/tmp",
        solo_mode=False,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        port=12345,
        created_at=datetime.now(),
        is_active=True,
        workspace_id=None,
        workspace_name=None,
        workspace_role=None,
        agent_session_id="sess-abc-123",
    )
    assert tab.agent_session_id == "sess-abc-123"


def test_terminal_tab_agent_session_id_defaults_none() -> None:
    """agent_session_id is optional and defaults to None."""
    tab = TerminalTab(
        id="tab-1",
        name="test",
        shell="bash",
        cwd="/tmp",
        solo_mode=False,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        port=12345,
        created_at=datetime.now(),
        is_active=True,
        workspace_id=None,
        workspace_name=None,
        workspace_role=None,
        agent_session_id=None,
    )
    assert tab.agent_session_id is None


def test_managed_session_has_hard_recovery_fields() -> None:
    """ManagedSession tracks hard recovery state."""
    now = datetime.now()
    session = ManagedSession(
        id="sess-1",
        workspace_id="ws-1",
        tab_id="tab-1",
        tmux_session="tmux-1",
        agent_type=AgentType.CLAUDE,
        title="Agent 1",
        role=WorkspaceSessionRole.WORKER,
        solo_mode=True,
        status=ManagedSessionStatus.IDLE,
        workspace_path="/tmp/ws",
        created_at=now,
        updated_at=now,
    )
    assert session.hard_recovery_task_id is None
    assert session.hard_recovery_attempts == 0
    assert session.last_hard_recovery_at is None


def test_normalize_session_sets_hard_recovery_defaults() -> None:
    """Session normalization backfills hard recovery fields for old state."""
    item = {
        "id": "s-1",
        "workspace_id": "ws-1",
        "tab_id": "t-1",
        "tmux_session": "tmux-1",
        "agent_type": "claude",
        "title": "Agent",
        "role": "worker",
        "solo_mode": True,
        "workspace_path": "/tmp/ws",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "status": "idle",
    }
    normalized = workspace_manager._normalize_session_item(item)
    assert normalized["hard_recovery_task_id"] is None
    assert normalized["hard_recovery_attempts"] == 0
    assert normalized["last_hard_recovery_at"] is None


def test_normalize_session_preserves_existing_hard_recovery_state() -> None:
    """Normalization does not clobber existing hard recovery fields."""
    now = datetime.now()
    item = {
        "id": "s-1",
        "workspace_id": "ws-1",
        "tab_id": "t-1",
        "tmux_session": "tmux-1",
        "agent_type": "claude",
        "title": "Agent",
        "role": "worker",
        "solo_mode": True,
        "workspace_path": "/tmp/ws",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "status": "working",
        "hard_recovery_task_id": "task-1",
        "hard_recovery_attempts": 1,
        "last_hard_recovery_at": now.isoformat(),
    }
    normalized = workspace_manager._normalize_session_item(item)
    assert normalized["hard_recovery_task_id"] == "task-1"
    assert normalized["hard_recovery_attempts"] == 1
    assert normalized["last_hard_recovery_at"] is not None


def _make_session_and_task(
    *,
    task_status: WorkspaceTaskStatus = WorkspaceTaskStatus.WORKING,
    role: WorkspaceSessionRole = WorkspaceSessionRole.WORKER,
    agent_type: AgentType = AgentType.CLAUDE,
    auto_continue_attempts: int = 0,
    hard_recovery_attempts: int = 0,
    last_hard_recovery_at: datetime | None = None,
    last_auto_continue_at: datetime | None = None,
) -> tuple[ManagedSession, WorkspaceTask]:
    """Helper to build a session/task pair for threshold tests."""
    now = datetime.now()
    session_id = "sess-1" if role == WorkspaceSessionRole.WORKER else "sess-review"
    reviewer_id = "sess-review" if role == WorkspaceSessionRole.REVIEWER else None
    worker_id = "sess-1"
    task = WorkspaceTask(
        id="task-1",
        workspace_id="ws-1",
        title="Test task",
        prompt="Do the thing",
        agent_type=agent_type,
        status=task_status,
        session_id=worker_id,
        review_session_id=reviewer_id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    session = ManagedSession(
        id=session_id,
        workspace_id="ws-1",
        tab_id="tab-1",
        tmux_session="tmux-1",
        agent_type=agent_type,
        title="Agent",
        role=role,
        solo_mode=True,
        task_id="task-1",
        current_task_id="task-1",
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        workspace_path="/tmp/ws",
        auto_continue_task_id="task-1",
        auto_continue_attempts=auto_continue_attempts,
        hard_recovery_task_id="task-1" if hard_recovery_attempts > 0 else None,
        hard_recovery_attempts=hard_recovery_attempts,
        last_hard_recovery_at=last_hard_recovery_at,
        last_auto_continue_at=last_auto_continue_at,
        last_activity_at=now - timedelta(seconds=30),
        created_at=now,
        updated_at=now,
    )
    return session, task


def test_worker_vs_reviewer_role_detection() -> None:
    """is_worker and is_reviewer detection from task/session bindings."""
    w_session, w_task = _make_session_and_task(
        task_status=WorkspaceTaskStatus.WORKING,
        role=WorkspaceSessionRole.WORKER,
    )
    is_worker = bool(w_task.session_id and w_task.session_id == w_session.id)
    is_reviewer = bool(w_task.review_session_id and w_task.review_session_id == w_session.id)
    assert is_worker is True
    assert is_reviewer is False

    r_session, r_task = _make_session_and_task(
        task_status=WorkspaceTaskStatus.REVIEW,
        role=WorkspaceSessionRole.REVIEWER,
    )
    is_worker = bool(r_task.session_id and r_task.session_id == r_session.id)
    is_reviewer = bool(r_task.review_session_id and r_task.review_session_id == r_session.id)
    assert is_worker is False
    assert is_reviewer is True


def test_latest_report_for_task_returns_most_recent() -> None:
    """_latest_report_for_task picks the latest report by created_at."""
    import uuid

    ws_id = "ws-latest"
    task_id = "task-latest"
    now = datetime.now()

    r1 = AgentReport(
        id=str(uuid.uuid4()),
        workspace_id=ws_id,
        task_id=task_id,
        session_id="s1",
        state=AgentReportState.STARTED,
        message="started",
        changed_files=[],
        created_at=now - timedelta(minutes=5),
    )
    r2 = AgentReport(
        id=str(uuid.uuid4()),
        workspace_id=ws_id,
        task_id=task_id,
        session_id="s1",
        state=AgentReportState.READY_FOR_REVIEW,
        message="ready",
        changed_files=["a.py"],
        created_at=now,
    )
    workspace_manager.reports[r1.id] = r1
    workspace_manager.reports[r2.id] = r2
    try:
        latest = workspace_manager._latest_report_for_task(task_id)
        assert latest is not None
        assert latest.state == AgentReportState.READY_FOR_REVIEW
        assert latest.message == "ready"
    finally:
        del workspace_manager.reports[r1.id]
        del workspace_manager.reports[r2.id]


def test_latest_report_for_task_returns_none_when_no_reports() -> None:
    latest = workspace_manager._latest_report_for_task("nonexistent-task-id-xyz")
    assert latest is None


def test_reviewer_fallback_recovery_call_ids_are_verdict_specific_and_stable() -> None:
    """No-trigger reviewer recovery uses verdict-specific IDs that stay stable per attempt."""
    session, task = _make_session_and_task(
        task_status=WorkspaceTaskStatus.REVIEW,
        role=WorkspaceSessionRole.REVIEWER,
        hard_recovery_attempts=1,
    )
    workspace_manager.tasks[task.id] = task
    try:
        first = workspace_manager._build_hard_recovery_reviewer_fallback_prompt(
            task, session, "api error", recovery_attempt=2
        )
        retry = workspace_manager._build_hard_recovery_reviewer_fallback_prompt(
            task, session, "api error", recovery_attempt=2
        )
        assert first == retry
        assert f"{task.id}-review-started-recovery-cycle-1-attempt-2" in first
        for verdict in ("review-passed", "review-failed", "review-needs-input"):
            assert f"{task.id}-{verdict}-recovery-cycle-1-attempt-2" in first
        later = workspace_manager._build_hard_recovery_reviewer_fallback_prompt(
            task, session, "api error", recovery_attempt=3
        )
        assert f"{task.id}-review-started-recovery-cycle-1-attempt-3" in later
        assert first != later
    finally:
        workspace_manager.tasks.pop(task.id, None)
