"""Workspace board projection and tab metadata synchronization."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _WorkspaceBoardMixin(_WorkspaceManagerBase):
    def _queued_count(self, session_id: str) -> int:
        return len(
            [
                task
                for task in self.tasks.values()
                if task.session_id == session_id and task.status == WorkspaceTaskStatus.QUEUED
            ]
        )

    def _with_assignment_summary(self, session: ManagedSession) -> ManagedSession:
        current_task_id = session.current_task_id or session.task_id
        return session.model_copy(
            update={
                "task_id": session.task_id,
                "current_task_id": current_task_id,
                "queued_count": self._queued_count(session.id),
            }
        )

    async def get_board(
        self,
        workspace_id: str,
        *,
        tasks_limit: int | None = None,
        tasks_cursor: str | None = None,
    ) -> WorkspaceBoard:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        await self._refresh_session_statuses(workspace_id)
        self._reconcile_task_report_statuses(workspace_id)
        self._reconcile_workspace_session_pointers(workspace_id)
        self._cleanup_stale_orchestrator_assignments(workspace_id)
        await self._prune_orphan_workspace_tabs(workspace_id)
        self._sync_workspace_tab_metadata(workspace_id)
        tasks = [
            (
                task.model_copy(
                    update={
                        "prompt": (
                            "System-managed Feedback Reaper task. Inspect its reports and "
                            "summary-run audit for lifecycle details."
                        )
                    }
                )
                if task.system_internal and task.internal_kind == "feedback_reaper"
                else task
            )
            for task in self.tasks.values()
            if task.workspace_id == workspace_id
        ]

        from claude_hub.services.board_pagination import paginate_board_tasks

        page_tasks, tasks_pagination = paginate_board_tasks(
            tasks,
            limit=tasks_limit,
            cursor=tasks_cursor,
        )
        task_ids = {task.id for task in page_tasks}
        reports = [
            report
            for report in self.latest_reports_per_task_for_workspace(workspace_id)
            if report.task_id in task_ids
        ]
        sessions = self.sessions_for_workspace(workspace_id)
        return WorkspaceBoard(
            workspace=self.workspaces[workspace_id],
            tasks=page_tasks,
            sessions=sessions,
            reports=reports,
            markdown_documents=self.markdown_documents_for_workspace(workspace_id),
            snapshot_path=str(self.snapshot_path(workspace_id)),
            tasks_pagination=tasks_pagination,
        )

    def _sync_workspace_tab_metadata(self, workspace_id: str) -> None:
        for session in self._sessions_for_workspace_raw(workspace_id):
            self._sync_session_tab_metadata(session)

    def _sync_session_tab_metadata(self, session: ManagedSession) -> None:
        workspace = self.workspaces.get(session.workspace_id)
        if not workspace:
            return
        ttyd_manager.set_tab_workspace_metadata(
            tab_id=session.tab_id,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=session.role,
            managed_session_id=session.id,
        )
