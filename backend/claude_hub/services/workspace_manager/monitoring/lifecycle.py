"""Observe Task lifecycle changes and announce the ones a human must act on.

Whatever wrote a Task (worker report, reviewer, runtime loss, a person), the
monitor compares the derived lifecycle with the persisted ``lifecycle_mark``.
A change is recorded once and emitted once as a ``lifecycle`` mailbox event,
which wakes ``task wait`` long-polls. Attention phases are delivered once to
the injected notifier, with retry, from a background job so a slow or failing
notifier never holds up the monitor. Nothing here depends on an LLM deciding
to notify.
"""

from claude_hub.models.schemas import TERMINAL_PHASES, TaskLifecycleMark, TaskPhase
from claude_hub.models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from claude_hub.services.task_lifecycle import (
    NOTIFY_PHASES,
    TaskNotice,
    TaskNotifier,
    attention_notice,
    plan_digest,
    plan_notice,
)
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
                and self._task_notice_wanted(task, lifecycle.reason)
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

    def _task_notice_wanted(self, task: WorkspaceTask, reason: str) -> bool:
        """Whether a person outside the work should hear about this reason."""

        if reason in SELF_REPORTED_REASONS:
            return False
        parent = self.tasks.get(task.parent_task_id) if task.parent_task_id else None
        if parent is not None and parent.status not in {
            WorkspaceTaskStatus.DONE,
            WorkspaceTaskStatus.FAILED,
        }:
            # The parent Task's agent supervises its children; it is told
            # through the Task mailbox, not the person.
            return False
        return True

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
        """Send settled attention notices and new plan checkpoints; returns sends."""

        notifier = self._task_notifier
        if notifier is None:
            return 0
        now = self._runtime_now()
        delivered = attempted = 0
        changed = False
        for task in [task for task in self.tasks.values() if task.workspace_id == workspace_id]:
            if attempted >= NOTICES_PER_PASS:
                break
            mark = task.lifecycle_mark
            if mark is None or (mark.next_notify_at and now < mark.next_notify_at):
                continue
            if mark.notified_at is None and mark.phase in NOTIFY_PHASES:
                settle = (
                    HEURISTIC_SETTLE_SECONDS
                    if mark.reason in HEURISTIC_REASONS
                    else NOTICE_SETTLE_SECONDS
                )
                if (now - mark.changed_at).total_seconds() < settle:
                    continue
                key = f"{mark.phase.value}:{mark.reason}"
                if (
                    mark.last_notice_key == key
                    and mark.last_notice_at is not None
                    and now - mark.last_notice_at < NOTICE_REPEAT_COOLDOWN
                ):
                    # The same condition flapped back; it was just announced.
                    self._record_notice_outcome(task.id, "unroutable", now, attention=True)
                    changed = True
                    continue
                notice = attention_notice(
                    task, mark.phase, mark.reason, self._task_notice_detail(task, mark)
                )
                attempted += 1
                outcome = await self._send_task_notice(notifier, task, notice)
                delivered += outcome == "sent"
                self._record_notice_outcome(task.id, outcome, now, attention=True, key=key)
                changed = True
                continue
            digest = plan_digest(task)
            if (
                digest
                and digest != mark.plan_digest
                and mark.phase not in TERMINAL_PHASES
                and self._task_notice_wanted(task, mark.reason)
                and (plan := plan_notice(task)) is not None
            ):
                attempted += 1
                outcome = await self._send_task_notice(notifier, task, plan)
                delivered += outcome == "sent"
                self._record_notice_outcome(task.id, outcome, now, plan=digest)
                changed = True
        if changed:
            self._save_workspace_state(workspace_id)
        return delivered

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
        self,
        task_id: str,
        outcome: str,
        now: datetime,
        *,
        attention: bool = False,
        plan: str | None = None,
        key: str | None = None,
    ) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.lifecycle_mark is None:
            return
        mark = task.lifecycle_mark
        if outcome == "failed" and mark.notify_attempts + 1 < NOTICE_MAX_ATTEMPTS:
            attempts = mark.notify_attempts + 1
            backoff = min(NOTICE_SETTLE_SECONDS * 2**attempts, NOTICE_MAX_BACKOFF_SECONDS)
            update: dict[str, Any] = {
                "notify_attempts": attempts,
                "next_notify_at": now + timedelta(seconds=backoff),
            }
        else:
            update = {"notify_attempts": 0, "next_notify_at": None}
            if attention:
                update["notified_at"] = now
            if key is not None:
                update.update(last_notice_key=key, last_notice_at=now)
            if plan is not None:
                update["plan_digest"] = plan
        self.tasks[task_id] = task.model_copy(
            update={"lifecycle_mark": mark.model_copy(update=update)}
        )

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
