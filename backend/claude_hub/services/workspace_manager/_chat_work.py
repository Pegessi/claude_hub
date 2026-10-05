"""Chat-owned work, projected from schedules, workspace tasks and reports.

No parallel task state machine or transcript injection: scheduling/dispatch/report
acceptance remain owned by the existing managers. A work id is a stable schedule
id and every execution has a durable source_work_id before any launch awaits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Literal

import claude_hub.services.workspace_manager as _wm
from claude_hub.models.schemas import (
    AgentReportCreate,
    AgentReportState,
    AgentType,
    ChatWorkCreate,
    ChatWorkExecution,
    ChatWorkReport,
    ChatWorkResult,
    ChatWorkUpdate,
    ChatWorkView,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManualTaskControlRequest,
    ScheduledTask,
    ScheduledTaskKind,
    SessionKind,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services.ttyd_manager import ttyd_manager

_ACTIVE = {
    WorkspaceTaskStatus.TODO,
    WorkspaceTaskStatus.QUEUED,
    WorkspaceTaskStatus.WORKING,
    WorkspaceTaskStatus.REVIEW,
}
_MODEL_ENV = {AgentType.CODEX: "CODEX_MODEL", AgentType.CLAUDE: "ANTHROPIC_MODEL"}


class _ChatWorkMixin:
    def _chat_work_record(self, tab_id: str, work_id: str) -> ScheduledTask:
        work = self.scheduled_tasks.get(work_id)
        if work is None or work.source_tab_id != tab_id or not work.work_kind:
            raise KeyError(work_id)
        return work

    def _chat_work_executions(self, work: ScheduledTask) -> list[WorkspaceTask]:
        return sorted(
            (
                task
                for task in self.tasks.values()
                if task.source_work_id == work.id and task.workspace_id == work.workspace_id
            ),
            key=lambda task: (task.created_at, task.id),
            reverse=True,
        )

    def _chat_work_active(self, work: ScheduledTask) -> WorkspaceTask | None:
        return next(
            (
                task
                for task in self._chat_work_executions(work)
                if task.status in _ACTIVE and not task.manual_aborted_at
            ),
            None,
        )

    def _chat_work_launch_request(self, work: ScheduledTask) -> EnsureWorkspaceAgentRequest:
        # Preserve the source provider's environment unless a named preset was
        # explicitly selected. Do not copy one provider's endpoint into another.
        source = ttyd_manager.get_tab(work.source_tab_id or "")
        if work.work_inherit_source_env:
            if source is None or source.agent_type != work.agent_type:
                raise ValueError(
                    "Source Chat launch environment is unavailable or its provider changed"
                )
            env = dict(source.env)
        else:
            env = {}
        if work.work_model:
            key = _MODEL_ENV.get(work.agent_type)
            if key is None:
                raise ValueError("Explicit model selection is supported for Claude and Codex only")
            env[key] = work.work_model
        return EnsureWorkspaceAgentRequest(
            agent_type=work.agent_type,
            cwd=work.work_cwd,
            env=env,
            env_preset=work.work_env_preset,
            ephemeral=True,
            caller_owned_ephemeral=True,
            role=WorkspaceSessionRole.ORCHESTRATOR,
            reuse_existing=False,
        )

    async def create_chat_work(self, tab_id: str, payload: ChatWorkCreate) -> ChatWorkView:
        source = ttyd_manager.get_tab(tab_id)
        if source is None or source.session_kind != SessionKind.CHAT or source.workspace_role:
            raise ValueError("Linked work requires an existing top-level Chat tab")
        if source.target != ExecutionTarget.LOCAL:
            raise ValueError("Chat-linked work currently supports local source Chats only")
        if payload.kind == "monitor" and payload.task_mode != WorkspaceTaskMode.REVIEWED:
            raise ValueError("Monitor checks use the existing internal reviewed execution mode")
        if payload.workspace_id not in self.workspaces:
            raise ValueError("Workspace not found")
        fingerprint = hashlib.sha256(
            json.dumps(
                payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        work_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"claude-hub:chat-work:{tab_id}:{payload.request_key}")
        )
        existing = self.scheduled_tasks.get(work_id)
        if existing:
            if existing.source_request_fingerprint != fingerprint:
                raise ValueError("request_key already exists with different input")
            return self.chat_work_view(self._chat_work_record(tab_id, work_id))
        agent_type = payload.agent_type or source.agent_type
        if agent_type == AgentType.TERMINAL:
            raise ValueError("Linked work requires an AI agent")
        if payload.model and agent_type not in _MODEL_ENV:
            raise ValueError("Explicit model selection is supported for Claude and Codex only")
        cwd = str(
            Path(payload.cwd or source.cwd or self.workspaces[payload.workspace_id].path).resolve()
        )
        if not Path(cwd).is_dir():
            raise ValueError("Work cwd must be an existing local directory")
        model = payload.model
        if not model and not payload.env_preset and agent_type == source.agent_type:
            model = source.env.get(_MODEL_ENV.get(agent_type, ""))
        now = _wm._now()
        work = ScheduledTask(
            id=work_id,
            name=payload.title.strip(),
            kind=ScheduledTaskKind.HUB_TASK,
            run_at=now if payload.kind == "task" else None,
            interval_seconds=payload.interval_seconds,
            next_run_at=now,
            workspace_id=payload.workspace_id,
            agent_type=agent_type,
            task_title=payload.title.strip(),
            message=payload.prompt.strip(),
            source_tab_id=tab_id,
            source_request_key=payload.request_key,
            source_request_fingerprint=fingerprint,
            work_kind=payload.kind,
            work_task_mode=payload.task_mode,
            work_cwd=cwd,
            work_model=model,
            work_env_preset=payload.env_preset,
            work_inherit_source_env=agent_type == source.agent_type and not payload.env_preset,
            created_at=now,
            updated_at=now,
        )
        # Save intent before launch; retry returns this same record even if a
        # transport disconnect occurs while the first worker is starting.
        self.scheduled_tasks[work.id] = work
        try:
            self._save_scheduled_tasks()
        except Exception:
            self.scheduled_tasks.pop(work.id, None)
            raise
        await self._fire_scheduled_task(work, now, manual=True)
        return self.chat_work_view(work)

    def list_chat_work(self, tab_id: str) -> list[ChatWorkView]:
        records = sorted(
            (
                work
                for work in self.scheduled_tasks.values()
                if work.source_tab_id == tab_id and work.work_kind
            ),
            key=lambda work: work.created_at,
            reverse=True,
        )
        return [self.chat_work_view(work) for work in records]

    def chat_work_view(self, work: ScheduledTask) -> ChatWorkView:
        executions = self._chat_work_executions(work)
        active = self._chat_work_active(work)
        latest = executions[0] if executions else None
        status: Literal[
            "running", "waiting", "paused", "stopped", "completed", "failed", "review"
        ] = "waiting"
        if work.work_stopped_at:
            status = "stopped"
        elif work.work_completed_at or (
            work.work_kind == "task" and latest and latest.status == WorkspaceTaskStatus.DONE
        ):
            status = "completed"
        elif work.work_pause_requested:
            status = "paused"
        elif active:
            status = "review" if active.status == WorkspaceTaskStatus.REVIEW else "running"
        elif work.last_status == "error" or (
            latest and latest.status == WorkspaceTaskStatus.FAILED
        ):
            status = "failed"
        elif not work.enabled:
            status = "paused"
        result = None
        rows = []
        reports_by_task: dict[str, list[Any]] = {}
        for item in self.reports.values():
            if item.workspace_id == work.workspace_id and item.task_id:
                reports_by_task.setdefault(item.task_id, []).append(item)
        for index, task in enumerate(executions):
            reports = sorted(
                reports_by_task.get(task.id, []), key=lambda report: report.created_at, reverse=True
            )
            report = (
                self.reports.get(task.chat_work_report_id)
                if task.chat_work_report_id
                else next(
                    (item for item in reports if item.chat_work_outcome is not None),
                    next((item for item in reports if item.session_id != "system"), None),
                )
            )
            summary = task.chat_work_summary or (
                report.message[:2000] if report else task.failure_reason
            )
            if index < 20:
                rows.append(
                    ChatWorkExecution(
                        task_id=task.id,
                        status=task.status.value,
                        created_at=task.created_at,
                        updated_at=task.updated_at,
                        summary=summary,
                        outcome=task.chat_work_outcome,
                    )
                )
            outcome = task.chat_work_outcome or (report.chat_work_outcome if report else None)
            if outcome in {"no_change", "completed"} and (
                report is None
                or report.state
                not in {AgentReportState.COMPLETED, AgentReportState.READY_FOR_REVIEW}
            ):
                outcome = None
            if result is not None or outcome == "no_change":
                continue
            kind = outcome
            if (
                work.work_kind == "task"
                and kind == "completed"
                and task.status != WorkspaceTaskStatus.DONE
            ):
                kind = "decision"
            if not kind:
                if task.status == WorkspaceTaskStatus.FAILED:
                    kind = "failed"
                elif report and report.state in {
                    AgentReportState.BLOCKED,
                    AgentReportState.NEEDS_INPUT,
                }:
                    kind = "decision"
                elif work.work_kind == "task" and task.status in {
                    WorkspaceTaskStatus.DONE,
                    WorkspaceTaskStatus.REVIEW,
                }:
                    kind = "completed" if task.status == WorkspaceTaskStatus.DONE else "decision"
            if kind and summary:
                result = ChatWorkResult(
                    kind=kind,
                    summary=summary,
                    task_id=task.id,
                    report_id=report.id if report else None,
                    validation=report.validation[:2000] if report and report.validation else None,
                    created_at=report.created_at if report else task.updated_at,
                )
        if result is None and work.last_status == "error":
            result = ChatWorkResult(
                kind="failed",
                summary=(work.last_error or "Launch failed")[:2000],
                created_at=work.updated_at,
            )
        return ChatWorkView(
            id=work.id,
            source_tab_id=work.source_tab_id or "",
            workspace_id=work.workspace_id or "",
            title=work.name,
            kind=work.work_kind or "task",
            status=status,
            agent_type=work.agent_type,
            model=work.work_model,
            cwd=work.work_cwd or "",
            interval_seconds=work.interval_seconds,
            next_run_at=work.next_run_at if work.enabled else None,
            run_count=work.run_count,
            active_task_id=active.id if active else None,
            latest_result=result,
            executions=rows,
            created_at=work.created_at,
            updated_at=max([work.updated_at] + [t.updated_at for t in executions[:20]]),
        )

    async def update_chat_work(
        self, tab_id: str, work_id: str, payload: ChatWorkUpdate
    ) -> ChatWorkView:
        lock = self._sched_fire_locks.setdefault(work_id, asyncio.Lock())
        async with lock:
            original = self._chat_work_record(tab_id, work_id)
            work = original.model_copy(deep=True)
            if (work.work_stopped_at or work.work_completed_at) and payload.action != "stop":
                raise ValueError("Stopped or completed work cannot be resumed or edited")
            if payload.interval_seconds is not None:
                if work.work_kind != "monitor":
                    raise ValueError("Only monitors have an interval")
                work.interval_seconds = payload.interval_seconds
                work.next_run_at = (
                    self._compute_next_run(work, _wm._now()) if work.enabled else None
                )
            if payload.prompt is not None:
                if not payload.prompt.strip():
                    raise ValueError("prompt must not be blank")
                work.message = payload.prompt.strip()
            if payload.action == "pause":
                if work.work_kind != "monitor":
                    raise ValueError(
                        "Pause applies to future monitor checks; use stop for one-shot work"
                    )
                work.enabled = False
                work.work_pause_requested = True
                work.next_run_at = None
            elif payload.action == "resume":
                if self._chat_work_active(work) and work.work_kind != "monitor":
                    raise ValueError("An execution is still active")
                if work.work_kind == "task" and any(
                    t.status == WorkspaceTaskStatus.DONE for t in self._chat_work_executions(work)
                ):
                    raise ValueError("Completed work cannot be resumed")
                work.enabled = True
                work.work_pause_requested = False
                work.last_status = "retry_requested"
                work.last_error = None
                work.next_run_at = _wm._now()
            elif payload.action == "stop":
                work.enabled = False
                work.work_stopped_at = work.work_stopped_at or _wm._now()
                work.next_run_at = None
            work.updated_at = _wm._now()
            self.scheduled_tasks[work.id] = work
            try:
                self._save_scheduled_tasks()
            except Exception:
                self.scheduled_tasks[work.id] = original
                raise
            if payload.action == "stop":
                # Persist stop intent first. Repeat Stop can finish interrupted
                # cleanup; the normal abort event preserves prior evidence.
                await self._stop_chat_work_executions(work)
            return self.chat_work_view(work)

    async def _stop_chat_work_executions(self, work: ScheduledTask) -> None:
        async with self.workspace_mutation_lock(work.workspace_id):
            for task in self._chat_work_executions(work)[:20]:
                current_task = self.tasks.get(task.id)
                if current_task is None:
                    continue
                task = current_task
                if task.status in _ACTIVE and not task.manual_aborted_at:
                    if task.status in {
                        WorkspaceTaskStatus.QUEUED,
                        WorkspaceTaskStatus.WORKING,
                        WorkspaceTaskStatus.REVIEW,
                    }:
                        try:
                            await self.abort_task(
                                task.id,
                                ManualTaskControlRequest(
                                    reason="Stopped from source Chat",
                                    call_id=f"chat-work-stop:{work.id}:{task.id}",
                                ),
                            )
                        except KeyError as exc:
                            # Task deletion can complete while interruption awaits.
                            if exc.args != (task.id,) or task.id in self.tasks:
                                raise
                            continue
                    else:
                        self.tasks[task.id] = task.model_copy(
                            update={
                                "manual_aborted_at": _wm._now(),
                                "manual_abort_reason": "Stopped before dispatch",
                            }
                        )
                        self._save_state()
                await self._cleanup_chat_work_session(task)

    async def _cleanup_chat_work_session(self, task: WorkspaceTask) -> None:
        """Retry owned teardown without trusting mutable task assignment ids."""
        async with self.workspace_mutation_lock(task.workspace_id):
            current_task = self.tasks.get(task.id)
            if current_task is None:
                return
            task = current_task
            if not task.manual_aborted_at and task.status not in {
                WorkspaceTaskStatus.DONE,
                WorkspaceTaskStatus.FAILED,
            }:
                return
            session = self.sessions.get(task.chat_work_owned_session_id or "")
            workspace = self.workspaces.get(task.workspace_id)
            if (
                session is None
                or session.tab_id != task.chat_work_owned_tab_id
                or session.workspace_id != task.workspace_id
                or not session.caller_owned_ephemeral
                or not session.ephemeral
                or session.role not in self._CLEANUP_ALLOWED_SESSION_ROLES
                or session.task_id not in {None, task.id}
                or session.current_task_id not in {None, task.id}
                or self._non_terminal_tasks_referencing_session(session.id)
                or (
                    workspace is not None
                    and session.id
                    in {workspace.resident_agent_session_id, workspace.dispatcher_session_id}
                )
            ):
                return
            await self._best_effort_delete_session(session.id)

    async def report_chat_work(
        self, tab_id: str, work_id: str, payload: ChatWorkReport
    ) -> ChatWorkView:
        # Match control lock order: schedule -> workspace. Completion cannot
        # race a stop/pause snapshot while report intake awaits cleanup.
        lock = self._sched_fire_locks.setdefault(work_id, asyncio.Lock())
        async with lock:
            return await self._report_chat_work_locked(tab_id, work_id, payload)

    async def _report_chat_work_locked(
        self, tab_id: str, work_id: str, payload: ChatWorkReport
    ) -> ChatWorkView:
        work = self._chat_work_record(tab_id, work_id)
        async with self.workspace_mutation_lock(work.workspace_id):
            task = self.tasks.get(payload.task_id)
            if (
                task is None
                or task.source_work_id != work.id
                or task.workspace_id != work.workspace_id
            ):
                raise ValueError("Outcome must belong to this work's execution")
            report = self.reports.get(payload.report_id) if payload.report_id else None
            if payload.report_id is None:
                if not payload.session_id:
                    raise ValueError(
                        "session_id is required when submitting a new execution report"
                    )
                # The canonical intake validates assignment, commits outcome and
                # report evidence before terminal cleanup, and retains reviewed
                # task acceptance rules. Retries after cleanup find that report.
                call_id = (payload.call_id or "").strip() or (
                    "chat-work:"
                    + task.id
                    + ":"
                    + hashlib.sha256(
                        json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()
                    ).hexdigest()[:24]
                )
                report = next(
                    (
                        item
                        for item in self.reports.values()
                        if item.task_id == task.id and item.call_id == call_id
                    ),
                    None,
                )
                if report is None:
                    report = await self.create_report(
                        payload.session_id,
                        AgentReportCreate(
                            task_id=task.id,
                            call_id=call_id,
                            state=(
                                AgentReportState.NEEDS_INPUT
                                if payload.kind == "decision"
                                else (
                                    AgentReportState.WORKING
                                    if payload.kind == "progress" and work.work_kind == "task"
                                    else AgentReportState.COMPLETED
                                )
                            ),
                            message=payload.summary,
                            validation=payload.validation,
                            chat_work_outcome=payload.kind,
                        ),
                    )
                elif (
                    report.session_id != payload.session_id
                    or report.message != payload.summary
                    or report.chat_work_outcome != payload.kind
                    or report.validation != payload.validation
                ):
                    raise ValueError("call_id already exists with different report input")
            if (
                report is None
                or report.task_id != task.id
                or report.workspace_id != work.workspace_id
            ):
                raise ValueError("Outcome must cite a report from this work's execution")
            if report.chat_work_outcome and report.chat_work_outcome != payload.kind:
                raise ValueError("Report already contains a different outcome")
            task = self.tasks[task.id]
            if task.chat_work_report_id == report.id:
                if (
                    task.chat_work_outcome != payload.kind
                    or task.chat_work_summary != payload.summary
                ):
                    raise ValueError("Report already classified with different input")
                return self.chat_work_view(work)
            if payload.kind in {"no_change", "completed"} and report.state not in {
                AgentReportState.COMPLETED,
                AgentReportState.READY_FOR_REVIEW,
            }:
                raise ValueError("Terminal outcomes require a completion report")
            previous = (
                self.reports.get(task.chat_work_report_id) if task.chat_work_report_id else None
            )
            if previous and report.created_at < previous.created_at:
                raise ValueError("Cannot replace a newer outcome with an older report")
            self.tasks[task.id] = task.model_copy(
                update={
                    "chat_work_outcome": payload.kind,
                    "chat_work_report_id": report.id,
                    "chat_work_summary": payload.summary,
                }
            )
            self._save_state()
            if payload.kind == "completed" and work.work_kind == "monitor":
                work.enabled = False
                work.work_completed_at = _wm._now()
                work.next_run_at = None
                work.updated_at = _wm._now()
                self._save_scheduled_tasks()
        return self.chat_work_view(work)

    async def _fire_chat_work_task(self, work: ScheduledTask) -> None:
        from claude_hub.services.task_dependencies import require_task_dependencies

        session = None
        task = self._create_task(
            work.workspace_id,
            WorkspaceTaskCreate(
                title=work.task_title or work.name,
                prompt=work.message or "",
                agent_type=work.agent_type,
                task_mode=work.work_task_mode,
                timeout_seconds=3600,
            ),
            system_internal=work.work_kind == "monitor",
            internal_kind="scheduled" if work.work_kind == "monitor" else None,
            source_work_id=work.id,
        )
        # Ownership is already saved by _create_task. Enrich assignment before
        # any worker exists, with an explicit source tab (the worker tab differs).
        protocol = (
            "\n\nLinked Chat work: "
            + work.id
            + "; source Chat: "
            + (work.source_tab_id or "")
            + ". Submit progress/final execution reports using one command: claude-hub work report "
            + work.id
            + " --tab-id "
            + (work.source_tab_id or "")
            + " --task-id "
            + task.id
            + " --session-id SESSION_ID --kind OUTCOME --summary 'brief evidence-based result' "
            "--validation 'checks and evidence'. Use the assigned session ID from this assignment. "
            "This command writes the normal task report and outcome atomically before terminal cleanup; "
            "do not send a separate final completion report first. "
            "OUTCOME is no_change for an unchanged successful check, anomaly for a change requiring attention, "
            "decision for needed input, progress for a milestone (finishes this monitor check but keeps a "
            "one-shot task working), completed only when the entire requested objective "
            "is achieved (ends recurring monitoring). A successful single check is NOT monitor completion. "
            "Keep work inside the assigned scope and cwd; do not create another recurring schedule."
        )
        self.tasks[task.id] = task.model_copy(update={"prompt": task.prompt + protocol})
        self._save_state()
        try:
            session = await self.ensure_workspace_agent(
                work.workspace_id, self._chat_work_launch_request(work)
            )
            task = self.tasks[task.id]
            if task.status != WorkspaceTaskStatus.TODO or work.work_stopped_at:
                raise RuntimeError("Linked execution changed before dispatch")
            require_task_dependencies(self.tasks, task)
            task = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.QUEUED,
                    "session_id": session.id,
                    "chat_work_owned_session_id": session.id,
                    "chat_work_owned_tab_id": session.tab_id,
                    "queued_at": _wm._now(),
                    "dispatch_attempt": task.dispatch_attempt + 1,
                    "dispatch_reason": "Chat linked work",
                    "updated_at": _wm._now(),
                }
            )
            self.tasks[task.id] = task
            self._save_state()
            await self._dispatch_task_to_session(task, session)
        except Exception as exc:
            current = self.tasks[task.id]
            if current.status in {WorkspaceTaskStatus.TODO, WorkspaceTaskStatus.QUEUED}:
                self.tasks[task.id] = current.model_copy(
                    update={
                        "status": WorkspaceTaskStatus.FAILED,
                        "failure_reason": str(exc)[:2000],
                        "failed_at": _wm._now(),
                        "updated_at": _wm._now(),
                    }
                )
                self._save_state()
            if session:
                await self._best_effort_delete_session(session.id)
            raise

    async def _reconcile_chat_work(self) -> None:
        """Bounded recovery: fail closed for interrupted pre-dispatch launches.

        Active dispatched tasks retain the existing report, timeout and orphan
        handlers. No model is launched to reconcile an uncertain attempt.
        """
        changed = False
        for work in list(self.scheduled_tasks.values()):
            if not work.source_tab_id:
                continue
            lock = self._sched_fire_locks.setdefault(work.id, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                current_work = self.scheduled_tasks.get(work.id)
                if current_work is None:
                    continue
                work = current_work
                if work.work_stopped_at:
                    await self._stop_chat_work_executions(work)
                    continue
                executions = self._chat_work_executions(work)
                if (
                    work.work_kind == "monitor"
                    and any(
                        report.chat_work_outcome == "completed"
                        and report.state
                        in {AgentReportState.COMPLETED, AgentReportState.READY_FOR_REVIEW}
                        and report.task_id in {task.id for task in executions}
                        for report in self.reports.values()
                    )
                    and not work.work_completed_at
                ):
                    work.enabled = False
                    work.work_completed_at = _wm._now()
                    work.next_run_at = None
                    changed = True
                if not executions and work.run_count and not work.enabled:
                    work.last_status = "error"
                    work.last_error = "Launch interrupted before task creation; resume explicitly"
                    changed = True
                for task in executions[:20]:
                    current_task = self.tasks.get(task.id)
                    if current_task is None:
                        continue
                    task = current_task
                    if task.status == WorkspaceTaskStatus.TODO and not task.manual_aborted_at:
                        self.tasks[task.id] = task.model_copy(
                            update={
                                "status": WorkspaceTaskStatus.FAILED,
                                "failure_reason": "Launch interrupted before dispatch; resume explicitly",
                                "failed_at": _wm._now(),
                                "updated_at": _wm._now(),
                            }
                        )
                        work.enabled = False
                        work.next_run_at = None
                        work.last_status = "error"
                        work.last_error = "Launch interrupted before dispatch; resume explicitly"
                        self._save_state()
                        changed = True
                    await self._cleanup_chat_work_session(task)
        if changed:
            self._save_scheduled_tasks()
