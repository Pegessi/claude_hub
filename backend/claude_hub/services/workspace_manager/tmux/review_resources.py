"""Review resource selection, tab cleanup and historical report parsing."""

from claude_hub.services.workspace_manager._constants import (
    ORPHAN_TAB_PRUNE_GRACE_SECONDS,
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    ExecutionTarget,
    GoalPacketStatus,
    ManagedSession,
    ManagedSessionStatus,
    Optional,
    ReviewDecision,
    SessionKind,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
    datetime,
    logger,
    state_policy,
    ttyd_manager,
    uuid,
)

from .._base import _WorkspaceManagerBase


class _ReviewResourcesMixin(_WorkspaceManagerBase):
    async def _prune_orphan_workspace_tabs(self, workspace_id: str) -> int:
        """Delete managed terminal tabs for this workspace that have no backing
        ManagedSession.

        Terminal tabs (ttyd_manager) and managed sessions (workspace_manager)
        persist to separate state files. When a session is removed without its
        tab — e.g. historical temporary-reviewer lifecycle desync — the tab is
        left behind with ``workspace_id``/``workspace_role`` set but no session
        row. Such orphan tabs show up in the terminal tab bar yet are absent
        from the "Manage Agents" board (which lists sessions), so they cannot
        respond to dispatch and cannot be deleted from that UI.

        This reconciler removes those orphans. It is deliberately conservative:

        * Only tabs that carry this ``workspace_id`` are considered, so manual
          terminal tabs (no ``workspace_id``) are never touched.
        * A tab whose id backs a live session is kept.
        * A tab created within ``ORPHAN_TAB_PRUNE_GRACE_SECONDS`` is kept, to
          avoid racing the gap between ``create_tab`` and session registration
          in ``_create_managed_session``.

        Returns the number of orphan tabs pruned.
        """
        live_tab_ids = {
            session.tab_id
            for session in self.sessions.values()
            if session.workspace_id == workspace_id
        }
        now = self._runtime_now()
        orphan_tab_ids: list[str] = []
        for tab in ttyd_manager.list_tabs():
            if tab.workspace_id != workspace_id:
                continue
            if tab.id in live_tab_ids:
                continue
            # A direct Chat may carry workspace_id purely as display/grouping
            # metadata (Hub subagents inherit their parent's workspace). It has
            # no ManagedSession by design, so "no backing session" is not an
            # orphan condition; only role-bearing managed tabs are reaped.
            if tab.workspace_role is None and not tab.managed_session_id:
                continue
            created_at = tab.created_at
            if created_at is not None:
                # Tab timestamps and _now() are both naive local datetimes.
                age_seconds = (now - created_at).total_seconds()
                if age_seconds < ORPHAN_TAB_PRUNE_GRACE_SECONDS:
                    continue
            orphan_tab_ids.append(tab.id)

        pruned = 0
        for tab_id in orphan_tab_ids:
            logger.warning(
                "Pruning orphan managed terminal tab with no backing session "
                "workspace_id=%s tab_id=%s",
                workspace_id,
                tab_id,
            )
            try:
                await ttyd_manager.delete_tab(tab_id)
                pruned += 1
            except Exception:
                logger.exception(
                    "Failed to prune orphan managed tab workspace_id=%s tab_id=%s",
                    workspace_id,
                    tab_id,
                )
        return pruned

    def _is_fallback_reaper_report(self, report: AgentReport) -> bool:
        if report.review_decision != ReviewDecision.REQUEST:
            return False
        blob = " ".join(
            part
            for part in (report.message, report.message_en, report.review_reason)
            if isinstance(part, str)
        ).lower()
        return "fallback reaper" in blob or "background dispatcher" in blob

    def _review_cycle_has_reviewer_activity(self, task_id: str, review_cycle: int) -> bool:
        """True when review_started or a terminal reviewer verdict exists for the cycle."""

        reviewer_states = {
            AgentReportState.REVIEW_STARTED,
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }
        for report in self.reports.values():
            if report.task_id != task_id:
                continue
            if int(report.review_cycle or 0) != review_cycle:
                continue
            if report.state in reviewer_states:
                return True
        return False

    def _first_available_reviewer(
        self,
        workspace_id: str,
        *,
        target: ExecutionTarget,
        cwd: str,
        remote_profile_id: str | None = None,
        remote_cwd: str | None = None,
        session_kind: SessionKind | None = None,
    ) -> Optional[ManagedSession]:
        """Internal review dispatch only — not CLI agent-create reuse.

        Hub review routing requires the worker's execution directory as well
        as target; sharing a repo does not mean sharing a worktree. CLI reuse uses
        ``_find_compatible_workspace_agent`` instead.
        """

        from claude_hub.services.workspace_identity import normalize_remote_cwd, resolve_path

        wanted_remote_cwd = (
            normalize_remote_cwd(remote_cwd) if target == ExecutionTarget.REMOTE else None
        )
        reviewers = [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.REVIEWER
            and (session_kind is None or session.session_kind == session_kind)
            and not session.archived
            and not session.chat_paused
            and session.status != ManagedSessionStatus.STOPPED
            and session.runtime_status == AgentRuntimeStatus.IDLE
            and not session.task_id
            and not session.current_task_id
            and not self._reviewer_has_active_task_binding(session)
            and session.target == target
            and (
                target != ExecutionTarget.LOCAL
                or resolve_path(session.workspace_path or self.workspaces[workspace_id].path)
                == resolve_path(cwd)
            )
            and (
                target != ExecutionTarget.REMOTE
                or (
                    (session.remote_profile_id or remote_profile_id) == remote_profile_id
                    and normalize_remote_cwd(session.remote_cwd) == wanted_remote_cwd
                )
            )
        ]
        if not reviewers:
            return None
        return sorted(reviewers, key=lambda session: (session.ephemeral, session.created_at))[0]
