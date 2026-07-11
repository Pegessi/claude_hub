"""Managed session lifecycle."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _SessionsMixin:
    # Initialized in _StateMixin.__init__; annotation-only declaration (no value,
    # so no runtime attribute is created) lets mypy type the rebind below.
    reports: "dict[str, AgentReport]"

    def delete_task(self, task_id: str) -> None:
        task = self.tasks.pop(task_id, None)
        if not task:
            raise KeyError(task_id)

        self.reports = {
            report_id: report
            for report_id, report in self.reports.items()
            if report.task_id != task_id
        }
        for session_id, session in list(self.sessions.items()):
            if session.task_id == task_id or session.current_task_id == task_id:
                self.sessions[session_id] = session.model_copy(
                    update={
                        "task_id": None,
                        "current_task_id": None,
                        "auto_continue_task_id": None,
                        "auto_continue_attempts": 0,
                        "last_auto_continue_at": None,
                        "hard_recovery_task_id": None,
                        "hard_recovery_attempts": 0,
                        "last_hard_recovery_at": None,
                        "prompt_retry_task_id": None,
                        "prompt_retry_attempted_at": None,
                        "updated_at": _wm._now(),
                    }
                )
        self._save_state()

    async def ensure_workspace_agent(
        self,
        workspace_id: str,
        payload_or_agent_type: EnsureWorkspaceAgentRequest | AgentType = AgentType.CODEX,
    ) -> ManagedSession:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        if isinstance(payload_or_agent_type, EnsureWorkspaceAgentRequest):
            payload = payload_or_agent_type
        else:
            payload = EnsureWorkspaceAgentRequest(
                agent_type=payload_or_agent_type,
                reuse_existing=True,
            )

        if payload.role == WorkspaceSessionRole.DISPATCHER:
            existing = self._dispatcher_session(workspace)
            if existing:
                self._sync_session_tab_metadata(existing)
                return existing
        elif payload.role == WorkspaceSessionRole.REVIEWER and payload.reuse_existing:
            existing = self._first_available_reviewer(workspace.id)
            if existing:
                self._sync_session_tab_metadata(existing)
                return existing
        elif payload.reuse_existing:
            existing = self._first_available_workspace_agent(workspace.id)
            if existing:
                self._sync_session_tab_metadata(existing)
                return existing

        session = await self._create_managed_session(workspace, payload)
        bootstrap_prompt = self._build_session_bootstrap_prompt(workspace, session)
        if bootstrap_prompt:
            await self.send_session_message(session.id, bootstrap_prompt)
        return session

    async def _create_managed_session(
        self,
        workspace: Workspace,
        payload: EnsureWorkspaceAgentRequest,
    ) -> ManagedSession:
        role = payload.role
        role_count = len(
            [
                session
                for session in self._sessions_for_workspace_raw(workspace.id)
                if session.role == role
            ]
        )
        if role == WorkspaceSessionRole.DISPATCHER:
            session_id = f"{workspace.session_prefix}-dispatcher"
            title = payload.title or f"{workspace.name} Dispatcher"
        elif role == WorkspaceSessionRole.REVIEWER:
            session_id = f"{workspace.session_prefix}-reviewer-{role_count + 1}"
            title = payload.title or f"{workspace.name} Reviewer {role_count + 1}"
        else:
            session_id = f"{workspace.session_prefix}-agent-{role_count + 1}"
            title = payload.title or f"{workspace.name} Agent {role_count + 1}"

        if session_id in self.sessions:
            session_id = f"{session_id}-{uuid.uuid4().hex[:6]}"

        session_target = payload.target or ExecutionTarget.LOCAL
        local_cwd = payload.cwd or workspace.path
        remote_profile_id: str | None = None
        remote_cwd: str | None = None
        remote_reconnect = (
            payload.remote_reconnect
            if payload.remote_reconnect is not None
            else workspace.remote_reconnect
        )
        if session_target == ExecutionTarget.REMOTE:
            remote_profile_id = payload.remote_profile_id or workspace.remote_profile_id
            if not remote_profile_id:
                raise ValueError("Remote agent requires remote_profile_id")
            if not remote_profile_manager.get_profile(remote_profile_id):
                raise ValueError(f"Remote profile not found: {remote_profile_id}")
            remote_cwd = self._resolve_remote_cwd(
                profile_id=remote_profile_id,
                requested_cwd=payload.remote_cwd,
                workspace_cwd=workspace.remote_cwd,
            )

        remote_forward_port = (
            self._next_remote_forward_port() if session_target == ExecutionTarget.REMOTE else None
        )
        session_workspace_path = (
            (remote_cwd or local_cwd) if session_target == ExecutionTarget.REMOTE else local_cwd
        )
        if payload.env:
            tab = await ttyd_manager.create_tab(
                name=title,
                cwd=local_cwd if session_target == ExecutionTarget.LOCAL else None,
                solo_mode=payload.solo_mode,
                agent_type=payload.agent_type,
                target=session_target,
                remote_profile_id=remote_profile_id,
                remote_cwd=remote_cwd,
                remote_reconnect=remote_reconnect,
                remote_forward_port=remote_forward_port,
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                workspace_role=role,
                env=payload.env,
            )
        else:
            tab = await ttyd_manager.create_tab(
                name=title,
                cwd=local_cwd if session_target == ExecutionTarget.LOCAL else None,
                solo_mode=payload.solo_mode,
                agent_type=payload.agent_type,
                target=session_target,
                remote_profile_id=remote_profile_id,
                remote_cwd=remote_cwd,
                remote_reconnect=remote_reconnect,
                remote_forward_port=remote_forward_port,
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                workspace_role=role,
            )
        now = _wm._now()
        session = ManagedSession(
            id=session_id,
            workspace_id=workspace.id,
            task_id=None,
            tab_id=tab.id,
            role=role,
            agent_type=payload.agent_type,
            status=ManagedSessionStatus.SPAWNING,
            runtime_status=AgentRuntimeStatus.IDLE,
            current_task_id=None,
            queued_count=0,
            title=title,
            branch=None,
            workspace_path=session_workspace_path,
            tmux_session=f"claude-hub-{tab.id[:8]}",
            target=session_target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            solo_mode=payload.solo_mode,
            ephemeral=payload.ephemeral,
            env=payload.env,
            remote_forward_port=remote_forward_port,
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        if role == WorkspaceSessionRole.DISPATCHER:
            self.workspaces[workspace.id] = workspace.model_copy(
                update={"dispatcher_session_id": session.id, "updated_at": now}
            )
        self._save_state()
        return session

    def _workspace_remote_cwd(self, workspace: Workspace) -> str:
        return self._resolve_remote_cwd(
            profile_id=workspace.remote_profile_id,
            requested_cwd=workspace.remote_cwd,
            workspace_cwd=None,
        )

    def _resolve_remote_cwd(
        self,
        profile_id: str | None,
        requested_cwd: str | None,
        workspace_cwd: str | None,
    ) -> str:
        if requested_cwd:
            return requested_cwd
        if workspace_cwd:
            return workspace_cwd
        if profile_id:
            profile = remote_profile_manager.get_profile(profile_id)
            if profile and profile.default_cwd:
                return profile.default_cwd
        return "~"

    def _next_remote_forward_port(self) -> int:
        used_ports = {
            session.remote_forward_port
            for session in self.sessions.values()
            if session.remote_forward_port is not None
        }
        port = REMOTE_FORWARD_PORT_BASE
        while port in used_ports:
            port += 1
        return port

    async def delete_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        blocking = [
            task
            for task in self.tasks.values()
            if (task.session_id == session_id or task.review_session_id == session_id)
            and task.status != WorkspaceTaskStatus.DONE
        ]
        if blocking:
            raise RuntimeError("Cannot delete an agent with queued, working, or review tasks")

        self.sessions.pop(session_id, None)
        workspace = self.workspaces.get(session.workspace_id)
        if workspace:
            ws_update: dict[str, Any] = {}
            if workspace.dispatcher_session_id == session_id:
                ws_update["dispatcher_session_id"] = None
            # Resident teardown via Delete: if this session is the workspace's
            # resident, clear the resident pointer and reset last_run_at. We ALSO
            # set resident_agent_enabled=False so that Delete means "stop it", not
            # "restart next tick" — otherwise the next resident tick would simply
            # recreate the session the user just deleted (surprising). Pause is the
            # way to keep the session disabled-for-auto but alive.
            if workspace.resident_agent_session_id == session_id:
                ws_update["resident_agent_session_id"] = None
                ws_update["resident_agent_last_run_at"] = None
                ws_update["resident_agent_enabled"] = False
            if ws_update:
                ws_update["updated_at"] = _wm._now()
                self.workspaces[workspace.id] = workspace.model_copy(update=ws_update)
        self._save_state()
        try:
            await ttyd_manager.delete_tab(session.tab_id)
        except Exception:
            logger.exception(
                "Failed to delete terminal tab after removing workspace session "
                "session_id=%s tab_id=%s",
                session.id,
                session.tab_id,
            )
