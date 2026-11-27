"""Workspace queue draining and stranded ownership recovery."""

from claude_hub.services.task_dependencies import task_dependency_blockers
from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403

# Compatibility export for the historical aggregate mixin.
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

        if refresh_sessions:
            await self._refresh_session_statuses(workspace_id)
        # Rehome stranded implicitly-associated tasks BEFORE acquiring the
        # dispatch lock so session provisioning (which acquires
        # workspace_mutation_lock) never inverts lock order against callers
        # that hold workspace_mutation_lock -> _dispatch_locks.
        await self._rehome_stranded_queued_tasks(workspace_id)

        lock = self._dispatch_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            await self._dispatch_workspace_locked(
                workspace_id,
                refresh_sessions=False,
            )

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
            current_id = session.task_id or session.current_task_id
            current = self.tasks.get(current_id) if current_id else None
            if current and self._task_execution_finished(current):
                self._release_task_session(current)
                self._save_workspace_state(workspace_id)
                session = self.sessions[session.id]
            next_task = self._next_queued_task(session.id)
            if not next_task:
                continue
            await self._dispatch_task_to_session(next_task, session)

    def _can_dispatch_to(self, session: ManagedSession) -> bool:
        if session.id in self._retiring_session_ids:
            return False
        if (
            session.archived
            or session.role not in {WorkspaceSessionRole.ORCHESTRATOR, WorkspaceSessionRole.WORKER}
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
            # Human acceptance owns the result, not an idle worker seat. Keep
            # the seat only while implementation/review can still be running.
            if current and current.status != WorkspaceTaskStatus.DONE:
                if not self._task_execution_finished(current):
                    return False
        return True

    def _task_execution_finished(self, task: WorkspaceTask) -> bool:
        return (
            task.status == WorkspaceTaskStatus.REVIEW
            and (
                task.human_acceptance_requested_at is not None or task.review_skipped_at is not None
            )
            and not state_policy.review_in_flight(
                task.review_requested_at, task.review_completed_at
            )
        )

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
        return not self._task_execution_finished(current)

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

    def _session_blocks_implicit_queue_progress(
        self,
        session: Optional[ManagedSession],
        task_id: str,
    ) -> bool:
        if session is None:
            return True
        if session.task_id == task_id or session.current_task_id == task_id:
            return False
        if (
            session.id in self._retiring_session_ids
            or session.archived
            or session.chat_paused
            or session.status == ManagedSessionStatus.STOPPED
            or session.runtime_status in {AgentRuntimeStatus.OFFLINE, AgentRuntimeStatus.ATTENTION}
        ):
            return True
        return False

    async def _rehome_stranded_queued_tasks(self, workspace_id: str) -> None:
        """Provision a fresh task-owned ephemeral session when an implicitly
        associated session (e.g. ``related_task_id``) goes offline, stops, or
        stalls in ``ATTENTION`` before claiming a queued follow-up task.

        Explicitly user-pinned tasks (``User selected target agent``) keep
        waiting on their chosen session.
        """
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return
        candidates = sorted(
            [
                task
                for task in self.tasks.values()
                if task.workspace_id == workspace_id
                and self._workspace_owns_task(task)
                and task.status == WorkspaceTaskStatus.QUEUED
                and not task.dispatch_pending
                and (task.dispatch_reason or "") not in _NON_MIGRATABLE_DISPATCH_REASONS
                and not task_dependency_blockers(self.tasks, task)
            ],
            key=_sort_time,
        )
        for task in candidates:
            if task.id in self._rehoming_task_ids:
                continue
            self._rehoming_task_ids.add(task.id)
            try:
                live = self.tasks.get(task.id)
                if (
                    live is None
                    or live.status != WorkspaceTaskStatus.QUEUED
                    or live.dispatch_pending
                    or (live.dispatch_reason or "") in _NON_MIGRATABLE_DISPATCH_REASONS
                    or task_dependency_blockers(self.tasks, live)
                ):
                    continue
                current_session = self.sessions.get(live.session_id or "")
                if not self._session_blocks_implicit_queue_progress(current_session, live.id):
                    continue
                if (
                    workspace.target == ExecutionTarget.LOCAL
                    and live.auto_worktree
                    and not live.worktree_path
                ):
                    continue
                cwd = (
                    live.worktree_path
                    if (workspace.target == ExecutionTarget.LOCAL and live.auto_worktree)
                    else workspace.path
                )
                inherited_env = dict(current_session.env) if current_session else {}
                inherited_preset = current_session.env_preset if current_session else None
                replacement = await self.ensure_workspace_agent(
                    workspace.id,
                    EnsureWorkspaceAgentRequest(
                        agent_type=live.agent_type,
                        session_kind=live.session_kind,
                        role=WorkspaceSessionRole.ORCHESTRATOR,
                        target=workspace.target,
                        reuse_existing=False,
                        ephemeral=True,
                        caller_owned_ephemeral=True,
                        cwd=cwd,
                        env=inherited_env,
                        env_preset=inherited_preset,
                    ),
                )
                adopted = False
                try:
                    async with self.workspace_mutation_lock(workspace_id):
                        latest = self.tasks.get(task.id)
                        latest_session = (
                            self.sessions.get(latest.session_id or "") if latest else None
                        )
                        if (
                            latest is not None
                            and latest.status == WorkspaceTaskStatus.QUEUED
                            and not latest.dispatch_pending
                            and (latest.dispatch_reason or "")
                            not in _NON_MIGRATABLE_DISPATCH_REASONS
                            and self._session_blocks_implicit_queue_progress(
                                latest_session, latest.id
                            )
                            and replacement.id in self.sessions
                        ):
                            self.tasks[latest.id] = latest.model_copy(
                                update={
                                    "session_id": replacement.id,
                                    "dispatch_reason": "Dedicated task session",
                                    "updated_at": self._runtime_now(),
                                }
                            )
                            self._save_state()
                            adopted = True
                finally:
                    if not adopted:
                        await asyncio.shield(self._best_effort_delete_session(replacement.id))
            finally:
                self._rehoming_task_ids.discard(task.id)

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
            matched_lesson_ids = self._record_inlined_lesson_hits(task, lesson_context, now=now)
            merged_lesson_ids = list(
                dict.fromkeys([*task.feedback_lesson_ids, *matched_lesson_ids])
            )
            self.tasks[task.id] = apply_task_update(
                task,
                {
                    "status": WorkspaceTaskStatus.WORKING,
                    "session_id": task.session_id or session.id,
                    "feedback_lesson_ids": merged_lesson_ids,
                    "started_at": task.started_at or now,
                    "updated_at": now,
                },
                cause="dispatch",
            )
            self._save_state()
