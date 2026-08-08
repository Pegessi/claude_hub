"""Task creation and feedback summarization."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _TasksMixin:
    def create_task(self, workspace_id: str, payload: WorkspaceTaskCreate) -> WorkspaceTask:
        return self._create_task(workspace_id, payload)

    def _create_task(
        self,
        workspace_id: str,
        payload: WorkspaceTaskCreate,
        *,
        system_internal: bool = False,
        internal_kind: str | None = None,
    ) -> WorkspaceTask:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        if payload.related_task_id and payload.related_task_id not in self.tasks:
            raise KeyError(payload.related_task_id)
        # Validate session_id if provided (dispatch target hint)
        if payload.session_id:
            session = self.sessions.get(payload.session_id)
            if not session or session.workspace_id != workspace_id:
                raise KeyError(payload.session_id)
            if session.role != WorkspaceSessionRole.ORCHESTRATOR:
                raise ValueError("Tasks can only be assigned to workspace agents")
        title = payload.title.strip()
        prompt = payload.prompt.strip()
        if not title:
            raise ValueError("Task title is required")
        if not prompt and not payload.attachments:
            raise ValueError("Task description is required")

        task_id = str(uuid.uuid4())
        now = _wm._now()
        attachments = self._persist_attachments(workspace_id, task_id, payload.attachments)
        autonomy_policy = (
            payload.autonomy_policy or AutonomyPolicy()
            if payload.task_mode == WorkspaceTaskMode.AUTONOMOUS
            else None
        )
        task = WorkspaceTask(
            id=task_id,
            workspace_id=workspace_id,
            title=title,
            prompt=prompt,
            attachments=attachments,
            goal_packet=payload.goal_packet,
            review_profiles=payload.review_profiles,
            agent_type=payload.agent_type,
            task_mode=payload.task_mode,
            execution_complexity=payload.execution_complexity,
            origin=payload.origin,
            autonomy_policy=autonomy_policy,
            autonomous_run=(
                self._default_autonomous_run(task_id, autonomy_policy.max_iterations)
                if autonomy_policy
                else None
            ),
            status=WorkspaceTaskStatus.TODO,
            related_task_id=payload.related_task_id,
            session_id=payload.session_id or None,
            clear_context=payload.clear_context,
            system_internal=system_internal,
            internal_kind=internal_kind,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task_id] = task
        self._save_state()
        logger.info(
            "Created workspace task id=%s workspace_id=%s title=%r related_task_id=%s agent_type=%s",
            task.id,
            workspace_id,
            task.title,
            task.related_task_id,
            task.agent_type,
        )
        return task

    async def summarize_workspace_feedback(
        self,
        workspace_id: str,
        payload: FeedbackSummaryRequest | None = None,
    ) -> FeedbackSummaryRun:
        payload = payload or FeedbackSummaryRequest()
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)
        now = _wm._now()
        store = self._feedback_store()
        summary_input = store.prepare_summary_input(
            workspace_id,
            self._workspace_task_records_dir(workspace_id),
            mode=payload.mode,
            limit=payload.limit,
            force=payload.force,
            now=now,
        )
        if summary_input["cache_hit"]:
            # Still prune the processed index of deleted-disk entries even when
            # no new records are being summarized.
            store.commit_summary_input(summary_input, [])
            run = FeedbackSummaryRun(
                id=summary_input["run_id"],
                workspace_id=workspace_id,
                task_id=None,
                mode=payload.mode,
                input_record_ids=[],
                cache_hit=True,
                prompt_version=summary_input["prompt_version"],
                skipped_reason=summary_input["skipped_reason"],
                created_at=now,
                completed_at=now,
            )
            store.write_summary_run(workspace_id, run)
            return run

        prompt, committed_task_ids, committed_paths = self._build_workspace_feedback_summary_prompt(
            workspace, summary_input
        )
        # Commit only the records that actually made it into the prompt
        # (after global-budget trimming). Dropped records stay unprocessed
        # and will be retried on the next run.
        processed_count = store.commit_summary_input(summary_input, committed_paths)
        task = self._create_task(
            workspace_id,
            WorkspaceTaskCreate(
                title="Feedback Reaper: summarize workspace lessons",
                prompt=prompt,
                task_mode=WorkspaceTaskMode.REVIEWED,
                execution_complexity=WorkspaceTaskExecutionComplexity.AUTO,
            ),
            system_internal=True,
            internal_kind="feedback_reaper",
        )
        self._record_system_task_audit(
            task=task,
            message="Internal Feedback Reaper task created for workspace lesson summarization.",
            message_zh="已创建内部 Feedback Reaper 任务用于总结 workspace lessons。",
            validation=(
                "system_internal=true; internal_kind=feedback_reaper; board_visible=false; "
                f"summary_run_id={summary_input['run_id']}; "
                f"input_record_ids={json.dumps(committed_task_ids)}; "
                f"processed_count={processed_count}"
            ),
        )
        run = FeedbackSummaryRun(
            id=summary_input["run_id"],
            workspace_id=workspace_id,
            task_id=task.id,
            mode=payload.mode,
            input_record_ids=committed_task_ids,
            cache_hit=False,
            prompt_version=summary_input["prompt_version"],
            created_at=now,
        )
        store.write_summary_run(workspace_id, run)
        await self.start_task(task.id, StartTaskRequest(clear_context=payload.clear_context))
        return run
