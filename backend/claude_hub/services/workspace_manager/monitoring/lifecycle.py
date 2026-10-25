"""Observe Task lifecycle changes and announce the ones a human must act on.

Whatever wrote a Task (worker report, reviewer, runtime loss, a person), the
monitor compares the derived lifecycle with the persisted ``lifecycle_mark``.
A change is recorded once and emitted once as a ``lifecycle`` mailbox event,
which wakes ``task wait`` long-polls. Attention phases are delivered once to
the injected notifier, with retry, from a background job so a slow or failing
notifier never holds up the monitor. Nothing here depends on an LLM deciding
to notify.
"""

from claude_hub.models.schemas import (
    TERMINAL_PHASES,
    TaskExecutionControl,
    TaskLifecycleMark,
    TaskPhase,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
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


class _TaskLifecycleObserverMixin(_WorkspaceManagerBase):
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

    async def _tick_task_lifecycles(self, workspace_id: str) -> None:
        """Observe, resolve owner loss, and start notice delivery for one workspace.

        Runs under the workspace mutation lock so it never saves another
        transaction's staged changes or races a rollback.
        """

        async with self.workspace_mutation_lock(workspace_id):
            self._observe_task_lifecycles(workspace_id)
            if self._reconcile_orphaned_tasks(workspace_id):
                self._observe_task_lifecycles(workspace_id)
        if self._has_pending_task_notices(workspace_id):
            self._schedule_task_notices(workspace_id)

    def _reconcile_orphaned_tasks(self, workspace_id: str) -> int:
        """Resolve Tasks whose owner has been gone past the policy grace."""

        now = self._runtime_now()
        if self._workspace_agents_all_lost(workspace_id):
            # Every agent vanished at once: more likely the tmux server or
            # sampling failed than each agent; never fail work on that alone.
            logger.warning(
                "Lifecycle reconciler skipped: every workspace agent looks gone " "workspace_id=%s",
                workspace_id,
            )
            return 0
        resolved = 0
        for task in [task for task in self.tasks.values() if task.workspace_id == workspace_id]:
            mark = task.lifecycle_mark
            if mark is None or mark.phase != TaskPhase.ORPHANED:
                continue
            policy = OWNER_LOSS_POLICY.get(mark.reason)
            if policy is None or now - mark.changed_at < policy[0]:
                continue
            if task.execution_control == TaskExecutionControl.WORKSPACE:
                # Subagent, scheduled and Feedback Reaper Tasks keep their own
                # loss handling and retry paths.
                if task.task_mode == WorkspaceTaskMode.SUBAGENT:
                    continue
                if task.system_internal and task.internal_kind in {
                    "scheduled",
                    "feedback_reaper",
                }:
                    continue
                if task.status != WorkspaceTaskStatus.WORKING:
                    continue
            elif task.status not in {WorkspaceTaskStatus.TODO, WorkspaceTaskStatus.WORKING}:
                continue
            self.tasks[task.id] = apply_task_update(
                task,
                {
                    "status": WorkspaceTaskStatus.FAILED,
                    "failure_reason": policy[1],
                    "failed_at": now,
                    "updated_at": now,
                },
                cause="reconcile",
            )
            logger.warning(
                "Lifecycle reconciler failed orphaned task task_id=%s reason=%s",
                task.id,
                mark.reason,
            )
            resolved += 1
        if resolved:
            self._save_workspace_state(workspace_id)
        return resolved

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
        return parent is not None and parent.status == WorkspaceTaskStatus.WORKING

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
        self.tasks[task_id] = task.model_copy(
            update={"lifecycle_mark": mark.model_copy(update=update)}
        )
        return True

    def _task_notice_detail(self, task: WorkspaceTask, mark: TaskLifecycleMark) -> str:
        """The question or result the person needs, from the newest relevant record."""

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
