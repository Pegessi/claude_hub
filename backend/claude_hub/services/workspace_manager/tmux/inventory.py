"""Workspace session inventory, ownership reconciliation, and reuse selection."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _SessionInventoryMixin(_WorkspaceManagerBase):
    # FAILED is terminal for agent reuse even though a human may continue it later.
    _TERMINAL_TASK_STATUSES_FOR_REUSE = frozenset(
        {WorkspaceTaskStatus.DONE, WorkspaceTaskStatus.FAILED}
    )

    def sessions_for_workspace(
        self, workspace_id: str, *, include_archived: bool = False
    ) -> list[ManagedSession]:
        sessions = self._sessions_for_workspace_raw(workspace_id)
        return [
            self._with_assignment_summary(session)
            for session in sessions
            if include_archived or not session.archived
        ]

    def _sessions_for_workspace_raw(self, workspace_id: str) -> list[ManagedSession]:
        return [
            session for session in self.sessions.values() if session.workspace_id == workspace_id
        ]

    def _workspace_agents(
        self,
        workspace_id: str,
        include_stopped: bool = False,
    ) -> list[ManagedSession]:
        return [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.ORCHESTRATOR
            and not session.archived
            and (include_stopped or session.status != ManagedSessionStatus.STOPPED)
        ]

    def _dispatcher_session(self, workspace: Workspace) -> Optional[ManagedSession]:
        """Resolve the stored dispatcher pointer only.

        Does not scan for arbitrary dispatcher sessions and must not be used
        as a CLI reuse shortcut — agent create reuse goes through
        ``_find_compatible_workspace_agent`` strict matching.
        """
        if not workspace.dispatcher_session_id:
            return None
        session = self.sessions.get(workspace.dispatcher_session_id)
        if session and not session.archived and session.status != ManagedSessionStatus.STOPPED:
            return session
        self.workspaces[workspace.id] = workspace.model_copy(
            update={"dispatcher_session_id": None, "updated_at": self._runtime_now()}
        )
        self._save_state()
        return None

    def _session_has_active_canonical_ownership(self, session: ManagedSession) -> bool:
        """True when a non-terminal task canonically owns this session via task.session_id."""
        for task in self.tasks.values():
            if task.workspace_id != session.workspace_id:
                continue
            if task.session_id != session.id:
                continue
            if task.status not in self._TERMINAL_TASK_STATUSES_FOR_REUSE:
                return True
        return False

    def _orchestrator_assignment_is_orphaned(
        self, session: ManagedSession, candidate_id: str
    ) -> bool:
        """Visible orphan while idle: task terminal or canonically owned by another live session."""
        if session.runtime_status != AgentRuntimeStatus.IDLE:
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        task = self.tasks.get(candidate_id)
        if task is None or task.workspace_id != session.workspace_id:
            return True
        if task.status in self._TERMINAL_TASK_STATUSES_FOR_REUSE:
            return True
        canonical_session_id = task.session_id
        if not canonical_session_id:
            # Active dispatch/binding window before task.session_id is set.
            return False
        if canonical_session_id == session.id:
            return False
        owner = self.sessions.get(canonical_session_id)
        if owner is None:
            return False
        if owner.workspace_id != task.workspace_id:
            return False
        if owner.status == ManagedSessionStatus.STOPPED:
            return False
        return True

    def _non_terminal_tasks_referencing_session(
        self,
        session_id: str,
        *,
        exclude_task_id: str | None = None,
    ) -> list[WorkspaceTask]:
        return [
            other
            for other in self.tasks.values()
            if other.id != exclude_task_id
            and (other.session_id == session_id or other.review_session_id == session_id)
            and other.status not in self._TERMINAL_TASK_STATUSES_FOR_REUSE
        ]

    def _session_raw_task_bindings_block_cleanup(
        self,
        session: ManagedSession,
        *,
        exclude_task_id: str | None = None,
    ) -> bool:
        """True when raw task_id/current_task_id bind a non-orphaned non-terminal task."""
        for candidate_id in {session.task_id, session.current_task_id}:
            if not candidate_id:
                continue
            if exclude_task_id and candidate_id == exclude_task_id:
                continue
            if self._orchestrator_assignment_is_orphaned(session, candidate_id):
                continue
            other = self.tasks.get(candidate_id)
            if other is not None and other.status not in self._TERMINAL_TASK_STATUSES_FOR_REUSE:
                return True
        return False

    def _orchestrator_assignment_should_clear(self, session: ManagedSession) -> bool:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return False
        if not session.task_id and not session.current_task_id:
            return False
        candidate_ids = {value for value in (session.task_id, session.current_task_id) if value}
        if not any(
            self._orchestrator_assignment_is_orphaned(session, candidate_id)
            for candidate_id in candidate_ids
        ):
            return False
        return not self._session_has_active_canonical_ownership(session)

    def _cleared_orchestrator_assignment(
        self, session: ManagedSession, now: datetime
    ) -> ManagedSession:
        return session.model_copy(
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
                "prompt_retry_task_id": None,
                "prompt_retry_attempted_at": None,
                "updated_at": now,
                "last_activity_at": now,
            }
        )

    def _reconcile_orchestrator_session_assignment(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None or not self._orchestrator_assignment_should_clear(session):
            return False
        now = self._runtime_now()
        logger.info(
            "Cleaning stale orchestrator assignment session_id=%s task_ids=%s",
            session.id,
            sorted({value for value in (session.task_id, session.current_task_id) if value}),
        )
        self.sessions[session_id] = self._cleared_orchestrator_assignment(session, now)
        return True

    def _cleanup_stale_orchestrator_assignments(self, workspace_id: str) -> bool:
        """Clear visibly orphaned orchestrator task_id/current_task_id under save.

        An assignment is orphaned when the session is idle and the referenced
        task is terminal or its canonical session_id points at another live
        session in the same workspace. Unassigned canonical bindings
        (session_id unset) and missing/stopped canonical owners are never cleared.
        """
        changed = False
        for session in self._sessions_for_workspace_raw(workspace_id):
            if session.archived or session.role != WorkspaceSessionRole.ORCHESTRATOR:
                continue
            if self._reconcile_orchestrator_session_assignment(session.id):
                changed = True
        if changed:
            self._save_state()
        return changed

    def _session_blocks_agent_reuse(self, session: ManagedSession) -> bool:
        if self._queued_count(session.id) > 0:
            return True
        if session.role == WorkspaceSessionRole.REVIEWER:
            return self._reviewer_has_active_task_binding(session)
        if session.role == WorkspaceSessionRole.ORCHESTRATOR:
            # Never block on raw task_id/current_task_id alone; stale fields are
            # reconciled before reuse. Block only on active canonical ownership.
            return self._session_has_active_canonical_ownership(session)
        return False

    def _find_compatible_workspace_agent(
        self,
        workspace: Workspace,
        payload: EnsureWorkspaceAgentRequest,
    ) -> Optional[ManagedSession]:
        from claude_hub.services.env_preset_resolver import effective_launch_envs_match
        from claude_hub.services.workspace_identity import (
            effective_local_agent_cwd,
            local_agent_cwd_allowed_for_workspace,
            normalize_remote_cwd,
            validate_local_agent_cwd_for_workspace,
        )

        self._cleanup_stale_orchestrator_assignments(workspace.id)

        requested_target = self._effective_agent_target(workspace, payload)
        requested_cwd = payload.cwd or workspace.path
        if requested_target == ExecutionTarget.LOCAL:
            requested_local_cwd = validate_local_agent_cwd_for_workspace(
                workspace.path, requested_cwd
            )
        else:
            requested_local_cwd = effective_local_agent_cwd(requested_cwd)
        requested_remote_cwd = self._resolve_remote_cwd(
            profile_id=payload.remote_profile_id or workspace.remote_profile_id,
            requested_cwd=payload.remote_cwd,
            workspace_cwd=workspace.remote_cwd,
        )

        compatible: list[ManagedSession] = []
        reconcile_changed = False
        for session in self._sessions_for_workspace_raw(workspace.id):
            if session.archived or session.role != payload.role:
                continue
            if session.agent_type != payload.agent_type:
                continue
            if session.session_kind != payload.session_kind or session.chat_paused:
                continue
            if session.solo_mode != payload.solo_mode:
                continue
            if session.status == ManagedSessionStatus.STOPPED:
                continue
            if session.runtime_status != AgentRuntimeStatus.IDLE:
                continue
            if session.role == WorkspaceSessionRole.ORCHESTRATOR:
                if self._reconcile_orchestrator_session_assignment(session.id):
                    reconcile_changed = True
                session = self.sessions[session.id]
            if self._session_blocks_agent_reuse(session):
                continue
            if session.ephemeral or session.caller_owned_ephemeral:
                continue
            if session.target != requested_target:
                continue
            if requested_target == ExecutionTarget.REMOTE:
                session_profile = session.remote_profile_id or workspace.remote_profile_id
                requested_profile = payload.remote_profile_id or workspace.remote_profile_id
                if session_profile != requested_profile:
                    continue
                session_remote_cwd = normalize_remote_cwd(
                    session.remote_cwd or workspace.remote_cwd
                )
                if session_remote_cwd != normalize_remote_cwd(requested_remote_cwd):
                    continue
            elif not local_agent_cwd_allowed_for_workspace(workspace.path, session.workspace_path):
                continue
            elif effective_local_agent_cwd(session.workspace_path) != requested_local_cwd:
                continue
            if not effective_launch_envs_match(session.env, payload.env):
                continue
            compatible.append(session)

        if reconcile_changed:
            self._save_state()

        if not compatible:
            return None
        return sorted(compatible, key=lambda session: session.created_at)[0]

    def _first_available_workspace_agent(self, workspace_id: str) -> Optional[ManagedSession]:
        agents = self._workspace_agents(workspace_id)
        return agents[0] if agents else None

    def _reviewer_is_active(self, task: WorkspaceTask) -> bool:
        """Return True when the task has a reviewer session that is actively
        working on its review. A missing, idle, or stopped reviewer means the
        prior dispatch got stuck and re-dispatch is allowed."""
        if not task.review_session_id:
            return False
        reviewer = self.sessions.get(task.review_session_id)
        if not reviewer or reviewer.archived:
            return False
        if reviewer.status == ManagedSessionStatus.STOPPED:
            return False
        if reviewer.runtime_status == AgentRuntimeStatus.IDLE:
            return False
        if reviewer.task_id != task.id and reviewer.current_task_id != task.id:
            return False
        return True

    def _reviewer_dispatch_stuck(self, task: WorkspaceTask) -> bool:
        """Reaper-only test: return True when the task's review dispatch has
        positively failed and a fresh dispatch is warranted.

        This is deliberately stricter than ``not _reviewer_is_active``. The
        fallback reaper must NOT re-dispatch a reviewer that is simply slow:
        the terminal classifier reports ``runtime_status == IDLE`` between
        bursts of model output, and a reviewer reading/thinking over a large
        review prompt emits no terminal frame change for minutes. Treating that
        transient IDLE as "stuck" (the old behaviour) made the reaper re-send
        the review prompt to a reviewer that was actively working, producing a
        duplicate "fallback reaper" review report and a confusing status.

        A dispatch is considered stuck only when the reviewer is genuinely
        unavailable:

        - the task has no ``review_session_id``;
        - the referenced reviewer session no longer exists;
        - the reviewer session is STOPPED;
        - the reviewer is bound to a *different* task.

        A reviewer that is bound to THIS task and not stopped is presumed
        mid-review and is never reaped here regardless of IDLE duration. The
        genuine "bound but the prompt never landed" failure is handled
        separately by the input-box backstop in ``_reap_stuck_reviews``.
        """
        if not task.review_session_id:
            return True
        reviewer = self.sessions.get(task.review_session_id)
        if not reviewer or reviewer.archived:
            return True
        if reviewer.status == ManagedSessionStatus.STOPPED:
            return True
        if reviewer.task_id != task.id and reviewer.current_task_id != task.id:
            return True
        return False

    async def _reviewer_prompt_still_pending(self, task: WorkspaceTask) -> bool:
        """Backstop for the genuine "bound but the prompt never landed" failure.

        ``_reviewer_dispatch_stuck`` deliberately refuses to reap a reviewer that
        is bound to THIS task and not stopped, because a slow/thinking reviewer
        is reported IDLE by the terminal classifier. But if ``_request_task_review``
        bound the reviewer and the review prompt never actually left the tmux
        input box (e.g. the send raised after binding), the reviewer would sit
        bound + IDLE forever and would never be recovered.

        Return True only when the reviewer's review prompt is *still verifiably
        sitting in the terminal input box* — the same positive signal the
        monitor's ``_detect_prompt_dispatch_stall`` already trusts. A reviewer
        that is genuinely working has already submitted the prompt, so its input
        box is empty and this returns False.
        """
        if not task.review_session_id:
            return False
        reviewer = self.sessions.get(task.review_session_id)
        if not reviewer or reviewer.session_kind == SessionKind.CHAT or not reviewer.tmux_session:
            return False
        try:
            output = await self._capture_tmux_output(reviewer.tmux_session)
        except RuntimeError as exc:
            logger.warning(
                "Could not inspect reviewer prompt for reaper task_id=%s session_id=%s: %s",
                task.id,
                reviewer.id,
                exc,
            )
            return False
        # Check every prefix that could legitimately be in the reviewer's
        # input box (review prompt or hard-recovery reviewer prompt). If none
        # match, the prompt was submitted and the reviewer is genuinely idle.
        for prefix in self._expected_pending_prompt_prefixes(reviewer, task):
            if self._message_still_in_input(output, prefix):
                return True
        return False

    async def _review_dispatch_failed(self, task: WorkspaceTask) -> bool:
        """Reaper gate: True when this task's review dispatch should be re-sent.

        Combines the strict reviewer-availability predicate with the input-box
        backstop so the fallback reaper only re-dispatches on positive evidence
        of a failed dispatch, never on transient reviewer IDLE."""
        if self._reviewer_dispatch_stuck(task):
            return True
        return await self._reviewer_prompt_still_pending(task)

    def _reviewer_has_active_task_binding(self, session: ManagedSession) -> bool:
        return any(
            task.workspace_id == session.workspace_id
            and task.review_session_id == session.id
            and task.status in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}
            for task in self.tasks.values()
        )

    def _release_stale_reviewer_for_task(
        self, task: WorkspaceTask, *, updated_at: datetime
    ) -> None:
        """Clear the task's stale review_session_id reference and reset any
        reviewer sessions whose task_id/current_task_id still point at this
        task but are not actively working on it. Used to unstick review
        dispatch after a prompt send failure or reviewer crash."""
        # Release any reviewer session that still carries this task's id.
        self._release_reviewer_session(
            task,
            status=ManagedSessionStatus.IDLE,
            runtime_status=AgentRuntimeStatus.IDLE,
            updated_at=updated_at,
            include_stale_assignments=True,
        )
        # Clear the task's own stale reference so future dispatch succeeds.
        current = self.tasks.get(task.id)
        if not current:
            return
        if current.review_session_id and not self._reviewer_is_active(current):
            self.tasks[current.id] = current.model_copy(
                update={
                    "review_session_id": None,
                    "updated_at": updated_at,
                }
            )

    def _cleanup_stale_reviewer_assignments(self, workspace_id: str) -> bool:
        """Drop stale task_id/current_task_id values from REVIEWER sessions
        where the referenced task no longer exists, is not awaiting review,
        or does not list the session as its review_session_id. Returns True
        if any session was modified."""
        changed = False
        now = self._runtime_now()
        for session in self._sessions_for_workspace_raw(workspace_id):
            if session.archived or session.role != WorkspaceSessionRole.REVIEWER:
                continue
            if not session.task_id and not session.current_task_id:
                continue
            candidate_id = session.task_id or session.current_task_id
            if not candidate_id:
                continue
            task = self.tasks.get(candidate_id)
            if task is not None and not self._workspace_owns_task(task):
                continue
            should_reset = False
            if not task or task.workspace_id != workspace_id:
                should_reset = True
            elif task.status == WorkspaceTaskStatus.DONE:
                should_reset = True
            elif task.review_session_id != session.id:
                # Session claims the task but the task does not reference this
                # session as its reviewer — the assignment is stale.
                should_reset = True
            elif (
                session.runtime_status == AgentRuntimeStatus.IDLE
                and session.status == ManagedSessionStatus.IDLE
            ):
                # An idle reviewer may still be intentionally bound to a task
                # after a terminal verdict. Only clear the fields when the task
                # itself is no longer in a protected working/review phase.
                should_reset = task.status not in {
                    WorkspaceTaskStatus.WORKING,
                    WorkspaceTaskStatus.REVIEW,
                }
            if not should_reset:
                continue
            logger.info(
                "Cleaning stale reviewer assignment session_id=%s task_id=%s",
                session.id,
                candidate_id,
            )
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
                    "prompt_retry_task_id": None,
                    "prompt_retry_attempted_at": None,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            changed = True
        return changed
