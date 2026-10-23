"""Report/status reconciliation and task assignment release."""

from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _ReportReconciliationMixin(_WorkspaceManagerBase):
    def _latest_report_for_task(self, task_id: str) -> AgentReport | None:
        """Return the most recent report for a task (any state)."""
        reports = sorted(
            [report for report in self.reports.values() if report.task_id == task_id],
            key=lambda report: report.created_at,
        )
        return reports[-1] if reports else None

    def _auto_continue_completion_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_completion_reason(output)

    def _auto_continue_interruption_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_interruption_reason(output)

    def _auto_continue_recent_output_segment(self, output: str) -> str:
        return state_policy.auto_continue_recent_output_segment(output)

    def _auto_continue_output_looks_busy(self, output: str) -> bool:
        return state_policy.auto_continue_output_looks_busy(output)

    def _task_has_post_cycle_report(self, task_id: str, cycle: int) -> bool:
        """Return True if any report for this task has review_cycle > cycle.

        Used to detect whether continue_task successfully wrote its next-cycle
        WORKING report after a sealed verdict. If such a report exists, the
        verdict-reopen path already ran and the sealed-round auto-continue
        must not fire a second time.
        """
        for report in self.reports.values():
            if report.task_id != task_id:
                continue
            if report.review_cycle is not None and report.review_cycle > cycle:
                return True
        return False

    def _sealed_cycle_verdict_state(self, task_id: str, cycle: int) -> AgentReportState | None:
        """Return the terminal review verdict state at `cycle`, or None.

        Iterates review_* reports for the task scoped to exactly `cycle`,
        preferring terminal states (REVIEW_PASSED / REVIEW_FAILED /
        REVIEW_NEEDS_INPUT) over intermediate ones. Used by sealed-round
        recovery to distinguish auto-continuable verdicts from
        review_needs_input, which parks for human attention and must NOT be
        auto-continued.
        """
        terminal = {
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }
        latest_terminal: AgentReportState | None = None
        latest_any: AgentReportState | None = None
        latest_ts = None
        for report in self.reports.values():
            if report.task_id != task_id:
                continue
            if not report.state.value.startswith("review_"):
                continue
            if report.review_cycle is None or report.review_cycle != cycle:
                continue
            if latest_ts is None or report.created_at > latest_ts:
                latest_ts = report.created_at
                latest_any = report.state
            if report.state in terminal:
                latest_terminal = report.state
        return latest_terminal or latest_any

    def _reconcile_task_report_statuses(self, workspace_id: str) -> None:
        changed = False
        reports_by_task: dict[str, AgentReport] = {}
        for report in self.reports_for_workspace(workspace_id):
            if report.task_id:
                reports_by_task[report.task_id] = report

        for task_id, report in reports_by_task.items():
            task = self.tasks.get(task_id)
            if (
                not task
                or not self._workspace_owns_task(task)
                or task.workspace_id != workspace_id
                or task.status == WorkspaceTaskStatus.DONE
            ):
                continue
            # Cycle gate: never reconcile task status from a report belonging to
            # a different work round. A prior-round verdict (e.g. an old
            # REVIEW_PASSED stamped at review_cycle=1) must not resurrect itself
            # after continue_task opened round 2; legacy reports (cycle 0) are
            # likewise ignored once the task has advanced past round 0.
            if report.review_cycle != task.review_cycle:
                continue
            if report.state not in {
                AgentReportState.READY_FOR_REVIEW,
                AgentReportState.COMPLETED,
            }:
                continue
            if task.status == WorkspaceTaskStatus.REVIEW:
                continue
            if task.reviewed_at != report.created_at:
                continue
            if (
                task.updated_at > report.created_at
                and task.started_at
                and task.started_at > report.created_at
            ):
                continue

            self.tasks[task_id] = apply_task_update(
                task,
                {
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": report.created_at,
                    "updated_at": report.created_at,
                },
                cause="reconcile",
            )
            changed = True

        if changed:
            self._save_state()

    def _latest_report_state(self, task_id: str) -> AgentReportState | None:
        reports = sorted(
            [report for report in self.reports.values() if report.task_id == task_id],
            key=lambda report: report.created_at,
        )
        return reports[-1].state if reports else None

    def _latest_report_has_risk(self, task_id: str, risk_level: str) -> bool:
        reports = sorted(
            [report for report in self.reports.values() if report.task_id == task_id],
            key=lambda report: report.created_at,
        )
        return bool(reports and reports[-1].risk_level == risk_level)

    def _latest_review_report_state(self, task_id: str) -> AgentReportState | None:
        reports = sorted(
            [
                report
                for report in self.reports.values()
                if report.task_id == task_id and report.state.value.startswith("review_")
            ],
            key=lambda report: report.created_at,
        )
        return reports[-1].state if reports else None

    def _latest_review_report_for_task(self, task_id: str) -> AgentReport | None:
        """Return the most recent REVIEW_* AgentReport on the task (or None)."""

        reports = sorted(
            [
                report
                for report in self.reports.values()
                if report.task_id == task_id and report.state.value.startswith("review_")
            ],
            key=lambda report: report.created_at,
        )
        return reports[-1] if reports else None

    def _map_runtime_status(self, status: TerminalAgentStatus) -> ManagedSessionStatus:
        return state_policy.managed_status_from_runtime(status.status)

    def _is_spawn_grace_period(self, session: ManagedSession) -> bool:
        if session.status != ManagedSessionStatus.SPAWNING:
            return False
        return (self._runtime_now() - session.created_at).total_seconds() < 90

    def _status_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> ManagedSessionStatus:
        return state_policy.managed_status_from_report(state, session.role, session.status)

    def _runtime_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> AgentRuntimeStatus:
        return state_policy.runtime_status_from_report(state, session.runtime_status)

    def _task_status_from_report(self, state: AgentReportState) -> Optional[WorkspaceTaskStatus]:
        return state_policy.task_status_from_report(state)

    def _is_stale_report_for_aborted_task(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> bool:
        if not task.manual_aborted_at:
            return False
        return task.session_id != session.id and task.review_session_id != session.id

    def _release_task_session(self, task: WorkspaceTask) -> None:
        if not self._workspace_owns_task(task):
            return
        if not task.session_id:
            return
        session = self.sessions.get(task.session_id)
        if not session:
            return
        if session.task_id == task.id or session.current_task_id == task.id:
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "last_auto_continue_at": None,
                    "hard_recovery_task_id": None,
                    "hard_recovery_attempts": 0,
                    "last_hard_recovery_at": None,
                    "updated_at": self._runtime_now(),
                }
            )

    def _release_reviewer_session(
        self,
        task: WorkspaceTask,
        *,
        status: ManagedSessionStatus,
        runtime_status: AgentRuntimeStatus,
        updated_at: datetime,
        include_stale_assignments: bool = False,
    ) -> None:
        session_ids: set[str] = set()
        if task.review_session_id:
            session_ids.add(task.review_session_id)
        if include_stale_assignments:
            session_ids.update(
                session.id
                for session in self.sessions.values()
                if session.role == WorkspaceSessionRole.REVIEWER
                and (session.task_id == task.id or session.current_task_id == task.id)
            )

        for session_id in session_ids:
            session = self.sessions.get(session_id)
            if (
                not session
                or session.role != WorkspaceSessionRole.REVIEWER
                or (session.task_id != task.id and session.current_task_id != task.id)
            ):
                continue
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": status,
                    "runtime_status": runtime_status,
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "last_auto_continue_at": None,
                    "hard_recovery_task_id": None,
                    "hard_recovery_attempts": 0,
                    "last_hard_recovery_at": None,
                    "updated_at": updated_at,
                    "last_activity_at": updated_at,
                }
            )

    async def _cleanup_reviewer_for_terminal_task(
        self,
        task: WorkspaceTask,
        *,
        updated_at: datetime,
        delete_tabs: bool = True,
    ) -> list[str]:
        """Release persistent reviewers and delete task-scoped temporary reviewers."""
        if not self._workspace_owns_task(task):
            return []

        ephemeral_tab_ids: list[str] = []
        session_ids: set[str] = set()
        if task.review_session_id:
            session_ids.add(task.review_session_id)
        session_ids.update(
            session.id
            for session in self.sessions.values()
            if session.role == WorkspaceSessionRole.REVIEWER
            and (session.task_id == task.id or session.current_task_id == task.id)
        )

        for session_id in session_ids:
            live_task = self.tasks.get(task.id)
            if (
                self.workspaces.get(task.workspace_id) is None
                or live_task is None
                or live_task.workspace_id != task.workspace_id
                or live_task.created_at != task.created_at
                or not self._workspace_owns_task(live_task)
            ):
                return ephemeral_tab_ids
            session = self.sessions.get(session_id)
            if not session or session.role != WorkspaceSessionRole.REVIEWER:
                continue
            if session.ephemeral:
                self.sessions.pop(session.id, None)
                if delete_tabs:
                    try:
                        await ttyd_manager.delete_tab(session.tab_id)
                    except Exception:
                        logger.exception(
                            "Failed to delete temporary reviewer tab session_id=%s tab_id=%s",
                            session.id,
                            session.tab_id,
                        )
                else:
                    ephemeral_tab_ids.append(session.tab_id)
                continue
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "last_auto_continue_at": None,
                    "hard_recovery_task_id": None,
                    "hard_recovery_attempts": 0,
                    "last_hard_recovery_at": None,
                    "updated_at": updated_at,
                    "last_activity_at": updated_at,
                }
            )
        return ephemeral_tab_ids

    def _rename_task_assignment_tab(self, task: WorkspaceTask, session: ManagedSession) -> None:
        title = task.title.strip() or session.title
        renamed = ttyd_manager.rename_tab(session.tab_id, title)
        if not renamed:
            logger.warning(
                "Could not rename workspace session tab for task session_id=%s tab_id=%s task_id=%s",
                session.id,
                session.tab_id,
                task.id,
            )

    def _assign_current_task(
        self, session_id: str, task_id: str, *, rename_tab: bool = True
    ) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        self._require_workspace_execution(task)
        session = self.sessions.get(session_id)
        if not session:
            return
        task = self.tasks.get(task_id)
        title = task.title.strip() if task and task.title.strip() else session.title
        if task and rename_tab:
            self._rename_task_assignment_tab(task, session)
        self.sessions[session_id] = session.model_copy(
            update={
                "task_id": task_id,
                "current_task_id": task_id,
                "title": title,
                "auto_continue_task_id": task_id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "updated_at": self._runtime_now(),
            }
        )
