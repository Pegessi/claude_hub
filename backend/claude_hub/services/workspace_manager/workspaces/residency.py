"""Resident-agent scheduling, due checks, and wake-up delivery."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _ResidentAgentMixin(_WorkspaceManagerBase):
    def request_resident_run(self, workspace_id: str) -> Workspace:
        """Flag the resident to run on the next monitor tick (manual "run now").

        Stamps ``resident_agent_run_requested_at``; ``_resident_agent_due`` then
        returns True on the next tick (within WORKSPACE_MONITOR_INTERVAL_SECONDS),
        and ``_run_resident_agent`` consumes/clears the flag when the cycle fires.
        The override respects Enable but bypasses Pause and the interval/activity
        gates — an explicit user request is a deliberate one-off.

        Raises ``KeyError`` if the workspace is missing, ``ValueError`` if the
        resident is not enabled (nothing to run). If the resident session is
        currently WORKING the flag still stays set and simply fires on the next
        idle tick (the WORKING-skip in ``_run_resident_agent`` defers it without
        clearing the request), so the manual run is not lost.
        """
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)
        if not workspace.resident_agent_enabled:
            raise ValueError("Resident agent is not enabled for this workspace")
        updated = workspace.model_copy(
            update={
                "resident_agent_run_requested_at": self._runtime_now(),
                "updated_at": self._runtime_now(),
            }
        )
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
    def _normalize_periodic_tasks(
        tasks: Optional[list["ResidentPeriodicTask"]],
    ) -> list["ResidentPeriodicTask"]:
        """Normalize a periodic-task list: trim text, drop empties, keep order.

        Each entry keeps its client-supplied id (or the model default) so the UI
        can stably identify rows across edits; entries whose text is blank after
        trimming are dropped. ``None`` means "not provided" and yields an empty
        list (safe default; callers only pass ``None`` on create where absence
        means no periodic tasks).
        """
        result: list["ResidentPeriodicTask"] = []
        for task in tasks or []:
            text = (task.text or "").strip()
            if not text:
                continue
            result.append(task.model_copy(update={"text": text}))
        return result

    def _resident_next_run_at(
        self, workspace: "Workspace", last_run: Optional[datetime]
    ) -> Optional[datetime]:
        """Compute the resident's next overdue-backstop wake time for the UI.

        This mirrors the backstop arm in ``_resident_agent_due``: ``last_run +
        interval + stable jitter``. Returns ``None`` when the resident is
        disabled or paused (no automatic scheduling) or when ``last_run`` is
        ``None`` (bootstrap — it is due immediately, so there is no future time
        to show). The activity fast-path can still wake the resident EARLIER
        than this; the UI copy makes that clear. This value is advisory only —
        the authoritative trigger remains ``_resident_agent_due``.
        """
        if not workspace.resident_agent_enabled or workspace.resident_agent_paused:
            return None
        if last_run is None:
            return None
        interval_seconds = max(1, workspace.resident_agent_interval_minutes) * 60
        jitter = self._resident_jitter_seconds(workspace, interval_seconds)
        return last_run + timedelta(seconds=interval_seconds + jitter)

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
        """True when this workspace saw a real task OUTCOME or linked report.

        Activity means terminal/progress timestamps on non-``system_internal``
        tasks (``completed_at``, ``reviewed_at``, ``human_accepted_at``) or a
        report whose ``task_id`` resolves to such a task in this workspace.
        Freshly-proposed TODO tasks (all outcome fields ``None``) do not count.
        Reports with ``task_id=None``, dangling ids, or ``system_internal`` tasks
        do not count. When ``since`` is ``None`` any qualifying outcome/report
        counts.
        """

        def _after(value: Optional[datetime]) -> bool:
            if value is None:
                return False
            return since is None or value > since

        for task in self.tasks.values():
            if task.workspace_id != workspace_id or task.system_internal:
                continue
            if (
                _after(task.completed_at)
                or _after(task.reviewed_at)
                or _after(task.human_accepted_at)
            ):
                return True
        for report in self.reports.values():
            if report.workspace_id != workspace_id:
                continue
            if not _after(report.created_at):
                continue
            task_id = report.task_id
            if not task_id:
                continue
            linked_task = self.tasks.get(task_id)
            if (
                linked_task is None
                or linked_task.workspace_id != workspace_id
                or linked_task.system_internal
            ):
                continue
            return True
        return False

    def _resident_agent_due(self, workspace: Workspace, now: datetime) -> bool:
        """Return True when a resident agent should run this tick.

        Event-gated ("Option C") trigger. The cheap 5s monitor tick is only the
        wakeup; whether the resident actually fires is decided here:

        * **Disabled** -> never due.
        * **Manual run-now** -> if ``resident_agent_run_requested_at`` is set (the
          run-now endpoint stamped it), fire on the next tick regardless of the
          interval or activity gate. It still respects Enable (a disabled
          resident is never driven) but overrides Pause: an explicit user "run
          now" is an intentional one-off even while auto-scheduling is paused.
          The flag is cleared by ``_run_resident_agent`` once the cycle fires.
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

        Net: ``due = enabled AND (run_requested OR last_run is None
                                  OR (activity_since AND elapsed >= debounce)
                                  OR elapsed >= interval + jitter)``.
        """
        if not workspace.resident_agent_enabled:
            return False
        # Manual run-now override: an explicit user request fires on the next
        # tick even while paused (it is a deliberate one-off), but never while
        # disabled (guarded above). Checked BEFORE the paused early-return.
        if workspace.resident_agent_run_requested_at is not None:
            return True
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
        now = self._runtime_now()
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
        if workspace.resident_agent_session_id and existing is None:
            self._reconcile_workspace_session_pointers(workspace.id)
            workspace = self.workspaces[workspace.id]
        if existing is not None and existing.archived:
            return
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
            # NOTE: reuse_existing is False on purpose: the generic reuse path only
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
        # We also clear resident_agent_run_requested_at (the manual run-now flag
        # is a one-off — consume it now that the cycle is firing) and recompute
        # resident_agent_next_run_at from the new last_run for the UI countdown.
        now = self._runtime_now()
        stamped = workspace.model_copy(
            update={
                "resident_agent_session_id": session.id,
                "resident_agent_last_run_at": now,
                "resident_agent_run_requested_at": None,
            }
        )
        self.workspaces[workspace.id] = stamped.model_copy(
            update={
                "resident_agent_next_run_at": self._resident_next_run_at(stamped, now),
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
        prompt = self._runtime_resident_prompt(workspace, base_url, session.id)
        await self.send_session_message(session.id, prompt)
