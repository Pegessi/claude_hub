"""Task admission and dispatch-target selection."""

from claude_hub.models.schemas import validate_workspace_session_surface
from claude_hub.services.task_dependencies import require_task_dependencies
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _TaskStartMixin(_WorkspaceManagerBase):
    async def _start_task_workspace(
        self,
        task_id: str,
        payload: StartTaskRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or StartTaskRequest()
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
        # do not re-dispatch. A crash between the dispatch side effect and
        # the state persist could leave a stale TODO status on disk; in that
        # case a retry would call start_task again. To avoid duplicate
        # dispatch, we check the task's session_id: if it is set, the task
        # was already assigned to a worker and we only ensure the dispatch
        # runs (dispatch_workspace is idempotent).
        if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
            if task.session_id:
                await self.dispatch_workspace(workspace.id)
            return self.tasks[task_id]

        # A compatibility start payload can override the provider even when an
        # existing agent avoids the normal creation/admission path.
        validate_workspace_session_surface(payload.agent_type or task.agent_type, task.session_kind)

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
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot be started")
        if task.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
            return task
        require_task_dependencies(self.tasks, task)
        validate_workspace_session_surface(payload.agent_type or task.agent_type, task.session_kind)
        selected_kind = task.session_kind
        if payload.target_session_id:
            selected = self.sessions.get(payload.target_session_id)
            if selected is None or selected.workspace_id != workspace.id:
                raise KeyError(payload.target_session_id)
            if selected.role != WorkspaceSessionRole.ORCHESTRATOR:
                raise RuntimeError("Tasks can only be assigned to workspace agents")
            if selected.session_kind != task.session_kind:
                raise ValueError("The selected agent does not match the Task execution surface")

        base_update: dict[str, Any] = {
            "status": WorkspaceTaskStatus.QUEUED,
            "queued_at": task.queued_at or self._runtime_now(),
            "updated_at": self._runtime_now(),
            "dispatch_pending": False,
            "manual_aborted_at": None,
            "manual_abort_reason": None,
            # Each (re-)dispatch gets a fresh attempt ordinal so the dispatch
            # call_id (dispatch:{task.id}:{dispatch_attempt}) is unique per
            # attempt. Followups may carry a different assignment prompt; a
            # new call_id keeps them compatible with the immutable-call_id
            # payload invariant. Crash recovery reuses the stored attempt, so
            # the same call_id no-ops correctly.
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

        task = task.model_copy(update=base_update)
        decision = await self._choose_dispatch_target(workspace, task, payload)
        # Choosing a target may provision an agent; a todo edit may have
        # changed the stored contract meanwhile. Never overwrite it.
        stored = self.tasks[task_id]
        if stored.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot be started")
        if stored.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
            return stored
        validate_workspace_session_surface(
            payload.agent_type or stored.agent_type, stored.session_kind
        )
        require_task_dependencies(self.tasks, stored)
        provider_changed = not payload.agent_type and stored.agent_type != task.agent_type
        if stored.session_kind != task.session_kind or provider_changed:
            raise ValueError("The Task changed while selecting its agent; start it again")
        task = stored.model_copy(update=base_update)
        if decision is None:
            task = task.model_copy(
                update={
                    "session_id": None,
                    # Preserve the stored clear-context flag when the start call
                    # does not override it, so the dispatcher decision later
                    # still applies the user's "Clear context" choice.
                    "clear_context": (
                        payload.clear_context
                        if payload.clear_context is not None
                        else task.clear_context
                    ),
                    "dispatch_reason": "Waiting for dispatcher agent decision",
                    "dispatch_pending": True,
                    "updated_at": self._runtime_now(),
                }
            )
            self.tasks[task.id] = task
            self._save_state()
            logger.info(
                "Workspace task id=%s is waiting for dispatcher decision related_task_id=%s",
                task.id,
                task.related_task_id,
            )
            await self._request_dispatch_decision(workspace, task)
            return self.tasks[task.id]

        target, clear_context, reason = decision
        autonomous_run = task.autonomous_run
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS and autonomous_run is not None:
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
        task = task.model_copy(
            update={
                "session_id": target.id,
                "clear_context": clear_context,
                "dispatch_reason": reason,
                "dispatch_pending": False,
                "autonomous_run": autonomous_run,
                "updated_at": self._runtime_now(),
            }
        )
        self.tasks[task.id] = task
        self._save_state()
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
    ) -> Optional[tuple[ManagedSession, bool, str]]:
        agents = [
            agent
            for agent in self._workspace_agents(workspace.id, include_stopped=True)
            if agent.session_kind == task.session_kind
        ]
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
            if target.role != WorkspaceSessionRole.ORCHESTRATOR:
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
                    and target.session_kind == task.session_kind
                    and self._can_dispatch_to(target)
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
                and existing.session_kind == task.session_kind
                and self._can_dispatch_to(existing)
            ):
                return (
                    existing,
                    bool(requested_clear),
                    "Continuing previous task assignment",
                )

        free_agents = [
            agent
            for agent in agents
            if agent.agent_type == task.agent_type and self._can_dispatch_to(agent)
        ]
        if len(free_agents) == 1:
            target = free_agents[0]
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                requested_clear if requested_clear is not None else should_clear,
                "Only one workspace agent is available",
            )
        if len(free_agents) > 1:
            target = self._least_queued_agent(free_agents)
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                requested_clear if requested_clear is not None else should_clear,
                "Selected least queued available workspace agent",
            )

        # One agent carries at most one open Task: never queue behind an agent
        # that another Task holds. Parallel Tasks get parallel agents.
        target = await self._provision_dedicated_agent(workspace, task)
        return target, bool(requested_clear), DEDICATED_AGENT_DISPATCH_REASON

    async def _provision_dedicated_agent(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
    ) -> ManagedSession:
        """Create one agent for ``task``, launched like its existing siblings."""

        template = self._agent_launch_template(workspace.id, task)
        is_chat = task.session_kind == SessionKind.CHAT
        logger.info(
            "Provisioning dedicated workspace agent workspace_id=%s task_id=%s "
            "agent_type=%s session_kind=%s template_session_id=%s",
            workspace.id,
            task.id,
            task.agent_type,
            task.session_kind,
            template.id if template else None,
        )
        return await self.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                session_kind=task.session_kind,
                role=WorkspaceSessionRole.ORCHESTRATOR,
                reuse_existing=False,
                env=dict(template.env) if template and is_chat else {},
                env_preset=template.env_preset if template else None,
                target=None if template is None or is_chat else template.target,
                remote_profile_id=None if template is None else template.remote_profile_id,
                remote_cwd=None if template is None else template.remote_cwd,
                remote_reconnect=None if template is None or is_chat else template.remote_reconnect,
            ),
        )

    def _agent_launch_template(
        self,
        workspace_id: str,
        task: WorkspaceTask,
    ) -> Optional[ManagedSession]:
        """Most recent agent of the Task's provider and surface, if any."""

        siblings = [
            agent
            for agent in self._workspace_agents(workspace_id, include_stopped=True)
            if agent.agent_type == task.agent_type and agent.session_kind == task.session_kind
        ]
        return max(siblings, key=lambda agent: agent.created_at) if siblings else None

    def _can_assign_or_queue_to(self, session: ManagedSession) -> bool:
        if (
            session.archived
            or session.role != WorkspaceSessionRole.ORCHESTRATOR
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

    def _least_queued_agent(self, agents: list[ManagedSession]) -> ManagedSession:
        def runtime_rank(agent: ManagedSession) -> int:
            if agent.runtime_status == AgentRuntimeStatus.IDLE:
                return 0
            if agent.runtime_status == AgentRuntimeStatus.OFFLINE:
                return 1
            return 2

        return sorted(
            agents,
            key=lambda agent: (
                self._queued_count(agent.id),
                runtime_rank(agent),
                1 if agent.current_task_id or agent.task_id else 0,
                agent.created_at,
            ),
        )[0]

    def _has_prior_task_history(self, session_id: str) -> bool:
        return any(
            task.session_id == session_id
            and task.status in {WorkspaceTaskStatus.REVIEW, WorkspaceTaskStatus.DONE}
            for task in self.tasks.values()
        )
