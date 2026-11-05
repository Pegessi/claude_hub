"""Runtime observations only; Task recovery belongs to monitoring.liveness."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _StatusRefreshMixin(_WorkspaceManagerBase):
    # All dispatch variants have distinct first lines in the terminal input box.
    _PROMPT_PREFIX_INITIAL_DISPATCH = "New workspace task assigned."
    _PROMPT_PREFIX_REVIEW = "Review workspace task."
    _PROMPT_PREFIX_CONTINUE = "Continue workspace task from review."
    _PROMPT_PREFIX_HARD_RECOVERY = "⚠️  Your previous context was automatically cleared because the"

    async def _refresh_session_statuses(
        self,
        workspace_id: Optional[str] = None,
    ) -> None:
        sessions = self._monitor_refresh_sessions(workspace_id)
        # The status fetch is one batched I/O call shared by every session;
        # the per-session refresh below is split out so the background monitor
        # can bound each session independently.
        statuses = await self._collect_session_agent_statuses(sessions)
        changed = False
        for session in sessions:
            changed = (
                await self._refresh_one_session_status(
                    session,
                    statuses,
                )
                or changed
            )

        if changed:
            self._save_state()

    def _monitor_refresh_sessions(self, workspace_id: Optional[str]) -> list[ManagedSession]:
        """Sessions a status refresh round covers, in stable iteration order."""
        return [
            session
            for session in self.sessions.values()
            if not session.archived
            and (workspace_id is None or session.workspace_id == workspace_id)
        ]

    async def _collect_session_agent_statuses(
        self, sessions: list[ManagedSession]
    ) -> dict[str, Any]:
        """Batch-fetch runtime agent statuses keyed by terminal tab id."""
        tab_ids = [
            session.tab_id for session in sessions if session.session_kind != SessionKind.CHAT
        ]
        if not tab_ids:
            return {}
        return {
            status.tab_id: status
            for status in await ttyd_manager.list_tab_agent_statuses(tab_ids=tab_ids)
        }

    async def _refresh_one_session_status(
        self,
        session: ManagedSession,
        statuses: dict[str, Any],
    ) -> bool:
        """Refresh one session. Returns whether persisted state changed.

        This is the per-item body of ``_refresh_session_statuses`` (same
        ordering and arguments). Keeping it a separate coroutine lets the
        background monitor time out one hung session without cancelling the
        rest of the round.
        """
        changed = False
        session_id = session.id
        if session.status in {ManagedSessionStatus.DONE, ManagedSessionStatus.ERROR}:
            return changed
        if session.session_kind == SessionKind.CHAT:
            return self._refresh_chat_session_status(session) or changed
        status = statuses.get(session.tab_id)
        if not status:
            return changed
        live = self.sessions.get(session_id)
        if live is None or live.tab_id != session.tab_id or live.created_at != session.created_at:
            return changed
        session = live
        next_status = self._map_runtime_status(status)
        runtime_status = status.status
        if next_status == ManagedSessionStatus.STOPPED and self._is_spawn_grace_period(session):
            return changed

        current_task_id = session.current_task_id or session.task_id
        update: dict[str, Any] = {
            "status": next_status,
            "runtime_status": runtime_status,
            "current_task_id": current_task_id,
            "updated_at": status.sampled_at,
        }
        if status.last_changed_at:
            update["last_activity_at"] = status.last_changed_at

        self.sessions[session_id] = session.model_copy(update=update)
        return True

    def _current_monitor_entities(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> tuple[ManagedSession, WorkspaceTask] | None:
        """Return the exact live monitor owners, rejecting deletion or reuse."""
        live = self.sessions.get(session.id)
        live_task = self.tasks.get(task.id)
        if (
            self.workspaces.get(session.workspace_id) is None
            or live is None
            or live.workspace_id != session.workspace_id
            or live.tab_id != session.tab_id
            or live.created_at != session.created_at
            or live_task is None
            or live_task.workspace_id != task.workspace_id
            or live_task.created_at != task.created_at
            or not self._workspace_owns_task(live_task)
        ):
            return None
        return live, live_task

    def _expected_pending_prompt_prefixes(
        self, session: ManagedSession, task: WorkspaceTask
    ) -> list[str]:
        """Input signatures only; timing and recovery belong to liveness."""
        return (
            [self._PROMPT_PREFIX_REVIEW, self._PROMPT_PREFIX_HARD_RECOVERY]
            if session.role == WorkspaceSessionRole.REVIEWER
            else [
                self._PROMPT_PREFIX_INITIAL_DISPATCH,
                self._PROMPT_PREFIX_CONTINUE,
                self._PROMPT_PREFIX_HARD_RECOVERY,
            ]
        )
