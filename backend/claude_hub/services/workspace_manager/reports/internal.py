"""Report processing split from the compatibility mixin."""

from .._constants import *  # noqa: F401,F403
from .review_requests import _ReportReviewRequestMixin


class _InternalReportHandlingMixin(_ReportReviewRequestMixin):
    def _is_goal_packet_approval_review(
        self,
        task: WorkspaceTask,
        report: AgentReport,
    ) -> bool:
        return (
            task.task_mode == WorkspaceTaskMode.REVIEWED
            and report.state == AgentReportState.WORKING
            and task.goal_packet is not None
            and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
        )

    async def _handle_internal_task_report(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
    ) -> None:
        if task.internal_kind == "feedback_reaper" and report.state in {
            AgentReportState.BLOCKED,
            AgentReportState.NEEDS_INPUT,
        }:
            updated = self._mark_feedback_summary_retryable(
                task,
                reason=f"Feedback Reaper reported {report.state.value}; ready to retry",
                validation=(
                    f"report_id={report.id}; report_state={report.state.value}; "
                    "pending_input_preserved=true"
                ),
            )
            await self.dispatch_workspace(updated.workspace_id)
            return
        if report.state not in {
            AgentReportState.COMPLETED,
            AgentReportState.READY_FOR_REVIEW,
        }:
            return
        now = self._runtime_now()
        updated = self.tasks[task.id].model_copy(
            update={
                "status": WorkspaceTaskStatus.DONE,
                "review_skipped_at": now,
                "review_skip_reason": "System-internal task completed without human review.",
                "completed_at": now,
                "human_accepted_at": None,
                "updated_at": now,
            }
        )
        self.tasks[task.id] = updated
        summary_run = None
        processed_count = None
        if updated.internal_kind == "feedback_reaper":
            store = self._feedback_store()
            pending_run = store.summary_run_for_task(updated.workspace_id, updated.id)
            try:
                if pending_run is not None:
                    processed_count = store.commit_staged_summary_input(
                        updated.workspace_id,
                        pending_run.id,
                    )
                summary_run = store.complete_summary_run(
                    updated.workspace_id,
                    updated.id,
                    report,
                    now=now,
                )
                if pending_run is not None:
                    store.discard_staged_summary_input(
                        updated.workspace_id,
                        pending_run.id,
                    )
            except Exception as exc:
                error_text = str(exc).replace("\n", " ")[:400]
                retryable = self._mark_feedback_summary_retryable(
                    updated,
                    reason="Feedback Reaper completion persistence failed; ready to retry",
                    validation=(
                        f"completion_report_id={report.id}; pending_input_preserved=true; "
                        f"error_type={type(exc).__name__}; error={error_text}"
                    ),
                )
                await self.dispatch_workspace(retryable.workspace_id)
                return
        self._record_system_task_audit(
            task=updated,
            message=(
                "Internal Feedback Reaper completed and was archived without ordinary review."
            ),
            message_zh="内部 Feedback Reaper 已完成，并已跳过普通 review 归档。",
            validation=(
                f"completion_report_id={report.id}; session_id={session.id}; "
                "review_gate=skipped_internal"
                + (
                    f"; summary_run_id={summary_run.id}; "
                    f"created_lesson_ids={json.dumps(summary_run.created_lesson_ids)}; "
                    f"merged_lesson_ids={json.dumps(summary_run.merged_lesson_ids)}; "
                    f"skipped_reason={summary_run.skipped_reason}; "
                    f"processed_count={processed_count}"
                    if summary_run
                    else ""
                )
            ),
            session_id=session.id,
        )
        self._write_task_record(updated)
        self._release_task_session(updated)
        if updated.internal_kind == "scheduled":
            await self._auto_cleanup_scheduled_session(updated, session)
        if updated.feedback_lesson_ids:
            self._feedback_store().increment_lesson_usage(
                updated.workspace_id,
                list(updated.feedback_lesson_ids),
                success=True,
                now=now,
            )
        self._save_state()
        await self.dispatch_workspace(updated.workspace_id)

    async def _auto_cleanup_scheduled_session(
        self, task: WorkspaceTask, session: ManagedSession
    ) -> None:
        """Delete the caller-owned ephemeral session that ran a scheduled task.

        A scheduled hub task spins up a throwaway orchestrator session to run
        the task. Once the task is DONE the session has no further purpose, so
        delete it (tab + tmux included) rather than leaving it to hold agent /
        reviewer resources. Guarded on ``caller_owned_ephemeral`` so a session
        the scheduler did not own is never torn down here.
        """
        sess = self.sessions.get(session.id)
        if sess is None or not sess.caller_owned_ephemeral:
            return
        try:
            await self.delete_session(session.id)
            logger.info(
                "Auto-cleaned scheduled task session task_id=%s session_id=%s",
                task.id,
                session.id,
            )
        except Exception:
            logger.exception(
                "Auto-cleanup failed for scheduled task session task_id=%s session_id=%s",
                task.id,
                session.id,
            )

    async def _should_request_task_review(
        self,
        task: WorkspaceTask,
        report: AgentReport,
        *,
        trigger_kind: str,
    ) -> bool:
        if trigger_kind != "agent_report":
            return True
        can_skip_review = False
        if (
            report.review_decision == ReviewDecision.SKIP
            and report.state == AgentReportState.COMPLETED
        ):
            can_skip_review = await self._can_skip_task_review(task, report)
        return state_policy.should_request_task_review(
            trigger_kind=trigger_kind,
            report_state=report.state,
            review_decision=report.review_decision,
            can_skip_review=can_skip_review,
        )

    async def _can_skip_task_review(self, task: WorkspaceTask, report: AgentReport) -> bool:
        return state_policy.can_skip_task_review(
            state_policy.ReviewSkipContext(
                report_state=report.state,
                evidence_gaps=self._completion_evidence_gaps(task, report),
                changed_files=report.changed_files,
                risk_level=report.risk_level,
                latest_review_state=self._latest_review_report_state(task.id),
                workspace_has_tracked_changes=await self._workspace_has_tracked_changes(
                    task.workspace_id
                ),
            )
        )

    def _completion_evidence_gaps(
        self,
        task: WorkspaceTask,
        report: AgentReport,
    ) -> list[str]:
        return state_policy.completion_evidence_gaps(
            report.state,
            has_goal_packet=task.goal_packet is not None,
            has_acceptance_check=bool(report.acceptance_check),
        )
