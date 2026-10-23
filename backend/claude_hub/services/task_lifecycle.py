"""Pure Task lifecycle derivation.

One function maps the persisted Task record plus observed owner facts to a
single ``TaskLifecycle``. It performs no I/O and never mutates state, so every
consumer (board, CLI, Dot, notifications, the future reconciler) sees the same
answer to "what phase is this Task in, who owns it, and must a human act?".
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..models.schemas import (
    ATTENTION_PHASES,
    AgentReportState,
    AgentRuntimeStatus,
    AutonomousRunPhase,
    ManagedSession,
    ManagedSessionStatus,
    TaskExecutionControl,
    TaskLifecycle,
    TaskOwner,
    TaskOwnerKind,
    TaskOwnerState,
    TaskPhase,
    TaskProgressState,
    WorkspaceTask,
    WorkspaceTaskStatus,
)


@dataclass(frozen=True)
class ChatTabFacts:
    """What the Hub knows about an initiator's source Chat tab."""

    exists: bool
    archived: bool = False


@dataclass(frozen=True)
class LifecycleFacts:
    task: WorkspaceTask
    latest_review_state: Optional[AgentReportState] = None
    worker_session: Optional[ManagedSession] = None
    reviewer_session: Optional[ManagedSession] = None
    chat_tab: Optional[ChatTabFacts] = None
    dependencies_blocked: bool = False
    blocked_by_task_id: Optional[str] = None


def session_owner_state(session: Optional[ManagedSession]) -> TaskOwnerState:
    if session is None or session.archived:
        return TaskOwnerState.GONE
    if session.status == ManagedSessionStatus.STOPPED:
        return TaskOwnerState.GONE
    if session.runtime_status == AgentRuntimeStatus.OFFLINE:
        return TaskOwnerState.GONE
    if session.status == ManagedSessionStatus.SPAWNING:
        return TaskOwnerState.ACTIVE
    if session.runtime_status == AgentRuntimeStatus.WORKING:
        return TaskOwnerState.ACTIVE
    return TaskOwnerState.IDLE


def chat_owner_state(task: WorkspaceTask, tab: Optional[ChatTabFacts]) -> TaskOwnerState:
    if tab is None:
        return TaskOwnerState.UNKNOWN
    if not tab.exists or tab.archived:
        return TaskOwnerState.GONE
    observation = task.runtime_observation
    if observation and observation.execution_epoch == task.execution_epoch:
        if observation.status == "active":
            return TaskOwnerState.ACTIVE
        if observation.status == "idle":
            return TaskOwnerState.IDLE
    return TaskOwnerState.UNKNOWN


def _session_owner(session_id: Optional[str], session: Optional[ManagedSession]) -> TaskOwner:
    if not session_id:
        return TaskOwner(kind=TaskOwnerKind.NONE)
    return TaskOwner(kind=TaskOwnerKind.SESSION, id=session_id, state=session_owner_state(session))


def _session_needs_user(session: Optional[ManagedSession]) -> bool:
    if session is None:
        return False
    return (
        session.status == ManagedSessionStatus.NEEDS_INPUT
        or session.runtime_status == AgentRuntimeStatus.ATTENTION
    )


def _holds(task: WorkspaceTask, session: Optional[ManagedSession]) -> bool:
    if session is None or task.status == WorkspaceTaskStatus.DONE:
        return False
    return (session.task_id or session.current_task_id) == task.id


def derive_task_lifecycle(facts: LifecycleFacts) -> TaskLifecycle:
    task = facts.task
    phase, reason, owner, since = _derive(facts)
    return TaskLifecycle(
        task_id=task.id,
        phase=phase,
        reason=reason,
        owner=owner,
        attention=phase in ATTENTION_PHASES,
        blocked_by_task_id=facts.blocked_by_task_id if phase == TaskPhase.QUEUED else None,
        holds_session=_holds(task, facts.worker_session),
        since=since or task.updated_at,
        legacy_status=task.status.value,
    )


def _derive(
    facts: LifecycleFacts,
) -> tuple[TaskPhase, str, TaskOwner, Optional[datetime]]:
    task = facts.task
    status = task.status
    if status == WorkspaceTaskStatus.DONE:
        reason = "accepted" if task.human_accepted_at else "completed"
        return TaskPhase.DONE, reason, TaskOwner(kind=TaskOwnerKind.NONE), task.completed_at
    if status == WorkspaceTaskStatus.FAILED:
        return TaskPhase.FAILED, "failed", TaskOwner(kind=TaskOwnerKind.NONE), task.failed_at
    if task.execution_control == TaskExecutionControl.INITIATOR:
        return _derive_initiator(facts)
    if status == WorkspaceTaskStatus.TODO:
        return _derive_draft(task)
    if status == WorkspaceTaskStatus.QUEUED:
        return _derive_queued(facts)
    if status == WorkspaceTaskStatus.WORKING:
        return _derive_working(facts)
    return _derive_review(facts)


def _derive_draft(
    task: WorkspaceTask,
) -> tuple[TaskPhase, str, TaskOwner, Optional[datetime]]:
    owner = TaskOwner(kind=TaskOwnerKind.NONE)
    if task.manual_aborted_at:
        return TaskPhase.DRAFT, "aborted", owner, task.manual_aborted_at
    if task.execution_epoch > 1:
        return TaskPhase.DRAFT, "handed_off_not_dispatched", owner, None
    if task.origin.value == "resident":
        return TaskPhase.DRAFT, "resident_proposal", owner, task.created_at
    return TaskPhase.DRAFT, "not_started", owner, task.created_at


def _derive_initiator(
    facts: LifecycleFacts,
) -> tuple[TaskPhase, str, TaskOwner, Optional[datetime]]:
    task = facts.task
    tab_id = task.source.tab_id if task.source else None
    owner = TaskOwner(
        kind=TaskOwnerKind.CHAT if tab_id else TaskOwnerKind.NONE,
        id=tab_id,
        state=chat_owner_state(task, facts.chat_tab) if tab_id else TaskOwnerState.UNKNOWN,
    )
    progress = task.latest_progress
    since = progress.reported_at if progress else None
    if task.execution_released or (progress and progress.state == TaskProgressState.RELEASED):
        return TaskPhase.ORPHANED, "initiator_released", owner, since
    if task.status == WorkspaceTaskStatus.TODO:
        if owner.state == TaskOwnerState.GONE:
            return TaskPhase.ORPHANED, "initiator_chat_gone", owner, None
        return TaskPhase.DRAFT, "registered_not_started", owner, task.created_at
    if owner.state == TaskOwnerState.GONE:
        return TaskPhase.ORPHANED, "initiator_chat_gone", owner, since
    if progress and progress.state == TaskProgressState.NEEDS_INPUT:
        return TaskPhase.NEEDS_USER, "initiator_needs_input", owner, since
    if progress and progress.state == TaskProgressState.BLOCKED:
        return TaskPhase.NEEDS_USER, "initiator_blocked", owner, since
    if task.status == WorkspaceTaskStatus.REVIEW:
        return TaskPhase.AWAITING_ACCEPTANCE, "initiator_ready", owner, since
    return TaskPhase.RUNNING, "initiator_working", owner, since or task.started_at


def _derive_queued(
    facts: LifecycleFacts,
) -> tuple[TaskPhase, str, TaskOwner, Optional[datetime]]:
    task = facts.task
    owner = _session_owner(task.session_id, facts.worker_session)
    since = task.queued_at
    if task.dispatch_pending:
        return TaskPhase.QUEUED, "awaiting_dispatch_decision", owner, since
    if facts.dependencies_blocked:
        return TaskPhase.QUEUED, "waiting_for_dependencies", owner, since
    if owner.kind == TaskOwnerKind.NONE:
        return TaskPhase.QUEUED, "no_agent_assigned", owner, since
    if owner.state == TaskOwnerState.GONE:
        return TaskPhase.ORPHANED, "assigned_agent_gone", owner, since
    if facts.blocked_by_task_id:
        return TaskPhase.QUEUED, "agent_held_by_other_task", owner, since
    return TaskPhase.QUEUED, "waiting_for_agent", owner, since


def _derive_working(
    facts: LifecycleFacts,
) -> tuple[TaskPhase, str, TaskOwner, Optional[datetime]]:
    task = facts.task
    owner = _session_owner(task.session_id, facts.worker_session)
    if owner.kind == TaskOwnerKind.NONE:
        return TaskPhase.ORPHANED, "working_without_agent", owner, None
    if owner.state == TaskOwnerState.GONE:
        return TaskPhase.ORPHANED, "agent_gone", owner, None
    if _session_needs_user(facts.worker_session):
        return TaskPhase.NEEDS_USER, "agent_needs_input", owner, None
    return TaskPhase.RUNNING, "agent_working", owner, task.started_at


def _derive_review(
    facts: LifecycleFacts,
) -> tuple[TaskPhase, str, TaskOwner, Optional[datetime]]:
    task = facts.task
    worker = _session_owner(task.session_id, facts.worker_session)
    run = task.autonomous_run
    if run and run.phase == AutonomousRunPhase.WAITING_FOR_HUMAN:
        return TaskPhase.NEEDS_USER, "autonomous_waiting_for_human", worker, None
    if task.review_requested_at and task.review_completed_at is None:
        reviewer = _session_owner(task.review_session_id, facts.reviewer_session)
        if reviewer.kind != TaskOwnerKind.NONE and reviewer.state == TaskOwnerState.GONE:
            return TaskPhase.ORPHANED, "reviewer_gone", reviewer, task.review_requested_at
        return TaskPhase.REVIEWING, "ai_review_in_progress", reviewer, task.review_requested_at
    verdict_at = task.review_completed_at
    if facts.latest_review_state == AgentReportState.REVIEW_NEEDS_INPUT:
        return TaskPhase.NEEDS_USER, "reviewer_needs_input", worker, verdict_at
    if facts.latest_review_state == AgentReportState.REVIEW_FAILED:
        return TaskPhase.NEEDS_USER, "review_failed_not_resumed", worker, verdict_at
    if task.human_acceptance_requested_at:
        return (
            TaskPhase.AWAITING_ACCEPTANCE,
            "awaiting_acceptance",
            worker,
            task.human_acceptance_requested_at,
        )
    if task.review_skipped_at:
        return TaskPhase.AWAITING_ACCEPTANCE, "review_skipped", worker, task.review_skipped_at
    if facts.latest_review_state == AgentReportState.REVIEW_PASSED:
        return TaskPhase.AWAITING_ACCEPTANCE, "review_passed", worker, verdict_at
    return TaskPhase.NEEDS_USER, "review_unrouted", worker, None
