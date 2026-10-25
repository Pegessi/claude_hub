"""Pure Task lifecycle derivation.

One function maps the persisted Task record plus observed owner facts to a
single ``TaskLifecycle``. It performs no I/O and never mutates state, so every
consumer (board, CLI, Dot, notifications, the future reconciler) sees the same
answer to "what phase is this Task in, who owns it, and must a human act?".
"""

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..models.schemas import (
    ATTENTION_PHASES,
    AgentReportState,
    AgentRuntimeStatus,
    AutonomousRunPhase,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
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


# A worker report moves the Task to review before a reviewer is selected or
# spawned; within this window an unrouted review is still being routed.
REVIEW_ROUTING_GRACE = timedelta(minutes=10)


@dataclass(frozen=True)
class LifecycleFacts:
    task: WorkspaceTask
    # Latest reviewer verdict for the Task's *current* review cycle only;
    # verdicts from earlier rounds no longer govern it.
    latest_review_state: Optional[AgentReportState] = None
    worker_session: Optional[ManagedSession] = None
    reviewer_session: Optional[ManagedSession] = None
    chat_tab: Optional[ChatTabFacts] = None
    dependencies_blocked: bool = False
    blocked_by_task_id: Optional[str] = None
    now: Optional[datetime] = None


def session_owner_state(session: Optional[ManagedSession]) -> TaskOwnerState:
    if session is None or session.archived:
        return TaskOwnerState.GONE
    if session.status == ManagedSessionStatus.STOPPED:
        return TaskOwnerState.GONE
    if session.runtime_status == AgentRuntimeStatus.OFFLINE:
        # Terminal OFFLINE persists as STOPPED; a managed Chat reports OFFLINE
        # for recoverable provider errors and keeps its session.
        if session.session_kind != SessionKind.CHAT:
            return TaskOwnerState.GONE
        return TaskOwnerState.UNKNOWN
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
        session.status in {ManagedSessionStatus.NEEDS_INPUT, ManagedSessionStatus.ERROR}
        or session.runtime_status == AgentRuntimeStatus.ATTENTION
    )


def _within(now: Optional[datetime], then: datetime, window: timedelta) -> bool:
    """True when ``then`` is recent, or when the clock cannot be compared."""
    if now is None or (now.tzinfo is None) != (then.tzinfo is None):
        return True
    return now - then < window


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
        reason_text=reason_text(reason),
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
    verdict = facts.latest_review_state
    # Only a final pass or a review skip requests acceptance, so these win over
    # any verdict text.
    if task.human_acceptance_requested_at:
        return (
            TaskPhase.AWAITING_ACCEPTANCE,
            "awaiting_acceptance",
            worker,
            task.human_acceptance_requested_at,
        )
    if task.review_skipped_at:
        return TaskPhase.AWAITING_ACCEPTANCE, "review_skipped", worker, task.review_skipped_at
    if verdict == AgentReportState.REVIEW_NEEDS_INPUT:
        return TaskPhase.NEEDS_USER, "reviewer_needs_input", worker, verdict_at
    if verdict == AgentReportState.REVIEW_FAILED:
        return TaskPhase.NEEDS_USER, "review_failed_not_resumed", worker, verdict_at
    if verdict == AgentReportState.REVIEW_PASSED:
        # A Goal Packet approval never requests acceptance; the Hub resumes
        # the worker (or its recovery retries), so nobody needs to accept yet.
        return TaskPhase.QUEUED, "plan_approved_resuming", worker, verdict_at
    if _within(facts.now, task.updated_at, REVIEW_ROUTING_GRACE):
        return TaskPhase.REVIEWING, "review_pending", worker, task.updated_at
    return TaskPhase.NEEDS_USER, "review_unrouted", worker, None


# ---------------------------------------------------------------------------
# Notices: what a human is told when a Task needs them. Pure text building;
# the monitor decides when and the injected notifier decides where.
# ---------------------------------------------------------------------------

NOTIFY_PHASES = frozenset(
    {TaskPhase.NEEDS_USER, TaskPhase.AWAITING_ACCEPTANCE, TaskPhase.ORPHANED, TaskPhase.FAILED}
)

REASON_TEXT = {
    "reviewer_needs_input": "AI reviewer has a question for you",
    "review_failed_not_resumed": "Review failed and was not sent back to the worker",
    "review_unrouted": "Waiting for review, but no reviewer picked it up",
    "agent_needs_input": "Agent is waiting for input or approval",
    "initiator_needs_input": "The originating Chat needs your input",
    "initiator_blocked": "The originating Chat reported a blocker",
    "autonomous_waiting_for_human": "Autonomous run is waiting for your decision",
    "awaiting_acceptance": "Finished; waiting for your acceptance",
    "review_skipped": "Finished (AI review skipped); waiting for your acceptance",
    "initiator_ready": "Finished; waiting for your acceptance",
    "agent_gone": "Its agent is gone (stopped, archived or deleted)",
    "working_without_agent": "Marked working, but no agent is assigned",
    "assigned_agent_gone": "The agent it was queued for is gone",
    "reviewer_gone": "Its reviewer is gone; the review cannot finish",
    "initiator_chat_gone": "The originating Chat was archived or deleted",
    "initiator_released": "The initiator released it and nobody took over",
    "failed": "Failed",
    "plan_approved_resuming": "Plan approved; resuming work",
    "agent_held_by_other_task": "Queued behind an agent held by",
    "awaiting_dispatch_decision": "Waiting for a dispatch decision",
    "waiting_for_dependencies": "Waiting for prerequisite Tasks",
    "no_agent_assigned": "No agent assigned yet",
    "waiting_for_agent": "Waiting for its agent to become free",
    "ai_review_in_progress": "AI review in progress",
    "review_pending": "Assigning a reviewer",
    "agent_working": "Agent working",
    "initiator_working": "Originating Chat working",
    "not_started": "Not started",
    "resident_proposal": "Resident agent proposal; not started",
    "aborted": "Aborted",
    "handed_off_not_dispatched": "Handed off but not dispatched",
    "registered_not_started": "Registered but not started",
    "accepted": "Accepted",
    "completed": "Completed",
}

# Notice text handed to Dot, in the user's language for Dot to relay.
NOTICE_REASON_TEXT = {
    "reviewer_needs_input": "AI reviewer 有問題需要你回答",
    "review_failed_not_resumed": "review 未通過，且沒有自動送回 worker",
    "review_unrouted": "等待 review 但沒有 reviewer 接手",
    "agent_needs_input": "agent 停在等待輸入或確認",
    "initiator_needs_input": "發起的 Chat 回報需要你的輸入",
    "initiator_blocked": "發起的 Chat 回報被阻塞",
    "autonomous_waiting_for_human": "自主模式在等你決定",
    "awaiting_acceptance": "已完成，等你驗收",
    "review_skipped": "已完成（跳過 AI review），等你驗收",
    "initiator_ready": "已完成，等你驗收",
    "agent_gone": "負責的 agent 已不在（停止、歸檔或刪除）",
    "working_without_agent": "狀態是進行中但沒有任何 agent",
    "assigned_agent_gone": "排隊目標 agent 已不在",
    "reviewer_gone": "reviewer 已不在，review 無人完成",
    "initiator_chat_gone": "發起它的 Chat 已歸檔或刪除",
    "initiator_released": "發起方已放棄執行，沒有人接手",
    "failed": "執行失敗",
    "plan_approved_resuming": "計劃已批准，正在恢復執行",
    "agent_held_by_other_task": "排在另一個 Task 佔用的 agent 後面",
    "awaiting_dispatch_decision": "等待派發決定",
    "waiting_for_dependencies": "等待前置 Task 完成",
    "no_agent_assigned": "尚未分配 agent",
    "waiting_for_agent": "等待 agent 空閒",
    "ai_review_in_progress": "AI review 進行中",
    "review_pending": "正在分配 reviewer",
    "agent_working": "agent 執行中",
    "initiator_working": "發起的 Chat 執行中",
    "not_started": "尚未開始",
    "resident_proposal": "常駐 agent 的提案，尚未開始",
    "aborted": "已中止",
    "handed_off_not_dispatched": "已交接但尚未派發",
    "registered_not_started": "已登記但尚未開始",
    "accepted": "已驗收",
    "completed": "已完成",
}

# Next steps differ by board status: `task continue` only reopens review or
# failed Tasks; a working Task is steered with a follow-up message.
NEXT_STEP_TEXT = {
    "working": "回覆或調整方向：`claude-hub task followup {ws} {id} --message ...`，或在飛書直接回覆 dot。",
    "review": "回覆或要求修改：`claude-hub task continue {id} --message ...`；驗收：`claude-hub task accept {id}`。",
    "failed": "重試：`claude-hub task continue {id} --message ...`。",
    "queued": "可在看板重新派發，或關閉 `claude-hub task abort {id} --reason ...`。",
    "todo": "可在看板派發，或關閉 `claude-hub task abort {id} --reason ...`。",
}
ORPHANED_STEP_TEXT = "決定去向：在看板重新派發，或關閉 `claude-hub task abort {id} --reason ...`。"

PHASE_TONE = {
    TaskPhase.NEEDS_USER: "warning",
    TaskPhase.AWAITING_ACCEPTANCE: "success",
    TaskPhase.ORPHANED: "warning",
    TaskPhase.FAILED: "error",
}


@dataclass(frozen=True)
class TaskNotice:
    kind: str  # "attention" or "plan"
    title: str
    text: str
    tone: str = "info"
    phase: str = ""
    reason: str = ""


# Delivers a notice for a Task and reports ``sent``; ``unroutable`` when
# nobody can be reached (not retried); ``failed`` when delivery failed (retried).
TaskNotifier = Callable[[WorkspaceTask, TaskNotice], Awaitable[str]]


def reason_text(reason: str) -> str:
    return REASON_TEXT.get(reason, reason)


def notice_reason_text(reason: str) -> str:
    return NOTICE_REASON_TEXT.get(reason) or reason_text(reason)


def attention_notice(
    task: WorkspaceTask, phase: TaskPhase, reason: str, detail: str = ""
) -> TaskNotice:
    lines = [f"**{notice_reason_text(reason)}**", f"Task `{task.id}`（{task.status.value}）"]
    step = (
        ORPHANED_STEP_TEXT
        if phase == TaskPhase.ORPHANED
        else NEXT_STEP_TEXT.get(task.status.value, "")
    )
    if step:
        lines += [step.format(id=task.id, ws=task.workspace_id)]
    if detail.strip():
        lines += ["", _clip(detail.strip(), 600)]
    return TaskNotice(
        kind="attention",
        title=f"{_clip(task.title, 60)}",
        text="\n".join(lines),
        tone=PHASE_TONE.get(phase, "info"),
        phase=phase.value,
        reason=reason,
    )


def plan_digest(task: WorkspaceTask) -> Optional[str]:
    packet = task.goal_packet
    if packet is None or not packet.objective.strip():
        return None
    payload = "\n".join([packet.objective, *packet.acceptance_criteria, *packet.out_of_scope])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def plan_notice(task: WorkspaceTask) -> Optional[TaskNotice]:
    """Early checkpoint: the worker's plan, while it is still cheap to redirect."""

    packet = task.goal_packet
    if packet is None or not packet.objective.strip():
        return None
    lines = [
        "**計劃已提交**（審核或執行中）",
        f"Task `{task.id}`",
        "",
        f"目標：{_clip(packet.objective, 400)}",
    ]
    if packet.acceptance_criteria:
        lines += ["", "完成標準："]
        lines += [f"- {_clip(item, 160)}" for item in packet.acceptance_criteria[:6]]
    if packet.out_of_scope:
        lines += ["", "不做："]
        lines += [f"- {_clip(item, 160)}" for item in packet.out_of_scope[:4]]
    lines += [
        "",
        "方向不對可隨時調整："
        f"`claude-hub task followup {task.workspace_id} {task.id} --message ...`，或在飛書回覆 dot。",
    ]
    return TaskNotice(kind="plan", title=f"計劃：{_clip(task.title, 50)}", text="\n".join(lines))


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" not in text else text
    return text if len(text) <= limit else text[: limit - 1] + "…"
