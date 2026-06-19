"""Status refresh, stall detection, auto-continue, reconcile."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _MonitorMixin:
    async def _refresh_session_statuses(
        self,
        workspace_id: Optional[str] = None,
        *,
        run_auto_continue: bool = False,
    ) -> None:
        sessions = [
            session
            for session in self.sessions.values()
            if workspace_id is None or session.workspace_id == workspace_id
        ]
        tab_ids = [session.tab_id for session in sessions]
        statuses = {
            status.tab_id: status
            for status in await ttyd_manager.list_tab_agent_statuses(tab_ids=tab_ids)
        }
        changed = False
        for session in sessions:
            session_id = session.id
            if session.status in {ManagedSessionStatus.DONE, ManagedSessionStatus.ERROR}:
                continue
            status = statuses.get(session.tab_id)
            if not status:
                continue
            next_status = self._map_runtime_status(status)
            runtime_status = status.status
            if next_status == ManagedSessionStatus.STOPPED and self._is_spawn_grace_period(session):
                continue

            current_task_id = session.current_task_id or session.task_id
            update: dict[str, Any] = {
                "status": next_status,
                "runtime_status": runtime_status,
                "current_task_id": current_task_id,
                "updated_at": status.sampled_at,
            }
            if status.last_changed_at:
                update["last_activity_at"] = status.last_changed_at

            if current_task_id:
                task = self.tasks.get(current_task_id)
                if task and task.status in {
                    WorkspaceTaskStatus.WORKING,
                    WorkspaceTaskStatus.REVIEW,
                }:
                    stalled_update = await self._detect_prompt_dispatch_stall(
                        session,
                        task,
                        status.sampled_at,
                    )
                    if stalled_update:
                        update.update(stalled_update)
                        next_status = update["status"]
                        runtime_status = update["runtime_status"]
                        task = self.tasks.get(current_task_id)
                        changed = True
                if (
                    run_auto_continue
                    and runtime_status == AgentRuntimeStatus.IDLE
                    and task
                    and task.status == WorkspaceTaskStatus.WORKING
                ):
                    auto_continue_update = await self._auto_continue_stopped_task(
                        session,
                        task,
                        status.sampled_at,
                    )
                    if auto_continue_update:
                        update.update(auto_continue_update)
                        next_status = update["status"]
                        runtime_status = update["runtime_status"]
                        changed = True
                if (
                    runtime_status == AgentRuntimeStatus.ATTENTION
                    and task
                    and task.status == WorkspaceTaskStatus.WORKING
                ):
                    if not state_policy.review_in_flight(
                        task.review_requested_at, task.review_completed_at
                    ):
                        report = AgentReport(
                            id=str(uuid.uuid4()),
                            workspace_id=task.workspace_id,
                            task_id=task.id,
                            session_id=session.id,
                            state=AgentReportState.NEEDS_INPUT,
                            message=status.detail
                            or "Agent runtime is waiting for input; reviewer diagnosis requested.",
                            changed_files=[],
                            validation=None,
                            risks=None,
                            review_cycle=task.review_cycle,
                            created_at=status.sampled_at,
                        )
                        self.reports[report.id] = report
                        await self._request_task_review(task, report)
                    update["task_id"] = current_task_id
                    changed = True

            self.sessions[session_id] = session.model_copy(update=update)
            changed = True
        if changed:
            self._save_state()

    async def _detect_prompt_dispatch_stall(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> dict[str, Any] | None:
        if self._prompt_dispatch_still_in_grace_period(session, task, sampled_at):
            return None
        try:
            output = await self._capture_tmux_output(session.tmux_session)
        except RuntimeError as exc:
            logger.warning(
                "Could not inspect workspace prompt dispatch for session_id=%s: %s",
                session.id,
                exc,
            )
            return None

        if not self._message_still_in_input(
            output,
            self._expected_pending_prompt_prefix(session, task),
        ):
            return None
        if self._latest_report_has_risk(task.id, PROMPT_STUCK_RISK_LEVEL):
            return {
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "updated_at": sampled_at,
            }
        if session.prompt_retry_task_id != task.id:
            await self._retry_pending_prompt_submit(session, task, sampled_at)
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "prompt_retry_task_id": task.id,
                "prompt_retry_attempted_at": sampled_at,
                "updated_at": sampled_at,
                "last_activity_at": sampled_at,
            }
        if self._prompt_dispatch_retry_still_in_grace_period(session, sampled_at):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "updated_at": sampled_at,
            }

        if session.role == WorkspaceSessionRole.REVIEWER:
            message = (
                "Reviewer prompt appears to still be sitting in the terminal input box; "
                "review did not start and manual recovery is required."
            )
            message_zh = "Reviewer prompt 似乎仍停留在终端输入框；评审未启动，需要手动恢复。"
            report_state = AgentReportState.REVIEW_NEEDS_INPUT
        else:
            message = (
                "Workspace task prompt appears to still be sitting in the terminal input box; "
                "the agent did not start executing and manual recovery is required."
            )
            message_zh = (
                "Workspace task prompt 似乎仍停留在终端输入框；Agent 未开始执行，需要手动恢复。"
            )
            report_state = AgentReportState.NEEDS_INPUT

        self._mark_prompt_dispatch_stalled(
            task_id=task.id,
            session_id=session.id,
            message=message,
            message_zh=message_zh,
            report_state=report_state,
            sampled_at=sampled_at,
        )
        return {
            "status": ManagedSessionStatus.NEEDS_INPUT,
            "runtime_status": AgentRuntimeStatus.ATTENTION,
            "updated_at": sampled_at,
        }

    def _expected_pending_prompt_prefix(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> str:
        if session.role == WorkspaceSessionRole.REVIEWER:
            return "Review workspace task."
        return "New workspace task assigned."

    def _prompt_dispatch_still_in_grace_period(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> bool:
        dispatch_started_at = (
            task.review_requested_at
            if session.role == WorkspaceSessionRole.REVIEWER
            else task.started_at
        )
        if not dispatch_started_at:
            return False
        return (
            sampled_at - dispatch_started_at
        ).total_seconds() < PROMPT_DISPATCH_STALL_GRACE_SECONDS

    def _prompt_dispatch_retry_still_in_grace_period(
        self,
        session: ManagedSession,
        sampled_at: datetime,
    ) -> bool:
        if not session.prompt_retry_attempted_at:
            return False
        return (
            sampled_at - session.prompt_retry_attempted_at
        ).total_seconds() < PROMPT_DISPATCH_RETRY_GRACE_SECONDS

    async def _retry_pending_prompt_submit(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> None:
        logger.warning(
            "Workspace prompt appears pending; retrying Enter submit session_id=%s task_id=%s role=%s",
            session.id,
            task.id,
            session.role,
        )
        await self._run_tmux("send-keys", "-t", session.tmux_session, "C-m")
        self.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "prompt_retry_task_id": task.id,
                "prompt_retry_attempted_at": sampled_at,
                "last_activity_at": sampled_at,
                "updated_at": sampled_at,
            }
        )
        self._save_state()

    def _mark_prompt_dispatch_stalled(
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
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.REVIEW,
                "reviewed_at": task.reviewed_at or sampled_at,
                "updated_at": sampled_at,
            }
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "task_id": task.id,
                "current_task_id": task.id,
                "last_activity_at": sampled_at,
                "updated_at": sampled_at,
            }
        )
        self._save_state()

    async def _auto_continue_stopped_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> dict[str, Any] | None:
        # Only the worker that owns a WORKING task may be auto-continued. After a
        # ``review_failed`` reopen the reviewer session intentionally stays bound
        # to the task (``current_task_id``) so the same reviewer handles the next
        # cycle — but it is NOT the task's worker. Without this guard the monitor
        # treats the idle reviewer as a worker owing a report and endlessly
        # auto-prompts it (action=report_missing); the reviewer re-posts its
        # verdict, which is correctly dropped as a stale duplicate, stranding the
        # task until the fallback reaper fires (~5 min later). The worker is
        # ``task.session_id``; the reviewer is ``task.review_session_id``.
        if task.session_id and task.session_id != session.id:
            return None
        if state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            return None
        latest_state = self._latest_report_state(task.id)
        if latest_state in {
            AgentReportState.READY_FOR_REVIEW,
            AgentReportState.COMPLETED,
            AgentReportState.BLOCKED,
            AgentReportState.NEEDS_INPUT,
        }:
            return None
        try:
            output = await self._capture_tmux_output(session.tmux_session)
        except RuntimeError as exc:
            logger.warning(
                "Could not inspect workspace agent output for auto-continue session_id=%s: %s",
                session.id,
                exc,
            )
            return None

        if self._auto_continue_output_looks_busy(output):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": (
                    session.auto_continue_attempts
                    if session.auto_continue_task_id == task.id
                    else 0
                ),
                "updated_at": sampled_at,
            }

        last_activity_at = session.last_activity_at
        if (
            last_activity_at
            and (sampled_at - last_activity_at).total_seconds() < AUTO_CONTINUE_IDLE_GRACE_SECONDS
        ):
            return None

        interruption_reason = self._auto_continue_interruption_reason(output)
        completion_reason = None
        if not interruption_reason:
            completion_reason = self._auto_continue_completion_reason(output)
        if not interruption_reason and not completion_reason:
            return None

        attempts = session.auto_continue_attempts if session.auto_continue_task_id == task.id else 0
        if (
            session.auto_continue_task_id == task.id
            and session.last_auto_continue_at
            and (sampled_at - session.last_auto_continue_at).total_seconds()
            < AUTO_CONTINUE_MIN_INTERVAL_SECONDS
        ):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "updated_at": sampled_at,
            }
        if attempts >= AUTO_CONTINUE_MAX_ATTEMPTS:
            self.tasks[task.id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": sampled_at,
                    "updated_at": sampled_at,
                }
            )
            logger.warning(
                "Workspace agent auto-continue limit reached session_id=%s task_id=%s attempts=%s",
                session.id,
                task.id,
                attempts,
            )
            return {
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "updated_at": sampled_at,
            }

        message = AUTO_CONTINUE_MESSAGE if interruption_reason else AUTO_REPORT_MISSING_MESSAGE
        # The agent's context may have been cleared since it learned the report
        # endpoint from its bootstrap/assignment prompt. Restate the endpoint so
        # a cleared agent always has a curl target to POST its report to.
        message = f"{message}\n\n{self._report_endpoint_curl(session, task.id)}"
        await self._send_tmux_message(session.tmux_session, message)
        attempts += 1
        logger.info(
            "Auto-prompted idle workspace agent session_id=%s task_id=%s "
            "attempt=%s/%s action=%s reason=%s",
            session.id,
            task.id,
            attempts,
            AUTO_CONTINUE_MAX_ATTEMPTS,
            "continue" if interruption_reason else "report_missing",
            interruption_reason or completion_reason,
        )
        return {
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
            "auto_continue_task_id": task.id,
            "auto_continue_attempts": attempts,
            "last_auto_continue_at": sampled_at,
            "last_activity_at": sampled_at,
            "updated_at": sampled_at,
        }

    def _auto_continue_completion_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_completion_reason(output)

    def _auto_continue_interruption_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_interruption_reason(output)

    def _auto_continue_recent_output_segment(self, output: str) -> str:
        return state_policy.auto_continue_recent_output_segment(output)

    def _auto_continue_output_looks_busy(self, output: str) -> bool:
        return state_policy.auto_continue_output_looks_busy(output)

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

            self.tasks[task_id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": report.created_at,
                    "updated_at": report.created_at,
                }
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
        return (_wm._now() - session.created_at).total_seconds() < 90

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
                    "updated_at": _wm._now(),
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
                    "updated_at": updated_at,
                    "last_activity_at": updated_at,
                }
            )

    async def _cleanup_reviewer_for_terminal_task(
        self,
        task: WorkspaceTask,
        *,
        updated_at: datetime,
    ) -> None:
        """Release persistent reviewers and delete task-scoped temporary reviewers."""

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
            session = self.sessions.get(session_id)
            if not session or session.role != WorkspaceSessionRole.REVIEWER:
                continue
            if session.ephemeral:
                self.sessions.pop(session.id, None)
                try:
                    await ttyd_manager.delete_tab(session.tab_id)
                except Exception:
                    logger.exception(
                        "Failed to delete temporary reviewer tab session_id=%s tab_id=%s",
                        session.id,
                        session.tab_id,
                    )
                continue
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "updated_at": updated_at,
                    "last_activity_at": updated_at,
                }
            )

    def _assign_current_task(self, session_id: str, task_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return
        task = self.tasks.get(task_id)
        title = task.title.strip() if task and task.title.strip() else session.title
        if task:
            renamed = ttyd_manager.rename_tab(session.tab_id, title)
            if not renamed:
                logger.warning(
                    "Could not rename workspace session tab for task session_id=%s tab_id=%s task_id=%s",
                    session.id,
                    session.tab_id,
                    task.id,
                )
        self.sessions[session_id] = session.model_copy(
            update={
                "task_id": task_id,
                "current_task_id": task_id,
                "title": title,
                "auto_continue_task_id": task_id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "updated_at": _wm._now(),
            }
        )
