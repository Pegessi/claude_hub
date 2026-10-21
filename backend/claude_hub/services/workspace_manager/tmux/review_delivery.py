"""Bounded verdict delivery recovery on the existing review-reaper tick."""

from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.task_graph import task_supervisor_consumer_key
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import (
    AgentReportState,
    AgentRuntimeStatus,
    ManagedSessionStatus,
    WorkspaceTask,
    WorkspaceTaskStatus,
    logger,
    settings,
    state_policy,
)


class _ReviewDeliveryMixin(_WorkspaceManagerBase):
    async def _recover_idle_review_delivery(self, task: WorkspaceTask) -> None:
        """Nudge once per dispatch, then surface attention without sealing the round.

        The caller holds the workspace dispatch lock. The normal status refresh
        supplies activity and runtime state (including frozen-frame handling).
        No separate timer or terminal input heuristics are introduced here.
        """
        if task.status not in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}:
            return
        if not state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            return
        reviewer = self.sessions.get(task.review_session_id or "")
        if (
            reviewer is None
            or reviewer.workspace_id != task.workspace_id
            or (reviewer.current_task_id or reviewer.task_id) != task.id
            or reviewer.runtime_status != AgentRuntimeStatus.IDLE
            or reviewer.status != ManagedSessionStatus.IDLE
            or state_policy.current_round_has_verdict(task.review_cycle, task.reviewed_cycle)
            or task.human_acceptance_requested_at is not None
        ):
            return
        now = self._runtime_now()
        assert task.review_requested_at is not None
        activity = [task.review_requested_at]
        if reviewer.last_activity_at:
            activity.append(reviewer.last_activity_at)
        for report in self.reports.values():
            if (
                report.task_id == task.id
                and report.session_id == reviewer.id
                and report.review_cycle == task.review_cycle
                and report.created_at >= task.review_requested_at
            ):
                if report.state in {
                    AgentReportState.REVIEW_PASSED,
                    AgentReportState.REVIEW_FAILED,
                    AgentReportState.REVIEW_NEEDS_INPUT,
                }:
                    return
                activity.append(report.created_at)
        timeout = settings.review_verdict_idle_timeout_seconds
        if (now - max(activity)).total_seconds() < timeout:
            return
        round_key = (
            f"{task.review_cycle}:{task.review_attempts}:"
            f"{task.review_requested_at.isoformat()}:{reviewer.id}"
        )
        call_id = f"review-delivery:{task.id}:{round_key}"
        message = (
            "Review verdict delivery appears stalled: this round is still open and no "
            "terminal verdict was recorded. Resume this SAME review; do not start a new "
            "round. If you already reached a verdict, resubmit it with its original "
            "call_id and evidence. Do not redo completed work or assume approval. "
            "A terminal reply alone does not record a verdict. If submission fails, "
            "surface the HTTP error to the operator. ACK this reminder in acked_call_ids: "
            f"{call_id}\n\n{self._review_submission_block(task, reviewer)}"
        )
        if task.review_delivery_round != round_key:
            # Persist intent before delivery. Replaying this same message is safe
            # through the durable mailbox's pending/processing/uncertain/ACK states.
            task = task.model_copy(
                update={
                    "review_delivery_round": round_key,
                    "review_delivery_nudged_at": now,
                    "review_delivery_escalated_at": None,
                    "review_delivery_message": message,
                }
            )
            self.tasks[task.id] = task
            self._save_state()
        nudged_at = task.review_delivery_nudged_at
        assert nudged_at is not None
        if task.review_delivery_escalated_at is not None:
            return
        # A crash may have persisted task intent before entering the mailbox.
        # Always resume that same envelope before testing the escalation bound.
        tracked = any(
            call_id in ids
            for ids in (
                reviewer.pending_call_ids,
                reviewer.processing_call_ids,
                reviewer.uncertain_call_ids,
                reviewer.delivered_call_ids,
            )
        )
        try:
            await self.send_session_message(
                reviewer.id, task.review_delivery_message or message, call_id=call_id
            )
        except Exception:
            # Mailbox owns retry/uncertainty; never create another nudge ID.
            logger.exception("Review delivery reminder failed task_id=%s", task.id)
        else:
            if not tracked and (now - nudged_at).total_seconds() >= timeout:
                # Delayed restart before enqueue: give the newly enqueued nudge
                # a full response window. A pre-enqueue failure still escalates.
                nudged_at = now
                task = task.model_copy(update={"review_delivery_nudged_at": now})
                self.tasks[task.id] = task
                self._save_state()
        if (now - nudged_at).total_seconds() < timeout:
            return
        self.task_mailbox.append_event(
            workspace_id=task.workspace_id,
            task_id=task.id,
            actor_session_id=reviewer.id,
            actor_role=TaskActorRole.RUNTIME,
            review_cycle=task.review_cycle,
            event_type=TaskEventType.NEEDS_INPUT,
            call_id=f"{call_id}:escalated",
            action="review:delivery_stalled",
            target=reviewer.id,
            consumer_key=task_supervisor_consumer_key(task),
            payload={
                "message": "Reviewer idle without a delivered verdict after one reminder; "
                "operator recovery required. Review round remains open.",
                "review_cycle": task.review_cycle,
                "risk_level": "review_delivery_stalled",
                "recovery_commands": task.review_delivery_message,
                "reminder_call_id": call_id,
            },
            persist=True,
            wake=True,
        )
        self.tasks[task.id] = task.model_copy(update={"review_delivery_escalated_at": now})
        self._save_state()
