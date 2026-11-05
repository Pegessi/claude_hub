"""Single Task liveness evaluator, recovery budget and attention delivery owner.

Provider/mailbox adapters supply facts and enforce this owner's admission
fences; they never decide Task recovery or maintain another retry budget.
"""

from dataclasses import dataclass
from typing import Callable

import claude_hub.services.managed_chat as managed_chat
from claude_hub.models.schema_domains.lifecycle import TaskLiveness
from claude_hub.models.schemas import (
    TERMINAL_PHASES,
    TaskExecutionControl,
    TaskLifecycleMark,
    TaskPhase,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from claude_hub.services.agent_stream.base import NativeInputNotSubmittedError
from claude_hub.services.task_graph import task_supervisor_consumer_key
from claude_hub.services.task_lifecycle import (
    NOTIFY_PHASES,
    TaskNotice,
    TaskNotifier,
    attention_notice,
    plan_digest,
    plan_notice,
)
from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403

# A phase must hold this long before it is announced, so a momentary state
# does not page anyone.
NOTICE_SETTLE_SECONDS = 30
# Reasons inferred only from sampled runtime output (a terminal that looks
# stuck, an uncertain Chat delivery) must hold longer before they page.
HEURISTIC_REASONS = frozenset({"agent_needs_input"})
HEURISTIC_SETTLE_SECONDS = 180
# The same reason is not announced again within this window, even if the
# phase flapped away and back.
NOTICE_REPEAT_COOLDOWN = timedelta(minutes=30)
NOTICE_MAX_ATTEMPTS = 6
NOTICE_MAX_BACKOFF_SECONDS = 3600
# One send may not hold the delivery job longer than this.
NOTICE_SEND_TIMEOUT_SECONDS = 20
NOTICES_PER_PASS = 5
# Conditions already older than this when first observed (e.g. on upgrade)
# are recorded and visible on the board, but not pushed.
NOTICE_BACKFILL_HORIZON = timedelta(hours=24)
# Initiator Chats report these themselves, so their own reply already
# reached the person; a second notice would be a duplicate.
SELF_REPORTED_REASONS = frozenset({"initiator_needs_input", "initiator_blocked", "initiator_ready"})


# How long an owner may stay gone before the reconciler resolves the Task, and
# the reason recorded. Agent loss is resolved quickly because `task continue`
# puts a failed Task back to work; a Chat loss waits for a week because only a
# handoff can revive a failed initiator record.
OWNER_LOSS_POLICY: dict[str, tuple[timedelta, str]] = {
    "agent_gone": (
        timedelta(minutes=5),
        "Its agent is gone (stopped, archived or deleted). "
        "`claude-hub task continue <id>` puts it back to work on an available agent.",
    ),
    "working_without_agent": (
        timedelta(minutes=5),
        "Marked working without any agent. "
        "`claude-hub task continue <id>` puts it back to work on an available agent.",
    ),
    "initiator_chat_gone": (
        timedelta(days=7),
        "The originating Chat was archived or deleted and nobody took over. "
        "`claude-hub task handoff` can give it to a new executor.",
    ),
}


QUICKLY_RESOLVED_REASONS = frozenset(
    reason for reason, (grace, _text) in OWNER_LOSS_POLICY.items() if grace <= timedelta(hours=1)
)


MAX_RECOVERY_ATTEMPTS = 2


@dataclass(frozen=True)
class LivenessFacts:
    """Observations, never competing recovery policies.

    A future registered job/timer contributes ``wake_deadline`` here. Its
    producer must renew or complete that obligation; it gets no recovery loop.
    """

    task: WorkspaceTask
    session: ManagedSession | None
    now: datetime
    activity: datetime
    reviewing: bool = False
    resume_verdict: bool = False
    active: bool = False
    dead: bool = False
    outage: bool = False
    blocked: str = ""
    prompt_pending: bool = False
    interruption: str = ""
    completion: bool = False
    owner_loss_reason: str = ""
    watch_deadline: datetime | None = None
    watch_activity: datetime | None = None
    wake_deadline: datetime | None = None


@dataclass(frozen=True)
class LivenessDecision:
    reason: str
    action: str = "wait"
    detail: str = ""


def evaluate_liveness(f: LivenessFacts, state: TaskLiveness) -> LivenessDecision:
    """The sole Task recovery policy, including all deadlines and budgets."""
    task, now = f.task, f.now
    if task.status in {
        WorkspaceTaskStatus.TODO,
        WorkspaceTaskStatus.DONE,
        WorkspaceTaskStatus.FAILED,
    }:
        if not f.owner_loss_reason:
            return LivenessDecision("inactive")
    if task.human_acceptance_requested_at is not None:
        return LivenessDecision("human_acceptance")
    # Explicit SUBAGENT report-inactivity budgets remain authoritative. This
    # records failure; it never interrupts or resets the running provider turn.
    if (
        task.task_mode == WorkspaceTaskMode.SUBAGENT
        and task.status == WorkspaceTaskStatus.WORKING
        and task.timeout_seconds
        and (now - task.updated_at).total_seconds() > task.timeout_seconds
    ):
        return LivenessDecision("task_timeout", "fail", f"timeout after {task.timeout_seconds}s")
    # A real question may block a still-open native turn. Routing it never
    # interrupts, answers, resets or otherwise changes that turn.
    if f.active and not f.blocked:
        return LivenessDecision("active_turn")
    if state.escalated_at is not None:
        return LivenessDecision(state.reason)
    if f.wake_deadline is not None and now < f.wake_deadline:
        return LivenessDecision("registered_wake")
    if f.blocked:
        if (now - state.since).total_seconds() < HEURISTIC_SETTLE_SECONDS:
            return LivenessDecision("blocked_runtime")
        return LivenessDecision("blocked_runtime", "escalate", f.blocked)
    age = (now - f.activity).total_seconds()
    if f.owner_loss_reason and task.execution_control == TaskExecutionControl.INITIATOR:
        grace, detail = OWNER_LOSS_POLICY[f.owner_loss_reason]
        return LivenessDecision(
            f.owner_loss_reason, "fail" if now - state.since >= grace else "wait", detail
        )
    if (
        task.execution_control != TaskExecutionControl.WORKSPACE
        or task.status == WorkspaceTaskStatus.QUEUED
    ):
        if f.watch_deadline is not None and now >= f.watch_deadline:
            return LivenessDecision(
                "watch_stalled", "escalate", "No progress within the configured Dot watch interval."
            )
        return LivenessDecision("waiting_for_dispatch")
    if f.dead:
        if f.outage:
            return LivenessDecision(
                "runtime_outage",
                "escalate",
                "All terminal agents look unavailable; automatic destructive recovery is held.",
            )
        if f.reviewing:
            action, delay, reason = "replace_reviewer", 60, "review_owner_lost"
        elif task.task_mode == WorkspaceTaskMode.SUBAGENT:
            return LivenessDecision("agent_gone", "fail", "session died")
        elif task.system_internal and task.internal_kind == "scheduled":
            action, delay, reason = "replace_worker", 60, "scheduled_owner_lost"
        elif task.system_internal and task.internal_kind == "feedback_reaper":
            return LivenessDecision("agent_gone", "escalate", "Feedback worker is unavailable.")
        else:
            reason = f.owner_loss_reason or "agent_gone"
            grace, detail = OWNER_LOSS_POLICY.get(reason, OWNER_LOSS_POLICY["agent_gone"])
            return LivenessDecision(
                reason, "fail" if now - state.since >= grace else "wait", detail
            )
        if age < delay:
            return LivenessDecision(reason)
    elif f.resume_verdict:
        action, delay, reason = "resume_verdict", 0, "sealed_verdict_handoff"
    elif task.status == WorkspaceTaskStatus.REVIEW and not f.reviewing:
        return LivenessDecision("review_wait")
    elif f.prompt_pending:
        action, delay, reason = "submit_prompt", 20, "prompt_pending"
        if state.action == "submit_prompt" and state.action_at is not None:
            if (now - state.action_at).total_seconds() < 10:
                return LivenessDecision(reason)
            return LivenessDecision(
                reason,
                "escalate",
                "Prompt remains in the terminal input box after submission retry.",
            )
    else:
        reason = "review_idle" if f.reviewing else "worker_idle"
        delay = settings.review_verdict_idle_timeout_seconds if f.reviewing else 60
        action = "nudge"
        if f.completion:
            delay = 20
        if f.interruption:
            delay = 20
            # One shared budget: a safe Claude context reset can be the second
            # recovery action, never an independent hard-recovery allowance.
            if state.attempts == 1 and f.session and f.session.agent_type == AgentType.CLAUDE:
                action = "reset_context"
    if age < delay or (
        state.action_at is not None and (now - state.action_at).total_seconds() < delay
    ):
        return LivenessDecision(reason)
    if state.attempts >= MAX_RECOVERY_ATTEMPTS:
        action = "fail" if reason == "scheduled_owner_lost" else "escalate"
        return LivenessDecision(
            reason,
            action,
            "Recovery budget exhausted after two attempts; operator action is required.",
        )
    return LivenessDecision(reason, action, f.interruption)


class _TaskLivenessMixin(_WorkspaceManagerBase):
    def configure_liveness_watch(
        self, source: Callable[[WorkspaceTask], tuple[datetime, float] | None]
    ) -> None:
        self._liveness_watch_source = source

    @staticmethod
    def _liveness_reviewing(task: WorkspaceTask) -> bool:
        return not state_policy.current_round_has_verdict(
            task.review_cycle, task.reviewed_cycle
        ) and (
            state_policy.review_in_flight(task.review_requested_at, task.review_completed_at)
            or (task.status == WorkspaceTaskStatus.REVIEW and not task.review_completed_at)
            or (
                task.task_mode == WorkspaceTaskMode.REVIEWED
                and task.goal_packet is not None
                and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
            )
        )

    async def _liveness_facts(
        self, task: WorkspaceTask, *, ignore_call_id: str | None = None
    ) -> LivenessFacts:
        now = self._runtime_now()
        sealed = state_policy.current_round_has_verdict(task.review_cycle, task.reviewed_cycle)
        reviewing = self._liveness_reviewing(task)
        resume = (
            sealed
            and task.status == WorkspaceTaskStatus.REVIEW
            and not task.human_acceptance_requested_at
            and not self._task_has_post_cycle_report(task.id, task.review_cycle)
            and self._sealed_cycle_verdict_state(task.id, task.review_cycle)
            in {AgentReportState.REVIEW_PASSED, AgentReportState.REVIEW_FAILED}
        )
        session_id = task.review_session_id if reviewing else task.session_id
        session = self.sessions.get(session_id or "")
        bound = (
            session is not None
            and session.workspace_id == task.workspace_id
            and (session.current_task_id or session.task_id) == task.id
        )
        dead = (
            session is None
            or not bound
            or session.archived
            or session.status == ManagedSessionStatus.STOPPED
        )
        active, blocked, pending, interruption, completion = False, "", False, "", False
        activity = [task.started_at or task.updated_at]
        if reviewing:
            activity = [
                task.review_requested_at
                or (task.goal_packet.updated_at if task.goal_packet else None)
                or task.updated_at
            ]
        if session is not None and bound and not dead:
            if session.last_activity_at:
                activity.append(session.last_activity_at)
            runtime = session.runtime_status
            if session.session_kind == SessionKind.CHAT:
                runtime = managed_chat.runtime_status(session)
                if managed_chat.has_pending_question(session):
                    blocked = "A provider question or permission request needs a human answer."
                elif session.chat_paused:
                    blocked = "Chat was explicitly paused; resume it when ready."
                elif session.uncertain_call_ids or managed_chat.question_input_blocked(
                    session, "liveness"
                ):
                    blocked = "Chat delivery or a submitted answer is uncertain; inspect its receipt before retrying."
                elif runtime != AgentRuntimeStatus.WORKING and managed_chat.is_turn_running(
                    session
                ):
                    blocked = (
                        "Chat turn ownership is unknown; inspect the provider before resuming."
                    )
            else:
                dead = runtime == AgentRuntimeStatus.OFFLINE
            active = (
                runtime == AgentRuntimeStatus.WORKING
                or session.status == ManagedSessionStatus.SPAWNING
            )
            if (
                not blocked
                and not dead
                and (not active or session.session_kind == SessionKind.TERMINAL)
            ):
                if runtime in {AgentRuntimeStatus.ATTENTION, AgentRuntimeStatus.OFFLINE}:
                    blocked = "Provider needs attention (permission, quota or transport cause is not yet established)."
                elif session.uncertain_call_ids:
                    blocked = "Message delivery is uncertain; automatic replay is held."
                elif any(c != ignore_call_id for c in session.pending_call_ids):
                    blocked = "A pending message has not reached its executor; inspect the mailbox."
                elif session.session_kind == SessionKind.TERMINAL:
                    try:
                        output = await asyncio.wait_for(
                            self._capture_tmux_output(session.tmux_session), timeout=2
                        )
                    except Exception:
                        output = ""
                        blocked = "Terminal observation is unavailable; inspect the runtime before sending input."
                    active = active or self._auto_continue_output_looks_busy(output)
                    pending = any(
                        self._message_still_in_input(output, prefix)
                        for prefix in self._expected_pending_prompt_prefixes(session, task)
                    )
                    if pending:
                        active = False  # Exact unsent input is not an executing turn.
                    interruption = self._auto_continue_interruption_reason(output) or ""
                    completion = bool(self._auto_continue_completion_reason(output))
        for report in self.reports.values():
            if (
                report.task_id == task.id
                and report.review_cycle == task.review_cycle
                and report.session_id == session_id
            ):
                activity.append(report.created_at)
                if report.risk_level != PROMPT_STUCK_RISK_LEVEL and report.state in {
                    AgentReportState.BLOCKED,
                    AgentReportState.NEEDS_INPUT,
                    AgentReportState.REVIEW_NEEDS_INPUT,
                }:
                    # Only the most recent report may park the obligation.
                    latest = self._latest_report_for_task(task.id)
                    if latest and latest.id == report.id:
                        blocked = report.message or "Executor explicitly requested human input."
        if dead and not reviewing and task.system_internal and task.internal_kind == "scheduled":
            activity = [task.updated_at]
        lifecycle = self.task_lifecycles([task])[task.id]
        owner_reason = lifecycle.reason if lifecycle.reason in OWNER_LOSS_POLICY else ""
        deadline = watch_activity = None
        source = getattr(self, "_liveness_watch_source", None)
        watch = source(task) if source is not None else None
        if watch is not None:
            watched_at, hours = watch
            moments = activity + [task.updated_at, watched_at]
            if task.latest_progress:
                moments.append(task.latest_progress.reported_at)
            if task.runtime_observation:
                moments.append(task.runtime_observation.observed_at)
            watch_activity = max(moments)
            deadline = watch_activity + timedelta(hours=hours)
        return LivenessFacts(
            task=task,
            session=session,
            now=now,
            activity=max(activity),
            reviewing=reviewing,
            resume_verdict=resume,
            active=active,
            dead=dead,
            outage=self._workspace_agents_all_lost(task.workspace_id),
            blocked=blocked,
            prompt_pending=pending,
            interruption=interruption,
            completion=completion,
            owner_loss_reason=owner_reason,
            watch_deadline=deadline,
            watch_activity=watch_activity,
        )

    async def _tick_task_lifecycles(self, workspace_id: str) -> None:
        """Project current verdicts and schedule delivery; no second evaluator."""
        async with self.workspace_mutation_lock(workspace_id):
            self._observe_task_lifecycles(workspace_id)
        if self._has_pending_task_notices(workspace_id):
            self._schedule_task_notices(workspace_id)

    async def _check_task_liveness(self, task_id: str) -> None:
        lock = self._liveness_locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            await self._evaluate_task_liveness(task_id)

    async def _evaluate_task_liveness(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.status in {WorkspaceTaskStatus.DONE, WorkspaceTaskStatus.FAILED}:
            return
        facts = await self._liveness_facts(task)
        async with self.workspace_mutation_lock(task.workspace_id):
            # Observation awaits cannot authorize effects against newer reports,
            # bindings, manual pause or a recycled friendly session id.
            if self.tasks.get(task_id) != task or (
                facts.session is not None and self.sessions.get(facts.session.id) != facts.session
            ):
                return
            generation = f"{task.execution_epoch}:{task.review_cycle}"
            state = task.liveness
            if state is None:
                state = self._initial_liveness(task, facts, generation)
            elif state.generation != generation:
                state = TaskLiveness(generation=generation, since=facts.now)
            watch_advanced = (
                state.reason == "watch_stalled"
                and facts.watch_activity is not None
                and state.watch_activity_at is not None
                and facts.watch_activity > state.watch_activity_at
            )
            if state.escalated_at and ((facts.active and not facts.blocked) or watch_advanced):
                # Concrete resumed execution closes attention but keeps the
                # recovery budget. Later violations form a new notice incident.
                state = state.model_copy(
                    update={
                        "escalated_at": None,
                        "detail": "",
                        "incident": state.incident + 1,
                        "since": facts.now,
                    }
                )
            state = state.model_copy(update={"watch_activity_at": facts.watch_activity})
            decision = evaluate_liveness(facts, state)
            if state.reason != decision.reason:
                state = state.model_copy(update={"reason": decision.reason, "since": facts.now})
                decision = evaluate_liveness(facts, state)
            # A claimed non-idempotent effect cannot be replayed after a crash.
            # Durable message delivery alone can safely resume the same envelope.
            if state.action_id and not state.action_finished:
                if (
                    state.action == "nudge"
                    and facts.session
                    and state.session_id == facts.session.id
                    and state.session_created_at == facts.session.created_at
                    and not facts.active
                    and not facts.blocked
                    and state.action_at is not None
                    and (facts.now - state.action_at).total_seconds()
                    < settings.review_verdict_idle_timeout_seconds
                ):
                    decision = LivenessDecision(state.reason, "nudge")
                elif not facts.active:
                    decision = LivenessDecision(
                        "recovery_uncertain",
                        "escalate",
                        "A claimed recovery action has no completion receipt; inspect before retrying.",
                    )
            if decision.action == "wait":
                if state != task.liveness:
                    self.tasks[task_id] = task.model_copy(update={"liveness": state})
                    self._save_workspace_state(task.workspace_id)
                return
            if decision.action in {"fail", "escalate"}:
                self._liveness_escalate(task, state, decision, facts.now)
                if (
                    decision.action == "fail"
                    and facts.session
                    and task.internal_kind == "scheduled"
                ):
                    await self._liveness_retire_worker(self.tasks[task.id], facts.session)
                return
            if state.action_finished or state.action_id is None:
                attempt = state.attempts + 1
                call_id = f"liveness:{task.id}:{generation}:{attempt}"
                message = self._liveness_message(task, facts, call_id)
                state = state.model_copy(
                    update={
                        "attempts": attempt,
                        "action": decision.action,
                        "action_id": call_id,
                        "action_at": facts.now,
                        "action_finished": False,
                        "session_id": facts.session.id if facts.session else None,
                        "session_created_at": facts.session.created_at if facts.session else None,
                        "message": message,
                    }
                )
            self.tasks[task_id] = task.model_copy(update={"liveness": state})
            self._save_workspace_state(task.workspace_id)
            task = self.tasks[task_id]
        await self._execute_liveness_action(task, facts, state)

    def _initial_liveness(
        self, task: WorkspaceTask, facts: LivenessFacts, generation: str
    ) -> TaskLiveness:
        # Consume old persisted recovery evidence once, without resetting the
        # budget on upgrade. Legacy envelopes retain their exact payload and ID.
        session = facts.session
        attempts = (
            max(0, task.dispatch_attempt - 1)
            if task.system_internal and task.internal_kind == "scheduled"
            else 0
        )
        if session is not None:
            if session.auto_continue_task_id == task.id:
                attempts += session.auto_continue_attempts
            if session.hard_recovery_task_id == task.id:
                attempts += session.hard_recovery_attempts
            if session.prompt_retry_task_id == task.id:
                attempts += 1
        state = TaskLiveness(generation=generation, since=facts.now, attempts=min(attempts, 2))
        expected_round = (
            f"{task.review_cycle}:{task.review_attempts}:{task.review_requested_at.isoformat()}:{task.review_session_id}"
            if task.review_requested_at
            else None
        )
        if (
            facts.reviewing
            and task.review_delivery_round == expected_round
            and task.review_delivery_message
        ):
            state = state.model_copy(
                update={
                    "attempts": max(1, state.attempts),
                    "action": "nudge",
                    "action_id": f"review-delivery:{task.id}:{task.review_delivery_round}",
                    "action_at": task.review_delivery_nudged_at or facts.now,
                    "message": task.review_delivery_message,
                    "session_id": task.review_session_id,
                    "session_created_at": session.created_at if session else None,
                    "escalated_at": task.review_delivery_escalated_at,
                }
            )
        if task.lifecycle_mark and facts.owner_loss_reason:
            state = state.model_copy(
                update={"since": task.lifecycle_mark.changed_at, "reason": facts.owner_loss_reason}
            )
        return state

    def _liveness_message(self, task: WorkspaceTask, facts: LivenessFacts, call_id: str) -> str:
        message = (
            f"Continue the assigned Task {task.id}. Hub observed an idle executor without its required handoff. "
            "Inspect current evidence and resume the same work; do not redo completed actions. "
            "If blocked, report the concrete blocker. A Chat reply alone does not record progress. "
            f"ACK this wake in acked_call_ids: {call_id}."
        )
        if facts.reviewing and facts.session:
            message += (
                " Resume this SAME review round and submit its verdict using the original verdict call_id. Never assume approval.\n\n"
                + self._review_submission_block(task, facts.session)
            )
        elif facts.session:
            message += "\n\n" + self._report_endpoint_curl(
                facts.session,
                task.id,
                purpose="liveness-progress",
                attempt=int(call_id.rsplit(":", 1)[1]),
            )
        return message

    def _liveness_escalate(
        self, task: WorkspaceTask, state: TaskLiveness, decision: LivenessDecision, now: datetime
    ) -> None:
        state = state.model_copy(
            update={
                "reason": decision.reason,
                "escalated_at": now,
                "detail": decision.detail,
                "action_finished": True,
            }
        )
        update: dict[str, Any] = {"liveness": state}
        if decision.action == "fail":
            update.update(
                status=WorkspaceTaskStatus.FAILED,
                failure_reason=decision.detail,
                failed_at=now,
                human_acceptance_requested_at=now,
                updated_at=now,
            )
        self.tasks[task.id] = apply_task_update(task, update, cause="reconcile")
        event, _created = self.task_mailbox.append_event(
            workspace_id=task.workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.RUNTIME,
            event_type=TaskEventType.NEEDS_INPUT,
            call_id=f"liveness:{task.id}:{state.generation}:{state.incident}:escalated",
            action="liveness:attention",
            review_cycle=task.review_cycle,
            consumer_key=task_supervisor_consumer_key(task),
            payload={
                "reason": decision.reason,
                "message": decision.detail,
                "attempts": state.attempts,
            },
            persist=False,
        )
        self._save_workspace_state(task.workspace_id)
        self.task_mailbox.wake_events([event])

    def _liveness_delivery_current(self, session: ManagedSession, call_id: str) -> bool:
        """Ownership fence for reserved recovery envelopes, including old upgrades."""
        if not call_id.startswith(("liveness:", "review-delivery:")):
            return True
        task = self.tasks.get(call_id.split(":", 2)[1])
        state = task.liveness if task else None
        return bool(
            task
            and state
            and state.action_id == call_id
            and not state.action_finished
            and state.generation == f"{task.execution_epoch}:{task.review_cycle}"
            and task.execution_control == TaskExecutionControl.WORKSPACE
            and task.status in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}
            and task.human_acceptance_requested_at is None
            and state.session_id == session.id
            and state.session_created_at == session.created_at
            and session.workspace_id == task.workspace_id
            and (session.current_task_id or session.task_id) == task.id
            and session.id
            == (
                task.review_session_id
                if self._liveness_reviewing(task)
                else task.session_id if task.status == WorkspaceTaskStatus.WORKING else None
            )
            and not session.archived
            and not session.chat_paused
            and session.status != ManagedSessionStatus.STOPPED
            and not session.uncertain_call_ids
            and (
                session.session_kind != SessionKind.CHAT
                or not (
                    managed_chat.has_pending_question(session)
                    or managed_chat.question_input_blocked(session, call_id)
                )
            )
        )

    async def _liveness_delivery_allowed(self, session_id: str, call_id: str) -> bool:
        """Called under mailbox locks at admission and every pre-submit retry."""
        if not call_id.startswith(("liveness:", "review-delivery:")):
            return True
        session = self.sessions[session_id]
        if not self._liveness_delivery_current(session, call_id):
            return False
        task = self.tasks[call_id.split(":", 2)[1]]
        facts = await self._liveness_facts(task, ignore_call_id=call_id)
        return bool(
            not facts.active
            and not facts.blocked
            and not facts.dead
            and facts.session
            and facts.session.id == session_id
            and self.tasks.get(task.id) == task
            and self._liveness_delivery_current(self.sessions[session_id], call_id)
        )

    def _liveness_native_guard(self, session_id: str, call_id: str) -> Callable[[], None] | None:
        if not call_id.startswith(("liveness:", "review-delivery:")):
            return None

        def guard() -> None:
            # Native input owns the turn lock here. Its own "sending" receipt
            # must not be mistaken for another active turn. This final callback
            # runs immediately before provider submission, after all startup waits.
            session = self.sessions.get(session_id)
            if session is None or not self._liveness_delivery_current(session, call_id):
                raise NativeInputNotSubmittedError("Task recovery wake was superseded")

        return guard

    def _retire_liveness_delivery(self, session_id: str, call_id: str) -> None:
        """Discard only proven-unsent recovery input; never manufacture an ACK."""
        session = self.sessions[session_id]
        if call_id in (
            session.processing_call_ids + session.uncertain_call_ids + session.delivered_call_ids
        ):
            return
        if (
            session.session_kind == SessionKind.CHAT
            and self._chat_delivery_state(session, call_id) != "not_sent"
        ):
            return
        update: dict[str, Any] = {
            "pending_call_ids": [c for c in session.pending_call_ids if c != call_id],
        }
        for key in ("pending_messages", "pending_attachments", "call_payload_fingerprints"):
            update[key] = {k: v for k, v in getattr(session, key).items() if k != call_id}
        self.sessions[session_id] = session.model_copy(update=update)
        task = self.tasks.get(call_id.split(":", 2)[1])
        if task:
            task_update: dict[str, Any] = {
                "pending_call_ids": [c for c in task.pending_call_ids if c != call_id]
            }
            if task.liveness and task.liveness.action_id == call_id:
                # Keep the spent claim ID/budget so no caller can resurrect it.
                task_update["liveness"] = task.liveness.model_copy(
                    update={
                        "action_finished": True,
                        "action": "cancelled_nudge",
                    }
                )
            self.tasks[task.id] = task.model_copy(update=task_update)
        self._save_workspace_state(session.workspace_id)

    async def _execute_liveness_action(
        self, task: WorkspaceTask, facts: LivenessFacts, state: TaskLiveness
    ) -> None:
        session = facts.session
        try:
            # Refresh provider truth immediately before any effect. Mailbox
            # delivery applies its own seat/incarnation and active-turn fences.
            fresh = await self._liveness_facts(task)
            if fresh.active or fresh.blocked or self.tasks.get(task.id) != task:
                await self._cancel_liveness_claim(task, state)
                return
            if session is not None and self.sessions.get(session.id) != session:
                await self._cancel_liveness_claim(task, state)
                return
            if state.action == "nudge" and session:
                await self.send_session_message(
                    session.id, state.message or "", call_id=state.action_id
                )
                live_session = self.sessions.get(session.id)
                if live_session is None or state.action_id not in (
                    live_session.processing_call_ids + live_session.delivered_call_ids
                ):
                    return  # Enqueue is not provider acceptance.
            elif state.action in {"submit_prompt", "reset_context"} and session:
                # Controls must share the same workspace -> pump ordering as
                # ordinary Hub input. Revalidate after lock contention; a newer
                # turn or Task cycle wins before Enter or /clear can be sent.
                async with self.workspace_mutation_lock(task.workspace_id):
                    guarded = await self._liveness_facts(task)
                    if (
                        guarded.active
                        or guarded.blocked
                        or self.tasks.get(task.id) != task
                        or self.sessions.get(session.id) != session
                    ):
                        await self._cancel_liveness_claim(task, state)
                        return
                    if state.action == "submit_prompt":
                        await self._run_tmux("send-keys", "-t", session.tmux_session, "Enter")
                    else:
                        await self._reset_session_context(session)
                        current = self._current_monitor_entities(session, task)
                        if current is None:
                            return
                        workspace = self.workspaces[task.workspace_id]
                        prompt = self._build_hard_recovery_worker_prompt(
                            workspace,
                            task,
                            session,
                            facts.interruption,
                            recovery_attempt=state.attempts,
                        )
                        if facts.reviewing:
                            prompt = self._build_hard_recovery_reviewer_fallback_prompt(
                                task, session, facts.interruption, recovery_attempt=state.attempts
                            )
                        await self.send_session_message(session.id, prompt, call_id=state.action_id)
            elif state.action == "resume_verdict":
                await self.continue_task(
                    task.id,
                    ContinueTaskRequest(
                        message="Continue workspace task from review (liveness recovered sealed verdict)."
                    ),
                    # This is a normal, sealed-verdict transition into a new
                    # cycle. Its durable continuation is not an old-cycle nudge.
                    call_id=f"continue:{state.action_id}",
                )
            elif state.action == "replace_reviewer":
                await self._liveness_replace_reviewer(task, facts.now)
            elif state.action == "replace_worker":
                await self._liveness_replace_worker(task, facts.now)
        except Exception:
            logger.exception("Liveness action failed task_id=%s action=%s", task.id, state.action)
            # Persisted claim remains: a failed/ambiguous effect never creates
            # unlimited new wake IDs, resets or replacement agents.
            return
        async with self.workspace_mutation_lock(task.workspace_id):
            live = self.tasks.get(task.id)
            if live and live.liveness and live.liveness.action_id == state.action_id:
                self.tasks[task.id] = live.model_copy(
                    update={"liveness": live.liveness.model_copy(update={"action_finished": True})}
                )
                self._save_workspace_state(task.workspace_id)

    async def _cancel_liveness_claim(self, task: WorkspaceTask, state: TaskLiveness) -> None:
        """A pre-effect fence proves this claim did nothing; release its budget."""
        async with self.workspace_mutation_lock(task.workspace_id):
            live = self.tasks.get(task.id)
            if live and live.liveness and live.liveness.action_id == state.action_id:
                cancelled = live.liveness.model_copy(
                    update={
                        "action_finished": True,
                        "action": None,
                        "action_id": None,
                        "message": None,
                        "action_at": None,
                        "attempts": max(0, state.attempts - 1),
                    }
                )
                self.tasks[task.id] = live.model_copy(update={"liveness": cancelled})
                self._save_workspace_state(task.workspace_id)

    async def _liveness_replace_reviewer(self, task: WorkspaceTask, now: datetime) -> None:
        if not task.session_id:
            return
        pending_gp = (
            task.goal_packet is not None
            and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
        )
        self._release_stale_reviewer_for_task(task, updated_at=now)
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=task.session_id,
            state=AgentReportState.WORKING if pending_gp else AgentReportState.READY_FOR_REVIEW,
            message="Liveness owner recovered missing review dispatch.",
            review_decision=ReviewDecision.REQUEST,
            review_reason="The assigned reviewer is unavailable; preserve the same review obligation.",
            review_cycle=task.review_cycle,
            created_at=now,
        )
        self.reports[report.id] = report
        await self._request_task_review(self.tasks[task.id], report)

    def _liveness_review_claim_valid(self, task: WorkspaceTask) -> bool:
        """Provisioning/rename awaits cannot extend a recovery claim's authority."""
        claim = task.liveness
        if claim is None or claim.action != "replace_reviewer" or claim.action_finished:
            return True  # Ordinary explicit review dispatch retains its contract.
        current = self.tasks.get(task.id)
        if current != task or not self._workspace_owns_task(current):
            return False
        original = self.sessions.get(claim.session_id or "")
        if original is None:
            return True
        return original.created_at == claim.session_created_at and not (
            (original.current_task_id or original.task_id) == task.id
            and (original.runtime_status == AgentRuntimeStatus.WORKING or original.chat_paused)
        )

    async def _liveness_replace_worker(self, task: WorkspaceTask, now: datetime) -> None:
        live = task
        dead_session = self.sessions.get(live.session_id or "")
        # Spawn a fresh throwaway worker BEFORE touching the dead one so a spawn
        # failure does not strand the task with no session at all.
        try:
            new_session = await self.ensure_workspace_agent(
                live.workspace_id,
                EnsureWorkspaceAgentRequest(
                    agent_type=live.agent_type,
                    session_kind=live.session_kind,
                    ephemeral=True,
                    caller_owned_ephemeral=True,
                    role=WorkspaceSessionRole.ORCHESTRATOR,
                    reuse_existing=False,
                    # Carry the original launch preset onto the replacement so
                    # a redispatch does not silently fall back to the built-in
                    # agent default. A vanished session row leaves no preset
                    # record to recover.
                    env_preset=dead_session.env_preset if dead_session else None,
                ),
            )
        except Exception:
            logger.exception(
                "Could not spawn replacement worker for orphaned scheduled task "
                "task_id=%s; rechecking recovery authority",
                live.id,
            )
            await self._liveness_fail_replacement(
                live,
                dead_session,
                reason="scheduled task worker died and no replacement worker could be spawned",
                now=now,
            )
            return

        # Success and failure use the same fresh observation under the mutation
        # lock; neither a provisioning result nor its exception grants authority.
        async with self.workspace_mutation_lock(task.workspace_id):
            if not await self._liveness_replacement_current(task, dead_session):
                if task.liveness is not None:
                    await self._cancel_liveness_claim(task, task.liveness)
                await self._liveness_retire_worker(self.tasks.get(task.id), new_session)
                return
            reattempt = live.dispatch_attempt + 1
            rebound = apply_task_update(
                live,
                {
                    "session_id": new_session.id,
                    "status": WorkspaceTaskStatus.QUEUED,
                    "queued_at": now,
                    "dispatch_attempt": reattempt,
                    "dispatch_reason": "scheduled worker died; redispatch to fresh worker",
                    "dispatch_pending": False,
                    "updated_at": now,
                },
                cause="scheduled",
            )
            self.tasks[live.id] = rebound
            self._save_state()
        try:
            await self._dispatch_task_to_session(rebound, new_session)
        except Exception:
            logger.exception(
                "Redispatch of scheduled task task_id=%s to session_id=%s failed",
                live.id,
                new_session.id,
            )
            async with self.workspace_mutation_lock(task.workspace_id):
                if (
                    self.tasks.get(rebound.id) != rebound
                    or not self._liveness_worker_idle(new_session)
                    or new_session.pending_call_ids
                    or new_session.processing_call_ids
                    or new_session.uncertain_call_ids
                ):
                    # Dispatch may have submitted or a newer contract owns it.
                    # Preserve its receipts and unfinished claim for reconciliation.
                    raise
                failed = apply_task_update(
                    rebound,
                    {
                        "status": WorkspaceTaskStatus.FAILED,
                        "failure_reason": "scheduled task redispatch to a fresh worker failed",
                        "failed_at": self._runtime_now(),
                        "updated_at": self._runtime_now(),
                    },
                    cause="scheduled",
                )
                self.tasks[failed.id] = failed
                self._save_state()
                await self._liveness_retire_worker(failed, new_session)
                if dead_session:
                    await self._liveness_retire_worker(failed, dead_session)
            return
        if dead_session:
            await self._liveness_retire_worker(self.tasks.get(task.id), dead_session)

    def _liveness_worker_idle(self, session: ManagedSession) -> bool:
        return bool(
            self.sessions.get(session.id) == session
            and session.status != ManagedSessionStatus.SPAWNING
            and session.runtime_status != AgentRuntimeStatus.WORKING
            and not session.chat_paused
            and (
                session.session_kind != SessionKind.CHAT
                or not (
                    managed_chat.is_turn_running(session)
                    or managed_chat.runtime_status(session) == AgentRuntimeStatus.WORKING
                    or managed_chat.has_pending_question(session)
                    or managed_chat.question_input_blocked(session, "liveness")
                )
            )
        )

    async def _liveness_retire_worker(
        self, task: WorkspaceTask | None, session: ManagedSession
    ) -> None:
        if task is None:
            return

        # delete_session rechecks this at its final workspace -> pump boundary.
        # Terminal Task status alone never authorizes deleting an active worker.
        def allowed() -> bool:
            return self.tasks.get(task.id) == task and self._liveness_worker_idle(session)

        if allowed():
            await self._best_effort_delete_session(session.id, before_delete=allowed)

    async def _liveness_replacement_current(
        self, task: WorkspaceTask, session: ManagedSession | None
    ) -> bool:
        observed = await self._liveness_facts(task)
        return bool(
            self.tasks.get(task.id) == task
            and self._workspace_owns_task(task)
            and task.status == WorkspaceTaskStatus.WORKING
            and not task.human_acceptance_requested_at
            and task.liveness
            and task.liveness.action == "replace_worker"
            and not task.liveness.action_finished
            and task.liveness.generation == f"{task.execution_epoch}:{task.review_cycle}"
            and observed.dead
            and not observed.active
            and not observed.blocked
            and self.sessions.get(task.session_id or "") == session
            and (session is None or self._liveness_worker_idle(session))
        )

    async def _liveness_fail_replacement(
        self,
        task: WorkspaceTask,
        dead_session: ManagedSession | None,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        async with self.workspace_mutation_lock(task.workspace_id):
            if not await self._liveness_replacement_current(task, dead_session):
                if task.liveness is not None:
                    await self._cancel_liveness_claim(task, task.liveness)
                return
            failed = apply_task_update(
                task,
                {
                    "status": WorkspaceTaskStatus.FAILED,
                    "failure_reason": reason,
                    "failed_at": now,
                    "human_acceptance_requested_at": now,
                    "updated_at": now,
                },
                cause="runtime_failure",
            )
            self.tasks[task.id] = failed
            self._record_system_task_audit(
                task=failed,
                message=f"Scheduled task failed to converge: {reason}.",
                message_zh=f"定时任务无法收敛，已失败：{reason}。",
                validation=(
                    f"dead_session_id={dead_session.id if dead_session else None}; "
                    f"dispatch_attempt={task.dispatch_attempt}; orphan_grace=60s"
                ),
                session_id=dead_session.id if dead_session else "system",
            )
            self._save_state()
            if dead_session is not None:
                await self._liveness_retire_worker(failed, dead_session)

    def _record_dispatch_failure(
        self,
        *,
        task_id: str,
        session_id: str,
        message: str,
        message_zh: str,
        report_state: AgentReportState,
        sampled_at: datetime,
    ) -> None:
        task = self.tasks.get(task_id)
        session = self.sessions.get(session_id)
        if not task or not session:
            return

        # When the prompt is stuck for a REVIEWER, the task was already in
        # REVIEW state (reviewer binding completed before send). Keep it there
        # and mark the session NEEDS_INPUT so the human sees attention needed.
        # When the prompt is stuck for any other role (worker/orchestrator/
        # dispatcher — i.e. an implementation-phase session), the task is
        # already in WORKING state; do NOT demote it to REVIEW — that would
        # incorrectly close the work phase and confuse the dispatch loop.
        # Instead, keep the task in WORKING and mark the session NEEDS_INPUT;
        # the monitor's auto-continue / stall-detector will nudge the worker
        # and retry the prompt.
        is_reviewer = session.role == WorkspaceSessionRole.REVIEWER
        updated_task_status = WorkspaceTaskStatus.REVIEW if is_reviewer else task.status
        updated_session_status = ManagedSessionStatus.NEEDS_INPUT
        updated_runtime_status = AgentRuntimeStatus.ATTENTION

        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session.id,
            state=report_state,
            message=message,
            message_en=message,
            message_zh=message_zh,
            changed_files=[],
            validation=None,
            risks="Prompt dispatch stalled before execution; terminal input needs manual inspection.",
            review_decision=ReviewDecision.SKIP,
            review_reason="Prompt dispatch did not reach execution; independent review cannot run until recovered.",
            risk_level=PROMPT_STUCK_RISK_LEVEL,
            review_cycle=task.review_cycle,
            created_at=sampled_at,
        )
        self.reports[report.id] = report
        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": updated_task_status,
                "reviewed_at": task.reviewed_at or sampled_at if is_reviewer else task.reviewed_at,
                "updated_at": sampled_at,
            },
            cause="prompt_stalled",
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "status": updated_session_status,
                "runtime_status": updated_runtime_status,
                "task_id": task.id,
                "current_task_id": task.id,
                "last_activity_at": sampled_at,
                "updated_at": sampled_at,
            }
        )
        self._save_state()

    def configure_task_notifier(self, notifier: TaskNotifier | None) -> None:
        self._task_notifier = notifier

    def _observe_task_lifecycles(self, workspace_id: str) -> int:
        """Record lifecycle changes and emit one ``lifecycle`` event per change."""

        tasks = [task for task in self.tasks.values() if task.workspace_id == workspace_id]
        if not tasks:
            return 0
        now = self._runtime_now()
        changes = 0
        events: list[TaskEvent] = []
        for task_id, lifecycle in self.task_lifecycles(tasks).items():
            task = self.tasks.get(task_id)
            if task is None:
                continue
            previous = task.lifecycle_mark
            if previous and (previous.phase, previous.reason) == (
                lifecycle.phase,
                lifecycle.reason,
            ):
                continue
            # First sight of an old condition (e.g. on upgrade) is recorded
            # quietly; a recent one, even a fresh failure, is announced.
            stale = previous is None and _older_than(now, lifecycle.since, NOTICE_BACKFILL_HORIZON)
            needs_notice = (
                lifecycle.phase in NOTIFY_PHASES
                and not stale
                and not self._notice_never_wanted(task, lifecycle.reason)
            )
            mark = TaskLifecycleMark(
                phase=lifecycle.phase,
                reason=lifecycle.reason,
                changed_at=now,
                notified_at=None if needs_notice else now,
                # A plan that existed before the Hub first observed the Task
                # is not news.
                plan_digest=previous.plan_digest if previous else plan_digest(task),
                last_notice_key=previous.last_notice_key if previous else None,
                last_notice_at=previous.last_notice_at if previous else None,
            )
            self.tasks[task_id] = task.model_copy(update={"lifecycle_mark": mark})
            changes += 1
            if stale or (previous is None and lifecycle.phase in TERMINAL_PHASES):
                continue
            event, _created = self.task_mailbox.append_event(
                workspace_id=workspace_id,
                task_id=task_id,
                actor_role=TaskActorRole.RUNTIME,
                event_type=TaskEventType.LIFECYCLE,
                call_id=f"lifecycle:{task_id}:{now.isoformat()}",
                action="lifecycle:changed",
                review_cycle=task.review_cycle,
                payload={
                    "from_phase": previous.phase.value if previous else None,
                    "from_reason": previous.reason if previous else None,
                    "phase": lifecycle.phase.value,
                    "reason": lifecycle.reason,
                    "attention": lifecycle.attention,
                    "blocked_by_task_id": lifecycle.blocked_by_task_id,
                    "owner": lifecycle.owner.model_dump(mode="json"),
                },
                persist=False,
            )
            events.append(event)
        if changes:
            self._save_workspace_state(workspace_id)
            self.task_mailbox.wake_events(events)
        return changes

    def _workspace_agents_all_lost(self, workspace_id: str) -> bool:
        agents = [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.ORCHESTRATOR
            and session.session_kind == SessionKind.TERMINAL
            and not session.archived
        ]
        return len(agents) >= 2 and all(
            session.status == ManagedSessionStatus.STOPPED
            or session.runtime_status == AgentRuntimeStatus.OFFLINE
            for session in agents
        )

    def _notice_never_wanted(self, task: WorkspaceTask, reason: str) -> bool:
        """Reasons nobody outside the work should be paged about."""

        if reason in SELF_REPORTED_REASONS:
            return True
        # Owner loss under workspace control is resolved within minutes
        # (reconciler, subagent or scheduled handling); the person hears about
        # the outcome once instead.
        return (
            reason in QUICKLY_RESOLVED_REASONS
            and task.execution_control == TaskExecutionControl.WORKSPACE
        )

    def _held_by_supervisor(self, task: WorkspaceTask) -> bool:
        """A working parent Task's agent supervises this child for now."""

        parent = self.tasks.get(task.parent_task_id) if task.parent_task_id else None
        if parent is None or parent.status != WorkspaceTaskStatus.WORKING:
            return False
        session = self.sessions.get(parent.session_id or "")
        return bool(
            session
            and session.runtime_status == AgentRuntimeStatus.WORKING
            and not session.chat_paused
        )

    def _task_notice_wanted(self, task: WorkspaceTask, reason: str) -> bool:
        return not self._notice_never_wanted(task, reason) and not self._held_by_supervisor(task)

    def _has_pending_task_notices(self, workspace_id: str) -> bool:
        if self._task_notifier is None:
            return False
        for task in self.tasks.values():
            mark = task.lifecycle_mark
            if task.workspace_id != workspace_id or mark is None:
                continue
            if mark.notified_at is None and mark.phase in NOTIFY_PHASES:
                return True
            digest = plan_digest(task)
            if digest and digest != mark.plan_digest and mark.phase not in TERMINAL_PHASES:
                return True
        return False

    def _schedule_task_notices(self, workspace_id: str) -> None:
        """Deliver pending notices in one background job per workspace."""

        if self._task_notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        current = self._task_notice_jobs.get(workspace_id)
        if current is not None and not current.done() and current.get_loop() is loop:
            return
        self._task_notice_jobs[workspace_id] = loop.create_task(
            self._run_task_notices(workspace_id),
            name=f"workspace-task-notices-{workspace_id}",
        )

    async def _run_task_notices(self, workspace_id: str) -> None:
        try:
            await self._deliver_task_notices(workspace_id)
        except Exception:
            logger.exception("Task notice delivery failed workspace_id=%s", workspace_id)

    async def _deliver_task_notices(self, workspace_id: str) -> int:
        """Send settled attention notices and new plan checkpoints; returns sends.

        Each notice is chosen from the Task's current mark under the workspace
        lock, sent without it, and its outcome is recorded only if the mark is
        still the one the notice was about.
        """

        notifier = self._task_notifier
        if notifier is None:
            return 0
        delivered = attempted = 0
        task_ids = [task.id for task in self.tasks.values() if task.workspace_id == workspace_id]
        for task_id in task_ids:
            if attempted >= NOTICES_PER_PASS:
                break
            async with self.workspace_mutation_lock(workspace_id):
                choice = self._next_task_notice(task_id)
            if choice is None:
                continue
            task, notice, token, kind = choice
            attempted += 1
            outcome = await self._send_task_notice(notifier, task, notice)
            delivered += outcome == "sent"
            async with self.workspace_mutation_lock(workspace_id):
                if self._record_notice_outcome(task_id, outcome, token, kind):
                    self._save_workspace_state(workspace_id)
        return delivered

    def _next_task_notice(
        self, task_id: str
    ) -> tuple[WorkspaceTask, TaskNotice, tuple[datetime, str], str] | None:
        """The notice due for this Task now, or None; quiet repeats are settled here."""

        task = self.tasks.get(task_id)
        mark = task.lifecycle_mark if task else None
        if task is None or mark is None:
            return None
        now = self._runtime_now()
        if mark.next_notify_at and now < mark.next_notify_at:
            return None
        token = (mark.changed_at, f"{mark.phase.value}:{mark.reason}")
        if mark.notified_at is None and mark.phase in NOTIFY_PHASES:
            heuristic = mark.reason in HEURISTIC_REASONS
            settle = HEURISTIC_SETTLE_SECONDS if heuristic else NOTICE_SETTLE_SECONDS
            if (now - mark.changed_at).total_seconds() < settle:
                return None
            if self._held_by_supervisor(task):
                return None
            if (
                heuristic
                and mark.last_notice_key == token[1]
                and mark.last_notice_at is not None
                and now - mark.last_notice_at < NOTICE_REPEAT_COOLDOWN
            ):
                # A sampled condition flapped back; it was just announced.
                self._record_notice_outcome(task_id, "unroutable", token, "attention")
                self._save_workspace_state(task.workspace_id)
                return None
            notice = attention_notice(
                task, mark.phase, mark.reason, self._task_notice_detail(task, mark)
            )
            return task, notice, token, "attention"
        digest = plan_digest(task)
        if (
            digest
            and digest != mark.plan_digest
            and mark.phase not in TERMINAL_PHASES
            and self._task_notice_wanted(task, mark.reason)
            and (plan := plan_notice(task)) is not None
        ):
            return task, plan, token, f"plan:{digest}"
        return None

    async def _send_task_notice(
        self, notifier: TaskNotifier, task: WorkspaceTask, notice: TaskNotice
    ) -> str:
        try:
            outcome = await asyncio.wait_for(
                notifier(task, notice), timeout=NOTICE_SEND_TIMEOUT_SECONDS
            )
        except Exception:
            logger.exception("Task notice delivery failed task_id=%s kind=%s", task.id, notice.kind)
            return "failed"
        return outcome if outcome in {"sent", "unroutable", "failed"} else "failed"

    def _record_notice_outcome(
        self, task_id: str, outcome: str, token: tuple[datetime, str], kind: str
    ) -> bool:
        """Apply a send outcome to the mark it was about; False if that mark is gone."""

        task = self.tasks.get(task_id)
        mark = task.lifecycle_mark if task else None
        if task is None or mark is None:
            return False
        if (mark.changed_at, f"{mark.phase.value}:{mark.reason}") != token:
            # The lifecycle moved on while this notice was in flight; the new
            # mark keeps its own pending notice.
            return False
        now = self._runtime_now()
        if outcome == "failed" and mark.notify_attempts + 1 < NOTICE_MAX_ATTEMPTS:
            attempts = mark.notify_attempts + 1
            backoff = min(NOTICE_SETTLE_SECONDS * 2**attempts, NOTICE_MAX_BACKOFF_SECONDS)
            update: dict[str, Any] = {
                "notify_attempts": attempts,
                "next_notify_at": now + timedelta(seconds=backoff),
            }
        else:
            update = {"notify_attempts": 0, "next_notify_at": None}
            if kind == "attention":
                update.update(notified_at=now, last_notice_key=token[1], last_notice_at=now)
            elif kind.startswith("plan:"):
                update["plan_digest"] = kind.removeprefix("plan:")
        update["delivery_outcome"] = outcome
        self.tasks[task_id] = task.model_copy(
            update={"lifecycle_mark": mark.model_copy(update=update)}
        )
        return True

    def _task_notice_detail(self, task: WorkspaceTask, mark: TaskLifecycleMark) -> str:
        """The question or result the person needs, from the newest relevant record."""

        if mark.reason == "liveness_attention" and task.liveness:
            return task.liveness.detail
        if mark.phase == TaskPhase.FAILED and task.failure_reason:
            return task.failure_reason
        progress = task.latest_progress
        if mark.reason.startswith("initiator_") and progress is not None:
            return progress.summary
        reports = [
            report
            for report in self.reports.values()
            if report.task_id == task.id and report.review_cycle == task.review_cycle
        ]
        if mark.reason.startswith("review"):
            reports = [report for report in reports if report.state.value.startswith("review_")]
        latest = max(reports, key=lambda report: report.created_at, default=None)
        if latest is None:
            return ""
        return str(latest.message_zh or latest.message or "")


def _older_than(now: datetime, then: datetime, age: timedelta) -> bool:
    """``then`` is more than ``age`` before ``now``; incomparable clocks are not."""

    if (now.tzinfo is None) != (then.tzinfo is None):
        return False
    return now - then > age
