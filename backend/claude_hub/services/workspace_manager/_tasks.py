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
        lock = self._feedback_summary_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            return await self._summarize_workspace_feedback_locked(
                workspace_id,
                payload or FeedbackSummaryRequest(),
            )

    def _active_feedback_summary_task(self, workspace_id: str) -> WorkspaceTask | None:
        candidates = [
            task
            for task in self.tasks.values()
            if task.workspace_id == workspace_id
            and task.system_internal
            and task.internal_kind == "feedback_reaper"
            and task.status != WorkspaceTaskStatus.DONE
            and task.manual_aborted_at is None
        ]
        return max(candidates, key=lambda task: task.created_at) if candidates else None

    def _mark_feedback_summary_retryable(
        self,
        task: WorkspaceTask,
        *,
        reason: str,
        validation: str,
    ) -> WorkspaceTask:
        current = self.tasks.get(task.id, task)
        self._release_task_session(current)
        now = _wm._now()
        updated = current.model_copy(
            update={
                "status": WorkspaceTaskStatus.TODO,
                "session_id": None,
                "clear_context": None,
                "dispatch_reason": reason,
                "dispatch_pending": False,
                "review_session_id": None,
                "review_requested_at": None,
                "review_completed_at": None,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "reviewed_at": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "completed_at": None,
                "queued_at": None,
                "started_at": None,
                "updated_at": now,
            }
        )
        self.tasks[task.id] = updated
        self._record_system_task_audit(
            task=updated,
            message="Feedback Reaper task paused in Todo and can be retried.",
            message_zh="Feedback Reaper 任务已回到 Todo，可安全重试。",
            validation=validation,
            state=AgentReportState.BLOCKED,
        )
        self._save_state()
        return updated

    async def _start_feedback_summary_task(
        self,
        task: WorkspaceTask,
        run: FeedbackSummaryRun,
        *,
        clear_context: bool,
    ) -> FeedbackSummaryRun:
        try:
            await self.start_task(task.id, StartTaskRequest(clear_context=clear_context))
        except Exception as exc:
            error_text = str(exc).replace("\n", " ")[:400]
            self._mark_feedback_summary_retryable(
                task,
                reason=f"Feedback Reaper dispatch failed: {type(exc).__name__}",
                validation=(
                    f"summary_run_id={run.id}; retryable=true; "
                    f"error_type={type(exc).__name__}; error={error_text}"
                ),
            )
            raise RuntimeError(
                f"Feedback Reaper dispatch failed; task {task.id} remains visible in Todo "
                "and can be retried"
            ) from exc
        return run

    def _complete_empty_feedback_summary_retry(
        self,
        task: WorkspaceTask,
        run: FeedbackSummaryRun,
        summary_input: dict[str, Any],
    ) -> FeedbackSummaryRun:
        """Close a previously visible preparation failure when no input remains."""

        now = _wm._now()
        store = self._feedback_store()
        store.commit_summary_input(summary_input, [])
        completed_run = run.model_copy(
            update={
                "mode": run.mode,
                "input_record_ids": [],
                "cache_hit": True,
                "prompt_version": summary_input["prompt_version"],
                "skipped_reason": summary_input["skipped_reason"],
                "completed_at": now,
            }
        )
        store.write_summary_run(task.workspace_id, completed_run)
        completed_task = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.DONE,
                "review_skipped_at": now,
                "review_skip_reason": "No feedback records remained when preparation retried.",
                "completed_at": now,
                "updated_at": now,
            }
        )
        self.tasks[task.id] = completed_task
        self._record_system_task_audit(
            task=completed_task,
            message="Feedback Reaper retry completed without dispatch because no input remained.",
            message_zh="Feedback Reaper 重试时已无待处理输入，因此未调度 agent 即完成。",
            validation=f"summary_run_id={run.id}; skipped_reason={summary_input['skipped_reason']}",
            state=AgentReportState.COMPLETED,
        )
        self._save_state()
        self._write_task_record(completed_task)
        return completed_run

    async def _prepare_and_start_feedback_summary_task(
        self,
        workspace: Workspace,
        payload: FeedbackSummaryRequest,
        task: WorkspaceTask,
        run: FeedbackSummaryRun,
        *,
        summary_input: dict[str, Any] | None = None,
    ) -> FeedbackSummaryRun:
        """Prepare, persist, and dispatch one already-visible summary task."""

        store = self._feedback_store()
        try:
            if summary_input is None:
                summary_input = store.prepare_summary_input(
                    workspace.id,
                    self._workspace_task_records_dir(workspace.id),
                    mode=payload.mode,
                    limit=payload.limit,
                    force=payload.force,
                    now=_wm._now(),
                )
                # The provisional run is the lifecycle identity operators see
                # after a preparation failure. Reuse it on retry.
                summary_input["run_id"] = run.id
            if summary_input["cache_hit"]:
                return self._complete_empty_feedback_summary_retry(task, run, summary_input)
            prompt, committed_task_ids, committed_paths = (
                self._build_workspace_feedback_summary_prompt(workspace, summary_input)
            )
            prepared_task = task.model_copy(update={"prompt": prompt, "updated_at": _wm._now()})
            prepared_run = run.model_copy(
                update={
                    "mode": payload.mode,
                    "input_record_ids": committed_task_ids,
                    "cache_hit": False,
                    "prompt_version": summary_input["prompt_version"],
                    "skipped_reason": None,
                    "completed_at": None,
                }
            )
            self.tasks[task.id] = prepared_task
            store.write_summary_run(workspace.id, prepared_run)
            # Persist the real dispatch prompt before the stage file becomes a
            # restart signal that this task is ready to execute.
            self._save_state()
            store.stage_summary_input(summary_input, committed_paths)
        except Exception as exc:
            # A failed or partial stage write must not become the readiness
            # signal for dispatch on the next trigger.
            try:
                store.discard_staged_summary_input(workspace.id, run.id)
            except OSError:
                logger.exception(
                    "Failed to discard incomplete Feedback Reaper stage workspace_id=%s "
                    "run_id=%s",
                    workspace.id,
                    run.id,
                )
            error_text = str(exc).replace("\n", " ")[:400]
            self._mark_feedback_summary_retryable(
                task,
                reason=f"Feedback Reaper prompt preparation failed: {type(exc).__name__}",
                validation=(
                    f"summary_run_id={run.id}; prompt_preparation_retryable=true; "
                    f"error_type={type(exc).__name__}; error={error_text}"
                ),
            )
            raise RuntimeError(
                f"Feedback Reaper prompt preparation failed; task {task.id} remains visible "
                "in Todo and can be retried"
            ) from exc

        self._record_system_task_audit(
            task=prepared_task,
            message="Internal Feedback Reaper task prepared for workspace lesson summarization.",
            message_zh="内部 Feedback Reaper 任务已完成准备，将用于总结 workspace lessons。",
            validation=(
                "system_internal=true; internal_kind=feedback_reaper; board_visible=true; "
                f"summary_run_id={prepared_run.id}; "
                f"input_record_ids={json.dumps(committed_task_ids)}; "
                f"pending_record_count={len(committed_paths)}; processed_commit=deferred"
            ),
        )
        return await self._start_feedback_summary_task(
            prepared_task,
            prepared_run,
            clear_context=payload.clear_context,
        )

    async def _summarize_workspace_feedback_locked(
        self,
        workspace_id: str,
        payload: FeedbackSummaryRequest,
    ) -> FeedbackSummaryRun:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)
        now = _wm._now()
        store = self._feedback_store()

        active_task = self._active_feedback_summary_task(workspace_id)
        if active_task is not None:
            active_run = store.summary_run_for_task(workspace_id, active_task.id)
            if active_run is None:
                active_run = FeedbackSummaryRun(
                    id=str(uuid.uuid4()),
                    workspace_id=workspace_id,
                    task_id=active_task.id,
                    mode=payload.mode,
                    input_record_ids=[],
                    cache_hit=False,
                    created_at=now,
                )
                store.write_summary_run(workspace_id, active_run)
                active_task = self._mark_feedback_summary_retryable(
                    active_task,
                    reason="Feedback Reaper summary audit was missing and was rebuilt",
                    validation=f"summary_run_id={active_run.id}; missing_run_recovered=true",
                )
            session = self.sessions.get(active_task.session_id or "")
            assignment_missing = active_task.status in {
                WorkspaceTaskStatus.QUEUED,
                WorkspaceTaskStatus.WORKING,
                WorkspaceTaskStatus.REVIEW,
            } and (
                session is None
                or (
                    active_task.status != WorkspaceTaskStatus.QUEUED
                    and session.task_id != active_task.id
                    and session.current_task_id != active_task.id
                )
            )
            if assignment_missing:
                active_task = self._mark_feedback_summary_retryable(
                    active_task,
                    reason="Feedback Reaper assignment was missing and is ready to retry",
                    validation=f"summary_run_id={active_run.id}; orphan_assignment_recovered=true",
                )
            if active_task.status == WorkspaceTaskStatus.TODO:
                if not store.has_staged_summary_input(workspace_id, active_run.id):
                    return await self._prepare_and_start_feedback_summary_task(
                        workspace,
                        payload,
                        active_task,
                        active_run,
                    )
                return await self._start_feedback_summary_task(
                    active_task,
                    active_run,
                    clear_context=payload.clear_context,
                )
            return active_run

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

        task = self._create_task(
            workspace_id,
            WorkspaceTaskCreate(
                title="Feedback Reaper: summarize workspace lessons",
                prompt="Feedback Reaper prompt preparation is pending.",
                task_mode=WorkspaceTaskMode.REVIEWED,
                execution_complexity=WorkspaceTaskExecutionComplexity.AUTO,
            ),
            system_internal=True,
            internal_kind="feedback_reaper",
        )
        run = FeedbackSummaryRun(
            id=summary_input["run_id"],
            workspace_id=workspace_id,
            task_id=task.id,
            mode=payload.mode,
            input_record_ids=[],
            cache_hit=False,
            prompt_version=summary_input["prompt_version"],
            created_at=now,
        )
        store.write_summary_run(workspace_id, run)
        return await self._prepare_and_start_feedback_summary_task(
            workspace,
            payload,
            task,
            run,
            summary_input=summary_input,
        )
