"""Report processing split from the compatibility mixin."""

from ....models.task_mailbox import TaskActorRole, TaskEventType
from ...task_graph import task_supervisor_consumer_key
from .._constants import *  # noqa: F401,F403
from .internal import _InternalReportHandlingMixin

_REPORT_STATE_TO_TASK_EVENT: dict[AgentReportState, TaskEventType] = {
    AgentReportState.STARTED: TaskEventType.STARTED,
    AgentReportState.WORKING: TaskEventType.PROGRESS,
    AgentReportState.BLOCKED: TaskEventType.NEEDS_INPUT,
    AgentReportState.NEEDS_INPUT: TaskEventType.NEEDS_INPUT,
    AgentReportState.READY_FOR_REVIEW: TaskEventType.REPORT,
    AgentReportState.COMPLETED: TaskEventType.COMPLETED,
    AgentReportState.FAILED: TaskEventType.FAILED,
    AgentReportState.REVIEW_STARTED: TaskEventType.REVIEW_STARTED,
    AgentReportState.REVIEW_PASSED: TaskEventType.REVIEW_PASSED,
    AgentReportState.REVIEW_FAILED: TaskEventType.REVIEW_FAILED,
    AgentReportState.REVIEW_NEEDS_INPUT: TaskEventType.REVIEW_NEEDS_INPUT,
}


class _ReportEventsMixin(_InternalReportHandlingMixin):
    def _task_actor_role_for_session(self, session: ManagedSession) -> TaskActorRole:
        if session.role == WorkspaceSessionRole.REVIEWER:
            return TaskActorRole.REVIEWER
        if session.role == WorkspaceSessionRole.RESIDENT:
            return TaskActorRole.SUPERVISOR
        return TaskActorRole.WORKER

    def _task_event_type_for_report(
        self, state: AgentReportState, task: WorkspaceTask | None = None
    ) -> TaskEventType:
        """Map a report state to a Task event. REVIEWED worker COMPLETED is REPORT.

        Compat AgentEventType.COMPLETED is reserved for REVIEW_PASSED / a
        DIRECT-mode terminal completion. A REVIEWED worker completion waits
        for the reviewer and must project as PROGRESS.
        """
        if (
            state == AgentReportState.COMPLETED
            and task is not None
            and task.task_mode == WorkspaceTaskMode.REVIEWED
        ):
            return TaskEventType.REPORT
        return _REPORT_STATE_TO_TASK_EVENT.get(state, TaskEventType.PROGRESS)

    def _task_actor_for_report(self, report: AgentReport) -> tuple[str | None, TaskActorRole]:
        session = self.sessions.get(report.session_id)
        if session is not None and session.workspace_id == report.workspace_id:
            return session.id, self._task_actor_role_for_session(session)
        review_states = {
            AgentReportState.REVIEW_STARTED,
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }
        if report.state in review_states:
            return report.session_id or None, TaskActorRole.REVIEWER
        return report.session_id or None, TaskActorRole.WORKER

    def _canonical_report_event_payload(
        self,
        report: AgentReport,
        task: WorkspaceTask,
        *,
        actor_session_id: str | None,
        actor_role: TaskActorRole,
    ) -> dict[str, object]:
        return {
            "message": report.message,
            "report_id": report.id,
            "report_state": report.state.value,
            "task_id": task.id,
            "actor_role": actor_role.value,
            "actor_session_id": actor_session_id,
            "review_cycle": report.review_cycle,
        }

    def _canonical_report_bridge_payload(
        self, report: AgentReport, session: ManagedSession, task: WorkspaceTask
    ) -> dict[str, object]:
        """Authoritative TaskMailbox payload for a persisted report rewrite."""

        actor_role = self._task_actor_role_for_session(session)
        return self._canonical_report_event_payload(
            report, task, actor_session_id=session.id, actor_role=actor_role
        )

    def _bridge_report_to_agent_event(
        self,
        report: AgentReport,
        session: ManagedSession,
    ) -> tuple[str, str] | None:
        """Append a TaskMailbox event for a persisted report.

        All task-linked reports write a TaskEvent. Run-tree status, cursor, and
        context are not written here. Persist is deferred to the outer
        report-intake ``_save_workspace_state``.
        """
        if not report.task_id:
            return None
        task = self.tasks.get(report.task_id)
        if task is None or task.workspace_id != report.workspace_id:
            return None

        actor_role = self._task_actor_role_for_session(session)
        event_type = self._task_event_type_for_report(report.state, task)
        call_id = f"report:{report.id}"
        self.task_mailbox.append_event(
            workspace_id=report.workspace_id,
            task_id=task.id,
            actor_role=actor_role,
            event_type=event_type,
            call_id=call_id,
            action="report",
            consumer_key=task_supervisor_consumer_key(task),
            actor_session_id=session.id,
            review_cycle=report.review_cycle,
            payload=self._canonical_report_bridge_payload(report, session, task),
            report_id=report.id,
            persist=False,
        )

        return task_supervisor_consumer_key(task), task_supervisor_consumer_key(task)

    async def _after_report_recorded(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
    ) -> None:
        live = self.tasks.get(task.id)
        if live is None or not self._workspace_owns_task(live):
            return
        task = live
        if session.role == WorkspaceSessionRole.REVIEWER:
            await self._handle_review_report(task, session, report)
            return
        if session.role not in {
            WorkspaceSessionRole.ORCHESTRATOR,
            WorkspaceSessionRole.WORKER,
        }:
            return
        if task.system_internal:
            await self._handle_internal_task_report(task, session, report)
            return
        if self._is_goal_packet_approval_review(task, report):
            await self._request_task_review(task, report)
            return
        if not state_policy.is_review_gate_state(report.state):
            return
        # ------------------------------------------------------------------
        # After-review short-circuit: once the current work round already
        # carries a reviewer verdict (reviewed_cycle >= review_cycle), late
        # orchestrator gate-state reports must not trigger a second reviewer
        # dispatch. Returning here keeps the previous reviewer binding,
        # timestamps, and verdict intact; the fast-path in create_report also
        # already prevented any status/timestamp overwrite on the task row.
        #
        # NOTE: a legitimate reopen (review_failed → continue_task, or
        # goal-packet review_passed → implementation-phase reopen) bumps
        # review_cycle past reviewed_cycle, so current_round_has_verdict is
        # False and the new phase's gate report is correctly routed to a fresh
        # reviewer. This is the cycle-based replacement for the old
        # status==REVIEW + review_completed_at timestamp heuristic.
        # ------------------------------------------------------------------
        if state_policy.current_round_has_verdict(task.review_cycle, task.reviewed_cycle):
            logger.info(
                "Skipping re-review dispatch after recorded reviewer verdict "
                "workspace_id=%s task_id=%s session_id=%s report_state=%s "
                "review_cycle=%s reviewed_cycle=%s",
                task.workspace_id,
                task.id,
                session.id,
                report.state.value,
                task.review_cycle,
                task.reviewed_cycle,
            )
            return
        if state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            # Allow a new gate report to recover a stuck review: only skip if
            # the previously-assigned reviewer session is still actively
            # working on the review.
            if self._reviewer_is_active(task):
                return
            logger.info(
                "Recovering stuck review via agent report task_id=%s "
                "report_state=%s review_session_id=%s",
                task.id,
                report.state.value,
                task.review_session_id,
            )
            self._release_stale_reviewer_for_task(task, updated_at=self._runtime_now())
        if task.task_mode == WorkspaceTaskMode.SUBAGENT:
            # Subagent mode: the calling agent judges results. Never route to
            # AI review regardless of review_decision. On completion, mark
            # review-skipped so the task waits for the caller's acceptance.
            if report.state in {
                AgentReportState.COMPLETED,
                AgentReportState.READY_FOR_REVIEW,
            }:
                self._mark_task_review_skipped(task, report)
            return
        if task.task_mode == WorkspaceTaskMode.DIRECT:
            if report.review_decision == ReviewDecision.REQUEST:
                await self._request_task_review(task, report)
                return
            if report.state in {
                AgentReportState.COMPLETED,
                AgentReportState.READY_FOR_REVIEW,
            }:
                self._mark_task_review_skipped(task, report)
            return
        evidence_gaps = self._completion_evidence_gaps(task, report)
        if report.review_decision == ReviewDecision.SKIP and evidence_gaps:
            await self._request_goal_packet_supplement(task, session, report, evidence_gaps)
            return
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            await self._request_task_review(task, report)
            return
        should_review = await self._should_request_task_review(
            task,
            report,
            trigger_kind="agent_report",
        )
        if should_review:
            await self._request_task_review(task, report)
            return
        self._mark_task_review_skipped(task, report)
