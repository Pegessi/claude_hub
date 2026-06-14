"""Reviewer selection and review-report handling."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _ReviewMixin:
    def _reviewer_is_busy_with_other_task(
        self, reviewer: "ManagedSession", current_task_id: str
    ) -> bool:
        """Return True when the reviewer session is actively working on a
        review for a task other than ``current_task_id``. Used to decide
        whether we can reuse the task's previously-assigned reviewer or
        must fall through to an available one.

        A reviewer is considered busy when:
        - its session still carries another task's id (task_id / current_task_id)
          AND that task is still in a working or review state with an active
          review in flight.
        - OR another task in the workspace lists this reviewer as its
          review_session_id and has an active (non-completed) review.
        """
        # Session-level check: the terminal session itself is bound to
        # another task and is not idle.
        other_id = reviewer.task_id or reviewer.current_task_id
        if other_id and other_id != current_task_id:
            other_task = self.tasks.get(other_id)
            if (
                other_task
                and other_task.status
                in {
                    WorkspaceTaskStatus.WORKING,
                    WorkspaceTaskStatus.REVIEW,
                }
                and state_policy.review_in_flight(
                    other_task.review_requested_at, other_task.review_completed_at
                )
            ):
                return True

        # Task-level check: any other task in the workspace claims this
        # reviewer as its review_session_id with a review in flight.
        for task in self.tasks.values():
            if task.id == current_task_id:
                continue
            if task.review_session_id != reviewer.id:
                continue
            if task.workspace_id != reviewer.workspace_id:
                continue
            if task.status not in {
                WorkspaceTaskStatus.WORKING,
                WorkspaceTaskStatus.REVIEW,
            }:
                continue
            if state_policy.review_in_flight(
                task.review_requested_at, task.review_completed_at
            ):
                return True

        return False

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
                and not self._reviewer_is_busy_with_other_task(reviewer, task.id)
            ):
                return reviewer
            if reviewer and reviewer.status != ManagedSessionStatus.STOPPED:
                logger.info(
                    "Reviewer %s is busy with another task; falling through to first "
                    "available reviewer for task_id=%s",
                    reviewer.id,
                    task.id,
                )
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

        # ------------------------------------------------------------------
        # Stale / duplicate reviewer verdict guard (implementation phase).
        #
        # If no review is in flight for this task (review_requested_at is
        # cleared and there is no recorded verdict at/after this report), the
        # report is a stale or duplicate reviewer message — typically a second
        # goal-packet review_passed that arrives after continue_task already
        # reopened the task for implementation. Recording it as an
        # implementation verdict here would write a phantom
        # review_completed_at / status=REVIEW before implementation is done,
        # which the monitor reopen heuristic and late-report suppression then
        # turn into a permanently-stuck WORKING task. Ignore it.
        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        # Stale / duplicate reviewer verdict guard (implementation phase),
        # cycle-based.
        #
        # A reviewer verdict applies only to the round it was stamped for.
        # Once a reopen (continue_task after a prior failed verdict, or a goal
        # packet approval that dispatched the implementation phase) has bumped
        # ``task.review_cycle`` past the report's ``review_cycle``, this report
        # is a stale echo from a closed round. Recording it as a verdict here
        # would re-run continue_task / re-dispatch against an already-moved-on
        # task and strand it. Ignore it.
        #
        # NOTE: we compare against ``task.review_cycle`` (not
        # ``reviewed_cycle``) on purpose. The create_report fast-path already
        # applied this round's verdict and advanced ``reviewed_cycle`` to the
        # report's cycle *before* this handler runs, so a same-round verdict
        # has ``report.review_cycle == task.reviewed_cycle`` — using the
        # opens-round predicate here would wrongly drop the legitimate
        # REVIEW_FAILED re-dispatch below. Same-round duplicates are handled
        # idempotently by ``already_applied``.
        # ------------------------------------------------------------------
        if report.review_cycle < task.review_cycle:
            logger.info(
                "Ignoring stale reviewer verdict from closed round in _handle_review_report "
                "workspace_id=%s task_id=%s reviewer=%s decision=%s "
                "report_cycle=%s task_review_cycle=%s reviewed_cycle=%s",
                task.workspace_id,
                task.id,
                reviewer.id,
                report.state.value,
                report.review_cycle,
                task.review_cycle,
                task.reviewed_cycle,
            )
            return

        # ------------------------------------------------------------------
        # Same-cycle duplicate echo guard (no review in flight).
        #
        # A reviewer report is stamped with the task's *current* review_cycle
        # at intake, not the round it judged. So a duplicate / stale reviewer
        # message that arrives after continue_task already reopened the round
        # (e.g. a second goal-packet review_passed echo, once the task was
        # handed back to the worker for implementation) carries the new,
        # higher review_cycle and would spuriously satisfy
        # ``report_opens_review_round`` below — applying a phantom verdict and
        # stranding the task. continue_task clears review_requested_at, so no
        # review is in flight for this report; require one. (When the
        # create_report fast-path legitimately applied this round's verdict it
        # advanced reviewed_cycle to the report's cycle, so
        # ``report_opens_review_round`` is already False here and this guard is
        # skipped, leaving the idempotent / REVIEW_FAILED re-dispatch path
        # below intact.)
        # ------------------------------------------------------------------
        if state_policy.report_opens_review_round(
            report.review_cycle, task.reviewed_cycle
        ) and not state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            logger.info(
                "Ignoring duplicate reviewer verdict with no review in flight "
                "in _handle_review_report workspace_id=%s task_id=%s reviewer=%s "
                "decision=%s report_cycle=%s review_cycle=%s reviewed_cycle=%s",
                task.workspace_id,
                task.id,
                reviewer.id,
                report.state.value,
                report.review_cycle,
                task.review_cycle,
                task.reviewed_cycle,
            )
            return

        now = _wm._now()
        # ------------------------------------------------------------------
        # Idempotency guard (cycle-based): terminal review flags are written
        # synchronously in create_report() before the first save, which also
        # advances ``reviewed_cycle`` to this report's cycle. If this round's
        # verdict is already applied (``reviewed_cycle >= report.review_cycle``)
        # the persistent state is correct; re-run continue_task() for
        # REVIEW_FAILED but do not re-save the task or re-release the reviewer
        # (a no-op at best, and risks clobbering newer state).
        # ------------------------------------------------------------------
        already_applied = not state_policy.report_opens_review_round(
            report.review_cycle, task.reviewed_cycle
        )
        autonomous_next_phase: AutonomousRunPhase | None = None
        if not already_applied:
            task_update = state_policy.compute_reviewer_verdict_task_update(
                report_state=report.state,
                reviewer_session_id=reviewer.id,
                now=now,
                report_review_cycle=report.review_cycle,
                existing_review_completed_at=task.review_completed_at,
                existing_reviewed_at=task.reviewed_at,
                existing_human_acceptance_requested_at=task.human_acceptance_requested_at,
                preserve_existing_review_completed_at=False,
                preserve_existing_reviewed_at=True,
                preserve_existing_human_acceptance_requested_at=False,
            )
            if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                autonomous_run, autonomous_next_phase = self._autonomous_run_after_evaluation(
                    task, reviewer, report, now=now
                )
                if autonomous_run is not None:
                    task_update["autonomous_run"] = autonomous_run
                    task_update["human_acceptance_requested_at"] = (
                        now if autonomous_next_phase == AutonomousRunPhase.PASSED else None
                    )
            self.tasks[task.id] = task.model_copy(update=task_update)
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
            not state_policy.report_opens_review_round(report.review_cycle, task.reviewed_cycle)
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
            task_update = state_policy.compute_reviewer_verdict_task_update(
                report_state=report.state,
                reviewer_session_id=reviewer.id,
                now=now,
                report_review_cycle=report.review_cycle,
                existing_review_completed_at=task.review_completed_at,
                existing_reviewed_at=task.reviewed_at,
                existing_human_acceptance_requested_at=task.human_acceptance_requested_at,
                preserve_existing_review_completed_at=False,
                preserve_existing_reviewed_at=True,
                preserve_existing_human_acceptance_requested_at=False,
                human_acceptance_for_passed=False,
            )
            self.tasks[task.id] = task.model_copy(
                update={**task_update, "goal_packet": goal_packet}
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
