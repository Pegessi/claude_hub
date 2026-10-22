"""Session/task cleanup entry points and conversation-id updates."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _SessionCleanupMixin(_WorkspaceManagerBase):
    _CLEANUP_ALLOWED_SESSION_ROLES = frozenset(
        {WorkspaceSessionRole.ORCHESTRATOR, WorkspaceSessionRole.WORKER}
    )

    async def delete_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        async with self.workspace_mutation_lock(session.workspace_id):
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            blocking = self._non_terminal_tasks_referencing_session(session_id)
            if blocking:
                raise RuntimeError("Cannot delete an agent with queued, working, or review tasks")

            pump_lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
            async with pump_lock:
                session = self.sessions.get(session_id)
                if session is None:
                    raise KeyError(session_id)
                try:
                    await self._delete_session_locked(session)
                except BaseException:
                    self._quarantine_failed_session_deletion(session_id)
                    raise

    async def archive_session(self, workspace_id: str, session_id: str) -> ManagedSession:
        return await self._set_session_archived(workspace_id, session_id, archived=True)

    async def unarchive_session(self, workspace_id: str, session_id: str) -> ManagedSession:
        return await self._set_session_archived(workspace_id, session_id, archived=False)

    async def _set_session_archived(
        self, workspace_id: str, session_id: str, *, archived: bool
    ) -> ManagedSession:
        async with self.workspace_mutation_lock(workspace_id):
            pump_lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
            async with pump_lock:
                session = self.sessions.get(session_id)
                if session is None or session.workspace_id != workspace_id:
                    raise KeyError(session_id)
                if session.session_kind != SessionKind.CHAT:
                    raise ValueError(
                        "Session archive supports Chat only; use tab archive for terminals"
                    )
                if session.archived == archived:
                    return session
                if self._non_terminal_tasks_referencing_session(session_id) or (
                    self._session_raw_task_bindings_block_cleanup(session)
                ):
                    raise RuntimeError(
                        "Cannot archive or restore an agent with queued, working, or review tasks"
                    )
                cleanup = asyncio.create_task(
                    (
                        self._archive_session_locked(session)
                        if archived
                        else self._unarchive_session_locked(session)
                    ),
                    name=f"workspace-session-archive-{session_id}",
                )
                try:
                    await self._await_lifecycle_cleanup(cleanup)
                except BaseException:
                    if archived and not self.sessions[session_id].archived:
                        self._quarantine_failed_session_deletion(session_id)
                    raise
                return self.sessions[session_id]

    async def cleanup_task_session(self, task_id: str) -> TaskCleanupResult:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)

        if not self._workspace_owns_task(task):
            return TaskCleanupResult(
                task_id=task.id,
                session_id=None,
                action="skipped",
                reason="initiator task does not own managed sessions",
            )

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
