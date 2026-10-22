"""Committed workspace deletion and provider-fenced resource cleanup."""

from contextlib import AsyncExitStack

import claude_hub.services.managed_chat as managed_chat
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _WorkspaceCleanupMixin(_WorkspaceManagerBase):
    async def _cleanup_deleted_workspace(
        self,
        workspace_id: str,
        sessions: list[ManagedSession],
        provider_fences: dict[str, bool],
    ) -> None:
        errors: list[BaseException] = []
        for session in sessions:
            try:
                await self._cleanup_deleted_session(
                    session,
                    provider_fenced=provider_fences.get(session.id, False),
                )
            except BaseException as exc:
                errors.append(exc)
                logger.exception(
                    "Failed to clean deleted workspace session workspace_id=%s session_id=%s",
                    workspace_id,
                    session.id,
                )
        if errors:
            raise RuntimeError(
                f"could not finish cleanup for deleted workspace {workspace_id}"
            ) from errors[0]
        try:
            shutil.rmtree(self._runtime_state_root / workspace_id)
        except FileNotFoundError:
            pass

    async def delete_workspace(self, workspace_id: str) -> None:
        """Fence sessions, durably retire the workspace, then collect resources."""

        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        async with self.workspace_mutation_lock(workspace_id):
            sessions = [
                session
                for session in self.sessions.values()
                if session.workspace_id == workspace_id
            ]
            async with AsyncExitStack() as stack:
                for session in sorted(sessions, key=lambda item: item.id):
                    lock = self._pump_locks.setdefault(session.id, asyncio.Lock())
                    await stack.enter_async_context(lock)

                from claude_hub.services.agent_stream import fence_session_stream

                provider_fences: dict[str, bool] = {}
                try:
                    for session in sessions:
                        provider_fenced = await fence_session_stream(session.id)
                        provider_fenced = provider_fenced or (
                            session.archived and session.archive_provider_fenced
                        )
                        provider_fences[session.id] = provider_fenced
                        if session.session_kind == SessionKind.CHAT:
                            managed_chat.validate_delivery_state_discard(
                                session.workspace_id,
                                session.id,
                                acknowledged_call_ids=set(session.delivered_call_ids),
                                provider_fenced=provider_fenced,
                            )

                    # Keep durable owners until every tab/resource retirement
                    # succeeds; a partial failure can then retry the remaining
                    # resources without erasing stream/delivery evidence.
                    for session in sessions:
                        await self._retire_session_resources(session)

                    snapshot = self._snapshot_report_intake_workspace(workspace_id)
                    self.sessions = {
                        session_id: session
                        for session_id, session in self.sessions.items()
                        if session.workspace_id != workspace_id
                    }
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
                    try:
                        self._save_state()
                    except BaseException:
                        self._restore_report_intake_workspace(workspace_id, snapshot)
                        raise
                except BaseException:
                    for session in sessions:
                        self._quarantine_failed_session_deletion(session.id)
                    raise

                cleanup = asyncio.create_task(
                    self._cleanup_deleted_workspace(workspace_id, sessions, provider_fences),
                    name=f"workspace-delete-{workspace_id}",
                )
                await self._await_lifecycle_cleanup(cleanup)
