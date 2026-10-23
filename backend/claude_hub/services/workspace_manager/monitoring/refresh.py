"""Runtime status refresh and prompt-dispatch stall detection."""

from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _StatusRefreshMixin(_WorkspaceManagerBase):
    # All dispatch variants have distinct first lines in the terminal input box.
    _PROMPT_PREFIX_INITIAL_DISPATCH = "New workspace task assigned."
    _PROMPT_PREFIX_REVIEW = "Review workspace task."
    _PROMPT_PREFIX_CONTINUE = "Continue workspace task from review."
    _PROMPT_PREFIX_HARD_RECOVERY = "⚠️  Your previous context was automatically cleared because the"

    async def _refresh_session_statuses(
        self,
        workspace_id: Optional[str] = None,
        *,
        run_auto_continue: bool = False,
    ) -> None:
        sessions = [
            session
            for session in self.sessions.values()
            if not session.archived
            and (workspace_id is None or session.workspace_id == workspace_id)
        ]
        tab_ids = [
            session.tab_id for session in sessions if session.session_kind != SessionKind.CHAT
        ]
        statuses = (
            {
                status.tab_id: status
                for status in await ttyd_manager.list_tab_agent_statuses(tab_ids=tab_ids)
            }
            if tab_ids
            else {}
        )
        changed = False
        for session in sessions:
            session_id = session.id
            if session.status in {ManagedSessionStatus.DONE, ManagedSessionStatus.ERROR}:
                continue
            if session.session_kind == SessionKind.CHAT:
                changed = self._refresh_chat_session_status(session) or changed
                continue
            status = statuses.get(session.tab_id)
            if not status:
                continue
            live = self.sessions.get(session_id)
            if (
                live is None
                or live.tab_id != session.tab_id
                or live.created_at != session.created_at
            ):
                continue
            session = live
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
                if task is not None and not self._workspace_owns_task(task):
                    task = None
                if task and task.status in {
                    WorkspaceTaskStatus.WORKING,
                    WorkspaceTaskStatus.REVIEW,
                }:
                    stalled_update = await self._detect_prompt_dispatch_stall(
                        session,
                        task,
                        status.sampled_at,
                    )
                    current = self._current_monitor_entities(session, task)
                    if current is None:
                        continue
                    session, task = current
                    if stalled_update:
                        update.update(stalled_update)
                        next_status = update["status"]
                        runtime_status = update["runtime_status"]
                        changed = True
                if (
                    run_auto_continue
                    and runtime_status == AgentRuntimeStatus.IDLE
                    and task
                    and task.status in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}
                ):
                    auto_continue_update = await self._auto_continue_stopped_task(
                        session,
                        task,
                        status.sampled_at,
                    )
                    current = self._current_monitor_entities(session, task)
                    if current is None:
                        continue
                    session, task = current
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
                    # If the latest report is already a prompt-dispatch-stall
                    # marker, the stall detector handled the state transition
                    # (session NEEDS_INPUT, risk-level report recorded) and we
                    # must NOT fire _request_task_review here — that would
                    # incorrectly move the task to REVIEW when the worker never
                    # finished implementation, losing the work phase. Prompt
                    # stall is a delivery failure on the worker's own prompt,
                    # not a worker request for review; nudge/auto-recovery
                    # revives it, not a reviewer.
                    if not self._latest_report_has_risk(
                        task.id, PROMPT_STUCK_RISK_LEVEL
                    ) and not state_policy.review_in_flight(
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

            if current_task_id and task is not None:
                current = self._current_monitor_entities(session, task)
                if current is None:
                    continue
                live, task = current
            else:
                live = self.sessions.get(session_id)
                if (
                    live is None
                    or self.workspaces.get(session.workspace_id) is None
                    or live.workspace_id != session.workspace_id
                    or live.tab_id != session.tab_id
                    or live.created_at != session.created_at
                ):
                    continue
            self.sessions[session_id] = live.model_copy(update=update)
            changed = True

        # --- Failure detection: session death and timeout ---
        # After refreshing session statuses, scan WORKING tasks for two
        # failure conditions:
        #   1. The task's worker session is STOPPED or no longer exists.
        #   2. The task has been in WORKING longer than ``timeout_seconds``
        #      without a fresh update (no report, no activity).
        # Either moves the task to FAILED with a reason and requests human
        # acceptance so the caller can accept/continue.
        now = self._runtime_now()
        for task in list(self.tasks.values()):
            if not self._workspace_owns_task(task):
                continue
            if task.status != WorkspaceTaskStatus.WORKING:
                continue
            if workspace_id is not None and task.workspace_id != workspace_id:
                continue

            failure_reason: str | None = None

            # Session death and timeout apply to subagent tasks only. Reviewed /
            # autonomous WORKING tasks keep the existing recovery paths.
            if task.task_mode == WorkspaceTaskMode.SUBAGENT:
                worker_session = self.sessions.get(task.session_id) if task.session_id else None
                if task.session_id and (
                    worker_session is None or worker_session.status == ManagedSessionStatus.STOPPED
                ):
                    failure_reason = "session died"
                if failure_reason is None and task.timeout_seconds:
                    if (now - task.updated_at).total_seconds() > task.timeout_seconds:
                        failure_reason = f"timeout after {task.timeout_seconds}s"

            if failure_reason is not None:
                self.tasks[task.id] = apply_task_update(
                    task,
                    {
                        "status": WorkspaceTaskStatus.FAILED,
                        "failure_reason": failure_reason,
                        "failed_at": now,
                        "human_acceptance_requested_at": now,
                        "updated_at": now,
                    },
                    cause="runtime_failure",
                )
                changed = True
                logger.warning(
                    "Workspace task marked FAILED task_id=%s reason=%s",
                    task.id,
                    failure_reason,
                )

        if changed:
            self._save_state()

    def _current_monitor_entities(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> tuple[ManagedSession, WorkspaceTask] | None:
        """Return the exact live monitor owners, rejecting deletion or reuse."""
        live = self.sessions.get(session.id)
        live_task = self.tasks.get(task.id)
        if (
            self.workspaces.get(session.workspace_id) is None
            or live is None
            or live.workspace_id != session.workspace_id
            or live.tab_id != session.tab_id
            or live.created_at != session.created_at
            or live_task is None
            or live_task.workspace_id != task.workspace_id
            or live_task.created_at != task.created_at
            or not self._workspace_owns_task(live_task)
        ):
            return None
        return live, live_task

    async def _detect_prompt_dispatch_stall(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> dict[str, Any] | None:
        if session.session_kind == SessionKind.CHAT:
            return None
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None
        session, task = current
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
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None
        session, task = current

        # Check ALL known prompt prefixes rather than a single one. The
        # continue_task and hard-recovery paths send prompts that start with
        # different first lines than the initial-dispatch / review prompts;
        # previously the detector only looked for "New workspace task assigned."
        # / "Review workspace task." and silently missed stalled continue /
        # hard-recovery pastes, so a stuck Enter key on the continue prompt
        # was never retried and the worker sat idle indefinitely.
        for prefix in self._expected_pending_prompt_prefixes(session, task):
            if self._message_still_in_input(output, prefix):
                break
        else:
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

    def _expected_pending_prompt_prefixes(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> list[str]:
        """Return prompt first-lines that could legitimately be in the input box."""
        if session.role == WorkspaceSessionRole.REVIEWER:
            prefixes = [self._PROMPT_PREFIX_REVIEW]
            # A reviewer that just underwent hard recovery will have the
            # hard-recovery reviewer prompt sitting in the input box.
            if (
                session.hard_recovery_task_id == task.id
                and session.last_hard_recovery_at
                and task.review_requested_at
                and session.last_hard_recovery_at >= task.review_requested_at
            ):
                prefixes.append(self._PROMPT_PREFIX_HARD_RECOVERY)
            return prefixes
        prefixes = [self._PROMPT_PREFIX_INITIAL_DISPATCH, self._PROMPT_PREFIX_CONTINUE]
        if (
            session.hard_recovery_task_id == task.id
            and session.last_hard_recovery_at
            and task.started_at
            and session.last_hard_recovery_at >= task.started_at
        ):
            prefixes.append(self._PROMPT_PREFIX_HARD_RECOVERY)
        return prefixes

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
        await self._run_tmux("send-keys", "-t", session.tmux_session, "Enter")
        async with self.workspace_mutation_lock(session.workspace_id):
            current = self._current_monitor_entities(session, task)
            if current is None:
                return
            live, _live_task = current
            self.sessions[session.id] = live.model_copy(
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
