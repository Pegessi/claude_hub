"""Workspace board projection and tab metadata synchronization."""

from typing import Literal

from claude_hub.models.schemas import (
    TaskArchiveClosure,
    TaskExecutionControl,
    TaskLifecycle,
    TaskProgressState,
    redact_managed_sessions_for_public,
)
from claude_hub.services import mr_status_cache as mr_cache_mod
from claude_hub.services.board_pagination import paginate_board_tasks
from claude_hub.services.task_brief import build_task_brief_payload
from claude_hub.services.task_dependencies import task_dependency_blockers
from claude_hub.services.task_lifecycle import (
    ChatTabFacts,
    LifecycleFacts,
    derive_task_lifecycle,
)
from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.ttyd_manager import get_tab_native_runtime_snapshot
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
            lifecycle = derive_task_lifecycle(facts)
            mr_state, mr_label = self._task_delivery_mr_projection(task)
            if mr_state is not None:
                lifecycle = lifecycle.model_copy(
                    update={
                        "delivery_mr_state": mr_state,
                        "delivery_mr_label": mr_label,
                    }
                )
            lifecycles[task.id] = lifecycle
        return lifecycles

    def _task_delivery_mr_urls(self, task: WorkspaceTask) -> list[str]:
        """Extract MR URLs exclusively from explicit delivery artifacts/completion evidence (never title/prompt)."""
        texts: list[str | None] = []
        if task.latest_progress is not None:
            texts.extend(task.latest_progress.artifact_refs)
            if task.latest_progress.state != TaskProgressState.STARTED:
                texts.append(task.latest_progress.summary)
        for report in self.reports.values():
            if report.task_id != task.id or report.review_cycle != task.review_cycle:
                continue
            texts.extend(report.artifact_refs)
            if report.state in {
                AgentReportState.READY_FOR_REVIEW,
                AgentReportState.COMPLETED,
                AgentReportState.REVIEW_PASSED,
            }:
                texts.extend((report.message, report.message_zh, report.message_en))
        return mr_cache_mod.extract_codebase_mr_urls(texts)

    def _task_delivery_mr_projection(
        self, task: WorkspaceTask
    ) -> tuple[Literal["open", "merged"] | None, str | None]:
        urls = self._task_delivery_mr_urls(task)
        if not urls:
            return None, None
        snapshots = mr_cache_mod.mr_status_cache.observe(urls)
        open_urls = [
            url for url in urls if (snap := snapshots.get(url)) is not None and snap.state == "open"
        ]
        if open_urls:
            return "open", mr_cache_mod.format_mr_badge_label(open_urls)
        if all((snap := snapshots.get(url)) is not None and snap.state == "merged" for url in urls):
            return "merged", mr_cache_mod.format_mr_badge_label(urls)
        return None, None

    def _task_executor_is_busy(self, task: WorkspaceTask) -> bool:
        if task.review_requested_at and task.review_completed_at is None:
            return True
        if task.execution_control == TaskExecutionControl.WORKSPACE:
            worker = self.sessions.get(task.session_id) if task.session_id else None
            if worker is not None and (
                worker.status == ManagedSessionStatus.SPAWNING
                or worker.runtime_status == AgentRuntimeStatus.WORKING
            ):
                return True
            reviewer = self.sessions.get(task.review_session_id) if task.review_session_id else None
            if reviewer is not None and (
                reviewer.status == ManagedSessionStatus.SPAWNING
                or reviewer.runtime_status == AgentRuntimeStatus.WORKING
            ):
                return True
            return False
        if (
            task.runtime_observation is not None
            and task.runtime_observation.execution_epoch == task.execution_epoch
            and task.runtime_observation.status == "active"
        ):
            return True
        if task.source and task.source.tab_id:
            native = get_tab_native_runtime_snapshot(task.source.tab_id)
            if native is not None and getattr(native, "status", None) == AgentRuntimeStatus.WORKING:
                return True
        return False

    def _reconcile_natural_done_signals(self, workspace_id: str) -> bool:
        """Persist natural DONE transitions (archived originating Chat or confirmed merged MR)."""
        changed = False
        now = self._runtime_now()
        for task_id, task in list(self.tasks.items()):
            if task.workspace_id != workspace_id:
                continue
            if task.status not in {
                WorkspaceTaskStatus.TODO,
                WorkspaceTaskStatus.WORKING,
                WorkspaceTaskStatus.REVIEW,
            }:
                continue
            if (
                task.execution_control == TaskExecutionControl.INITIATOR
                and not task.execution_released
                and task.source is not None
                and task.source.tab_id
            ):
                facts = self._task_chat_tab_facts(task)
                if facts is not None and facts.exists and facts.archived:
                    obs_seq = (
                        task.runtime_observation.stream_sequence
                        if task.runtime_observation is not None
                        else -1
                    )
                    closure = TaskArchiveClosure(
                        tab_id=task.source.tab_id,
                        execution_epoch=task.execution_epoch,
                        previous_status=task.status,
                        last_stream_sequence=obs_seq,
                        closed_at=now,
                    )
                    self.tasks[task_id] = apply_task_update(
                        task,
                        {
                            "status": WorkspaceTaskStatus.DONE,
                            "completed_at": now,
                            "closed_reason": "chat_archived",
                            "archive_closure": closure,
                            "updated_at": now,
                        },
                        cause="chat_archive",
                    )
                    changed = True
                    continue

            if task.status in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}:
                if self._task_executor_is_busy(task):
                    continue
                mr_state, _ = self._task_delivery_mr_projection(task)
                if mr_state == "merged":
                    self.tasks[task_id] = apply_task_update(
                        task,
                        {
                            "status": WorkspaceTaskStatus.DONE,
                            "completed_at": now,
                            "closed_reason": "mr_merged",
                            "archive_closure": None,
                            "updated_at": now,
                        },
                        cause="mr_merged",
                    )
                    changed = True

        if changed:
            self._save_workspace_state(workspace_id)
        return changed

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
        self._reconcile_natural_done_signals(workspace_id)
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

    def get_board_overview(self, *, done_per_workspace: int = 8) -> dict[str, Any]:
        """Lightweight read-only cross-workspace triage projection (all open + recent done)."""
        workspaces = self.list_workspaces()
        all_tasks: list[WorkspaceTask] = []
        all_reports: list[AgentReport] = []
        all_sessions: list[ManagedSession] = []
        all_lifecycles: dict[str, TaskLifecycle] = {}
        attention_counts: dict[str, int] = {}

        for ws in workspaces:
            self._reconcile_natural_done_signals(ws.id)
            ws_tasks = [
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
                if task.workspace_id == ws.id
            ]
            page_tasks, _ = paginate_board_tasks(ws_tasks, limit=done_per_workspace, cursor=None)
            lifecycles = self.task_lifecycles(page_tasks)
            attention_counts[ws.id] = sum(1 for lc in lifecycles.values() if lc.attention)
            task_ids = {t.id for t in page_tasks}
            reports = [
                report
                for report in self.latest_reports_per_task_for_workspace(ws.id)
                if report.task_id in task_ids
            ]
            all_tasks.extend(page_tasks)
            all_reports.extend(reports)
            all_sessions.extend(self.sessions_for_workspace(ws.id, include_archived=False))
            all_lifecycles.update(lifecycles)

        return {
            "workspaces": workspaces,
            "tasks": all_tasks,
            "reports": all_reports,
            "sessions": redact_managed_sessions_for_public(all_sessions),
            "task_lifecycles": all_lifecycles,
            "attention_counts": attention_counts,
        }

    def get_task_brief(self, workspace_id: str, task_id: str) -> dict[str, Any]:
        """Return a bounded (<2 KB) AI handoff and context-recovery brief for one Task."""
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)
        task = self.require_workspace_task(workspace_id, task_id)
        lifecycle = self.task_lifecycles([task])[task.id]
        reports = self.reports_for_task(workspace_id, task_id)
        events = self.list_task_mailbox_events(
            workspace_id, task_id, since_sequence=0, subtree=False
        )
        ws_tasks = {t.id: t for t in self.tasks.values() if t.workspace_id == workspace_id}
        blockers = task_dependency_blockers(ws_tasks, task)
        return build_task_brief_payload(
            workspace=workspace,
            task=task,
            lifecycle=lifecycle,
            reports=reports,
            events=events,
            blockers=blockers,
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
