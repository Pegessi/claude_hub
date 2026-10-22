"""Workspace identity reconciliation and CRUD operations."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _WorkspaceLifecycleMixin(_WorkspaceManagerBase):
    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        return self.workspaces.get(workspace_id)

    def _workspaces_for_identity_key(self, identity_key: str) -> list[Workspace]:
        from claude_hub.services.workspace_identity import workspace_identity

        matches = [
            ws for ws in self.workspaces.values() if workspace_identity(ws).key == identity_key
        ]
        return sorted(matches, key=lambda ws: (ws.created_at, ws.id))

    def _reconcile_workspace_session_pointers(self, workspace_id: str) -> None:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return
        updates: dict[str, Any] = {}
        if workspace.dispatcher_session_id:
            session = self.sessions.get(workspace.dispatcher_session_id)
            if (
                session is None
                or session.archived
                or session.status == ManagedSessionStatus.STOPPED
            ):
                updates["dispatcher_session_id"] = None
        if workspace.resident_agent_session_id:
            session = self.sessions.get(workspace.resident_agent_session_id)
            if (
                session is None
                or session.archived
                or session.status == ManagedSessionStatus.STOPPED
            ):
                updates["resident_agent_session_id"] = None
        if updates:
            updates["updated_at"] = self._runtime_now()
            self.workspaces[workspace_id] = workspace.model_copy(update=updates)
            self._save_state()

    def _assert_no_duplicate_identity(self, payload: WorkspaceCreate | WorkspaceEnsure) -> None:
        from claude_hub.services.workspace_identity import (
            DuplicateWorkspaceError,
            workspace_identity_for_fields,
        )

        if getattr(payload, "allow_duplicate", False):
            return
        identity = workspace_identity_for_fields(
            path=payload.path,
            target=payload.target,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=payload.remote_cwd,
        )
        matches = self._workspaces_for_identity_key(identity.key)
        if matches:
            raise DuplicateWorkspaceError(identity.key, matches)

    def create_workspace(self, payload: WorkspaceCreate) -> Workspace:
        from claude_hub.services.workspace_identity import (
            canonical_workspace_path,
            normalize_remote_cwd,
            reject_relative_local_path,
        )

        reject_relative_local_path(payload.path, field="path")
        source_path = Path(payload.path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"Local workspace dir does not exist: {source_path}")
        if payload.target == ExecutionTarget.REMOTE:
            if not payload.remote_profile_id:
                raise ValueError("Remote workspace requires remote_profile_id")
            if not remote_profile_manager.get_profile(payload.remote_profile_id):
                raise ValueError(f"Remote profile not found: {payload.remote_profile_id}")

        self._assert_no_duplicate_identity(payload)
        canonical_path = canonical_workspace_path(str(source_path))

        workspace_id = str(uuid.uuid4())
        now = self._runtime_now()
        prefix = payload.session_prefix or _slug(payload.name)
        interval_minutes = max(1, payload.resident_agent_interval_minutes)
        directive = (payload.resident_agent_directive or "").strip() or None
        resident_title = (payload.resident_agent_title or "").strip() or None
        resident_cwd = (payload.resident_agent_cwd or "").strip() or None
        periodic_tasks = self._normalize_periodic_tasks(payload.resident_agent_periodic_tasks)
        stored_remote_cwd = normalize_remote_cwd(payload.remote_cwd) if payload.remote_cwd else None
        workspace = Workspace(
            id=workspace_id,
            name=payload.name,
            path=canonical_path,
            default_branch=payload.default_branch,
            session_prefix=prefix,
            dispatcher_session_id=None,
            target=payload.target,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=stored_remote_cwd,
            remote_reconnect=payload.remote_reconnect,
            resident_agent_enabled=payload.resident_agent_enabled,
            resident_agent_paused=payload.resident_agent_paused,
            resident_agent_interval_minutes=interval_minutes,
            resident_agent_session_id=None,
            resident_agent_directive=directive,
            resident_agent_periodic_tasks=periodic_tasks,
            resident_agent_last_run_at=None,
            resident_agent_type=payload.resident_agent_type,
            resident_agent_env=dict(payload.resident_agent_env or {}),
            resident_agent_solo_mode=payload.resident_agent_solo_mode,
            resident_agent_master_mode=payload.resident_agent_master_mode,
            resident_agent_title=resident_title,
            resident_agent_target=payload.resident_agent_target,
            resident_agent_remote_profile_id=payload.resident_agent_remote_profile_id,
            resident_agent_cwd=resident_cwd,
            resident_agent_remote_reconnect=payload.resident_agent_remote_reconnect,
            created_at=now,
            updated_at=now,
        )
        self.workspaces[workspace_id] = workspace
        self._save_state()
        return workspace

    def ensure_workspace(self, payload: WorkspaceEnsure) -> Workspace:
        from claude_hub.services.workspace_identity import (
            default_workspace_create_name,
            reject_relative_local_path,
            select_workspace_candidate,
            workspace_identity_for_fields,
        )

        reject_relative_local_path(payload.path, field="path")
        source_path = Path(payload.path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"Local workspace dir does not exist: {source_path}")
        if payload.target == ExecutionTarget.REMOTE:
            if not payload.remote_profile_id:
                raise ValueError("Remote workspace requires remote_profile_id")
            if not remote_profile_manager.get_profile(payload.remote_profile_id):
                raise ValueError(f"Remote profile not found: {payload.remote_profile_id}")

        identity = workspace_identity_for_fields(
            path=str(source_path),
            target=payload.target,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=payload.remote_cwd,
        )
        matches = self._workspaces_for_identity_key(identity.key)
        if matches:
            chosen = select_workspace_candidate(matches, requested_path=str(source_path))
            self._reconcile_workspace_session_pointers(chosen.id)
            return chosen

        if not payload.create_if_missing:
            raise KeyError(
                f"No workspace for identity {identity.key}. Pass create_if_missing=true to create."
            )

        create_name = (payload.name or "").strip() or default_workspace_create_name(source_path)
        create_payload = WorkspaceCreate(
            name=create_name,
            path=str(source_path),
            default_branch=payload.default_branch,
            session_prefix=payload.session_prefix,
            target=payload.target,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=payload.remote_cwd,
            remote_reconnect=payload.remote_reconnect,
            allow_duplicate=payload.allow_duplicate,
        )
        return self.create_workspace(create_payload)

    def update_workspace(self, workspace_id: str, payload: WorkspaceUpdate) -> Workspace:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        update_kwargs: dict[str, Any] = {}
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise ValueError("Workspace name cannot be empty")
            update_kwargs["name"] = name
        if payload.path is not None:
            from claude_hub.services.workspace_identity import (
                canonical_workspace_path,
                reject_relative_local_path,
            )

            new_path = payload.path.strip()
            if not new_path:
                raise ValueError("Local workspace dir cannot be empty")
            reject_relative_local_path(new_path, field="path")
            source_path = Path(new_path).expanduser().resolve()
            if not source_path.exists() or not source_path.is_dir():
                raise ValueError(f"Local workspace dir does not exist: {source_path}")
            update_kwargs["path"] = canonical_workspace_path(str(source_path))
        if payload.default_branch is not None:
            branch = payload.default_branch.strip()
            if not branch:
                raise ValueError("Default branch cannot be empty")
            update_kwargs["default_branch"] = branch
        if payload.remote_cwd is not None:
            from claude_hub.services.workspace_identity import normalize_remote_cwd

            value = payload.remote_cwd.strip()
            update_kwargs["remote_cwd"] = normalize_remote_cwd(value) if value else None
        if payload.remote_reconnect is not None:
            update_kwargs["remote_reconnect"] = payload.remote_reconnect
        if payload.resident_agent_enabled is not None:
            update_kwargs["resident_agent_enabled"] = payload.resident_agent_enabled
        if payload.resident_agent_paused is not None:
            update_kwargs["resident_agent_paused"] = payload.resident_agent_paused
        if payload.resident_agent_interval_minutes is not None:
            if payload.resident_agent_interval_minutes < 1:
                raise ValueError("resident_agent_interval_minutes must be >= 1")
            update_kwargs["resident_agent_interval_minutes"] = (
                payload.resident_agent_interval_minutes
            )
        if payload.resident_agent_directive is not None:
            directive = payload.resident_agent_directive.strip()
            update_kwargs["resident_agent_directive"] = directive or None
        if payload.resident_agent_periodic_tasks is not None:
            update_kwargs["resident_agent_periodic_tasks"] = self._normalize_periodic_tasks(
                payload.resident_agent_periodic_tasks
            )
        if payload.resident_agent_type is not None:
            update_kwargs["resident_agent_type"] = payload.resident_agent_type
        if payload.resident_agent_env is not None:
            update_kwargs["resident_agent_env"] = dict(payload.resident_agent_env)
        if payload.resident_agent_solo_mode is not None:
            update_kwargs["resident_agent_solo_mode"] = payload.resident_agent_solo_mode
        if payload.resident_agent_master_mode is not None:
            update_kwargs["resident_agent_master_mode"] = payload.resident_agent_master_mode
        if payload.resident_agent_title is not None:
            title = payload.resident_agent_title.strip()
            update_kwargs["resident_agent_title"] = title or None
        if payload.resident_agent_target is not None:
            update_kwargs["resident_agent_target"] = payload.resident_agent_target
        if payload.resident_agent_remote_profile_id is not None:
            profile_id = payload.resident_agent_remote_profile_id.strip()
            update_kwargs["resident_agent_remote_profile_id"] = profile_id or None
        if payload.resident_agent_cwd is not None:
            cwd = payload.resident_agent_cwd.strip()
            update_kwargs["resident_agent_cwd"] = cwd or None
        if payload.resident_agent_remote_reconnect is not None:
            update_kwargs["resident_agent_remote_reconnect"] = (
                payload.resident_agent_remote_reconnect
            )

        if update_kwargs and {"path", "remote_cwd"} & update_kwargs.keys():
            from claude_hub.services.workspace_identity import (
                DuplicateWorkspaceError,
                workspace_identity_for_fields,
            )

            effective = workspace.model_copy(update=update_kwargs)
            new_identity = workspace_identity_for_fields(
                path=effective.path,
                target=effective.target,
                remote_profile_id=effective.remote_profile_id,
                remote_cwd=effective.remote_cwd,
            )
            for match in self._workspaces_for_identity_key(new_identity.key):
                if match.id != workspace_id:
                    raise DuplicateWorkspaceError(new_identity.key, [match])

        # Resident launch-config invalidation
        # ------------------------------------
        # The resident's agent_type/env/solo_mode are LAUNCH-TIME properties: they
        # are only applied on the CREATE path (inside the EnsureWorkspaceAgentRequest
        # in _run_resident_agent). The reuse path re-drives whatever live session is
        # tracked by resident_agent_session_id and does NOT rebuild the request, so a
        # config change here would otherwise be silently ignored while a session is
        # alive — worst case claude->terminal keeps prompting the stale claude session
        # forever. To make any of type/env/solo_mode changes actually take effect, we
        # clear resident_agent_session_id (and drop the old ManagedSession row) so the
        # next tick recreates the resident with the new launch config.
        #
        # Tab teardown: delete_session / delete_workspace tear down the old tab via
        # `await ttyd_manager.delete_tab(...)` — but BOTH are async and update_workspace
        # is SYNC, so we cannot await an async teardown here. Per design we therefore do
        # NOT call delete_tab from this sync path; instead we only drop the ManagedSession
        # row, which makes the old tab a session-less orphan that the existing
        # _prune_orphan_workspace_tabs reconciler (run on the monitor loop) cleans up.
        # This keeps sync code sync-safe and reuses the established orphan-tab pruner.
        #
        # Disable teardown: when resident_agent_enabled flips True -> False in this
        # update, we tear the resident down the SAME way (clear the pointer + drop the
        # ManagedSession so the orphan-tab pruner removes the tab) and additionally
        # reset resident_agent_last_run_at so a future re-enable starts clean. This is
        # the ENABLE master switch: OFF means "stop AND tear down", no orphan left
        # running. PAUSE (resident_agent_paused) deliberately does NOT come through
        # here — pausing keeps resident_agent_session_id and the ManagedSession intact
        # so the user can still open the resident terminal and chat manually; it only
        # stops automatic scheduling (handled in _resident_agent_due).
        disabling_resident = (
            workspace.resident_agent_enabled is True
            and update_kwargs.get("resident_agent_enabled") is False
        )
        old_resident_session_id = workspace.resident_agent_session_id
        if old_resident_session_id is not None and (
            disabling_resident or self._resident_launch_config_changed(workspace, update_kwargs)
        ):
            update_kwargs["resident_agent_session_id"] = None
            self.sessions.pop(old_resident_session_id, None)
            if disabling_resident:
                update_kwargs["resident_agent_last_run_at"] = None

        if not update_kwargs:
            return workspace

        updated = workspace.model_copy(update={**update_kwargs, "updated_at": self._runtime_now()})
        # Recompute the UI next-run hint whenever anything that feeds it may have
        # changed (interval, enable, pause, or last_run reset on disable). This is
        # advisory only; the authoritative trigger is _resident_agent_due.
        updated = updated.model_copy(
            update={
                "resident_agent_next_run_at": self._resident_next_run_at(
                    updated, updated.resident_agent_last_run_at
                )
            }
        )
        self.workspaces[workspace_id] = updated
        self._save_state()
        return updated
