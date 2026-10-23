"""Observe Task lifecycle changes and announce the ones a human must act on.

Whatever wrote a Task (worker report, reviewer, runtime loss, a person), the
monitor compares the derived lifecycle with the persisted ``lifecycle_mark``.
A change is recorded once, emitted once as a ``lifecycle`` mailbox event (so
``task wait`` and Dot wake on it), and attention phases are delivered once to
the injected notifier, with retry. Nothing here depends on an LLM deciding to
notify.
"""

from claude_hub.models.schemas import TERMINAL_PHASES, TaskLifecycleMark, TaskPhase
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
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

# A phase must hold this long before it is announced, so a momentary runtime
# sample (e.g. a terminal flickering to "attention") does not page anyone.
NOTICE_SETTLE_SECONDS = 30
NOTICE_MAX_ATTEMPTS = 6
NOTICE_MAX_BACKOFF_SECONDS = 3600
# Conditions already older than this when first observed (e.g. on upgrade)
# are recorded and visible on the board, but not pushed.
NOTICE_BACKFILL_HORIZON = timedelta(hours=24)


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
            first_sight = previous is None
            stale = first_sight and (
                lifecycle.phase in TERMINAL_PHASES
                or _older_than(now, lifecycle.since, NOTICE_BACKFILL_HORIZON)
            )
            needs_notice = lifecycle.phase in NOTIFY_PHASES and not stale
            mark = TaskLifecycleMark(
                phase=lifecycle.phase,
                reason=lifecycle.reason,
                changed_at=now,
                notified_at=None if needs_notice else now,
                plan_digest=(
                    previous.plan_digest
                    if previous
                    # Plans that existed before observation are not news.
                    else (
                        plan_digest(task) if stale or lifecycle.phase in TERMINAL_PHASES else None
                    )
                ),
            )
            self.tasks[task_id] = task.model_copy(update={"lifecycle_mark": mark})
            changes += 1
            if stale:
                continue
            self.task_mailbox.append_event(
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
        if changes:
            self._save_state()
        return changes

    async def _deliver_task_notices(self, workspace_id: str) -> int:
        """Send settled attention notices and new plan checkpoints; returns sends."""

        notifier = self._task_notifier
        if notifier is None:
            return 0
        now = self._runtime_now()
        delivered = 0
        for task in [task for task in self.tasks.values() if task.workspace_id == workspace_id]:
            mark = task.lifecycle_mark
            if mark is None:
                continue
            if mark.next_notify_at and now < mark.next_notify_at:
                continue
            if (
                mark.notified_at is None
                and mark.phase in NOTIFY_PHASES
                and (now - mark.changed_at).total_seconds() >= NOTICE_SETTLE_SECONDS
            ):
                notice = attention_notice(
                    task, mark.phase, mark.reason, self._task_notice_detail(task, mark)
                )
                outcome = await self._send_task_notice(notifier, task, notice)
                delivered += outcome == "sent"
                self._record_notice_outcome(task.id, outcome, now, attention=True)
                continue
            digest = plan_digest(task)
            if (
                digest
                and digest != mark.plan_digest
                and mark.phase not in TERMINAL_PHASES
                and (plan := plan_notice(task)) is not None
            ):
                outcome = await self._send_task_notice(notifier, task, plan)
                delivered += outcome == "sent"
                self._record_notice_outcome(task.id, outcome, now, plan=digest)
        return delivered

    async def _send_task_notice(
        self, notifier: TaskNotifier, task: WorkspaceTask, notice: TaskNotice
    ) -> str:
        try:
            outcome = await notifier(task, notice)
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
            if plan is not None:
                update["plan_digest"] = plan
        self.tasks[task_id] = task.model_copy(
            update={"lifecycle_mark": mark.model_copy(update=update)}
        )
        self._save_state()

    def _task_notice_detail(self, task: WorkspaceTask, mark: TaskLifecycleMark) -> str:
        """The question or result the person needs, from the newest relevant record."""

        if mark.phase == TaskPhase.FAILED and task.failure_reason:
            return task.failure_reason
        progress = task.latest_progress
        if mark.reason.startswith("initiator_") and progress is not None:
            return progress.summary
        reports = sorted(
            (report for report in self.reports.values() if report.task_id == task.id),
            key=lambda report: report.created_at,
        )
        if mark.reason.startswith("review"):
            reports = [report for report in reports if report.state.value.startswith("review_")]
        latest = reports[-1] if reports else None
        if latest is None:
            return ""
        return str(latest.message_zh or latest.message or "")


def _older_than(now: datetime, then: datetime, age: timedelta) -> bool:
    """``then`` is more than ``age`` before ``now``; incomparable clocks are not."""

    if (now.tzinfo is None) != (then.tzinfo is None):
        return False
    return now - then > age
