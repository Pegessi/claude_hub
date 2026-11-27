"""Task admission and dispatch-target selection."""

import os

from claude_hub.models.schemas import validate_workspace_session_surface
from claude_hub.services import work_pipeline, workspace_identity
from claude_hub.services.task_dependencies import require_task_dependencies
from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _TaskStartMixin(_WorkspaceManagerBase):
    def _validate_start_admission_inputs(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        payload: StartTaskRequest,
    ) -> None:
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot be started")
        if task.status != WorkspaceTaskStatus.WORKING:
            require_task_dependencies(self.tasks, task)
        validate_workspace_session_surface(payload.agent_type or task.agent_type, task.session_kind)
        if payload.related_task_id and payload.related_task_id not in self.tasks:
            raise KeyError(payload.related_task_id)
        if payload.target_session_id:
            selected = self.sessions.get(payload.target_session_id)
            if selected is None or selected.workspace_id != workspace.id:
                raise KeyError(payload.target_session_id)
            if selected.role not in {
                WorkspaceSessionRole.ORCHESTRATOR,
                WorkspaceSessionRole.WORKER,
            }:
                raise RuntimeError("Tasks can only be assigned to workspace agents")
            if selected.session_kind != task.session_kind:
                raise ValueError("The selected agent does not match the Task execution surface")

    async def _start_task_workspace(
        self,
        task_id: str,
        payload: StartTaskRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or StartTaskRequest()
        start_lock = self._task_start_locks.setdefault(task_id, asyncio.Lock())
        async with start_lock:
            return await self._start_task_workspace_locked(task_id, payload)

    async def _start_task_workspace_locked(
        self,
        task_id: str,
        payload: StartTaskRequest,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot be started")

        if task.status != WorkspaceTaskStatus.WORKING:
            require_task_dependencies(self.tasks, task)

        # Idempotency: if the task was already started (QUEUED or WORKING),
        # do not re-dispatch.
        if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
            if task.session_id:
                await self.dispatch_workspace(workspace.id)
            return self.tasks[task_id]

        self._validate_start_admission_inputs(workspace, task, payload)

        logger.info(
            "Starting workspace task id=%s workspace_id=%s title=%r payload_target_session_id=%s "
            "payload_related_task_id=%s stored_related_task_id=%s current_session_id=%s status=%s",
            task.id,
            workspace.id,
            task.title,
            payload.target_session_id,
            payload.related_task_id,
            task.related_task_id,
            task.session_id,
            task.status,
        )
        await self._refresh_session_statuses(workspace.id)
        # Refresh can yield to a todo edit. Do not create an agent from the
        # pre-refresh surface and only discover that it was invalid afterward.
        task = self.tasks[task_id]
        if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
            return task
        self._validate_start_admission_inputs(workspace, task, payload)

        initial_payload = payload
        for _attempt in range(3):
            payload = initial_payload
            task = self.tasks[task_id]
            if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
                return task
            self._validate_start_admission_inputs(workspace, task, payload)
            selected_kind = task.session_kind

            created_ephemeral_session_id: str | None = None
            try:
                if workspace.target == ExecutionTarget.LOCAL and task.auto_worktree:
                    try:
                        await work_pipeline._resolve_canonical_git_repo_root(workspace.path)
                    except RuntimeError:
                        if not await work_pipeline.is_confirmed_non_git_directory(workspace.path):
                            raise
                        async with self.workspace_mutation_lock(workspace.id):
                            # The Git probe yields; apply only this decision to the
                            # current task so concurrent todo edits are preserved.
                            task = self.tasks[task_id]
                            if task.status in (
                                WorkspaceTaskStatus.QUEUED,
                                WorkspaceTaskStatus.WORKING,
                            ):
                                return task
                            self._validate_start_admission_inputs(workspace, task, payload)
                            logger.info(
                                "Disabling auto_worktree for confirmed non-Git workspace task_id=%s path=%s",
                                task.id,
                                workspace.path,
                            )
                            task = task.model_copy(
                                update={
                                    "auto_worktree": False,
                                    "updated_at": self._runtime_now(),
                                }
                            )
                            self.tasks[task.id] = task
                            self._save_workspace_state(workspace.id)
                    task = self.tasks[task_id]
                    if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
                        return task
                    self._validate_start_admission_inputs(workspace, task, payload)
                    if task.session_kind != selected_kind:
                        continue
                if workspace.target == ExecutionTarget.LOCAL and task.auto_worktree:
                    wt_path, wt_branch = await work_pipeline.provision_task_worktree(
                        workspace, task
                    )
                    task = self.tasks[task_id]
                    if task.worktree_path != wt_path or task.worktree_branch != wt_branch:
                        task = task.model_copy(
                            update={
                                "worktree_path": wt_path,
                                "worktree_branch": wt_branch,
                                "updated_at": self._runtime_now(),
                            }
                        )
                        self.tasks[task.id] = task
                        self._save_workspace_state(workspace.id)

                    if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
                        return task
                    self._validate_start_admission_inputs(workspace, task, payload)

                # Only explicit affinity can reuse a session. Worktree tasks also
                # require that the selected session runs in the task worktree.
                wanted_wt = (
                    workspace_identity.resolve_path(wt_path)
                    if workspace.target == ExecutionTarget.LOCAL and task.auto_worktree
                    else None
                )
                inherited_env: dict[str, str] = {}
                inherited_preset: str | None = None
                target = self.sessions.get(payload.target_session_id or "")
                if target is not None:
                    inherited_env = dict(target.env)
                    inherited_preset = target.env_preset

                def compatible(candidate: ManagedSession | None) -> bool:
                    return bool(
                        candidate
                        and candidate.workspace_id == workspace.id
                        and candidate.session_kind == selected_kind
                        and self._can_assign_or_queue_to(candidate)
                        and (
                            wanted_wt is None
                            or workspace_identity.resolve_path(
                                candidate.workspace_path or workspace.path
                            )
                            == wanted_wt
                        )
                    )

                if not payload.target_session_id:
                    related = self.tasks.get(payload.related_task_id or task.related_task_id or "")
                    related_target = (
                        self.sessions.get(related.session_id or "") if related else None
                    )
                    if compatible(related_target):
                        target = related_target
                    else:
                        target = self.sessions.get(task.session_id or "")
                        if not compatible(target):
                            target = None
                        elif related_target is not None:
                            # Do not let the later selection choose a related
                            # session that failed the worktree or liveness check.
                            assert target is not None
                            payload = payload.model_copy(update={"target_session_id": target.id})
                elif (
                    target
                    and wanted_wt
                    and workspace_identity.resolve_path(target.workspace_path or workspace.path)
                    != wanted_wt
                ):
                    target = None
                if target is None:
                    task_agent = await self.ensure_workspace_agent(
                        workspace.id,
                        EnsureWorkspaceAgentRequest(
                            agent_type=payload.agent_type or task.agent_type,
                            session_kind=selected_kind,
                            role=WorkspaceSessionRole.ORCHESTRATOR,
                            target=workspace.target,
                            reuse_existing=False,
                            ephemeral=True,
                            caller_owned_ephemeral=True,
                            cwd=wt_path if wanted_wt else workspace.path,
                            env=inherited_env,
                            env_preset=inherited_preset,
                        ),
                    )
                    created_ephemeral_session_id = task_agent.id
                    payload = payload.model_copy(update={"target_session_id": task_agent.id})

                self._validate_start_admission_inputs(workspace, self.tasks[task_id], payload)

                # Session preparation awaits external work; a todo edit may have changed
                # prerequisites or auto_worktree meanwhile. Never overwrite that newer contract.
                task = self.tasks[task_id]
                if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
                    return task
                self._validate_start_admission_inputs(workspace, task, payload)

                base_update: dict[str, Any] = {
                    "status": WorkspaceTaskStatus.QUEUED,
                    "queued_at": task.queued_at or self._runtime_now(),
                    "updated_at": self._runtime_now(),
                    "dispatch_pending": False,
                    "manual_aborted_at": None,
                    "manual_abort_reason": None,
                    "dispatch_attempt": task.dispatch_attempt + 1,
                }
                if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                    run = task.autonomous_run or self._default_autonomous_run(
                        task.id,
                        task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
                    )
                    phase = AutonomousRunPhase.DISPATCHING
                    base_update["autonomous_run"] = run.model_copy(
                        update={
                            "phase": phase,
                            "status_summary": self._autonomous_phase_label(phase),
                            "next_action": self._autonomous_next_action(phase),
                        }
                    )
                if payload.agent_type:
                    base_update["agent_type"] = payload.agent_type
                if payload.related_task_id:
                    if payload.related_task_id not in self.tasks:
                        raise KeyError(payload.related_task_id)
                    base_update["related_task_id"] = payload.related_task_id

                staged_task = apply_task_update(task, base_update, cause="start")
                decision = await self._choose_dispatch_target(workspace, staged_task, payload)
                target, clear_context, reason = decision

                # Re-read live task after _choose_dispatch_target await and enforce
                # the auto_worktree isolation invariant before committing!
                latest_task = self.tasks[task_id]
                if latest_task.status in (
                    WorkspaceTaskStatus.QUEUED,
                    WorkspaceTaskStatus.WORKING,
                ):
                    return latest_task
                self._validate_start_admission_inputs(workspace, latest_task, payload)
                if workspace.target == ExecutionTarget.LOCAL and latest_task.auto_worktree:
                    wt_path_str = latest_task.worktree_path
                    wt_ok = (
                        wt_path_str is not None
                        and bool(wt_path_str)
                        and workspace_identity.resolve_path(wt_path_str)
                        != workspace_identity.resolve_path(workspace.path)
                        and workspace_identity.resolve_path(target.workspace_path or "")
                        == workspace_identity.resolve_path(wt_path_str)
                    )
                    if not wt_ok:
                        continue

                staged_task = apply_task_update(latest_task, base_update, cause="start")
                autonomous_run = staged_task.autonomous_run
                if (
                    staged_task.task_mode == WorkspaceTaskMode.AUTONOMOUS
                    and autonomous_run is not None
                ):
                    phase = AutonomousRunPhase.WORKING
                    autonomous_run = autonomous_run.model_copy(
                        update={
                            "phase": phase,
                            "status_summary": self._autonomous_phase_label(phase),
                            "active_session_ids": list(
                                dict.fromkeys([*autonomous_run.active_session_ids, target.id])
                            ),
                            "next_action": self._autonomous_next_action(phase),
                        }
                    )
                committed_task = staged_task.model_copy(
                    update={
                        "session_id": target.id,
                        "clear_context": clear_context,
                        "dispatch_reason": reason,
                        "dispatch_pending": False,
                        "autonomous_run": autonomous_run,
                        "updated_at": self._runtime_now(),
                    }
                )
                self.tasks[committed_task.id] = committed_task
                self._save_state()
                task = committed_task
                break
            finally:
                if created_ephemeral_session_id is not None:
                    live_after = self.tasks.get(task_id)
                    if live_after is None or live_after.session_id != created_ephemeral_session_id:
                        await asyncio.shield(
                            self._best_effort_delete_session(created_ephemeral_session_id)
                        )
        else:
            raise RuntimeError(
                "Task worktree contract changed repeatedly during start admission; retry start_task"
            )
        logger.info(
            "Workspace task id=%s queued for session_id=%s session_title=%r related_task_id=%s "
            "clear_context=%s reason=%r",
            task.id,
            target.id,
            target.title,
            task.related_task_id,
            clear_context,
            reason,
        )
        await self.dispatch_workspace(workspace.id)
        return self.tasks[task.id]

    async def _choose_dispatch_target(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        payload: StartTaskRequest,
    ) -> tuple[ManagedSession, bool, str]:
        # The "Clear context" checkbox is stored on the task at creation
        # (task.clear_context) but a start call may also override it inline
        # (payload.clear_context). Resolve the explicit request once so every
        # dispatch branch below honors it: an inline payload value wins, then
        # the stored task flag, then None (let each branch fall back to its
        # default/heuristic). Without this fallback, continuity and related-task
        # branches dropped the stored flag and never sent /clear.
        requested_clear = (
            payload.clear_context if payload.clear_context is not None else task.clear_context
        )
        if payload.target_session_id:
            target = self.sessions.get(payload.target_session_id)
            if not target or target.workspace_id != workspace.id:
                raise KeyError(payload.target_session_id)
            if target.role not in {WorkspaceSessionRole.ORCHESTRATOR, WorkspaceSessionRole.WORKER}:
                raise RuntimeError("Tasks can only be assigned to workspace agents")
            if target.session_kind != task.session_kind:
                raise ValueError("The selected agent does not match the Task execution surface")
            if not self._can_assign_or_queue_to(target):
                if (
                    target.status == ManagedSessionStatus.STOPPED
                    or target.runtime_status == AgentRuntimeStatus.OFFLINE
                ):
                    raise RuntimeError("Offline workspace agents cannot accept tasks")
                if target.runtime_status == AgentRuntimeStatus.ATTENTION:
                    raise RuntimeError("Workspace agents waiting for input cannot accept new tasks")
                raise RuntimeError("Selected workspace agent cannot accept tasks yet")
            return (
                target,
                bool(requested_clear),
                "User selected target agent",
            )

        related_task_id = payload.related_task_id or task.related_task_id
        if related_task_id:
            related = self.tasks.get(related_task_id)
            target = None
            if related and related.session_id:
                target = self.sessions.get(related.session_id)
                if (
                    target
                    and target.workspace_id == workspace.id
                    and target.session_kind == task.session_kind
                    and self._can_assign_or_queue_to(target)
                ):
                    logger.info(
                        "Dispatch target selected from related task: task_id=%s related_task_id=%s "
                        "target_session_id=%s related_status=%s",
                        task.id,
                        related_task_id,
                        target.id,
                        related.status,
                    )
                    return (
                        target,
                        bool(requested_clear),
                        f"Related to task {related_task_id}",
                    )
            logger.info(
                "Related task did not provide a dispatch target: task_id=%s related_task_id=%s "
                "related_exists=%s related_session_id=%s related_target_runtime=%s",
                task.id,
                related_task_id,
                bool(related),
                related.session_id if related else None,
                target.runtime_status if target else None,
            )

        if task.session_id:
            existing = self.sessions.get(task.session_id)
            if (
                existing
                and existing.workspace_id == workspace.id
                and existing.session_kind == task.session_kind
                and self._can_assign_or_queue_to(existing)
            ):
                return (
                    existing,
                    bool(requested_clear),
                    "Continuing previous task assignment",
                )

        raise RuntimeError("No explicitly associated workspace agent is available")

    def _can_assign_or_queue_to(self, session: ManagedSession) -> bool:
        if (
            session.archived
            or session.role not in {WorkspaceSessionRole.ORCHESTRATOR, WorkspaceSessionRole.WORKER}
            or session.chat_paused
        ):
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        if session.runtime_status == AgentRuntimeStatus.WORKING:
            return True
        if self._is_holding_unresolved_review_task(session):
            return True
        return self._can_dispatch_to(session)
