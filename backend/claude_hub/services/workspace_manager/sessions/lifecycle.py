"""Cancellation-safe retirement and task deletion lifecycle."""

import claude_hub.services.agent_stream as agent_stream
import claude_hub.services.managed_chat as managed_chat
from claude_hub.services.agent_stream import tailer as stream_lifecycle
from claude_hub.services.task_dependencies import TaskHasDependentsError
from claude_hub.services.task_graph import TaskHasDescendantsError, task_has_descendants
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _SessionLifecycleMixin(_WorkspaceManagerBase):
    reports: "dict[str, AgentReport]"

    @staticmethod
    async def _await_lifecycle_cleanup(task: "asyncio.Task[Any]") -> None:
        """Finish committed cleanup even if its caller is cancelled."""

        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        task.result()
        if cancellation is not None:
            raise cancellation

    def _quarantine_failed_session_deletion(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        update: dict[str, Any] = {
            "status": ManagedSessionStatus.NEEDS_INPUT,
            "runtime_status": AgentRuntimeStatus.ATTENTION,
            "updated_at": self._runtime_now(),
        }
        if session.session_kind == SessionKind.CHAT:
            update["chat_paused"] = True
        self.sessions[session_id] = session.model_copy(update=update)
        try:
            self._save_state()
        except Exception:
            logger.exception(
                "Failed to persist retained session after deletion failure session_id=%s",
                session_id,
            )

    @staticmethod
    async def _retire_session_resources(session: ManagedSession) -> None:
        """Release the discoverable tab before committing ownership removal."""

        await ttyd_manager.delete_tab(session.tab_id)

    async def _cleanup_deleted_session(
        self, session: ManagedSession, *, provider_fenced: bool
    ) -> None:
        """Remove retained stream and delivery evidence after durable deletion."""

        from claude_hub.services.agent_stream import discard_session_stream

        discard_kwargs: dict[str, Any] = {"acknowledged_call_ids": set(session.delivered_call_ids)}
        if provider_fenced:
            discard_kwargs["provider_fenced"] = True
        await discard_session_stream(
            session.workspace_id,
            session.id,
            **discard_kwargs,
        )

    async def _delete_session_locked(self, session: ManagedSession) -> None:
        """Delete one session while workspace and pump locks are held."""

        from claude_hub.services.agent_stream import fence_session_stream

        provider_fenced = await fence_session_stream(session.id)
        if session.session_kind == SessionKind.CHAT:
            managed_chat.validate_delivery_state_discard(
                session.workspace_id,
                session.id,
                acknowledged_call_ids=set(session.delivered_call_ids),
                provider_fenced=provider_fenced,
            )

        # Resource retirement is retryable while the durable session still
        # exists. Evidence remains untouched until ownership removal commits.
        await self._retire_session_resources(session)

        snapshot = self._snapshot_report_intake_workspace(session.workspace_id)
        self.sessions.pop(session.id, None)
        self._clear_retired_session_pointers(session)
        try:
            self._save_state()
        except BaseException:
            self._restore_report_intake_workspace(session.workspace_id, snapshot)
            raise

        cleanup = asyncio.create_task(
            self._cleanup_deleted_session(session, provider_fenced=provider_fenced),
            name=f"workspace-session-delete-{session.id}",
        )
        await self._await_lifecycle_cleanup(cleanup)

    async def _archive_session_locked(self, session: ManagedSession) -> None:
        """Release runtime under workspace/pump locks, retaining all evidence."""
        provider_fenced = await agent_stream.fence_session_stream(session.id)
        managed_chat.validate_delivery_state_discard(
            session.workspace_id,
            session.id,
            acknowledged_call_ids=set(session.delivered_call_ids),
            provider_fenced=provider_fenced,
        )
        # The headless tab owns navigation metadata; the managed stream owns
        # the provider transport. Archive both without deleting either store.
        tab = await ttyd_manager.archive_tab(session.tab_id)
        if tab is None:
            raise RuntimeError("Managed Chat backing tab is missing; cannot archive session")
        await stream_lifecycle.stop_session_stream(session.id)
        # Provider shutdown may durably capture its conversation id. Preserve
        # that latest identity instead of restoring the pre-fence snapshot.
        session = self.sessions[session.id]
        snapshot = self._snapshot_report_intake_workspace(session.workspace_id)
        now = self._runtime_now()
        self.sessions[session.id] = session.model_copy(
            update={
                "archived": True,
                "archived_at": now,
                "chat_paused": True,
                "status": ManagedSessionStatus.STOPPED,
                "runtime_status": AgentRuntimeStatus.OFFLINE,
                "updated_at": now,
            }
        )
        self._clear_retired_session_pointers(session)
        try:
            self._save_state()
        except BaseException:
            self._restore_report_intake_workspace(session.workspace_id, snapshot)
            raise

    def _clear_retired_session_pointers(self, session: ManagedSession) -> None:
        workspace = self.workspaces.get(session.workspace_id)
        if workspace is None:
            return
        update: dict[str, Any] = {}
        if workspace.dispatcher_session_id == session.id:
            update["dispatcher_session_id"] = None
        if workspace.resident_agent_session_id == session.id:
            update.update(
                resident_agent_session_id=None,
                resident_agent_last_run_at=None,
                resident_agent_enabled=False,
            )
        if update:
            update["updated_at"] = self._runtime_now()
            self.workspaces[workspace.id] = workspace.model_copy(update=update)

    async def _unarchive_session_locked(self, session: ManagedSession) -> None:
        # Keep the managed session unavailable until the backing tab is restored.
        tab = await ttyd_manager.unarchive_tab(session.tab_id)
        if tab is None:
            raise RuntimeError("Managed Chat backing tab is missing; cannot restore session")
        self.sessions[session.id] = session.model_copy(
            update={
                "archived": False,
                "archived_at": None,
                "chat_paused": False,
                "status": ManagedSessionStatus.IDLE,
                "runtime_status": AgentRuntimeStatus.IDLE,
                "updated_at": self._runtime_now(),
            }
        )
        try:
            self._save_state()
        except BaseException:
            self.sessions[session.id] = session
            raise
        stream_lifecycle.release_session_stream_fence(session.id)
        try:
            await managed_chat.start(self.sessions[session.id])
        except Exception:
            logger.exception("Best-effort managed Chat restart failed session_id=%s", session.id)

    @staticmethod
    def _effective_agent_target(
        workspace: Workspace,
        payload: EnsureWorkspaceAgentRequest,
    ) -> ExecutionTarget:
        """Omitted agent target defaults to LOCAL (not workspace.target)."""
        return payload.target or ExecutionTarget.LOCAL

    def delete_task(self, task_id: str) -> None:
        from claude_hub.services.workspace_manager._task_execution import TaskExecutionConflict

        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if not self._workspace_owns_task(task) and any(
            session.task_id == task_id or session.current_task_id == task_id
            for session in self.sessions.values()
        ):

            raise TaskExecutionConflict("initiator_task_has_managed_session_reference")
        if task_has_descendants(self.tasks, task.workspace_id, task):
            raise TaskHasDescendantsError(
                f"Cannot delete task {task_id}: it has child tasks. "
                "Delete or reparent descendants first."
            )

        dependents = [
            item.id for item in self.tasks.values() if task_id in item.depends_on_task_ids
        ]
        if dependents:
            raise TaskHasDependentsError(
                f"Cannot delete task {task_id}: required by {', '.join(dependents)}. "
                "Remove dependency edges or delete dependent tasks first."
            )

        workspace_id = task.workspace_id
        snapshot = self._snapshot_report_intake_workspace(workspace_id)

        if task.system_internal and task.internal_kind == "feedback_reaper":
            try:
                self._feedback_store().abandon_summary_run(
                    workspace_id,
                    task.id,
                    reason="task_deleted",
                    now=self._runtime_now(),
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
            if self._workspace_owns_task(task) and (
                session.task_id == task_id or session.current_task_id == task_id
            ):
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
                        "updated_at": self._runtime_now(),
                    }
                )

        try:
            self._save_workspace_state(workspace_id)
        except Exception:
            self._restore_report_intake_workspace(workspace_id, snapshot)
            raise
        self._task_activity_cursors.pop(task_id, None)
