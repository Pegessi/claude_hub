"""Workspace queue draining and stranded ownership recovery."""

from claude_hub.services.task_dependencies import task_dependency_blockers
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403

# Only an explicit operator target is a hard affinity; all automatic
# preferences may yield to another compatible free agent.
_NON_MIGRATABLE_DISPATCH_REASONS = frozenset({"User selected target agent"})


class _DispatchQueueMixin(_WorkspaceManagerBase):
    async def dispatch_workspace(
        self,
        workspace_id: str,
        *,
        refresh_sessions: bool = True,
    ) -> None:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)

        lock = self._dispatch_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            await self._dispatch_workspace_locked(
                workspace_id,
                refresh_sessions=refresh_sessions,
            )
        self._schedule_stranded_provisioning(workspace_id)

    def _schedule_stranded_provisioning(self, workspace_id: str) -> None:
        """Start at most one background job per workspace that gives stranded
        queued Tasks their own agents.

        Agent startup takes tens of seconds, so callers of ``dispatch_workspace``
        (the monitor loop, report intake, aborts) never wait for it, and the
        single job per workspace cannot provision twice for one Task.
        """

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        current = self._stranded_provisioning_jobs.get(workspace_id)
        if current is not None and not current.done() and current.get_loop() is loop:
            return
        if not self._stranded_queued_tasks(workspace_id):
            return
        self._stranded_provisioning_jobs[workspace_id] = loop.create_task(
            self._run_stranded_provisioning(workspace_id),
            name=f"workspace-stranded-provisioning-{workspace_id}",
        )

    async def _run_stranded_provisioning(self, workspace_id: str) -> None:
        try:
            if await self._provision_agents_for_stranded_tasks(workspace_id):
                await self.dispatch_workspace(workspace_id, refresh_sessions=False)
        except Exception:
            logger.exception(
                "Stranded queued task provisioning failed workspace_id=%s", workspace_id
            )

    def _stranded_queued_tasks(self, workspace_id: str) -> list[WorkspaceTask]:
        """Queued Tasks whose assigned agent is held by another open Task or gone."""

        stranded: list[WorkspaceTask] = []
        for task in self.tasks.values():
            if task.workspace_id != workspace_id or not self._workspace_owns_task(task):
                continue
            if task.status != WorkspaceTaskStatus.QUEUED or task.dispatch_pending:
                continue
            if not task.session_id or task.dispatch_reason in _NON_MIGRATABLE_DISPATCH_REASONS:
                continue
            if task_dependency_blockers(self.tasks, task):
                continue
            assigned = self.sessions.get(task.session_id)
            if assigned is None or self._agent_cannot_resume_work(assigned):
                stranded.append(task)
                continue
            holder_id = assigned.task_id or assigned.current_task_id
            holder = self.tasks.get(holder_id) if holder_id else None
            if holder and holder.id != task.id and holder.status != WorkspaceTaskStatus.DONE:
                stranded.append(task)
        return sorted(stranded, key=_sort_time)

    @staticmethod
    def _agent_cannot_resume_work(session: ManagedSession) -> bool:
        """Archived, stopped, paused, or a terminal whose tmux disappeared."""

        if session.archived or session.chat_paused:
            return True
        if session.status == ManagedSessionStatus.STOPPED:
            return True
        return (
            session.session_kind == SessionKind.TERMINAL
            and session.runtime_status == AgentRuntimeStatus.OFFLINE
        )

    async def _provision_agents_for_stranded_tasks(self, workspace_id: str) -> bool:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return False
        provisioned = False
        now = self._runtime_now()
        stranded = self._stranded_queued_tasks(workspace_id)
        stranded_ids = {task.id for task in stranded}
        for task_id in list(self._dedicated_agent_attempts):
            owner = self.tasks.get(task_id)
            if owner is None or (
                owner.workspace_id == workspace_id and task_id not in stranded_ids
            ):
                self._dedicated_agent_attempts.pop(task_id, None)
        for task in stranded:
            last_attempt = self._dedicated_agent_attempts.get(task.id)
            if last_attempt and (now - last_attempt).total_seconds() < (
                DEDICATED_AGENT_RETRY_SECONDS
            ):
                continue
            self._dedicated_agent_attempts[task.id] = now
            try:
                agent = await self._provision_dedicated_agent(workspace, task)
            except Exception:
                logger.exception(
                    "Could not provision dedicated agent for stranded queued task "
                    "task_id=%s workspace_id=%s",
                    task.id,
                    workspace_id,
                )
                continue
            current = self.tasks.get(task.id)
            if current is None or current.status != WorkspaceTaskStatus.QUEUED:
                self._dedicated_agent_attempts.pop(task.id, None)
                continue
            self.tasks[task.id] = current.model_copy(
                update={
                    "session_id": agent.id,
                    "clear_context": True,
                    "dispatch_reason": DEDICATED_AGENT_DISPATCH_REASON,
                    "updated_at": self._runtime_now(),
                }
            )
            self._dedicated_agent_attempts.pop(task.id, None)
            self._save_state()
            provisioned = True
        return provisioned

    async def _dispatch_workspace_locked(
        self,
        workspace_id: str,
        *,
        refresh_sessions: bool,
    ) -> None:
        if refresh_sessions:
            await self._refresh_session_statuses(workspace_id)
        # Review dispatch has no retry mechanism of its own:
        # _request_task_review either succeeds or strands the task. Run the
        # stale-review reaper and stale-reviewer assignment cleanup before
        # every dispatch pass so a transient failure (prompt send error,
        # reviewer crash, reconciler-only REVIEW status) does not permanently
        # strand the task in "Awaiting AI review" with idle reviewers.
        if self._cleanup_stale_reviewer_assignments(workspace_id):
            self._save_state()
        await self._reap_stuck_reviews(workspace_id)
        # Drop managed terminal tabs left behind by removed sessions so leaked
        # reviewer tabs (visible in the tab bar but absent from Manage Agents)
        # do not accumulate.
        await self._prune_orphan_workspace_tabs(workspace_id)
        # Recover sessions that claimed a QUEUED task but crashed before the
        # assignment prompt was fully persisted as WORKING. The session's
        # task_id was persisted before the send (see _dispatch_task_to_session),
        # so _can_dispatch_to blocks re-dispatch to another session. Here we
        # re-send the assignment prompt; the dispatch call_id ensures
        # sender-side dedup: if the call_id is already in processing_call_ids
        # (sent to tmux), it is not re-sent; if it is still in
        # pending_call_ids, the pump sends it to the tmux inbox exactly once.
        await self._recover_queued_task_ownership(workspace_id)
        for session in self._workspace_agents(workspace_id, include_stopped=True):
            if not self._can_dispatch_to(session):
                logger.info(
                    "Skipping workspace session dispatch workspace_id=%s session_id=%s "
                    "runtime_status=%s task_id=%s current_task_id=%s",
                    workspace_id,
                    session.id,
                    session.runtime_status,
                    session.task_id,
                    session.current_task_id,
                )
                continue
            rebalanced_task = self._next_reassignable_queued_task(session.id, workspace_id)
            if rebalanced_task:
                logger.info(
                    "Reassigning queued workspace task id=%s from session_id=%s to free "
                    "session_id=%s",
                    rebalanced_task.id,
                    rebalanced_task.session_id,
                    session.id,
                )
                self.tasks[rebalanced_task.id] = rebalanced_task.model_copy(
                    update={
                        "session_id": session.id,
                        "clear_context": True,
                        "dispatch_reason": "Reassigned to newly available workspace agent",
                        "updated_at": self._runtime_now(),
                    }
                )
            next_task = self._next_queued_task(session.id)
            if not next_task:
                continue
            await self._dispatch_task_to_session(next_task, session)

    def _can_dispatch_to(self, session: ManagedSession) -> bool:
        if (
            session.archived
            or session.role != WorkspaceSessionRole.ORCHESTRATOR
            or session.chat_paused
        ):
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        if session.runtime_status != AgentRuntimeStatus.IDLE:
            return False
        if session.task_id or session.current_task_id:
            current_id = session.task_id or session.current_task_id
            current = self.tasks.get(current_id) if current_id else None
            # Hold the implementation agent for as long as its task lives on
            # the board. Only a DONE task (human-accepted) frees the agent,
            # so a REVIEW-state task — even after review_passed — keeps its
            # context locked for follow-up revisions or human rejection.
            if current and current.status != WorkspaceTaskStatus.DONE:
                return False
        return True

    def _is_review_passed(self, task: WorkspaceTask) -> bool:
        if task.review_completed_at is None:
            return False
        return self._latest_review_report_state(task.id) == AgentReportState.REVIEW_PASSED

    def _is_holding_unresolved_review_task(self, session: ManagedSession) -> bool:
        current_id = session.task_id or session.current_task_id
        if not current_id:
            return False
        current = self.tasks.get(current_id)
        if not current or current.status != WorkspaceTaskStatus.REVIEW:
            return False
        return True

    def _next_queued_task(self, session_id: str) -> Optional[WorkspaceTask]:
        tasks = [
            task
            for task in self.tasks.values()
            if self._workspace_owns_task(task)
            and task.session_id == session_id
            and task.status == WorkspaceTaskStatus.QUEUED
            and not task.dispatch_pending
            and not task_dependency_blockers(self.tasks, task)
        ]
        if not tasks:
            return None
        return sorted(tasks, key=_sort_time)[0]

    def _next_reassignable_queued_task(
        self,
        free_session_id: str,
        workspace_id: str,
    ) -> Optional[WorkspaceTask]:
        free_session = self.sessions.get(free_session_id)
        if free_session is None:
            return None
        candidates: list[WorkspaceTask] = []
        for task in self.tasks.values():
            if task.session_kind != free_session.session_kind:
                continue
            if task.agent_type != free_session.agent_type:
                continue
            if not self._workspace_owns_task(task):
                continue
            if task.workspace_id != workspace_id:
                continue
            if task.status != WorkspaceTaskStatus.QUEUED or task.dispatch_pending:
                continue
            if task_dependency_blockers(self.tasks, task):
                continue
            if task.session_id == free_session_id:
                continue
            # Hard, explicit user pins wait for exactly the agent the operator
            # chose — never migrate them. Every other queued task (auto
            # queued-behind, related-task continuity, previous-assignment
            # continuity, or a prior reassignment) prefers its assigned agent
            # but may migrate when that agent is stuck. Preference is preserved
            # below: we only migrate when the assigned agent CANNOT dispatch, so
            # an available pinned agent still picks the task up itself via
            # _next_queued_task.
            if task.dispatch_reason in _NON_MIGRATABLE_DISPATCH_REASONS:
                continue
            assigned = self.sessions.get(task.session_id or "")
            if (
                assigned is not None
                and task.dispatch_reason == DEDICATED_AGENT_DISPATCH_REASON
                and (self._runtime_now() - assigned.created_at).total_seconds()
                < DEDICATED_AGENT_BOOTSTRAP_GRACE_SECONDS
            ):
                # Its own agent is still starting; do not strand that agent.
                continue
            # Migrate to the now-free agent whenever the currently-assigned
            # agent cannot take the task: genuinely busy WORKING, idle but
            # holding a non-DONE task parked in REVIEW (review_passed yet
            # awaiting human acceptance keeps the agent locked indefinitely),
            # or gone (STOPPED/OFFLINE). Without this, a continuity-pinned task
            # starves forever behind an agent that is idle on paper but will
            # never free up until a human resolves its review task, while other
            # agents sit idle. The migration block sets clear_context=True, so
            # the fresh agent starts clean rather than inheriting absent
            # related-task context.
            if not assigned or self._can_dispatch_to(assigned):
                continue
            candidates.append(task)
        if not candidates:
            return None
        return sorted(candidates, key=_sort_time)[0]

    async def _recover_queued_task_ownership(self, workspace_id: str) -> None:
        """Re-send the assignment prompt for sessions that claimed a QUEUED
        task but crashed before the dispatch completed.

        ``_dispatch_task_to_session`` persists ``session.task_id`` before
        sending the prompt. A crash between that persist and the
        ``task.status = WORKING`` persist leaves the session holding a
        QUEUED task. ``_can_dispatch_to`` returns False for such sessions
        (they own a non-DONE task), so the normal dispatch loop skips them.
        This method re-sends the assignment prompt. The dispatch call_id
        (``f"dispatch:{task.id}:{task.dispatch_attempt}"``) gives
        sender-side dedup: if the call_id is already in
        ``processing_call_ids`` (sent to tmux) or ``delivered_call_ids``
        (ACKed by the worker via a report), ``send_session_message`` skips
        the re-send. If it is still in ``pending_call_ids``, the pump sends
        it to the tmux inbox exactly once.
        """
        for session in self._workspace_agents(workspace_id, include_stopped=True):
            if session.status == ManagedSessionStatus.STOPPED:
                continue
            task_id = session.task_id or session.current_task_id
            if not task_id:
                continue
            task = self.tasks.get(task_id)
            if (
                not task
                or not self._workspace_owns_task(task)
                or task.status != WorkspaceTaskStatus.QUEUED
            ):
                continue
            if task_dependency_blockers(self.tasks, task):
                continue
            logger.info(
                "Recovering queued task ownership: session_id=%s holds QUEUED task_id=%s; "
                "re-sending assignment prompt",
                session.id,
                task_id,
            )
            workspace = self.workspaces.get(task.workspace_id)
            if not workspace:
                continue
            lesson_context = self._lesson_context_payload(
                workspace,
                f"{task.title}\n{task.prompt}",
            )
            dispatch_call_id = f"dispatch:{task.id}:{task.dispatch_attempt}"
            try:
                await self._send_dispatch_message(
                    session.id,
                    dispatch_call_id,
                    self._build_task_assignment_prompt(
                        workspace,
                        task,
                        session,
                        lesson_context=lesson_context,
                    ),
                )
            except DeliveryUncertain:
                # The previous dispatch attempt ended in an ambiguous tmux
                # failure (call_id in uncertain_call_ids). Per the fail-closed
                # contract, the system MUST NOT auto-retry an uncertain
                # dispatch — only the explicit operator retry endpoint may
                # move it back to pending. Leave the task QUEUED and the
                # call_id uncertain; the operator must retry explicitly.
                logger.warning(
                    "Recovering queued task ownership: dispatch call_id=%s for "
                    "session_id=%s task_id=%s is uncertain; system will NOT "
                    "auto-retry. Operator retry required.",
                    dispatch_call_id,
                    session.id,
                    task_id,
                )
                continue
            current = self.tasks.get(task.id)
            if (
                current is None
                or current.status != WorkspaceTaskStatus.QUEUED
                or current.session_id not in (None, session.id)
            ):
                continue
            task = current
            now = self._runtime_now()
            self.tasks[task.id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.WORKING,
                    "session_id": task.session_id or session.id,
                    "started_at": task.started_at or now,
                    "updated_at": now,
                }
            )
            self._save_state()
