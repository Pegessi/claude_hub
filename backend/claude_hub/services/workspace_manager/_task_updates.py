"""Task updates and task-record writing."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _TaskUpdatesMixin:
    async def update_task_status(
        self,
        task_id: str,
        status: WorkspaceTaskStatus,
    ) -> WorkspaceTask:
        return await self.update_task(task_id, WorkspaceTaskUpdate(status=status))

    async def update_task(
        self,
        task_id: str,
        payload: WorkspaceTaskUpdate,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)

        now = _wm._now()
        update: dict[str, Any] = {"updated_at": now}

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
            ]
        )

        if has_todo_only_fields:
            if task.status != WorkspaceTaskStatus.TODO:
                raise ValueError("Only todo tasks can be edited")

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

        # Handle attachments for todo tasks
        effective_attachments = task.attachments
        if payload.add_attachments is not None or payload.removed_attachment_ids is not None:
            current_attachments = list(task.attachments)

            # Remove specified attachments
            if payload.removed_attachment_ids:
                remove_set = set(payload.removed_attachment_ids)
                removed = [a for a in current_attachments if a.id in remove_set]
                current_attachments = [a for a in current_attachments if a.id not in remove_set]
                # Delete files from disk
                for attachment in removed:
                    try:
                        Path(attachment.path).unlink(missing_ok=True)
                    except OSError:
                        logger.warning("Failed to delete attachment file: %s", attachment.path)

            # Add new attachments
            if payload.add_attachments:
                new_attachments = self._persist_attachments(
                    task.workspace_id, task.id, payload.add_attachments
                )
                current_attachments.extend(new_attachments)

            effective_attachments = current_attachments
            update["attachments"] = current_attachments

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

        self.tasks[task.id] = task.model_copy(update=update)
        if status == WorkspaceTaskStatus.DONE:
            self._write_task_record(self.tasks[task.id])
            self._release_task_session(self.tasks[task.id])
            await self._cleanup_reviewer_for_terminal_task(self.tasks[task.id], updated_at=now)
            if task.feedback_lesson_ids:
                self._feedback_store().increment_lesson_usage(
                    task.workspace_id,
                    list(task.feedback_lesson_ids),
                    success=True,
                    now=now,
                )
        elif status == WorkspaceTaskStatus.WORKING and task.session_id:
            self._assign_current_task(task.session_id, task.id)
        elif status == WorkspaceTaskStatus.REVIEW and task.session_id:
            # Manual REVIEW status transition must still trigger reviewer
            # dispatch so the task does not sit unreviewed.
            if not self._reviewer_is_active(self.tasks[task.id]):
                self._release_stale_reviewer_for_task(self.tasks[task.id], updated_at=now)
                review_report = AgentReport(
                    id=str(uuid.uuid4()),
                    workspace_id=task.workspace_id,
                    task_id=task.id,
                    session_id=task.session_id,
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
                    review_cycle=task.review_cycle,
                    created_at=now,
                )
                self.reports[review_report.id] = review_report
                await self._request_task_review(self.tasks[task.id], review_report)

        self._save_state()
        if status is not None:
            await self.dispatch_workspace(task.workspace_id)
        return self.tasks[task.id]

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
        payload = {
            "schema_version": 1,
            "archived_at": _wm._now().isoformat(),
            "workspace_id": task.workspace_id,
            "task": task.model_dump(mode="json"),
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
