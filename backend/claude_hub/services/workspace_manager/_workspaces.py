"""Workspace CRUD and the background monitor."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


def build_resident_agent_prompt(workspace: "Workspace", base_url: str) -> str:
    """Build the self-drive prompt for a workspace's resident agent.

    The resident agent is a standing, self-driven Claude session that wakes on a
    fixed interval. It does NOT receive normal task dispatch. Each cycle it (a)
    performs any recurring tasks named in the user directive, (b) maintains the
    workspace lesson catalog, and (c) proposes new tasks in TODO status for the
    user to approve — it never auto-starts work or performs destructive actions.
    """
    directive = (workspace.resident_agent_directive or "").strip()
    directive_block = (
        f"User directive (recurring tasks / focus):\n{directive}"
        if directive
        else "User directive: (none provided — focus on lesson maintenance and task proposals)."
    )
    ws = workspace.id
    return (
        "You are this workspace's RESIDENT self-driven maintenance agent. You wake up "
        "periodically to keep the workspace healthy. You are NOT assigned a single task; "
        "do not wait for a dispatch. Work autonomously this cycle, then stop.\n\n"
        f"Workspace id: {ws}\n"
        f"API base URL: {base_url}\n\n"
        f"{directive_block}\n\n"
        "Each cycle, do the following, in order:\n"
        "1. If the user directive specifies recurring or periodic tasks, perform them now "
        "(read-only investigation, status checks, summaries, etc.).\n"
        "2. Review the most recent workspace task records and MAINTAIN LESSONS:\n"
        f"   - List current lessons: curl -sS {base_url}/api/workspaces/{ws}/lessons\n"
        "   - Create or merge a genuinely new, reusable lesson (only when justified):\n"
        f"     curl -sS -X POST {base_url}/api/workspaces/{ws}/lessons "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","summary":"required one-line takeaway",'
        '"applies_when":["when this lesson applies"],'
        '"do":"what to do","avoid":"what to avoid","tags":["tag"]}\'\n'
        "   - Archive a stale or contradicted lesson (only when justified):\n"
        f"     curl -sS -X DELETE {base_url}/api/workspaces/{ws}/lessons/<lesson_id>\n"
        "3. PROPOSE new tasks for the user to decide on. Create them in TODO status only — "
        "do NOT start them and do NOT spawn agents:\n"
        f"   curl -sS -X POST {base_url}/api/workspaces/{ws}/tasks "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","prompt":"..."}\'\n'
        "   Newly created tasks stay in TODO; the user chooses whether to start them.\n\n"
        "Hard constraints: do NOT merge branches, push, force-push, delete files, or take any "
        "destructive action. Do NOT auto-start proposed tasks. Keep changes to lessons and task "
        "proposals only. When this cycle's work is done, stop and wait for the next wake-up."
    )


class _WorkspacesMixin:
    # Initialized in _StateMixin.__init__; annotation-only declaration (no value,
    # so no runtime attribute is created) lets mypy type the rebind below.
    _monitor_task: "asyncio.Task[None] | None"
    # Initialized in _StateMixin.__init__; annotation-only declarations let mypy
    # type the dict-comprehension rebinds in delete_workspace below.
    tasks: "dict[str, WorkspaceTask]"
    reports: "dict[str, AgentReport]"

    def list_workspaces(self) -> list[Workspace]:
        return sorted(self.workspaces.values(), key=lambda item: item.created_at)

    def start_background_monitor(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._background_monitor_loop())

    async def stop_background_monitor(self) -> None:
        if not self._monitor_task:
            return
        self._monitor_task.cancel()
        try:
            await self._monitor_task
        except asyncio.CancelledError:
            pass
        finally:
            self._monitor_task = None

    async def _background_monitor_loop(self) -> None:
        while True:
            try:
                await self._refresh_session_statuses(run_auto_continue=True)
                for workspace_id in list(self.workspaces):
                    await self.dispatch_workspace(workspace_id, refresh_sessions=False)
                await self._tick_resident_agents()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace background monitor failed")
            await asyncio.sleep(WORKSPACE_MONITOR_INTERVAL_SECONDS)

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        return self.workspaces.get(workspace_id)

    def create_workspace(self, payload: WorkspaceCreate) -> Workspace:
        source_path = Path(payload.path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"Local workspace dir does not exist: {source_path}")
        if payload.target == ExecutionTarget.REMOTE:
            if not payload.remote_profile_id:
                raise ValueError("Remote workspace requires remote_profile_id")
            if not remote_profile_manager.get_profile(payload.remote_profile_id):
                raise ValueError(f"Remote profile not found: {payload.remote_profile_id}")

        workspace_id = str(uuid.uuid4())
        now = _wm._now()
        prefix = payload.session_prefix or _slug(payload.name)
        interval_minutes = max(1, payload.resident_agent_interval_minutes)
        directive = (payload.resident_agent_directive or "").strip() or None
        workspace = Workspace(
            id=workspace_id,
            name=payload.name,
            path=str(source_path),
            default_branch=payload.default_branch,
            session_prefix=prefix,
            dispatcher_session_id=None,
            target=payload.target,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=payload.remote_cwd,
            remote_reconnect=payload.remote_reconnect,
            resident_agent_enabled=payload.resident_agent_enabled,
            resident_agent_interval_minutes=interval_minutes,
            resident_agent_session_id=None,
            resident_agent_directive=directive,
            resident_agent_last_run_at=None,
            created_at=now,
            updated_at=now,
        )
        self.workspaces[workspace_id] = workspace
        self._save_state()
        return workspace

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
            new_path = payload.path.strip()
            if not new_path:
                raise ValueError("Local workspace dir cannot be empty")
            resolved = Path(new_path).expanduser().resolve()
            if not resolved.exists() or not resolved.is_dir():
                raise ValueError(f"Local workspace dir does not exist: {resolved}")
            update_kwargs["path"] = str(resolved)
        if payload.default_branch is not None:
            branch = payload.default_branch.strip()
            if not branch:
                raise ValueError("Default branch cannot be empty")
            update_kwargs["default_branch"] = branch
        if payload.remote_cwd is not None:
            value = payload.remote_cwd.strip()
            update_kwargs["remote_cwd"] = value or None
        if payload.remote_reconnect is not None:
            update_kwargs["remote_reconnect"] = payload.remote_reconnect
        if payload.resident_agent_enabled is not None:
            update_kwargs["resident_agent_enabled"] = payload.resident_agent_enabled
        if payload.resident_agent_interval_minutes is not None:
            if payload.resident_agent_interval_minutes < 1:
                raise ValueError("resident_agent_interval_minutes must be >= 1")
            update_kwargs["resident_agent_interval_minutes"] = (
                payload.resident_agent_interval_minutes
            )
        if payload.resident_agent_directive is not None:
            directive = payload.resident_agent_directive.strip()
            update_kwargs["resident_agent_directive"] = directive or None

        if not update_kwargs:
            return workspace

        updated = workspace.model_copy(update={**update_kwargs, "updated_at": _wm._now()})
        self.workspaces[workspace_id] = updated
        self._save_state()
        return updated

    def _resident_agent_due(self, workspace: Workspace, now: datetime) -> bool:
        """Return True when a resident agent should run this tick.

        Due immediately when never run before; otherwise due once at least
        ``resident_agent_interval_minutes`` have elapsed since the last run.
        """
        if not workspace.resident_agent_enabled:
            return False
        last_run = workspace.resident_agent_last_run_at
        if last_run is None:
            return True
        interval = timedelta(minutes=max(1, workspace.resident_agent_interval_minutes))
        return now - last_run >= interval

    async def _tick_resident_agents(self) -> None:
        """Fire due resident agents across all workspaces.

        Wrapped per-workspace so one failure cannot abort the rest of the tick.
        Skips a workspace whose resident session is currently working.
        """
        now = _wm._now()
        for workspace_id in list(self.workspaces):
            workspace = self.workspaces.get(workspace_id)
            if workspace is None or not self._resident_agent_due(workspace, now):
                continue
            try:
                await self._run_resident_agent(workspace)
            except Exception:
                logger.exception("Resident agent tick failed for workspace_id=%s", workspace_id)

    async def _run_resident_agent(self, workspace: Workspace) -> None:
        existing = self.sessions.get(workspace.resident_agent_session_id or "")
        if existing is not None and existing.status == ManagedSessionStatus.STOPPED:
            existing = None
        if existing is not None and existing.runtime_status == AgentRuntimeStatus.WORKING:
            # Busy from a prior cycle: skip without advancing the timer so it
            # retries on the next monitor tick.
            return

        if existing is not None:
            reused = True
            session = existing
        else:
            reused = False
            # reuse_existing is False on purpose: the generic reuse path only
            # matches ORCHESTRATOR sessions, so a resident session must be
            # tracked and reused via workspace.resident_agent_session_id here.
            # NOTE: ensure_workspace_agent already sends the bootstrap prompt,
            # which for the RESIDENT role IS build_resident_agent_prompt (see
            # _prompts._build_session_bootstrap_prompt routing). So a freshly
            # created resident has already received the self-drive prompt this
            # cycle and must NOT be sent a second copy below.
            session = await self.ensure_workspace_agent(
                workspace.id,
                EnsureWorkspaceAgentRequest(
                    agent_type=AgentType.CLAUDE,
                    role=WorkspaceSessionRole.RESIDENT,
                    reuse_existing=False,
                    title=f"{workspace.name} Resident",
                ),
            )

        # Persist the session id and advance the timer BEFORE sending so that a
        # failure in send_session_message does not leave resident_agent_session_id
        # unset (which would respawn a brand-new session/tab every monitor tick)
        # nor leave last_run_at stale (which would retry immediately every tick).
        now = _wm._now()
        self.workspaces[workspace.id] = workspace.model_copy(
            update={
                "resident_agent_session_id": session.id,
                "resident_agent_last_run_at": now,
                "updated_at": now,
            }
        )
        self._save_state()

        if not reused:
            # Bootstrap already delivered the resident prompt this cycle.
            return

        base_url = (
            f"http://127.0.0.1:{session.remote_forward_port}"
            if session.remote_forward_port
            else f"http://localhost:{settings.port}"
        )
        await self.send_session_message(
            session.id,
            build_resident_agent_prompt(workspace, base_url),
        )

    async def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace and all of its in-memory and on-disk state.

        Tears down every managed session's terminal tab (unconditionally — unlike
        ``delete_session`` there is no non-DONE-task guard), purges the
        workspace's tasks/sessions/reports, removes its on-disk state directory,
        then rewrites the index.
        """
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        for session in [s for s in self.sessions.values() if s.workspace_id == workspace_id]:
            self.sessions.pop(session.id, None)
            try:
                await ttyd_manager.delete_tab(session.tab_id)
            except Exception:
                logger.exception(
                    "Failed to delete terminal tab while deleting workspace "
                    "workspace_id=%s session_id=%s tab_id=%s",
                    workspace_id,
                    session.id,
                    session.tab_id,
                )

        self.tasks = {
            task_id: task
            for task_id, task in self.tasks.items()
            if task.workspace_id != workspace_id
        }
        self.reports = {
            report_id: report
            for report_id, report in self.reports.items()
            if report.workspace_id != workspace_id
        }
        self.workspaces.pop(workspace_id, None)

        shutil.rmtree(_wm.STATE_ROOT / workspace_id, ignore_errors=True)
        self._save_state()
