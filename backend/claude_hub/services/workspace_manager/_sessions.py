"""Managed session lifecycle."""

from __future__ import annotations

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ...models import TaskCleanupResult
from ..task_graph import TaskHasDescendantsError, task_has_descendants
from ._constants import *  # noqa: F401,F403


class _SessionsMixin:
    # Initialized in _StateMixin.__init__; annotation-only declaration (no value,
    # so no runtime attribute is created) lets mypy type the rebind below.
    reports: "dict[str, AgentReport]"

    @staticmethod
    def _effective_agent_target(
        workspace: Workspace,
        payload: EnsureWorkspaceAgentRequest,
    ) -> ExecutionTarget:
        """Omitted agent target defaults to LOCAL (not workspace.target)."""
        return payload.target or ExecutionTarget.LOCAL

    def delete_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task_has_descendants(self.tasks, task.workspace_id, task):
            raise TaskHasDescendantsError(
                f"Cannot delete task {task_id}: it has child tasks. "
                "Delete or reparent descendants first."
            )

        workspace_id = task.workspace_id
        snapshot = self._snapshot_report_intake_workspace(workspace_id)

        if task.system_internal and task.internal_kind == "feedback_reaper":
            try:
                self._feedback_store().abandon_summary_run(
                    workspace_id,
                    task.id,
                    reason="task_deleted",
                    now=_wm._now(),
                )
            except Exception:
                logger.exception(
                    "Failed to abandon Feedback Reaper summary run during task deletion "
                    "workspace_id=%s task_id=%s",
                    workspace_id,
                    task.id,
                )

        self.task_mailbox.purge_task_events(workspace_id, task.id)

        self.tasks.pop(task_id, None)
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

        try:
            self._save_state()
        except Exception:
            self._restore_report_intake_workspace(workspace_id, snapshot)
            raise

    async def ensure_workspace_agent(
        self,
        workspace_id: str,
        payload_or_agent_type: EnsureWorkspaceAgentRequest | AgentType = AgentType.CODEX,
    ) -> ManagedSession:
        from ..env_preset_resolver import (
            EnvPresetNotFoundError,
            EnvPresetParseError,
            merge_env_with_preset,
        )

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

        if payload.ephemeral and payload.reuse_existing:
            raise ValueError("Cannot combine ephemeral with reuse_existing")

        if payload.env_preset:
            try:
                merged_env = merge_env_with_preset(
                    preset=payload.env_preset,
                    explicit_env=dict(payload.env or {}),
                )
            except (EnvPresetNotFoundError, EnvPresetParseError) as exc:
                raise ValueError(str(exc)) from None
            payload = payload.model_copy(update={"env": merged_env})

        if payload.caller_owned_ephemeral and not payload.ephemeral:
            raise ValueError("caller_owned_ephemeral requires ephemeral=True")

        session_target = self._effective_agent_target(workspace, payload)
        if session_target == ExecutionTarget.LOCAL:
            from ..workspace_identity import (
                InvalidLocalAgentCwdError,
                validate_local_agent_cwd_for_workspace,
            )

            try:
                validate_local_agent_cwd_for_workspace(
                    workspace.path,
                    payload.cwd or workspace.path,
                )
            except InvalidLocalAgentCwdError as exc:
                raise ValueError(str(exc)) from None

        reuse_existing = payload.reuse_existing and not payload.ephemeral

        if reuse_existing:
            # Reuse is deliberately best-effort, not an idempotency boundary.
            # A compatible idle session that is already persisted is reused,
            # but overlapping create requests that both observe no session are
            # allowed to create separate agents for intentional parallel work.
            existing = self._find_compatible_workspace_agent(workspace, payload)
            if existing:
                self._sync_session_tab_metadata(existing)
                if payload.role == WorkspaceSessionRole.DISPATCHER:
                    if workspace.dispatcher_session_id != existing.id:
                        now = _wm._now()
                        self.workspaces[workspace.id] = workspace.model_copy(
                            update={"dispatcher_session_id": existing.id, "updated_at": now}
                        )
                        self._save_state()
                return existing

        # Serialize the stateful create itself so overlapping requests receive
        # distinct role counters/session ids and cannot overwrite each other.
        # Do not re-run the compatibility lookup inside this lock: doing so
        # would turn advisory reuse into hard concurrent de-duplication.
        async with self.workspace_mutation_lock(workspace_id):
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

        session_target = self._effective_agent_target(workspace, payload)
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
        cursor_transport = "terminal"
        cursor_create_kwargs: dict[str, Any] = {}
        cursor_session_id: str | None = None
        launch_env = dict(payload.env)
        if payload.agent_type == AgentType.CURSOR and session_target == ExecutionTarget.LOCAL:
            from ..ttyd_manager import (
                CURSOR_TRANSCRIPT_SCHEMA,
                SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS,
                cursor_cli_version_from_executable,
                cursor_data_dir_for_env,
                cursor_terminal_transcript_path,
            )

            cursor_session_id = str(uuid.uuid4())
            cursor_cli_version = cursor_cli_version_from_executable()
            if cursor_cli_version in SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS:
                cursor_data_dir = cursor_data_dir_for_env(launch_env)
                cursor_transport = "terminal_transcript"
                # The launched Cursor process must use the exact root recorded
                # in provenance. Do not infer it later from whichever HOME the
                # backend happens to have after a restart.
                launch_env["CURSOR_DATA_DIR"] = cursor_data_dir
                cursor_create_kwargs = {
                    "cursor_transport": cursor_transport,
                    "cursor_data_dir": cursor_data_dir,
                    "cursor_cli_version": cursor_cli_version,
                    "cursor_transcript_path": str(
                        cursor_terminal_transcript_path(
                            local_cwd, cursor_session_id, data_dir=cursor_data_dir
                        )
                    ),
                    "cursor_transcript_schema": CURSOR_TRANSCRIPT_SCHEMA,
                }
            # else: unsupported or unavailable CLI → fail closed to a plain
            # Cursor terminal tab (cursor_transport stays "terminal"). The
            # registry reports structured=False; the raw pane remains truthful.
        agent_session_id = payload.agent_session_id or cursor_session_id
        agent_session_kwargs: dict[str, Any] = (
            {"agent_session_id": agent_session_id} if agent_session_id is not None else {}
        )
        if launch_env:
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
                env=launch_env,
                **agent_session_kwargs,
                **cursor_create_kwargs,
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
                **agent_session_kwargs,
                **cursor_create_kwargs,
            )
        now = _wm._now()
        session = ManagedSession(
            id=session_id,
            workspace_id=workspace.id,
            task_id=None,
            tab_id=tab.id,
            role=role,
            agent_type=payload.agent_type,
            # Workspace worker/reviewer/orchestrator sessions always drive
            # the raw TUI control plane.  Never inherit a mocked, stale, or
            # migrated top-level tab's Chat surface.
            session_kind=SessionKind.TERMINAL,
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
            caller_owned_ephemeral=bool(payload.caller_owned_ephemeral),
            env=launch_env,
            remote_forward_port=remote_forward_port,
            agent_session_id=tab.agent_session_id,
            cursor_transport=tab.cursor_transport,
            cursor_data_dir=tab.cursor_data_dir,
            cursor_cli_version=tab.cursor_cli_version,
            cursor_transcript_path=tab.cursor_transcript_path,
            cursor_transcript_schema=tab.cursor_transcript_schema,
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
        from ..workspace_identity import normalize_remote_cwd

        if requested_cwd:
            return normalize_remote_cwd(requested_cwd)
        if workspace_cwd:
            return normalize_remote_cwd(workspace_cwd)
        if profile_id:
            profile = remote_profile_manager.get_profile(profile_id)
            if profile and profile.default_cwd:
                return normalize_remote_cwd(profile.default_cwd)
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

        blocking = self._non_terminal_tasks_referencing_session(session_id)
        if blocking:
            raise RuntimeError("Cannot delete an agent with queued, working, or review tasks")

        # A later create can reuse this human-readable session id. Remove the
        # structured stream before exposing that id for a new conversation.
        from ..agent_stream import discard_session_stream

        await discard_session_stream(session.workspace_id, session_id)

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

    _CLEANUP_ALLOWED_SESSION_ROLES = frozenset(
        {WorkspaceSessionRole.ORCHESTRATOR, WorkspaceSessionRole.WORKER}
    )

    async def cleanup_task_session(self, task_id: str) -> TaskCleanupResult:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)

        if task.system_internal or task.internal_kind:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=task.session_id,
                action="skipped",
                reason="task cleanup not allowed for internal/system tasks",
            )

        if task.status != WorkspaceTaskStatus.DONE:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=task.session_id,
                action="skipped",
                reason=f"task status is {task.status.value}; must be done",
            )

        session_id = task.session_id
        if not session_id:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=None,
                action="skipped",
                reason="task has no session_id",
            )

        session = self.sessions.get(session_id)
        if not session:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason="session no longer exists",
            )

        if session.workspace_id != task.workspace_id:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason="session belongs to a different workspace than the task",
            )

        if session.role not in self._CLEANUP_ALLOWED_SESSION_ROLES:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason=(
                    f"task cleanup not allowed for {session.role.value} sessions "
                    "(orchestrator/worker only)"
                ),
            )

        if not session.caller_owned_ephemeral:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason="session is not caller-owned ephemeral",
            )

        workspace = self.workspaces.get(task.workspace_id)
        if workspace is not None:
            if workspace.resident_agent_session_id == session_id:
                return TaskCleanupResult(
                    task_id=task_id,
                    session_id=session_id,
                    action="skipped",
                    reason="session is the workspace resident agent",
                )
            if workspace.dispatcher_session_id == session_id:
                return TaskCleanupResult(
                    task_id=task_id,
                    session_id=session_id,
                    action="skipped",
                    reason="session is the workspace dispatcher",
                )

        if session.runtime_status != AgentRuntimeStatus.IDLE:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason=f"session runtime_status is {session.runtime_status.value}; must be idle",
            )

        if session.status == ManagedSessionStatus.STOPPED:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason="session is stopped",
            )

        if self._session_raw_task_bindings_block_cleanup(session, exclude_task_id=task_id):
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason="session has active raw task binding to a non-terminal task",
            )

        blocking = self._non_terminal_tasks_referencing_session(
            session_id,
            exclude_task_id=task_id,
        )
        if blocking:
            return TaskCleanupResult(
                task_id=task_id,
                session_id=session_id,
                action="skipped",
                reason="other non-terminal tasks still reference this session",
            )

        await self.delete_session(session_id)
        return TaskCleanupResult(
            task_id=task_id,
            session_id=session_id,
            action="deleted",
            reason=None,
        )

    def set_session_agent_session_id(self, session_id: str, agent_session_id: str) -> bool:
        """Durably persist the provider conversation id for a workspace session.

        The captured conversation id (Claude/Codex/Cursor) is stored on the
        managed session so a cold restart can resume the same provider
        conversation via ``--resume`` (Claude/Cursor) or ``thread/resume``
        (Codex).

        This is only called after the provider has emitted the id in a
        system/init record, so ``agent_session_id_verified`` is set to True
        alongside the id. A cold restart seeds ``_conversation_id_verified``
        from this flag, never from the mere presence of a UUID.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return False
        if session.agent_session_id == agent_session_id and session.agent_session_id_verified:
            return True
        session.agent_session_id = agent_session_id
        session.agent_session_id_verified = True
        self._save_state()
        return True
