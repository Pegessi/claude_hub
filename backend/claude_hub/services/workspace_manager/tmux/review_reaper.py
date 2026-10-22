"""Orphan tab pruning and stuck-review recovery."""

from claude_hub.services.workspace_manager._constants import (
    ORPHAN_TAB_PRUNE_GRACE_SECONDS,
    REVIEW_REAPER_DISPATCH_GRACE_SECONDS,
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    ExecutionTarget,
    GoalPacketStatus,
    ManagedSession,
    ManagedSessionStatus,
    Optional,
    ReviewDecision,
    SessionKind,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
    datetime,
    logger,
    state_policy,
    ttyd_manager,
    uuid,
)

from .review_delivery import _ReviewDeliveryMixin


class _ReviewReaperMixin(_ReviewDeliveryMixin):
    async def _prune_orphan_workspace_tabs(self, workspace_id: str) -> int:
        """Delete managed terminal tabs for this workspace that have no backing
        ManagedSession.

        Terminal tabs (ttyd_manager) and managed sessions (workspace_manager)
        persist to separate state files. When a session is removed without its
        tab — e.g. historical temporary-reviewer lifecycle desync — the tab is
        left behind with ``workspace_id``/``workspace_role`` set but no session
        row. Such orphan tabs show up in the terminal tab bar yet are absent
        from the "Manage Agents" board (which lists sessions), so they cannot
        respond to dispatch and cannot be deleted from that UI.

        This reconciler removes those orphans. It is deliberately conservative:

        * Only tabs that carry this ``workspace_id`` are considered, so manual
          terminal tabs (no ``workspace_id``) are never touched.
        * A tab whose id backs a live session is kept.
        * A tab created within ``ORPHAN_TAB_PRUNE_GRACE_SECONDS`` is kept, to
          avoid racing the gap between ``create_tab`` and session registration
          in ``_create_managed_session``.

        Returns the number of orphan tabs pruned.
        """
        live_tab_ids = {
            session.tab_id
            for session in self.sessions.values()
            if session.workspace_id == workspace_id
        }
        now = self._runtime_now()
        orphan_tab_ids: list[str] = []
        for tab in ttyd_manager.list_tabs():
            if tab.workspace_id != workspace_id:
                continue
            if tab.id in live_tab_ids:
                continue
            created_at = tab.created_at
            if created_at is not None:
                # Tab timestamps and _now() are both naive local datetimes.
                age_seconds = (now - created_at).total_seconds()
                if age_seconds < ORPHAN_TAB_PRUNE_GRACE_SECONDS:
                    continue
            orphan_tab_ids.append(tab.id)

        pruned = 0
        for tab_id in orphan_tab_ids:
            logger.warning(
                "Pruning orphan managed terminal tab with no backing session "
                "workspace_id=%s tab_id=%s",
                workspace_id,
                tab_id,
            )
            try:
                await ttyd_manager.delete_tab(tab_id)
                pruned += 1
            except Exception:
                logger.exception(
                    "Failed to prune orphan managed tab workspace_id=%s tab_id=%s",
                    workspace_id,
                    tab_id,
                )
        return pruned

    def _review_dispatch_in_reaper_grace(self, task: WorkspaceTask, *, now: datetime) -> bool:
        """Return True when a review dispatch is recent enough that the
        reviewer may simply be slow to emit first tokens. The fallback
        reaper should not redispatch in this window even when
        ``_reviewer_is_active`` reports False, because the terminal
        classifier briefly reports IDLE between bursts of model output.

        We grant the grace based on whichever of the timestamps is
        more recent: when the review was last requested, when the goal
        packet was submitted (for pending-GP cases where review_requested_at
        was never set), or when the assigned reviewer last had terminal
        activity recorded.
        """
        candidates: list[datetime] = []
        if task.review_requested_at:
            candidates.append(task.review_requested_at)
        if (
            task.goal_packet is not None
            and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
            and task.goal_packet.updated_at
        ):
            candidates.append(task.goal_packet.updated_at)
        if task.review_session_id:
            reviewer = self.sessions.get(task.review_session_id)
            if reviewer and reviewer.last_activity_at:
                candidates.append(reviewer.last_activity_at)
        if not candidates:
            return False
        latest = max(candidates)
        return (now - latest).total_seconds() < REVIEW_REAPER_DISPATCH_GRACE_SECONDS

    def _is_fallback_reaper_report(self, report: AgentReport) -> bool:
        if report.review_decision != ReviewDecision.REQUEST:
            return False
        blob = " ".join(
            part
            for part in (report.message, report.message_en, report.review_reason)
            if isinstance(part, str)
        ).lower()
        return "fallback reaper" in blob or "background dispatcher" in blob

    def _review_cycle_has_reviewer_activity(self, task_id: str, review_cycle: int) -> bool:
        """True when review_started or a terminal reviewer verdict exists for the cycle."""

        reviewer_states = {
            AgentReportState.REVIEW_STARTED,
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }
        for report in self.reports.values():
            if report.task_id != task_id:
                continue
            if int(report.review_cycle or 0) != review_cycle:
                continue
            if report.state in reviewer_states:
                return True
        return False

    async def _reap_stuck_reviews(self, workspace_id: str) -> int:
        """Fallback reaper: find tasks whose review dispatch appears stuck
        (review-requested or REVIEW status with no active reviewer) and
        trigger a fresh review dispatch. Called from the periodic
        dispatch_workspace loop so transient failures do not permanently
        strand tasks in "Awaiting AI review".

        Returns the number of tasks that were re-dispatched."""
        reaped = 0
        for task in list(self.tasks.values()):
            if not self._workspace_owns_task(task):
                continue
            if task.workspace_id != workspace_id:
                continue
            if task.status == WorkspaceTaskStatus.DONE:
                continue
            if task.system_internal:
                continue
            # Sealed-round guard: once this work round already has a reviewer
            # verdict (``reviewed_cycle >= review_cycle``) the round is closed.
            # It only moves forward via a reopen (continue_task / review_failed,
            # which BUMP review_cycle) or human acceptance — never via re-review.
            # Re-dispatching here is the infinite-loop bug: _request_task_review
            # clears review_completed_at / human_acceptance_requested_at without
            # bumping review_cycle, so the reviewer's re-emitted verdict is
            # stamped at the already-judged cycle and dropped as a closed-round
            # echo by _reviewer_verdict_actionable. review_completed_at is never
            # rewritten, review_in_flight stays true, and the reaper re-fires
            # every loop forever (observed: review_passed applied 14×, zero diff).
            # A genuinely-stuck *unjudged* round has reviewed_cycle < review_cycle
            # and is unaffected, so legitimate crashed-reviewer recovery still
            # runs.
            if state_policy.current_round_has_verdict(task.review_cycle, task.reviewed_cycle):
                logger.debug(
                    "Skipping fallback reaper for task_id=%s: current round already "
                    "has a verdict (review_cycle=%s reviewed_cycle=%s)",
                    task.id,
                    task.review_cycle,
                    task.reviewed_cycle,
                )
                continue
            async with self.workspace_mutation_lock(task.workspace_id):
                live_task = self.tasks.get(task.id)
                if live_task is not None and self._workspace_owns_task(live_task):
                    await self._recover_idle_review_delivery(live_task)
            # Delivery recovery never creates a new review round.
            task = self.tasks.get(task.id, task)
            needs_review_dispatch = False
            if state_policy.review_in_flight(
                task.review_requested_at, task.review_completed_at
            ) and await self._review_dispatch_failed(task):
                # Review was requested but the dispatch positively failed: the
                # reviewer is missing/stopped/cross-bound, or the review prompt
                # is still verifiably sitting unsent in the tmux input box. A
                # reviewer that is bound to this task and merely IDLE (reading or
                # thinking over a large prompt) is NOT reaped here.
                needs_review_dispatch = True
            elif (
                task.status == WorkspaceTaskStatus.REVIEW
                and not task.review_completed_at
                and not task.human_acceptance_requested_at
                and await self._review_dispatch_failed(task)
            ):
                # Task is in REVIEW state with no reviewer progress — a
                # reconciler or manual status transition set REVIEW without
                # actually dispatching a reviewer.
                needs_review_dispatch = True
            elif (
                task.task_mode == WorkspaceTaskMode.REVIEWED
                and task.status == WorkspaceTaskStatus.WORKING
                and task.goal_packet is not None
                and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
                and not state_policy.review_in_flight(
                    task.review_requested_at, task.review_completed_at
                )
            ):
                # Goal packet was marked PENDING_REVIEW (the worker submitted
                # its GP working report) but _request_task_review never fired
                # or threw before persisting review_requested_at — e.g.
                # reviewer creation failed between the goal-packet status
                # write and the review dispatch save. The task appears in
                # WORKING state with a pending-review GP but no reviewer
                # bound; recover by re-dispatching a GP review. The grace
                # check below uses goal_packet.updated_at so a freshly-submitted
                # GP gets the same 60s dispatch window as a normal review.
                needs_review_dispatch = True
            if not needs_review_dispatch:
                continue
            if not task.session_id:
                continue
            now = self._runtime_now()
            if self._review_dispatch_in_reaper_grace(task, now=now):
                # Reviewer was just dispatched (or has very recent terminal
                # activity). The terminal classifier can briefly report IDLE
                # while the model produces its first tokens — wait the
                # configured grace before declaring the dispatch stuck.
                logger.debug(
                    "Skipping fallback reaper for task_id=%s within dispatch grace",
                    task.id,
                )
                continue
            latest_state = self._latest_report_state(task.id)
            # Detect the pending-goal-packet case: the task carries a GP in
            # PENDING_REVIEW but review was never dispatched (review_requested_at
            # is None). The trigger report must be WORKING state so that
            # _build_review_prompt produces the Goal Packet approval review
            # instructions rather than implementation review instructions.
            is_pending_gp_recovery = (
                task.task_mode == WorkspaceTaskMode.REVIEWED
                and task.goal_packet is not None
                and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
                and not state_policy.review_in_flight(
                    task.review_requested_at, task.review_completed_at
                )
            )
            if is_pending_gp_recovery:
                trigger_state = AgentReportState.WORKING
                trigger_message = (
                    "Re-dispatching pending Goal Packet review (fallback reaper); "
                    "initial GP review dispatch did not complete."
                )
                trigger_message_en = (
                    "Re-dispatching pending Goal Packet review (fallback reaper); "
                    "initial GP review dispatch did not complete."
                )
                trigger_message_zh = (
                    "重新分派等待中的 Goal Packet 评审（fallback reaper）；"
                    "初始 GP 评审分派未完成。"
                )
                trigger_reason = "Pending Goal Packet review recovered by background dispatcher."
            else:
                trigger_state = (
                    latest_state
                    if latest_state
                    in {
                        AgentReportState.READY_FOR_REVIEW,
                        AgentReportState.COMPLETED,
                        AgentReportState.BLOCKED,
                        AgentReportState.NEEDS_INPUT,
                    }
                    else AgentReportState.READY_FOR_REVIEW
                )
                trigger_message = (
                    "Re-dispatching stuck review task (fallback reaper); "
                    "prior reviewer dispatch did not complete."
                )
                trigger_message_en = (
                    "Re-dispatching stuck review task (fallback reaper); "
                    "prior reviewer dispatch did not complete."
                )
                trigger_message_zh = (
                    "重新分派卡住的 review 任务（fallback reaper）；" "之前的 reviewer 分派未完成。"
                )
                trigger_reason = "Stuck review recovered by background dispatcher."
            self._release_stale_reviewer_for_task(task, updated_at=now)
            trigger_report = AgentReport(
                id=str(uuid.uuid4()),
                workspace_id=task.workspace_id,
                task_id=task.id,
                session_id=task.session_id,
                state=trigger_state,
                message=trigger_message,
                message_en=trigger_message_en,
                message_zh=trigger_message_zh,
                changed_files=[],
                validation=None,
                risks=None,
                review_decision=ReviewDecision.REQUEST,
                review_reason=trigger_reason,
                risk_level=None,
                review_cycle=task.review_cycle,
                created_at=now,
            )
            self.reports[trigger_report.id] = trigger_report
            logger.info(
                "Reaping stuck review task_id=%s trigger_state=%s",
                task.id,
                trigger_state.value,
            )
            try:
                await self._request_task_review(self.tasks[task.id], trigger_report)
                reaped += 1
            except Exception:
                logger.exception("Failed to reap stuck review task_id=%s", task.id)
        return reaped

    def _first_available_reviewer(
        self,
        workspace_id: str,
        *,
        target: ExecutionTarget,
        cwd: str,
        remote_profile_id: str | None = None,
        remote_cwd: str | None = None,
        session_kind: SessionKind | None = None,
    ) -> Optional[ManagedSession]:
        """Internal review dispatch only — not CLI agent-create reuse.

        Hub review routing requires the worker's execution directory as well
        as target; sharing a repo does not mean sharing a worktree. CLI reuse uses
        ``_find_compatible_workspace_agent`` instead.
        """

        from claude_hub.services.workspace_identity import normalize_remote_cwd, resolve_path

        wanted_remote_cwd = (
            normalize_remote_cwd(remote_cwd) if target == ExecutionTarget.REMOTE else None
        )
        reviewers = [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.REVIEWER
            and (session_kind is None or session.session_kind == session_kind)
            and not session.archived
            and not session.chat_paused
            and session.status != ManagedSessionStatus.STOPPED
            and session.runtime_status == AgentRuntimeStatus.IDLE
            and not session.task_id
            and not session.current_task_id
            and not self._reviewer_has_active_task_binding(session)
            and session.target == target
            and (
                target != ExecutionTarget.LOCAL
                or resolve_path(session.workspace_path or self.workspaces[workspace_id].path)
                == resolve_path(cwd)
            )
            and (
                target != ExecutionTarget.REMOTE
                or (
                    (session.remote_profile_id or remote_profile_id) == remote_profile_id
                    and normalize_remote_cwd(session.remote_cwd) == wanted_remote_cwd
                )
            )
        ]
        if not reviewers:
            return None
        return sorted(reviewers, key=lambda session: (session.ephemeral, session.created_at))[0]
