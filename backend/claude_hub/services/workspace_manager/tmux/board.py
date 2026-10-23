"""Workspace board projection and tab metadata synchronization."""

from claude_hub.models.schemas import TaskExecutionControl, TaskLifecycle
from claude_hub.services.task_dependencies import task_dependency_blockers
from claude_hub.services.task_lifecycle import (
    ChatTabFacts,
    LifecycleFacts,
    derive_task_lifecycle,
)
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403
from claude_hub.services.workspace_state_policy import TERMINAL_REVIEW_STATES


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

    def task_lifecycles(self, tasks: list[WorkspaceTask]) -> dict[str, TaskLifecycle]:
        """Derive one lifecycle per Task from current records and owner liveness."""

        now = self._runtime_now()
        open_tasks = {
            task.id: task
            for task in tasks
            if task.status not in {WorkspaceTaskStatus.DONE, WorkspaceTaskStatus.FAILED}
        }
        current_verdicts: dict[str, AgentReport] = {}
        for report in self.reports.values():
            task = open_tasks.get(report.task_id or "")
            if task is None or report.state not in TERMINAL_REVIEW_STATES:
                continue
            if report.review_cycle != task.review_cycle:
                continue
            current = current_verdicts.get(task.id)
            if current is None or report.created_at > current.created_at:
                current_verdicts[task.id] = report
        lifecycles: dict[str, TaskLifecycle] = {}
        for task in tasks:
            if task.id not in open_tasks:
                lifecycles[task.id] = derive_task_lifecycle(LifecycleFacts(task=task, now=now))
                continue
            worker = self.sessions.get(task.session_id) if task.session_id else None
            blocked_by = None
            if worker is not None and task.status == WorkspaceTaskStatus.QUEUED:
                holder_id = worker.task_id or worker.current_task_id
                holder = self.tasks.get(holder_id) if holder_id else None
                if holder and holder.id != task.id and holder.status != WorkspaceTaskStatus.DONE:
                    blocked_by = holder.id
            verdict = current_verdicts.get(task.id)
            facts = LifecycleFacts(
                task=task,
                latest_review_state=verdict.state if verdict else None,
                worker_session=worker,
                reviewer_session=(
                    self.sessions.get(task.review_session_id) if task.review_session_id else None
                ),
                chat_tab=self._task_chat_tab_facts(task),
                dependencies_blocked=bool(
                    task.depends_on_task_ids and task_dependency_blockers(self.tasks, task)
                ),
                blocked_by_task_id=blocked_by,
                now=now,
            )
            lifecycles[task.id] = derive_task_lifecycle(facts)
        return lifecycles

    def _task_chat_tab_facts(self, task: WorkspaceTask) -> ChatTabFacts | None:
        tab_id = task.source.tab_id if task.source else None
        if not tab_id or task.execution_control != TaskExecutionControl.INITIATOR:
            return None
        tab = ttyd_manager.get_tab(tab_id)
        if tab is None:
            return ChatTabFacts(exists=False)
        return ChatTabFacts(exists=True, archived=bool(tab.archived))

    async def get_board(
        self,
        workspace_id: str,
        *,
        include_archived: bool = False,
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
        sessions = self.sessions_for_workspace(workspace_id, include_archived=include_archived)
        return WorkspaceBoard(
            workspace=self.workspaces[workspace_id],
            tasks=page_tasks,
            sessions=sessions,
            reports=reports,
            markdown_documents=self.markdown_documents_for_workspace(workspace_id),
            snapshot_path=str(self.snapshot_path(workspace_id)),
            tasks_pagination=tasks_pagination,
            task_lifecycles=self.task_lifecycles(page_tasks),
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
