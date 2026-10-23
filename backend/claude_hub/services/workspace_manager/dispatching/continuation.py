"""Dispatcher decisions, task continuation, and review requests."""

from claude_hub.services.task_dependencies import require_task_dependencies
from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _TaskContinuationMixin(_WorkspaceManagerBase):
    async def _apply_dispatch_decision_workspace(
        self,
        task_id: str,
        payload: DispatchDecisionRequest,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        require_task_dependencies(self.tasks, task)
        target = self.sessions.get(payload.target_session_id)
        if not target or target.workspace_id != task.workspace_id:
            raise KeyError(payload.target_session_id)
        if target.role != WorkspaceSessionRole.ORCHESTRATOR:
            raise RuntimeError("Dispatch decisions must target a workspace agent")
        if target.session_kind != task.session_kind:
            raise ValueError("The selected agent does not match the Task execution surface")

        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": WorkspaceTaskStatus.QUEUED,
                "session_id": target.id,
                # A user opt-in ("Clear context" checkbox -> task.clear_context)
                # must not be overridden by the dispatcher agent's discretion:
                # OR the stored flag so an explicit clear request always wins.
                "clear_context": bool(payload.clear_context) or bool(task.clear_context),
                "dispatch_reason": payload.reason or "Dispatcher selected target agent",
                "dispatch_pending": False,
                "queued_at": task.queued_at or self._runtime_now(),
                "updated_at": self._runtime_now(),
            },
            cause="dispatch_decision",
        )
        self._save_state()
        await self.dispatch_workspace(task.workspace_id)
        return self.tasks[task.id]

    async def _continue_task_workspace(
        self,
        task_id: str,
        payload: ContinueTaskRequest | None = None,
        call_id: str | None = None,
    ) -> WorkspaceTask:
        payload = payload or ContinueTaskRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.status not in (WorkspaceTaskStatus.REVIEW, WorkspaceTaskStatus.FAILED):
            raise RuntimeError("Only review or failed tasks can continue")

        require_task_dependencies(self.tasks, task)

        # For FAILED tasks whose original session is gone or stopped, reassign
        # to a new agent via start_task. The failure_reason stays on the task so
        # the assignment prompt surfaces it to the new worker; we clear it after
        # start_task returns (the prompt has already been built and sent).
        if task.status == WorkspaceTaskStatus.FAILED:
            existing_session = self.sessions.get(task.session_id) if task.session_id else None
            if existing_session is None or existing_session.status == ManagedSessionStatus.STOPPED:
                await self.start_task(task.id)
                task = self.tasks[task.id]
                self.tasks[task.id] = task.model_copy(
                    update={
                        "failure_reason": None,
                        "failed_at": None,
                    }
                )
                self._save_state()
                return self.tasks[task.id]

        if not task.session_id or task.session_id not in self.sessions:
            raise RuntimeError("Task has no original agent")

        # Capture failure context (if any) before it is cleared below so the
        # continue prompt can surface it to the worker.
        failure_reason = task.failure_reason if task.status == WorkspaceTaskStatus.FAILED else None

        now = self._runtime_now()
        self._ensure_session_can_continue_task(self.sessions[task.session_id], task)
        session = await self._rename_session_for_task(
            self.sessions[task.session_id],
            task,
            updated_at=now,
        )
        task = self.tasks[task.id]
        if task.status not in (WorkspaceTaskStatus.REVIEW, WorkspaceTaskStatus.FAILED):
            raise RuntimeError("Task changed while preparing to continue")
        require_task_dependencies(self.tasks, task)
        autonomous_run = task.autonomous_run
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            autonomous_run = autonomous_run or self._default_autonomous_run(
                task.id,
                task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
            )
            phase = AutonomousRunPhase.WORKING
            autonomous_run = autonomous_run.model_copy(
                update={
                    "phase": phase,
                    "status_summary": self._autonomous_phase_label(phase),
                    "active_session_ids": list(
                        dict.fromkeys([*autonomous_run.active_session_ids, session.id])
                    ),
                    "next_action": self._autonomous_next_action(phase),
                }
            )
        await self._resume_chat_session(session.id)
        session = self.sessions[session.id]
        # Opening a fresh work round: bump review_cycle so the worker's next
        # ready_for_review outranks the prior verdict (reviewed_cycle) and a
        # new reviewer is dispatched. Verdict timestamp fields are cleared too,
        # but cycle ordering — not timestamps — is what gates re-review.
        next_cycle = task.review_cycle + 1
        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": WorkspaceTaskStatus.WORKING,
                "started_at": now,
                "review_cycle": next_cycle,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "autonomous_run": autonomous_run,
                "updated_at": now,
                "dispatch_pending": False,
                "review_completed_at": None,
                "reviewed_at": None,
                "review_requested_at": None,
                "failure_reason": None,
                "failed_at": None,
            },
            cause="continue",
        )
        logger.info(
            "continue_task set status=WORKING and cleared stale goal-packet verdict fields "
            "task_id=%s review_completed_at_cleared=%s session_id=%s",
            task.id,
            task.review_completed_at is not None,
            task.session_id,
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "task_id": task.id,
                "current_task_id": task.id,
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "prompt_retry_task_id": None,
                "prompt_retry_attempted_at": None,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        continue_report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session.id,
            state=AgentReportState.WORKING,
            message=payload.message
            or ("Task continued after failure" if failure_reason else "Task continued from review"),
            changed_files=[],
            validation=None,
            risks=None,
            review_cycle=next_cycle,
            created_at=now,
        )
        self.reports[continue_report.id] = continue_report
        bridge_wake_target = self._bridge_report_to_agent_event(continue_report, session)
        self._save_state()
        if bridge_wake_target is not None:
            self._wake_report_intake_runs({bridge_wake_target})

        # Send the continue prompt; on tmux submit failure mark the dispatch
        # as stalled so the monitor stall-detector and auto-continue can
        # recover. Without this, a RuntimeError from _submit_tmux_message
        # propagates to the HTTP layer while state is already persisted as
        # WORKING, leaving a window where the task appears working but the
        # worker never received the prompt.
        try:
            await self.send_session_message(
                session.id,
                self._build_continue_prompt(
                    self.tasks[task.id], payload, session, failure_reason=failure_reason
                ),
                call_id=call_id,
            )
        except Exception as exc:
            logger.exception(
                "Failed to dispatch workspace continue prompt task_id=%s session_id=%s",
                task.id,
                session.id,
            )
            self._mark_prompt_dispatch_stalled(
                task_id=task.id,
                session_id=session.id,
                message=(
                    "Continue prompt could not be submitted to the terminal; "
                    "auto-recovery will nudge the worker. "
                    f"Error: {exc}"
                ),
                message_zh=(
                    "Continue prompt 未能提交到终端；自动恢复将提示 worker。" f"错误：{exc}"
                ),
                report_state=AgentReportState.NEEDS_INPUT,
                sampled_at=self._runtime_now(),
            )
        return self.tasks[task.id]

    def _ensure_session_can_continue_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> None:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            raise RuntimeError("Task original session is not a workspace agent")
        if session.status == ManagedSessionStatus.STOPPED:
            raise RuntimeError("Task original agent is stopped")

        assigned_ids = {
            assigned_id for assigned_id in (session.task_id, session.current_task_id) if assigned_id
        }
        busy_ids = [assigned_id for assigned_id in assigned_ids if assigned_id != task.id]
        for busy_id in busy_ids:
            busy_task = self.tasks.get(busy_id)
            if not busy_task or busy_task.status != WorkspaceTaskStatus.DONE:
                raise RuntimeError(
                    "Task original agent is busy with another task; "
                    "wait for that task to finish before requesting changes."
                )

    async def _request_task_review_workspace(
        self,
        task_id: str,
        payload: RequestTaskReviewRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or RequestTaskReviewRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot request review")
        if state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            # Only suppress re-dispatch when an assigned reviewer is actively
            # working on this task. If the reviewer session is idle, stopped,
            # or missing entirely, the prior review dispatch got stuck and we
            # must allow re-dispatch so the task does not sit forever in
            # "Awaiting AI review".
            if self._reviewer_is_active(task):
                return task
            logger.info(
                "Re-requesting review for stuck task_id=%s (review_requested_at=%s "
                "review_session_id=%s)",
                task.id,
                task.review_requested_at,
                task.review_session_id,
            )
            self._release_stale_reviewer_for_task(task, updated_at=self._runtime_now())
        if not task.session_id:
            raise RuntimeError("Task has no implementation agent")

        now = self._runtime_now()
        message = (
            payload.message.strip()
            if payload.message and payload.message.strip()
            else "Human requested reviewer checks."
        )
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=task.session_id,
            state=AgentReportState.READY_FOR_REVIEW,
            message=message,
            changed_files=[],
            validation=None,
            risks=None,
            review_decision=ReviewDecision.REQUEST,
            review_reason=message,
            risk_level=None,
            review_cycle=task.review_cycle,
            created_at=now,
        )
        self.reports[report.id] = report
        await self._request_task_review(task, report)
        return self.tasks[task.id]
