"""Reviewer selection and review-report handling."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _ReviewMixin:
    async def _select_or_create_reviewer(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
    ) -> ManagedSession:
        if task.review_session_id:
            reviewer = self.sessions.get(task.review_session_id)
            if (
                reviewer
                and reviewer.workspace_id == workspace.id
                and reviewer.role == WorkspaceSessionRole.REVIEWER
                and reviewer.status != ManagedSessionStatus.STOPPED
            ):
                return reviewer
        reviewer = self._first_available_reviewer(workspace.id)
        if reviewer:
            return reviewer
        return await self.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                title=f"{workspace.name} Temporary Reviewer",
                role=WorkspaceSessionRole.REVIEWER,
                reuse_existing=False,
                cwd=workspace.path,
                target=workspace.target,
                remote_profile_id=workspace.remote_profile_id,
                remote_cwd=workspace.remote_cwd,
                remote_reconnect=workspace.remote_reconnect,
                ephemeral=True,
            ),
        )

    async def _handle_review_report(
        self,
        task: WorkspaceTask,
        reviewer: ManagedSession,
        report: AgentReport,
    ) -> None:
        if report.state == AgentReportState.REVIEW_STARTED:
            return
        if report.state not in {
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }:
            return
        # Route to goal-packet review handler when this report corresponds
        # to a goal packet review decision — regardless of whether the packet
        # is still in PENDING_REVIEW (pre-create_report fast-path) or was
        # already transitioned to APPROVED / REJECTED by the create_report
        # fast-path single-save write.
        #
        # The second (APPROVED/REJECTED) branch is an idempotency path for
        # when the fast-path already transitioned the packet. To avoid
        # mis-routing implementation-phase reviews (where the packet was
        # approved in a *prior* review cycle and review_completed_at gets
        # rewritten by the implementation review's own fast-path), we also
        # require that goal_packet.updated_at be at or after the report
        # timestamp — meaning the packet itself was touched by this review
        # cycle, which only happens for goal packet reviews.
        packet = task.goal_packet
        packet_status = packet.status if packet else None
        is_goal_packet_review = task.task_mode == WorkspaceTaskMode.REVIEWED and (
            packet_status == GoalPacketStatus.PENDING_REVIEW
            or (
                packet_status in {GoalPacketStatus.APPROVED, GoalPacketStatus.REJECTED}
                and packet is not None
                and packet.updated_at is not None
                and packet.updated_at >= report.created_at
                and task.review_session_id == reviewer.id
                and task.review_completed_at is not None
                and task.review_completed_at >= report.created_at
            )
        )
        if is_goal_packet_review:
            await self._handle_goal_packet_review_report(task, reviewer, report)
            return

        now = _wm._now()
        # ------------------------------------------------------------------
        # Idempotency guard: terminal review flags are now written
        # synchronously in create_report() before the first save. If this
        # task already has review_completed_at set (at or after the report
        # timestamp), the persistent state is already correct; re-run
        # continue_task() for REVIEW_FAILED but do not re-save the task or
        # re-release the reviewer (would be a no-op at best, and risks
        # clobbering newer state if any).
        # ------------------------------------------------------------------
        already_applied = (
            task.review_completed_at is not None and task.review_completed_at >= report.created_at
        )
        autonomous_next_phase: AutonomousRunPhase | None = None
        if not already_applied:
            task_update = {
                "review_session_id": reviewer.id,
                "review_completed_at": now,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "reviewed_at": task.reviewed_at or now,
                "completed_at": None,
                "human_acceptance_requested_at": (
                    now if report.state == AgentReportState.REVIEW_PASSED else None
                ),
                "human_accepted_at": None,
                "updated_at": now,
            }
            if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                autonomous_run, autonomous_next_phase = self._autonomous_run_after_evaluation(
                    task, reviewer, report, now=now
                )
                if autonomous_run is not None:
                    task_update["autonomous_run"] = autonomous_run
                    task_update["human_acceptance_requested_at"] = (
                        now if autonomous_next_phase == AutonomousRunPhase.PASSED else None
                    )
            self.tasks[task.id] = task.model_copy(
                update={**task_update, "status": WorkspaceTaskStatus.REVIEW}
            )
            self._save_state()
            logger.info(
                "Reviewer terminal decision applied in _handle_review_report "
                "workspace_id=%s task_id=%s reviewer=%s decision=%s "
                "review_completed_at=%s",
                task.workspace_id,
                task.id,
                reviewer.id,
                report.state.value,
                now,
            )
        else:
            # Fields were already written by create_report fast-path; just
            # derive autonomous_next_phase so the REVIEW_FAILED re-dispatch
            # guard still works correctly for autonomous tasks.
            if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                _run, autonomous_next_phase = self._autonomous_run_after_evaluation(
                    task, reviewer, report, now=now
                )
            logger.info(
                "Reviewer terminal decision already applied; _handle_review_report "
                "skipping writes workspace_id=%s task_id=%s reviewer=%s decision=%s",
                task.workspace_id,
                task.id,
                reviewer.id,
                report.state.value,
            )

        if report.state != AgentReportState.REVIEW_FAILED:
            return
        updated_task = self.tasks[task.id]
        if updated_task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            if autonomous_next_phase != AutonomousRunPhase.REVISING:
                return
            if updated_task.review_attempts > MAX_AUTOMATED_REVIEW_FAILURES:
                return
        feedback = (
            "Autonomous evaluator requested changes.\n\n"
            if updated_task.task_mode == WorkspaceTaskMode.AUTONOMOUS
            else "Reviewer requested changes.\n\n"
        )
        feedback += (
            f"Reviewer session: {reviewer.id}\n"
            f"Review attempt: {updated_task.review_attempts}\n\n"
            f"{report.message}\n\n"
            "Address the required fixes, rerun appropriate validation, and report completed again."
        )
        await self.continue_task(updated_task.id, ContinueTaskRequest(message=feedback))

    async def _handle_goal_packet_review_report(
        self,
        task: WorkspaceTask,
        reviewer: ManagedSession,
        report: AgentReport,
    ) -> None:
        now = _wm._now()
        # ------------------------------------------------------------------
        # Idempotency guard: the create_report() fast-path already writes
        # review_completed_at and transitions the Goal Packet to
        # APPROVED / REJECTED. If those fields are in place, skip the task
        # rewrite and just dispatch continue_task feedback.
        # ------------------------------------------------------------------
        packet_current = task.goal_packet.status if task.goal_packet else None
        already_terminal_packet = packet_current in {
            GoalPacketStatus.APPROVED,
            GoalPacketStatus.REJECTED,
        }
        already_applied = (
            task.review_completed_at is not None
            and task.review_completed_at >= report.created_at
            and already_terminal_packet
        )

        if not already_applied:
            packet_status = GoalPacketStatus.PENDING_REVIEW
            if report.state == AgentReportState.REVIEW_PASSED:
                packet_status = GoalPacketStatus.APPROVED
            elif report.state == AgentReportState.REVIEW_FAILED:
                packet_status = GoalPacketStatus.REJECTED
            # This handler only runs for goal-packet review reports, so the
            # task always carries a goal packet here; assert for mypy.
            assert task.goal_packet is not None
            goal_packet = task.goal_packet.model_copy(
                update={
                    "status": packet_status,
                    "updated_at": now,
                }
            )
            self.tasks[task.id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "goal_packet": goal_packet,
                    "review_session_id": reviewer.id,
                    "review_completed_at": now,
                    "review_skipped_at": None,
                    "review_skip_reason": None,
                    "reviewed_at": task.reviewed_at or now,
                    "completed_at": None,
                    "human_acceptance_requested_at": None,
                    "human_accepted_at": None,
                    "updated_at": now,
                }
            )
            self._save_state()
            logger.info(
                "Goal packet reviewer decision applied in _handle_goal_packet_review_report "
                "workspace_id=%s task_id=%s reviewer=%s decision=%s packet=%s",
                task.workspace_id,
                task.id,
                reviewer.id,
                report.state.value,
                packet_status.value,
            )
        else:
            logger.info(
                "Goal packet reviewer decision already applied; "
                "_handle_goal_packet_review_report skipping writes "
                "workspace_id=%s task_id=%s reviewer=%s decision=%s",
                task.workspace_id,
                task.id,
                reviewer.id,
                report.state.value,
            )

        if report.state == AgentReportState.REVIEW_PASSED:
            feedback = (
                "Goal Packet approved.\n\n"
                f"Reviewer session: {reviewer.id}\n"
                f"Review attempt: {self.tasks[task.id].review_attempts}\n\n"
                f"{report.message}\n\n"
                "The task is back in working state. Begin implementation now, stay within the "
                "approved Goal Packet boundaries, run the validation plan, and map final "
                "acceptance_check evidence to the approved criteria before requesting final review."
            )
            await self.continue_task(task.id, ContinueTaskRequest(message=feedback))
            return
        if report.state == AgentReportState.REVIEW_FAILED:
            feedback = (
                "Goal Packet reviewer requested changes.\n\n"
                f"Reviewer session: {reviewer.id}\n"
                f"Review attempt: {self.tasks[task.id].review_attempts}\n\n"
                f"{report.message}\n\n"
                "Revise the Goal Packet and POST a new working report with goal_packet. "
                "Do not start implementation until a revised Goal Packet receives review_passed."
            )
            await self.continue_task(task.id, ContinueTaskRequest(message=feedback))
