"""Pure Task lifecycle derivation: one explicit phase, owner and attention flag."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import pytest

from claude_hub.models import (
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.models.schemas import (
    TaskExecutionControl,
    TaskOwnerKind,
    TaskOwnerState,
    TaskPhase,
    TaskProgressSnapshot,
    TaskProgressState,
    TaskSourceReference,
)
from claude_hub.services.task_lifecycle import (
    ChatTabFacts,
    LifecycleFacts,
    derive_task_lifecycle,
)

NOW = datetime(2026, 10, 24, 9, 0, 0)


def _task(**changes: Any) -> WorkspaceTask:
    base: dict[str, Any] = dict(
        id="task-1",
        workspace_id="ws-1",
        title="Title",
        prompt="Prompt",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id="worker-1",
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW,
    )
    base.update(changes)
    return WorkspaceTask(**base)


def _session(
    session_id: str = "worker-1",
    *,
    status: ManagedSessionStatus = ManagedSessionStatus.WORKING,
    runtime: AgentRuntimeStatus = AgentRuntimeStatus.WORKING,
    task_id: Optional[str] = "task-1",
    archived: bool = False,
    role: WorkspaceSessionRole = WorkspaceSessionRole.ORCHESTRATOR,
) -> ManagedSession:
    return ManagedSession(
        id=session_id,
        workspace_id="ws-1",
        tab_id=f"tab-{session_id}",
        role=role,
        agent_type=AgentType.CLAUDE,
        status=status,
        runtime_status=runtime,
        task_id=task_id,
        archived=archived,
        title=session_id,
        workspace_path="/repo",
        tmux_session=f"tmux-{session_id}",
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW,
    )


def _initiator(progress: Optional[TaskProgressState] = None, **changes: Any) -> WorkspaceTask:
    snapshot = (
        TaskProgressSnapshot(
            state=progress,
            summary="summary",
            call_id="c1",
            execution_epoch=1,
            reported_at=NOW - timedelta(minutes=5),
        )
        if progress
        else None
    )
    return _task(
        execution_control=TaskExecutionControl.INITIATOR,
        source=TaskSourceReference(kind="chat", tab_id="chat-1"),
        session_id=None,
        latest_progress=snapshot,
        **changes,
    )


def _phase(facts: LifecycleFacts) -> tuple[TaskPhase, str]:
    lifecycle = derive_task_lifecycle(facts)
    return lifecycle.phase, lifecycle.reason


def test_live_worker_is_running_and_holds_its_session() -> None:
    lifecycle = derive_task_lifecycle(LifecycleFacts(task=_task(), worker_session=_session()))
    assert lifecycle.phase == TaskPhase.RUNNING
    assert lifecycle.owner.kind == TaskOwnerKind.SESSION
    assert lifecycle.owner.state == TaskOwnerState.ACTIVE
    assert lifecycle.holds_session is True
    assert lifecycle.attention is False


@pytest.mark.parametrize(
    "session",
    [
        None,
        _session(archived=True),
        _session(status=ManagedSessionStatus.STOPPED),
        _session(runtime=AgentRuntimeStatus.OFFLINE),
    ],
)
def test_working_task_with_dead_worker_is_orphaned(session: Optional[ManagedSession]) -> None:
    lifecycle = derive_task_lifecycle(LifecycleFacts(task=_task(), worker_session=session))
    assert lifecycle.phase == TaskPhase.ORPHANED
    assert lifecycle.reason == "agent_gone"
    assert lifecycle.attention is True


def test_working_task_without_any_session_is_orphaned() -> None:
    assert _phase(LifecycleFacts(task=_task(session_id=None))) == (
        TaskPhase.ORPHANED,
        "working_without_agent",
    )


def test_worker_waiting_on_prompt_needs_user() -> None:
    session = _session(status=ManagedSessionStatus.NEEDS_INPUT, runtime=AgentRuntimeStatus.IDLE)
    assert _phase(LifecycleFacts(task=_task(), worker_session=session)) == (
        TaskPhase.NEEDS_USER,
        "agent_needs_input",
    )


def test_review_in_flight_is_owned_by_reviewer() -> None:
    task = _task(
        status=WorkspaceTaskStatus.REVIEW,
        review_session_id="reviewer-1",
        review_requested_at=NOW - timedelta(minutes=3),
    )
    reviewer = _session("reviewer-1", role=WorkspaceSessionRole.REVIEWER)
    lifecycle = derive_task_lifecycle(
        LifecycleFacts(task=task, worker_session=_session(), reviewer_session=reviewer)
    )
    assert lifecycle.phase == TaskPhase.REVIEWING
    assert lifecycle.owner.id == "reviewer-1"


def test_review_in_flight_with_dead_reviewer_is_orphaned() -> None:
    task = _task(
        status=WorkspaceTaskStatus.REVIEW,
        review_session_id="reviewer-1",
        review_requested_at=NOW - timedelta(minutes=3),
    )
    assert _phase(LifecycleFacts(task=task, reviewer_session=None)) == (
        TaskPhase.ORPHANED,
        "reviewer_gone",
    )


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (AgentReportState.REVIEW_NEEDS_INPUT, (TaskPhase.NEEDS_USER, "reviewer_needs_input")),
        (AgentReportState.REVIEW_FAILED, (TaskPhase.NEEDS_USER, "review_failed_not_resumed")),
        (AgentReportState.REVIEW_PASSED, (TaskPhase.AWAITING_ACCEPTANCE, "review_passed")),
    ],
)
def test_review_verdicts_name_the_waiting_party(
    verdict: AgentReportState, expected: tuple[TaskPhase, str]
) -> None:
    task = _task(
        status=WorkspaceTaskStatus.REVIEW,
        review_requested_at=NOW - timedelta(minutes=10),
        review_completed_at=NOW - timedelta(minutes=1),
    )
    facts = LifecycleFacts(task=task, latest_review_state=verdict, worker_session=_session())
    assert _phase(facts) == expected


def test_reviewer_question_still_holds_worker_until_policy_changes() -> None:
    """Documents today's occupancy so the later policy change is deliberate."""

    task = _task(
        status=WorkspaceTaskStatus.REVIEW,
        review_requested_at=NOW - timedelta(minutes=10),
        review_completed_at=NOW - timedelta(minutes=1),
    )
    idle_worker = _session(status=ManagedSessionStatus.IDLE, runtime=AgentRuntimeStatus.IDLE)
    lifecycle = derive_task_lifecycle(
        LifecycleFacts(
            task=task,
            latest_review_state=AgentReportState.REVIEW_NEEDS_INPUT,
            worker_session=idle_worker,
        )
    )
    assert lifecycle.phase == TaskPhase.NEEDS_USER
    assert lifecycle.holds_session is True


def test_human_acceptance_request_awaits_acceptance() -> None:
    task = _task(
        status=WorkspaceTaskStatus.REVIEW,
        human_acceptance_requested_at=NOW - timedelta(minutes=1),
    )
    lifecycle = derive_task_lifecycle(LifecycleFacts(task=task, worker_session=_session()))
    assert lifecycle.phase == TaskPhase.AWAITING_ACCEPTANCE
    assert lifecycle.since == task.human_acceptance_requested_at


def test_queued_behind_task_holding_the_agent_names_the_blocker() -> None:
    task = _task(
        id="task-2",
        status=WorkspaceTaskStatus.QUEUED,
        queued_at=NOW - timedelta(hours=1),
    )
    held = _session(task_id="task-1", status=ManagedSessionStatus.IDLE)
    lifecycle = derive_task_lifecycle(
        LifecycleFacts(task=task, worker_session=held, blocked_by_task_id="task-1")
    )
    assert (lifecycle.phase, lifecycle.reason) == (TaskPhase.QUEUED, "agent_held_by_other_task")
    assert lifecycle.blocked_by_task_id == "task-1"
    assert lifecycle.holds_session is False


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"dispatch_pending": True, "session_id": None}, "awaiting_dispatch_decision"),
        ({"session_id": None}, "no_agent_assigned"),
    ],
)
def test_queued_reasons(changes: dict[str, Any], reason: str) -> None:
    task = _task(status=WorkspaceTaskStatus.QUEUED, **changes)
    assert _phase(LifecycleFacts(task=task)) == (TaskPhase.QUEUED, reason)


def test_queued_on_gone_agent_is_orphaned() -> None:
    task = _task(status=WorkspaceTaskStatus.QUEUED)
    assert _phase(LifecycleFacts(task=task, worker_session=_session(archived=True))) == (
        TaskPhase.ORPHANED,
        "assigned_agent_gone",
    )


def test_queued_waiting_on_dependencies() -> None:
    task = _task(status=WorkspaceTaskStatus.QUEUED)
    facts = LifecycleFacts(task=task, worker_session=_session(), dependencies_blocked=True)
    assert _phase(facts) == (TaskPhase.QUEUED, "waiting_for_dependencies")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({}, "not_started"),
        ({"manual_aborted_at": NOW}, "aborted"),
        ({"execution_epoch": 2}, "handed_off_not_dispatched"),
        ({"origin": "resident"}, "resident_proposal"),
    ],
)
def test_draft_reasons(changes: dict[str, Any], reason: str) -> None:
    task = _task(status=WorkspaceTaskStatus.TODO, session_id=None, **changes)
    lifecycle = derive_task_lifecycle(LifecycleFacts(task=task))
    assert (lifecycle.phase, lifecycle.reason) == (TaskPhase.DRAFT, reason)
    assert lifecycle.attention is False


@pytest.mark.parametrize("tab", [ChatTabFacts(exists=False), ChatTabFacts(True, archived=True)])
def test_initiator_task_whose_chat_is_gone_is_orphaned(tab: ChatTabFacts) -> None:
    # Even an outstanding question cannot be answered in an archived Chat.
    task = _initiator(TaskProgressState.NEEDS_INPUT)
    lifecycle = derive_task_lifecycle(LifecycleFacts(task=task, chat_tab=tab))
    assert (lifecycle.phase, lifecycle.reason) == (TaskPhase.ORPHANED, "initiator_chat_gone")
    assert lifecycle.owner.kind == TaskOwnerKind.CHAT


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (TaskProgressState.WORKING, (TaskPhase.RUNNING, "initiator_working")),
        (TaskProgressState.NEEDS_INPUT, (TaskPhase.NEEDS_USER, "initiator_needs_input")),
        (TaskProgressState.BLOCKED, (TaskPhase.NEEDS_USER, "initiator_blocked")),
    ],
)
def test_initiator_progress_state_drives_phase(
    progress: TaskProgressState, expected: tuple[TaskPhase, str]
) -> None:
    facts = LifecycleFacts(task=_initiator(progress), chat_tab=ChatTabFacts(exists=True))
    assert _phase(facts) == expected


def test_released_initiator_task_is_orphaned_even_while_status_says_working() -> None:
    task = _initiator(TaskProgressState.RELEASED, execution_released=True)
    facts = LifecycleFacts(task=task, chat_tab=ChatTabFacts(exists=True))
    assert _phase(facts) == (TaskPhase.ORPHANED, "initiator_released")


@pytest.mark.parametrize(
    ("status", "phase"),
    [(WorkspaceTaskStatus.DONE, TaskPhase.DONE), (WorkspaceTaskStatus.FAILED, TaskPhase.FAILED)],
)
def test_terminal_status_wins_over_owner_liveness(
    status: WorkspaceTaskStatus, phase: TaskPhase
) -> None:
    lifecycle = derive_task_lifecycle(LifecycleFacts(task=_task(status=status)))
    assert lifecycle.phase == phase
    assert lifecycle.attention is False
    assert lifecycle.holds_session is False
