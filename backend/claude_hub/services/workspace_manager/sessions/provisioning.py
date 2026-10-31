"""Managed-agent reuse, validation, and provider provisioning."""

import claude_hub.services.managed_chat as managed_chat
from claude_hub.services.agent_stream.providers.spec import Surface, get_spec
from claude_hub.services.remote_profiles import reject_unsupported_interactive
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _SessionProvisioningMixin(_WorkspaceManagerBase):
    async def ensure_workspace_agent(
        self,
        workspace_id: str,
        payload_or_agent_type: EnsureWorkspaceAgentRequest | AgentType = AgentType.CODEX,
    ) -> ManagedSession:
        from claude_hub.services.env_preset_resolver import (
            EnvPresetNotFoundError,
            EnvPresetParseError,
            merge_env_with_preset,
        )
        from claude_hub.services.workspace_identity import (
            InvalidLocalAgentCwdError,
            validate_local_agent_cwd_for_workspace,
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

        # A direct-Chat provider absent from both managed surfaces is the
        # TraeX boundary (goal-packet / autonomous prompt / native sub-agent
        # contracts are not wired for it). Plain terminal workspace agents
        # remain allowed, and MASO is admitted for managed Chat. Fail fast
        # instead of creating a half-supported, terminal-degraded agent.
        provider_spec = get_spec(payload.agent_type)
        if (
            provider_spec.runs_on(Surface.DIRECT_CHAT)
            and not provider_spec.runs_on(Surface.MANAGED_CHAT)
            and not provider_spec.runs_on(Surface.MANAGED_TERMINAL)
        ):
            raise ValueError(
                "agent_type 'traex' is not supported for workspace agents; "
                "use claude, codex, or cursor (or open traex as a standalone tab)"
            )

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
                        now = self._runtime_now()
                        self.workspaces[workspace.id] = workspace.model_copy(
                            update={"dispatcher_session_id": existing.id, "updated_at": now}
                        )
                        self._save_state()
                return existing

        # Serialize the stateful create itself so overlapping requests receive
        # distinct role counters/session ids and cannot overwrite each other.
        # Do not re-run the compatibility lookup inside this lock: doing so
        # would turn advisory reuse into hard concurrent de-duplication.
        session: ManagedSession | None = None
        try:
            async with self.workspace_mutation_lock(workspace_id):
                session = await self._create_managed_session(workspace, payload)
            bootstrap_prompt = self._build_session_bootstrap_prompt(workspace, session)
            if bootstrap_prompt:
                await self._wait_for_agent_prompt(session)
                await self.send_session_message(session.id, bootstrap_prompt)
        except BaseException as exc:
            if session is None:
                # Validation and tab-creation failures keep their established
                # ValueError/KeyError/RuntimeError contracts. Persistence
                # failures are wrapped inside _create_managed_session after
                # its compensation decision is known.
                raise
            # A native bootstrap may already own a provider turn. Retire that
            # owner through the same fenced deletion path before releasing the
            # friendly session id; if retirement fails, keep the session
            # paused and visible instead of cross-wiring a replacement to it.
            cleanup_error: BaseException | None = None
            cleanup = asyncio.create_task(
                self.delete_session(session.id),
                name=f"workspace-bootstrap-rollback-{session.id}",
            )
            try:
                await self._await_lifecycle_cleanup(cleanup)
            except asyncio.CancelledError:
                # Preserve the bootstrap cancellation after cleanup below.
                if not isinstance(exc, asyncio.CancelledError):
                    raise
            except BaseException as rollback_exc:
                cleanup_error = rollback_exc
                logger.exception(
                    "Failed to safely retire workspace agent after bootstrap failure "
                    "workspace_id=%s session_id=%s tab_id=%s",
                    workspace_id,
                    session.id,
                    session.tab_id,
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if cleanup_error is not None:
                raise WorkspaceAgentInitializationError(
                    "Failed to initialize workspace agent; its fenced cleanup is incomplete"
                ) from exc
            raise WorkspaceAgentInitializationError(
                "Failed to initialize workspace agent; the new session was rolled back"
            ) from exc
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

        tab_reserved_ids = {
            tab.managed_session_id
            for tab in [*ttyd_manager.list_tabs(), *ttyd_manager.list_archived_tabs()]
            if tab.managed_session_id
        }
        while (
            session_id in self.sessions
            or session_id in tab_reserved_ids
            or managed_chat.has_residual_state(workspace.id, session_id)
        ):
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
            profile = remote_profile_manager.get_profile(remote_profile_id)
            if not profile:
                raise ValueError(f"Remote profile not found: {remote_profile_id}")
            reject_unsupported_interactive(profile)
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
        cursor_transport = "native" if payload.session_kind == SessionKind.CHAT else "terminal"
        cursor_create_kwargs: dict[str, Any] = {}
        cursor_session_id: str | None = None
        launch_env = dict(payload.env)
        if (
            payload.agent_type == AgentType.CURSOR
            and session_target == ExecutionTarget.LOCAL
            and payload.session_kind == SessionKind.TERMINAL
        ):
            from claude_hub.services.ttyd_manager import (
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
                session_kind=payload.session_kind,
                target=session_target,
                remote_profile_id=remote_profile_id,
                remote_cwd=remote_cwd,
                remote_reconnect=remote_reconnect,
                remote_forward_port=remote_forward_port,
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                workspace_role=role,
                managed_session_id=session_id,
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
                session_kind=payload.session_kind,
                target=session_target,
                remote_profile_id=remote_profile_id,
                remote_cwd=remote_cwd,
                remote_reconnect=remote_reconnect,
                remote_forward_port=remote_forward_port,
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                workspace_role=role,
                managed_session_id=session_id,
                **agent_session_kwargs,
                **cursor_create_kwargs,
            )
        now = self._runtime_now()
        session = ManagedSession(
            id=session_id,
            workspace_id=workspace.id,
            task_id=None,
            tab_id=tab.id,
            role=role,
            agent_type=payload.agent_type,
            session_kind=payload.session_kind,
            chat_paused=False,
            status=ManagedSessionStatus.SPAWNING,
            runtime_status=AgentRuntimeStatus.IDLE,
            current_task_id=None,
            queued_count=0,
            title=title,
            branch=None,
            workspace_path=session_workspace_path,
            tmux_session=(
                "" if payload.session_kind == SessionKind.CHAT else f"claude-hub-{tab.id[:8]}"
            ),
            target=session_target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            solo_mode=payload.solo_mode,
            ephemeral=payload.ephemeral,
            caller_owned_ephemeral=bool(payload.caller_owned_ephemeral),
            env=launch_env,
            env_preset=payload.env_preset,
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
        snapshot = self._snapshot_report_intake_workspace(workspace.id)
        self.sessions[session.id] = session
        if role == WorkspaceSessionRole.DISPATCHER:
            self.workspaces[workspace.id] = workspace.model_copy(
                update={"dispatcher_session_id": session.id, "updated_at": now}
            )
        try:
            self._save_state()
        except BaseException as persist_exc:
            self._restore_report_intake_workspace(workspace.id, snapshot)
            compensated = False
            try:
                # _save_state spans index + workspace files. A reported failure
                # can occur after this workspace was replaced, so first persist
                # the restored owner set. If that compensation also fails, keep
                # the backing tab as a durable identity reservation rather than
                # delete the only evidence for a possibly committed session.
                self._save_state()
                compensated = True
            except BaseException:
                logger.exception(
                    "Failed to compensate workspace state after agent create failure "
                    "workspace_id=%s session_id=%s tab_id=%s",
                    workspace.id,
                    session.id,
                    tab.id,
                )
            if compensated:
                cleanup = asyncio.create_task(
                    ttyd_manager.delete_tab(tab.id),
                    name=f"workspace-create-rollback-{session.id}",
                )
                try:
                    await self._await_lifecycle_cleanup(cleanup)
                except BaseException:
                    if isinstance(persist_exc, asyncio.CancelledError):
                        raise persist_exc
                    raise WorkspaceAgentInitializationError(
                        "Failed to persist the workspace agent; its tab cleanup is incomplete"
                    ) from persist_exc
            if isinstance(persist_exc, asyncio.CancelledError):
                raise
            raise WorkspaceAgentInitializationError(
                "Failed to create and persist the workspace agent; its identity remains reserved"
            ) from persist_exc
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
        from claude_hub.services.workspace_identity import normalize_remote_cwd

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
        port = settings.port + REMOTE_FORWARD_PORT_OFFSET
        while port in used_ports:
            port += 1
        return port
