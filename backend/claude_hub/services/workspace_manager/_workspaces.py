"""Workspace CRUD and the background monitor."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


def build_resident_agent_prompt(workspace: "Workspace", base_url: str, session_id: str) -> str:
    """Build the self-drive prompt for a workspace's resident agent.

    The resident agent is a standing, self-driven Claude session that wakes on a
    fixed interval. It does NOT receive normal task dispatch. Each cycle it (a)
    performs any recurring tasks named in the user directive, (b) maintains the
    workspace lesson catalog, and (c) proposes new tasks in TODO status for the
    user to approve — it never auto-starts work or performs destructive actions.

    When ``resident_agent_master_mode`` is enabled, the resident acts as an
    autonomous ORCHESTRATOR: each cycle it reads the board, creates tasks
    (default ``reviewed`` mode, so a reviewer agent vets the work), dispatches
    them to existing orchestrator worker sessions via an explicit
    ``target_session_id``, and performs the final acceptance itself once review
    has passed (PATCH ``status=done``) or sends the work back via ``continue``.
    It NEVER writes code and NEVER creates or deletes orchestrator worker
    sessions (the backend may still spin up an ephemeral reviewer on its own).
    """
    directive = (workspace.resident_agent_directive or "").strip()
    directive_block = (
        f"User directive (recurring tasks / focus):\n{directive}"
        if directive
        else "User directive: (none provided — focus on lesson maintenance and task proposals)."
    )
    ws = workspace.id
    if workspace.resident_agent_master_mode:
        return _build_resident_master_prompt(workspace, base_url, session_id, directive_block)
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


def _build_resident_master_prompt(
    workspace: "Workspace",
    base_url: str,
    session_id: str,
    directive_block: str,
) -> str:
    """Master-mode resident prompt: an autonomous ORCHESTRATOR / product-owner.

    Each cycle the resident reads the board, creates a small number of tasks
    (default ``reviewed`` mode — a reviewer agent vets the work), dispatches them
    to EXISTING orchestrator worker sessions via an explicit
    ``target_session_id``, and performs the final acceptance itself once review
    has passed (PATCH ``status=done``) or sends the work back via ``continue``.
    It NEVER writes code and NEVER creates or deletes orchestrator worker
    sessions (the backend may auto-spawn an ephemeral reviewer to vet a task —
    that is allowed; the resident just never provisions worker agents itself).
    """
    ws = workspace.id
    reports_endpoint = f"{base_url}/api/workspaces/sessions/{session_id}/reports"
    return (
        "You are this workspace's RESIDENT MASTER agent — an autonomous ORCHESTRATOR and "
        "product-owner. You do NOT write code yourself. Each wake-up you run ONE bounded "
        "orchestration pass, then STOP. Do not loop until the next wake-up: assess, create and "
        "dispatch a small number of tasks, accept finished ones, post a heartbeat, exit.\n\n"
        f"Workspace id: {ws}\n"
        f"API base URL: {base_url}\n"
        f"This resident session id: {session_id}\n\n"
        f"{directive_block}\n\n"
        "## Each cycle, in order\n\n"
        "1. Read the board to understand current state:\n"
        f"     curl -sS {base_url}/api/workspaces/{ws}/board\n"
        "   Inspect `tasks` (id, title, status, session_id, task_mode) and `sessions` (id, role, "
        "status, runtime_status). Review recent task outcomes and the user directive to decide "
        "what the workspace still needs next. Iterate on the requirements — refine the goal, do "
        "not just repeat finished work.\n\n"
        "2. Find the EXISTING worker agents you may dispatch to. A usable worker is a session "
        'with `role == "orchestrator"` whose `status` is not stopped and whose `runtime_status` '
        "is idle or working (NOT offline and NOT attention).\n"
        "   - If there are NO such orchestrator sessions, you MUST NOT create one and you MUST "
        "NOT start any task. Instead, degrade to proposal-only: create any tasks you think are "
        "needed in TODO status (step 3, but WITHOUT the start call) and say so in your heartbeat "
        '("no worker agents available — proposed N tasks for the user to start"). Then skip to '
        "step 6.\n\n"
        "3. Create the tasks you deem necessary this cycle. Create AT MOST 3 "
        "tasks per cycle to avoid a runaway backlog:\n"
        f"     curl -sS -X POST {base_url}/api/workspaces/{ws}/tasks "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","prompt":"detailed instructions for the worker"}\'\n'
        "   Leave task_mode at its default (reviewed): when the worker finishes, a "
        "reviewer agent vets the work before it returns to you for final acceptance. "
        "The backend reuses an idle reviewer or briefly spins one up on its own — that "
        "is fine and is NOT you creating an agent. Record each new task id from the "
        "response.\n\n"
        "4. Dispatch each task you just created onto an EXISTING orchestrator worker from step 2 "
        "(prefer an idle one; queuing behind a busy orchestrator is fine). Always pass an "
        "explicit target_session_id so the backend never auto-creates an agent:\n"
        f"     curl -sS -X POST {base_url}/api/workspaces/tasks/<task_id>/start "
        "-H 'Content-Type: application/json' "
        '-d \'{"target_session_id":"<existing-orchestrator-session-id>"}\'\n'
        '   target_session_id MUST be a session whose role is "orchestrator". NEVER target the '
        "resident, dispatcher, or reviewer sessions. If the start call returns an error (e.g. the "
        "agent went offline), leave the task in TODO and note it in the heartbeat — do NOT retry "
        "against a different role and do NOT create an agent.\n\n"
        "5. Accept reviewed work. Re-read the board and, for each task YOU created (this cycle "
        "or a previous one — track their ids; do NOT touch human-created tasks), wait until "
        'review has finished: that is when `status == "review" AND '
        "`human_acceptance_requested_at` is set AND `human_accepted_at` is null. (While the "
        "reviewer is still working the task is in review with no `human_acceptance_requested_at` "
        "yet — leave it alone and check again next cycle.) When a task reaches that "
        "awaiting-acceptance state, read the worker's latest report/output and the reviewer's "
        "verdict for that task, then validate it against what you asked for:\n"
        "   - If satisfactory, accept it:\n"
        f"       curl -sS -X PATCH {base_url}/api/workspaces/tasks/<task_id> "
        "-H 'Content-Type: application/json' "
        '-d \'{"status":"done"}\'\n'
        "   - If NOT satisfactory, send it back to the SAME worker with concrete feedback (this "
        "does NOT spawn a new worker; it re-dispatches to the original agent):\n"
        f"       curl -sS -X POST {base_url}/api/workspaces/tasks/<task_id>/continue "
        "-H 'Content-Type: application/json' "
        '-d \'{"message":"what is wrong and what to fix"}\'\n'
        "   Only ever accept or continue tasks YOU created. Never accept or modify tasks a human "
        "created or dispatched.\n\n"
        "6. (Optional, as before) Maintain workspace lessons when genuinely justified:\n"
        f"     curl -sS {base_url}/api/workspaces/{ws}/lessons\n"
        f"     curl -sS -X POST {base_url}/api/workspaces/{ws}/lessons "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","summary":"one-line takeaway",'
        '"applies_when":["when this applies"],"do":"...","avoid":"...","tags":["tag"]}\'\n\n'
        "## Hard constraints (never violate)\n"
        "- NEVER create or delete orchestrator worker sessions. Never call any agent-spawn "
        "endpoint to add a worker, never DELETE a session. You may ONLY dispatch to "
        "already-existing orchestrator sessions. If none exist, you propose tasks and stop. "
        "(The backend may auto-spawn a short-lived REVIEWER to vet a reviewed task — that is the "
        "backend's doing and is allowed; you never provision agents yourself.)\n"
        "- ALWAYS pass an explicit target_session_id when starting a task, and NEVER start a task "
        "when no orchestrator session exists (so the backend never auto-creates a default "
        "worker agent).\n"
        "- NEVER write code, edit files, commit, merge, push, or run destructive git commands. "
        "You are an orchestrator; the worker agents do the implementation.\n"
        "- Only accept/continue tasks YOU created; never touch human-driven tasks. Only accept a "
        "task after review has finished (`human_acceptance_requested_at` is set).\n\n"
        "## Heartbeat report (REQUIRED at the END of EVERY cycle)\n"
        "Post one workspace-level heartbeat summarizing this cycle. task_id is omitted for a "
        "workspace-level heartbeat:\n"
        f"    curl -sS -X POST {reports_endpoint} "
        "-H 'Content-Type: application/json' "
        '-d \'{"state":"working","message":"Resident orchestrator cycle: <summary>",'
        '"message_en":"Resident orchestrator cycle: <summary>",'
        '"message_zh":"常驻编排周期：<摘要>"}\'\n'
        "Replace <summary> with: requirements identified, tasks created, tasks dispatched (and to "
        "which agents), and tasks accepted this cycle (or 'no actionable work this cycle'). "
        "Always include message_en (concise English) and message_zh (concise 中文).\n\n"
        "When this cycle's bounded orchestration pass is done and the heartbeat is posted, STOP "
        "and wait for the next wake-up."
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
        resident_title = (payload.resident_agent_title or "").strip() or None
        resident_cwd = (payload.resident_agent_cwd or "").strip() or None
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
            resident_agent_paused=payload.resident_agent_paused,
            resident_agent_interval_minutes=interval_minutes,
            resident_agent_session_id=None,
            resident_agent_directive=directive,
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

        updated = workspace.model_copy(update={**update_kwargs, "updated_at": _wm._now()})
        self.workspaces[workspace_id] = updated
        self._save_state()
        return updated

    @staticmethod
    def _resident_launch_config_changed(
        workspace: "Workspace", update_kwargs: dict[str, Any]
    ) -> bool:
        """True when this update changes the resident's launch config to a DIFFERENT value.

        Launch config = agent_type / env / solo_mode (the three properties applied only
        when the resident session/tab is created). We compare the proposed new value
        (present in ``update_kwargs`` only when the caller supplied a non-None field)
        against the current workspace value, so a no-op write of the same value does NOT
        trigger a needless recreation.
        """
        for field in (
            "resident_agent_type",
            "resident_agent_env",
            "resident_agent_solo_mode",
            "resident_agent_target",
            "resident_agent_remote_profile_id",
            "resident_agent_cwd",
            "resident_agent_remote_reconnect",
        ):
            if field in update_kwargs and update_kwargs[field] != getattr(workspace, field):
                return True
        return False

    @staticmethod
    def _resident_jitter_seconds(workspace: "Workspace", interval_seconds: int) -> int:
        """Stable, deterministic per-workspace jitter in ``[0, interval_seconds)``.

        Spreads resident wake-ups across the interval so that many workspaces
        sharing the same interval do not all fire on the same monitor tick (the
        classic thundering-herd / synchronized-poll problem). The offset is
        derived from a SHA-256 of the workspace id, NOT Python's builtin
        ``hash()`` (which is randomized per process via PYTHONHASHSEED) nor any
        time/random source — so it is identical across processes and restarts,
        and unit-testable.
        """
        if interval_seconds <= 0:
            return 0
        digest = hashlib.sha256(workspace.id.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % interval_seconds

    def _workspace_activity_since(self, workspace_id: str, since: Optional[datetime]) -> bool:
        """True when this workspace saw a real task OUTCOME or external progress.

        "Activity" deliberately means a real *outcome* to learn from, NOT mere
        task creation/update. For a NON ``system_internal`` task we look ONLY at
        its terminal/progress timestamps — ``completed_at``, ``reviewed_at`` and
        ``human_accepted_at`` — and treat the task as activity when any of those
        is newer than ``since``. A freshly-proposed TODO task has all three set
        to ``None``, so it does NOT trip the gate. This is what prevents the
        resident self-retrigger loop: the resident's prompt makes it PROPOSE
        tasks via ``POST /tasks`` (non-``system_internal`` tasks whose
        ``created_at``/``updated_at`` are newer than the just-stamped
        ``last_run``); gating on outcomes rather than creations means those
        proposals never re-arm the activity fast-path.

        A non-resident report created after ``since`` also counts as activity:
        worker agents post reports (the resident's prompt uses ``/tasks`` and
        ``/lessons``, not ``/sessions/{id}/reports``), so a fresh report is
        genuine progress. As defense-in-depth we still exclude reports and tasks
        whose ``session_id`` matches the workspace's
        ``resident_agent_session_id`` in case a future prompt makes the resident
        emit them. ``system_internal`` tasks are excluded entirely. When
        ``since`` is ``None`` any existing outcome/report counts.
        """
        workspace = self.workspaces.get(workspace_id)
        resident_session_id = workspace.resident_agent_session_id if workspace is not None else None

        def _after(value: Optional[datetime]) -> bool:
            if value is None:
                return False
            return since is None or value > since

        for task in self.tasks.values():
            if task.workspace_id != workspace_id or task.system_internal:
                continue
            if resident_session_id is not None and task.session_id == resident_session_id:
                # Defense-in-depth: ignore tasks owned by the resident itself so
                # its own proposals can never count as activity.
                continue
            # Gate on real outcomes only (completed/reviewed/accepted), never on
            # creation/update — a freshly-proposed TODO has these all None.
            if (
                _after(task.completed_at)
                or _after(task.reviewed_at)
                or _after(task.human_accepted_at)
            ):
                return True
        for report in self.reports.values():
            if report.workspace_id != workspace_id:
                continue
            if resident_session_id is not None and report.session_id == resident_session_id:
                # The resident does not post reports, but guard anyway so a
                # future prompt change cannot let it re-trigger itself.
                continue
            if _after(report.created_at):
                return True
        return False

    def _resident_agent_due(self, workspace: Workspace, now: datetime) -> bool:
        """Return True when a resident agent should run this tick.

        Event-gated ("Option C") trigger. The cheap 5s monitor tick is only the
        wakeup; whether the resident actually fires is decided here:

        * **Disabled** -> never due.
        * **Bootstrap** (``last_run_at is None``) -> due once. The first run
          establishes the activity/timer baseline; it does not fire instantly on
          every empty boot because once it runs the baseline is stamped.
        * **Activity-gated fast path** -> if there has been real workspace
          activity since the last run AND at least
          ``RESIDENT_ACTIVITY_DEBOUNCE_SECONDS`` have elapsed, fire now. The
          debounce floor coalesces bursts so a flurry of task updates triggers at
          most one run per debounce window instead of one per event.
        * **Overdue backstop** -> even with no activity, fire once the full
          ``resident_agent_interval_minutes`` (plus a stable per-workspace jitter
          offset) have elapsed, so idle-but-enabled workspaces still get a
          periodic pass. This is the legacy fixed-interval path, demoted to a
          backstop.

        Net: ``due = enabled AND (last_run is None
                                  OR (activity_since AND elapsed >= debounce)
                                  OR elapsed >= interval + jitter)``.
        """
        if not workspace.resident_agent_enabled:
            return False
        # Paused = keep the session alive for manual chat, but stop automatic
        # scheduling (no self-drive runs). disabled OR paused -> not due.
        if workspace.resident_agent_paused:
            return False

        last_run = workspace.resident_agent_last_run_at
        if last_run is None:
            # Bootstrap: run once to establish the baseline.
            return True

        elapsed = now - last_run
        interval_seconds = max(1, workspace.resident_agent_interval_minutes) * 60

        # Activity-gated fast path: react to real work, but no more than once per
        # debounce window.
        debounce = timedelta(seconds=RESIDENT_ACTIVITY_DEBOUNCE_SECONDS)
        if elapsed >= debounce and self._workspace_activity_since(workspace.id, last_run):
            return True

        # Overdue backstop: fixed interval + stable jitter keeps idle workspaces
        # ticking and desynchronizes wake-ups across workspaces.
        jitter = self._resident_jitter_seconds(workspace, interval_seconds)
        backstop = timedelta(seconds=interval_seconds + jitter)
        return elapsed >= backstop

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
        """Create or reuse the workspace's resident agent and self-drive it.

        The resident is created with the workspace's configured
        ``resident_agent_type`` / ``resident_agent_env`` / ``resident_agent_solo_mode``
        (parity with normal workspace agents) rather than a hardcoded CLAUDE
        session with no env.

        TERMINAL edge case: a TERMINAL resident is a plain user shell with no LLM
        agent listening, so the self-drive prompt is pointless/harmful (it would
        be dumped as literal shell input). For TERMINAL we still create/track an
        openable tab and advance ``resident_agent_last_run_at`` (so it does not
        churn every tick), but we do NOT send the self-drive prompt on either the
        create path (suppressed in _build_session_bootstrap_prompt) or the reuse
        path (guarded below). CLAUDE/CURSOR/CODEX are CLI LLM agents and receive
        the same curl-based resident prompt as normal.
        """
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
            # cycle and must NOT be sent a second copy below. (For a TERMINAL
            # resident the bootstrap is suppressed to an empty string, so no
            # prompt is sent on create either.)
            session = await self.ensure_workspace_agent(
                workspace.id,
                EnsureWorkspaceAgentRequest(
                    agent_type=workspace.resident_agent_type,
                    env=dict(workspace.resident_agent_env or {}),
                    solo_mode=workspace.resident_agent_solo_mode,
                    role=WorkspaceSessionRole.RESIDENT,
                    reuse_existing=False,
                    title=(workspace.resident_agent_title or f"{workspace.name} Resident"),
                    target=workspace.resident_agent_target,
                    cwd=workspace.resident_agent_cwd,
                    remote_profile_id=workspace.resident_agent_remote_profile_id,
                    remote_cwd=workspace.resident_agent_cwd,
                    remote_reconnect=workspace.resident_agent_remote_reconnect,
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

        # A TERMINAL resident has no LLM agent to drive: advance the timer (done
        # above) and keep the tab, but never send the self-drive prompt.
        if workspace.resident_agent_type == AgentType.TERMINAL:
            return

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
            build_resident_agent_prompt(workspace, base_url, session.id),
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
