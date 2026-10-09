"""Task updates and task-record writing."""

from dataclasses import dataclass
from typing import Literal

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ..task_dependencies import require_task_dependencies, validate_task_dependencies
from ..task_graph import reparent_task
from ._attachments import _PreparedWorkspaceAttachment
from ._constants import *  # noqa: F401,F403


@dataclass(frozen=True)
class _TaskEditPlan:
    update: dict[str, Any]
    descendants: dict[str, dict[str, Any]]
    prepared_attachments: list[_PreparedWorkspaceAttachment]
    removed_attachments: list[WorkspaceAttachment]


class _TaskUpdatesMixin:
    async def update_task_status(
        self,
        task_id: str,
        status: WorkspaceTaskStatus,
    ) -> WorkspaceTask:
        return await self.update_task(task_id, WorkspaceTaskUpdate(status=status))

    def _prepare_task_edit(
        self, task: WorkspaceTask, payload: WorkspaceTaskUpdate, now: datetime
    ) -> _TaskEditPlan:
        update: dict[str, Any] = {"updated_at": now}
        if not self._workspace_owns_task(task):
            update["progress_revision"] = task.progress_revision + 1

        # Determine which fields require todo status
        has_todo_only_fields = any(
            [
                payload.title is not None,
                payload.prompt is not None,
                payload.add_attachments is not None,
                payload.removed_attachment_ids is not None,
                payload.related_task_id is not None,
                payload.clear_context is not None,
                payload.session_id is not None,
                payload.parent_task_id is not None,
                payload.depends_on_task_ids is not None,
                "agent_tag" in payload.model_fields_set,
            ]
        )

        if has_todo_only_fields:
            if task.status != WorkspaceTaskStatus.TODO:
                raise ValueError("Only todo tasks can be edited")

        if payload.depends_on_task_ids is not None:
            update["depends_on_task_ids"] = validate_task_dependencies(
                self.tasks, task.workspace_id, task.id, payload.depends_on_task_ids
            )
        if payload.status in (WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING):
            if task.status != WorkspaceTaskStatus.WORKING:
                require_task_dependencies(self.tasks, task.model_copy(update=update))

        # Compute effective title and prompt
        effective_title = task.title
        effective_prompt = task.prompt
        if payload.title is not None:
            effective_title = payload.title.strip()
            update["title"] = effective_title
        if payload.prompt is not None:
            effective_prompt = payload.prompt.strip()
            update["prompt"] = effective_prompt

        # Title validation
        if payload.title is not None and not effective_title:
            raise ValueError("Task title is required")

        prepared_attachments: list[_PreparedWorkspaceAttachment] = []
        removed_attachments: list[WorkspaceAttachment] = []
        effective_attachments = task.attachments
        if payload.add_attachments is not None or payload.removed_attachment_ids is not None:
            remove_set = set(payload.removed_attachment_ids or [])
            removed_attachments = [a for a in task.attachments if a.id in remove_set]
            effective_attachments = [a for a in task.attachments if a.id not in remove_set]
            prepared_attachments = self._prepare_attachments(
                task.workspace_id, task.id, payload.add_attachments or []
            )
            effective_attachments.extend(item.attachment for item in prepared_attachments)
            update["attachments"] = effective_attachments

        # Combined prompt + attachments validation for todo-only edits
        if has_todo_only_fields and not effective_prompt.strip() and not effective_attachments:
            raise ValueError("Task description is required")

        # Handle related_task_id for todo tasks
        if payload.related_task_id is not None:
            related_id = payload.related_task_id or None
            if related_id and related_id not in self.tasks:
                raise KeyError(related_id)
            if related_id == task.id:
                raise ValueError("A task cannot be related to itself")
            update["related_task_id"] = related_id

        staged_reparent: dict[str, dict[str, Any]] = {}
        if payload.parent_task_id is not None:
            staged_reparent = reparent_task(self.tasks, task, payload.parent_task_id or None)
            update.update(staged_reparent.pop(task.id))

        # Handle clear_context for todo tasks
        if payload.clear_context is not None:
            update["clear_context"] = payload.clear_context

        # Handle session_id (dispatch target hint) for todo tasks
        if payload.session_id is not None:
            if payload.session_id:
                session = self.sessions.get(payload.session_id)
                if not session or session.workspace_id != task.workspace_id:
                    raise KeyError(payload.session_id)
                if session.role != WorkspaceSessionRole.ORCHESTRATOR:
                    raise ValueError("Tasks can only be assigned to workspace agents")
                update["session_id"] = payload.session_id
            else:
                update["session_id"] = None
        if "agent_tag" in payload.model_fields_set:
            update["agent_tag"] = payload.agent_tag
        if payload.goal_packet is not None:
            update["goal_packet"] = payload.goal_packet
        if payload.review_profiles is not None:
            update["review_profiles"] = payload.review_profiles
        if payload.execution_complexity is not None:
            update["execution_complexity"] = payload.execution_complexity
        if payload.task_mode is not None:
            update["task_mode"] = payload.task_mode
            if payload.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                policy = payload.autonomy_policy or task.autonomy_policy or AutonomyPolicy()
                update["autonomy_policy"] = policy
                update["autonomous_run"] = (
                    payload.autonomous_run
                    or task.autonomous_run
                    or self._default_autonomous_run(task.id, policy.max_iterations)
                )
            else:
                update["autonomy_policy"] = None
                update["autonomous_run"] = None
        elif payload.autonomy_policy is not None:
            update["autonomy_policy"] = payload.autonomy_policy
            if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                update["autonomous_run"] = task.autonomous_run or self._default_autonomous_run(
                    task.id, payload.autonomy_policy.max_iterations
                )
        elif payload.autonomous_run is not None:
            update["autonomous_run"] = payload.autonomous_run
        status = payload.status
        if status is not None:
            update["status"] = status
            if status == WorkspaceTaskStatus.QUEUED:
                update["queued_at"] = task.queued_at or now
            elif status == WorkspaceTaskStatus.WORKING:
                update["started_at"] = task.started_at or now
                update["human_acceptance_requested_at"] = None
                update["human_accepted_at"] = None
            elif status == WorkspaceTaskStatus.REVIEW:
                update["reviewed_at"] = now
                update["human_acceptance_requested_at"] = task.human_acceptance_requested_at or now
            elif status == WorkspaceTaskStatus.DONE:
                update["completed_at"] = now
                update["human_accepted_at"] = now

        return _TaskEditPlan(
            update=update,
            descendants=staged_reparent,
            prepared_attachments=prepared_attachments,
            removed_attachments=removed_attachments,
        )

    def _read_task_edit_state(self, workspace_id: str) -> bytes | None:
        try:
            return self._workspace_state_file(workspace_id).read_bytes()
        except FileNotFoundError:
            return None

    def _save_task_edit_state(
        self, workspace_id: str, before: bytes | None, expected: bytes
    ) -> tuple[Literal["committed", "uncommitted", "unknown"], BaseException | None]:
        token = self._report_intake_workspace.set(workspace_id)
        try:
            try:
                self._save_state()
            except BaseException as error:
                try:
                    actual = self._read_task_edit_state(workspace_id)
                except BaseException as read_error:
                    logger.error(
                        "Task edit readback failed workspace_id=%s save_error=%s read_error=%s",
                        workspace_id,
                        type(error).__name__,
                        type(read_error).__name__,
                    )
                    # Failure classification must not turn an interrupt into an I/O error.
                    if not isinstance(error, Exception):
                        return "unknown", error
                    return "unknown", read_error
                if actual == expected:
                    return "committed", error
                if actual == before:
                    return "uncommitted", error
                return "unknown", error
            return "committed", None
        finally:
            self._report_intake_workspace.reset(token)

    def _cleanup_removed_task_attachments(self, attachments: list[WorkspaceAttachment]) -> None:
        # Another Task or a later edit may still reference a removed path.
        referenced = {
            Path(attachment.path) for task in self.tasks.values() for attachment in task.attachments
        }
        for attachment in attachments:
            path = Path(attachment.path)
            if path in referenced:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to delete removed attachment file: %s", path, exc_info=True)

    async def _update_task_workspace_fields(
        self, task_id: str, payload: WorkspaceTaskUpdate
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        async with self.workspace_mutation_lock(task.workspace_id):
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            now = _wm._now()
            plan = self._prepare_task_edit(task, payload, now)
            before = self._read_task_edit_state(task.workspace_id)
            snapshot = self._snapshot_report_intake_workspace(task.workspace_id)
            created_paths: list[Path] = []
            reviewer_tabs: list[str] = []
            review_report: AgentReport | None = None
            rename_session_id: str | None = None
            status = payload.status
            outcome: Literal["committed", "uncommitted", "unknown"] = "uncommitted"
            save_error: BaseException | None = None
            staged = False
            try:
                self._write_prepared_attachments(plan.prepared_attachments, created_paths)
                staged = True
                self.tasks[task.id] = task.model_copy(update=plan.update)
                for descendant_id, fields in plan.descendants.items():
                    self.tasks[descendant_id] = self.tasks[descendant_id].model_copy(update=fields)
                current = self.tasks[task.id]
                if status == WorkspaceTaskStatus.DONE and self._workspace_owns_task(current):
                    self._release_task_session(current)
                    reviewer_tabs = await self._cleanup_reviewer_for_terminal_task(
                        current, updated_at=now, delete_tabs=False
                    )
                elif (
                    status == WorkspaceTaskStatus.WORKING
                    and current.session_id
                    and self._workspace_owns_task(current)
                ):
                    rename_session_id = current.session_id
                    self._assign_current_task(current.session_id, current.id, rename_tab=False)
                elif (
                    status == WorkspaceTaskStatus.REVIEW
                    and current.session_id
                    and self._workspace_owns_task(current)
                    and not self._reviewer_is_active(current)
                ):
                    self._release_stale_reviewer_for_task(current, updated_at=now)
                    review_report = AgentReport(
                        id=str(uuid.uuid4()),
                        workspace_id=task.workspace_id,
                        task_id=task.id,
                        session_id=current.session_id,
                        state=AgentReportState.READY_FOR_REVIEW,
                        message="Task manually moved to review status.",
                        message_en="Task manually moved to review status.",
                        message_zh="任务被手动移至 review 状态。",
                        changed_files=[],
                        validation=None,
                        risks=None,
                        review_decision=ReviewDecision.REQUEST,
                        review_reason="Manual status transition to REVIEW.",
                        risk_level=None,
                        review_cycle=current.review_cycle,
                        created_at=now,
                    )
                    self.reports[review_report.id] = review_report
                expected = json.dumps(
                    self._workspace_state_payload(task.workspace_id), indent=2
                ).encode("utf-8")
                outcome, save_error = self._save_task_edit_state(
                    task.workspace_id, before, expected
                )
                if outcome != "committed":
                    assert save_error is not None
                    raise save_error
            except BaseException:
                if outcome == "unknown":
                    logger.error(
                        "Task edit commit outcome is unknown; candidate memory and old/new "
                        "attachment files retained workspace_id=%s task_id=%s",
                        task.workspace_id,
                        task.id,
                    )
                else:
                    if staged:
                        self._restore_report_intake_workspace(task.workspace_id, snapshot)
                    self._delete_created_attachment_paths(created_paths)
                raise

        try:
            if save_error is not None:
                if not isinstance(save_error, Exception):
                    raise save_error
                logger.warning(
                    "Task edit was committed despite a save error workspace_id=%s task_id=%s error=%s",
                    task.workspace_id,
                    task.id,
                    type(save_error).__name__,
                )
            current = self.tasks.get(task.id)
            if current is None:
                raise KeyError(task.id)
            if status == WorkspaceTaskStatus.DONE and self._workspace_owns_task(current):
                try:
                    self._write_task_record(current)
                finally:
                    for tab_id in reviewer_tabs:
                        try:
                            await ttyd_manager.delete_tab(tab_id)
                        except Exception:
                            logger.exception(
                                "Failed to delete temporary reviewer tab tab_id=%s", tab_id
                            )
                if task.feedback_lesson_ids:
                    self._feedback_store().increment_lesson_usage(
                        task.workspace_id,
                        list(task.feedback_lesson_ids),
                        success=True,
                        now=now,
                    )
            elif rename_session_id is not None:
                session = self.sessions.get(rename_session_id)
                if (
                    session is not None
                    and current.status == WorkspaceTaskStatus.WORKING
                    and current.session_id == session.id
                    and task.id in {session.task_id, session.current_task_id}
                ):
                    self._rename_task_assignment_tab(current, session)
            elif review_report is not None:
                if current.status == WorkspaceTaskStatus.REVIEW:
                    await self._request_task_review(current, review_report)
            current = self.tasks.get(task.id)
            if status is not None and current is not None and self._workspace_owns_task(current):
                await self.dispatch_workspace(task.workspace_id)
            return self.tasks[task.id]
        finally:
            self._cleanup_removed_task_attachments(plan.removed_attachments)

    def _write_task_record(self, task: WorkspaceTask) -> None:
        completed_at = task.completed_at or _wm._now()
        record_dir = self._workspace_task_records_dir(task.workspace_id)
        record_dir.mkdir(parents=True, exist_ok=True)
        timestamp = completed_at.isoformat(timespec="seconds").replace(":", "-")
        record_path = record_dir / f"{timestamp}-{task.id}.json"
        task_reports = [
            report
            for report in self.reports_for_workspace(task.workspace_id)
            if report.task_id == task.id
        ]
        session = self.sessions.get(task.session_id or "")
        # Archives must not reuse the state codec's private credential metadata.
        public_task = task.model_dump(mode="json")
        if public_task.get("agent_tag") is None:
            public_task.pop("agent_tag", None)
        payload = {
            "schema_version": 1,
            "archived_at": _wm._now().isoformat(),
            "workspace_id": task.workspace_id,
            "task": public_task,
            "session": session.model_dump(mode="json") if session else None,
            "reports": [report.model_dump(mode="json") for report in task_reports],
            "timeline": self._build_task_record_timeline(task, task_reports),
            "artifacts": self._build_task_record_artifacts(task_reports),
            "final_summary": self._task_record_final_summary(task_reports),
        }
        record_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _build_task_record_timeline(
        self,
        task: WorkspaceTask,
        reports: list[AgentReport],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [
            {
                "_timestamp": task.created_at,
                "at": task.created_at.isoformat(),
                "type": "task_created",
                "title": task.title,
            }
        ]
        for field, event_type in (
            ("queued_at", "task_queued"),
            ("started_at", "task_started"),
            ("reviewed_at", "task_reviewed"),
            ("completed_at", "task_completed"),
        ):
            value = getattr(task, field)
            if value:
                events.append({"_timestamp": value, "at": value.isoformat(), "type": event_type})
        for report in reports:
            events.append(
                {
                    "_timestamp": report.created_at,
                    "at": report.created_at.isoformat(),
                    "type": "agent_report",
                    "state": report.state.value,
                    "session_id": report.session_id,
                    "message": report.message,
                    "review_decision": report.review_decision.value,
                    "review_reason": report.review_reason,
                }
            )
        sorted_events = sorted(events, key=lambda item: item["_timestamp"])
        previous_at: datetime | None = None
        for event in sorted_events:
            timestamp = event.pop("_timestamp")
            elapsed_seconds = max(0, int((timestamp - task.created_at).total_seconds()))
            event["elapsed_seconds"] = elapsed_seconds
            event["elapsed"] = _format_duration(elapsed_seconds)
            since_previous_seconds = (
                0 if previous_at is None else max(0, int((timestamp - previous_at).total_seconds()))
            )
            event["duration_since_previous_seconds"] = since_previous_seconds
            event["duration_since_previous"] = _format_duration(since_previous_seconds)
            previous_at = timestamp
        return sorted_events

    def _build_task_record_artifacts(self, reports: list[AgentReport]) -> dict[str, Any]:
        changed_files: list[str] = []
        validations: list[str] = []
        risks: list[str] = []
        for report in reports:
            for file_path in report.changed_files:
                if file_path not in changed_files:
                    changed_files.append(file_path)
            if report.validation:
                validations.append(report.validation)
            if report.risks:
                risks.append(report.risks)
        return {
            "changed_files": changed_files,
            "commits": [],
            "validation": validations,
            "risks": risks,
        }

    def _task_record_final_summary(self, reports: list[AgentReport]) -> str:
        for report in reversed(reports):
            if report.state in {
                AgentReportState.COMPLETED,
                AgentReportState.READY_FOR_REVIEW,
            }:
                return report.message
        return reports[-1].message if reports else ""

    def reap_task_feedback(
        self,
        task_id: str,
        payload: FeedbackReaperRequest,
    ) -> FeedbackReaperRun:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        reports = [
            report
            for report in self.reports_for_workspace(task.workspace_id)
            if report.task_id == task.id
        ]
        return self._feedback_store().reap_task_feedback(workspace, task, reports, payload)

    async def update_task(self, task_id: str, payload: WorkspaceTaskUpdate) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if self._workspace_owns_task(task):
            async with self._workspace_execution_operation(task):
                return await self._update_task_workspace_fields(task_id, payload)
        async with self.workspace_mutation_lock(task.workspace_id):
            allowed = {
                "title",
                "prompt",
                "add_attachments",
                "removed_attachment_ids",
                "parent_task_id",
                "depends_on_task_ids",
                "agent_tag",
            }
            if payload.model_fields_set - allowed:
                from ._task_execution import TaskExecutionConflict

                raise TaskExecutionConflict("initiator_task_requires_progress_endpoint")
            token = self._report_intake_workspace.set(task.workspace_id)
            try:
                return await self._update_task_workspace_fields(task_id, payload)
            finally:
                self._report_intake_workspace.reset(token)
