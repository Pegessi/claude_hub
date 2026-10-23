"""Report processing split from the compatibility mixin."""

from claude_hub.services.task_transitions import apply_task_update

from .._constants import *  # noqa: F401,F403
from .autonomous import _AutonomousReportMixin


class _ReportReviewRequestMixin(_AutonomousReportMixin):
    async def _request_goal_packet_supplement(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
        gaps: list[str],
    ) -> None:
        now = self._runtime_now()
        gap_text = ", ".join(gaps)
        # Reopen-to-worker: this hands the task back for reviewable rework, so
        # open the next review round. The worker's subsequent gate report will
        # be stamped with the bumped cycle and thus outranks the prior verdict.
        # A retry of the same durable report must reopen the same round, not
        # advance it again. The report carries the cycle at which the logical
        # supplement was requested, so report.review_cycle + 1 is the stable
        # target even when a post-send failure replays this method.
        next_cycle = max(task.review_cycle, report.review_cycle + 1)
        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": WorkspaceTaskStatus.WORKING,
                "review_cycle": next_cycle,
                "reviewed_at": None,
                "updated_at": now,
            },
            cause="review_request",
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "task_id": task.id,
                "current_task_id": task.id,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        self._save_state()
        supplement_call_id = self._report_prompt_call_id(
            task.id,
            "goal-packet-supplement",
            attempt=report.id,
            cycle=next_cycle,
        )
        message = (
            "Your latest completion-style workspace report is missing required Goal Packet "
            f"audit evidence: {gap_text}.\n\n"
            "Please supplement the task before review or review-skip can proceed. If a Goal "
            "Packet has not been stored yet, include goal_packet with objective, "
            "acceptance_criteria, validation_plan, assumptions, out_of_scope, and "
            "handoff_requirements. Include acceptance_check mapping each acceptance criterion "
            "to status passed, failed, partial, or not_checked with evidence. Then POST a new "
            "ready_for_review or completed report.\n\n"
            "Supplement report example:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"completed",'
            '"message":"Supplemented Goal Packet evidence.",'
            '"message_en":"Supplemented Goal Packet evidence.",'
            '"message_zh":"已补充目标包验收证据。",'
            f'"call_id":"{supplement_call_id}",'
            '"goal_packet":{"objective":"Concrete task objective.",'
            '"acceptance_criteria":["Reviewer-checkable criterion."],'
            '"validation_plan":["Command or manual check."],'
            '"assumptions":[],"out_of_scope":[],"handoff_requirements":[]},'
            '"acceptance_check":[{"criterion":"Reviewer-checkable criterion.",'
            '"status":"passed","evidence":"Command, file, or manual check evidence."}],'
            '"changed_files":[],"validation":"Checks run.",'
            '"risks":"Residual risk or none",'
            '"review_decision":"request","review_reason":"Goal Packet evidence supplemented.",'
            '"risk_level":"low"}\''
        )
        logger.info(
            "Requesting Goal Packet supplement session_id=%s task_id=%s report_id=%s gaps=%s",
            session.id,
            task.id,
            report.id,
            gap_text,
        )
        await self.send_session_message(session.id, message)

    async def _workspace_has_tracked_changes(self, workspace_id: str) -> bool:
        workspace = self.workspaces.get(workspace_id)
        if not workspace or workspace.target != ExecutionTarget.LOCAL:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                workspace.path,
                "status",
                "--porcelain",
                "--untracked-files=no",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return True
        except OSError:
            return True
        if proc.returncode != 0:
            return False
        return bool(stdout.strip())

    def _mark_task_review_skipped(self, task: WorkspaceTask, report: AgentReport) -> None:
        task = self._require_workspace_execution(task)
        now = self._runtime_now()
        reason = report.review_reason or "Agent completed the task without requesting review."
        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": WorkspaceTaskStatus.REVIEW,
                "review_session_id": None,
                "review_requested_at": None,
                "review_completed_at": None,
                "review_skipped_at": now,
                "review_skip_reason": reason,
                "reviewed_at": now,
                "completed_at": None,
                "human_acceptance_requested_at": now,
                "human_accepted_at": None,
                "updated_at": now,
            },
            cause="review_request",
        )
        self._save_state()

    async def _request_task_review_workspace_report(
        self,
        task: WorkspaceTask,
        trigger_report: AgentReport,
    ) -> None:
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        reviewer = await self._select_or_create_reviewer(workspace, task)
        now = self._runtime_now()
        lesson_context = self._lesson_context_payload(
            workspace,
            f"{task.title}\n{task.prompt}\n{trigger_report.message}",
        )
        reviewer = await self._rename_session_for_task(reviewer, task, updated_at=now)
        autonomous_run = task.autonomous_run
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            autonomous_run = autonomous_run or self._default_autonomous_run(
                task.id,
                task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
            )
            phase = AutonomousRunPhase.EVALUATING
            autonomous_run = autonomous_run.model_copy(
                update={
                    "phase": phase,
                    "status_summary": self._autonomous_phase_label(phase),
                    "active_session_ids": list(
                        dict.fromkeys([*autonomous_run.active_session_ids, reviewer.id])
                    ),
                    "next_action": self._autonomous_next_action(phase),
                }
            )
        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": WorkspaceTaskStatus.REVIEW,
                "review_session_id": reviewer.id,
                "review_attempts": task.review_attempts + 1,
                "review_requested_at": now,
                "review_completed_at": None,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "completed_at": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "autonomous_run": autonomous_run,
                "updated_at": now,
            },
            cause="review_request",
        )
        # Capture the reviewer's previously-reviewed task before we overwrite it
        # below. The cross-task /clear decision keys off this session-local value
        # rather than scanning other tasks' review_session_id (which abort/skip/
        # stale-release paths null out, silently dropping the prior-history
        # signal and letting an unrelated review start without /clear).
        previous_review_task_id = reviewer.last_review_task_id
        self.sessions[reviewer.id] = reviewer.model_copy(
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
                "last_review_task_id": (
                    previous_review_task_id
                    if reviewer.session_kind == SessionKind.CHAT
                    else task.id
                ),
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        self._save_state()
        # Two independent reasons to clear the reviewer's context:
        #   1. The user's "Clear context" choice (task.clear_context) — the same
        #      per-task flag the worker honors in _dispatch_task_to_session. It was
        #      previously only wired to the worker, so ticking the box cleared the
        #      worker but never the reviewer (the reviewer ran with stale context).
        #   2. The reviewer last reviewed a *different* task. Same-task re-review
        #      cycles (review_failed -> fix -> completed, or goal-packet then
        #      implementation review) keep their context; a brand-new reviewer with
        #      no prior review (previous_review_task_id is None) pays no /clear
        #      round-trip.
        # Same reviewer + same task (reaper redispath) never clears, even when
        # clear_context is still set — that was the loop that blasted /clear
        # into a mis-bound seat.
        from ...session_seat import should_clear_reviewer_context

        user_requested_clear = bool(task.clear_context)
        unrelated_prior_review = (
            previous_review_task_id is not None and previous_review_task_id != task.id
        )
        should_clear_context = should_clear_reviewer_context(
            user_requested=user_requested_clear,
            previous_review_task_id=previous_review_task_id,
            task_id=task.id,
        )
        try:
            if should_clear_context:
                logger.info(
                    "Clearing reviewer context task_id=%s reviewer_id=%s "
                    "user_requested=%s unrelated_prior_review=%s",
                    task.id,
                    reviewer.id,
                    user_requested_clear,
                    unrelated_prior_review,
                )
                await self._reset_session_context(self.sessions[reviewer.id])
            if reviewer.session_kind == SessionKind.CHAT:
                self.sessions[reviewer.id] = self.sessions[reviewer.id].model_copy(
                    update={"last_review_task_id": task.id}
                )
                self._save_state()
            await self.send_session_message(
                reviewer.id,
                self._build_review_prompt(
                    workspace,
                    self.tasks[task.id],
                    self.sessions[reviewer.id],
                    trigger_report,
                    lesson_context=lesson_context,
                ),
            )
        except Exception as exc:
            logger.exception(
                "Failed to dispatch workspace review prompt task_id=%s reviewer_id=%s",
                task.id,
                reviewer.id,
            )
            self._mark_prompt_dispatch_stalled(
                task_id=task.id,
                session_id=reviewer.id,
                message=(
                    "Reviewer prompt could not be submitted to the terminal; "
                    f"manual recovery is required. Error: {exc}"
                ),
                message_zh=("Reviewer prompt 未能提交到终端；需要手动恢复。" f"错误：{exc}"),
                report_state=AgentReportState.REVIEW_NEEDS_INPUT,
                sampled_at=self._runtime_now(),
            )

    async def _request_task_review(self, task: WorkspaceTask, trigger_report: AgentReport) -> None:
        async with self._workspace_execution_operation(task) as live:
            await self._request_task_review_workspace_report(live, trigger_report)
