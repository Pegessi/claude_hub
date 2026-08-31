"""Task creation and feedback summarization."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ...models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from ..request_fingerprint import request_fingerprint
from ..task_graph import (
    make_task_consumer_key,
    resolve_task_tree_fields,
    task_inbox_consumer_key,
    tasks_in_subtree,
    top_level_tasks,
)
from ._constants import *  # noqa: F401,F403

_FOLLOWUP_ACTOR_ROLES = frozenset({TaskActorRole.SUPERVISOR, TaskActorRole.HUMAN})
_ABORT_ACTOR_ROLES = _FOLLOWUP_ACTOR_ROLES
_ABORT_STATUSES = frozenset(
    {
        WorkspaceTaskStatus.QUEUED,
        WorkspaceTaskStatus.WORKING,
        WorkspaceTaskStatus.REVIEW,
    }
)


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

        # Allocate the durable task id before parent validation so path,
        # self-parent, and cycle checks use the real id, not a placeholder.
        task_id = str(uuid.uuid4())
        parent_task_id, root_task_id, task_path = resolve_task_tree_fields(
            self.tasks, workspace_id, task_id, payload.parent_task_id or None
        )
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
            agent_tag=payload.agent_tag,
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
            timeout_seconds=payload.timeout_seconds,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            path=task_path,
            consumer_ack_sequence=0,
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

    def list_top_level_tasks(self, workspace_id: str) -> list[WorkspaceTask]:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        return top_level_tasks(self.tasks.values(), workspace_id)

    def list_task_subtree(self, workspace_id: str, root_task_id: str) -> list[WorkspaceTask]:
        root = self.require_workspace_task(workspace_id, root_task_id)
        return tasks_in_subtree(self.tasks.values(), workspace_id, root)

    def require_workspace_task(self, workspace_id: str, task_id: str) -> WorkspaceTask:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        task = self.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)
        return task

    def task_mailbox_consumer_key(
        self,
        workspace_id: str,
        task_id: str,
    ) -> str:
        return make_task_consumer_key(self.require_workspace_task(workspace_id, task_id).id)

    def list_task_mailbox_events(
        self,
        workspace_id: str,
        task_id: str,
        *,
        since_sequence: int = 0,
        subtree: bool = False,
    ) -> list[TaskEvent]:
        return self.task_mailbox.wait(
            workspace_id,
            self.task_mailbox_consumer_key(workspace_id, task_id),
            since_sequence=since_sequence,
            subtree=subtree,
        )

    async def wait_task_mailbox_events(
        self,
        workspace_id: str,
        task_id: str,
        *,
        since_sequence: int = 0,
        subtree: bool = False,
        timeout_seconds: float = 30.0,
    ) -> list[TaskEvent]:
        return await self.task_mailbox.wait_for(
            workspace_id,
            self.task_mailbox_consumer_key(workspace_id, task_id),
            since_sequence=since_sequence,
            subtree=subtree,
            timeout_seconds=timeout_seconds,
        )

    def ack_task_mailbox(
        self,
        workspace_id: str,
        sequence: int,
        task_id: str,
    ) -> WorkspaceTask:
        consumer_key = self.task_mailbox_consumer_key(workspace_id, task_id)
        self.task_mailbox.ack(workspace_id, consumer_key, sequence)
        return self.tasks[task_id]

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

    async def _followup_existing_task(
        self,
        task_id: str,
        message: str,
        call_id: Optional[str] = None,
    ) -> None:
        """Deliver an already-committed followup. No Task/mailbox writes.

        ``followup_task`` owns the single outer persist (mailbox + pending +
        TODO/QUEUED prompt). This helper only routes that committed intent.
        """
        task = self.tasks.get(task_id)
        if task is None:
            return
        if call_id and (
            call_id in task.processing_call_ids
            or call_id in task.delivered_call_ids
            or call_id in task.uncertain_call_ids
        ):
            logger.debug(
                "followup call_id=%s already claimed on task %s; skipping delivery",
                call_id,
                task_id,
            )
            return

        if task.status == WorkspaceTaskStatus.TODO:
            await self.start_task(task_id)
        elif task.status == WorkspaceTaskStatus.QUEUED:
            return
        elif task.status == WorkspaceTaskStatus.WORKING:
            if task.session_id:
                await self.send_session_message(task.session_id, message, call_id=call_id)
        elif task.status == WorkspaceTaskStatus.REVIEW:
            await self.continue_task(
                task_id,
                ContinueTaskRequest(message=message),
                call_id=call_id,
            )

    def _followup_prompt_block(self, message: str, call_id: Optional[str]) -> str:
        followup_text = message
        if call_id:
            followup_text = f"[call_id:{call_id}]\n{message}"
        return f"[followup] {followup_text}"

    def _stage_task_followup_pending(self, task_id: str, call_id: Optional[str]) -> bool:
        """Stage ``call_id`` on the Task outbox. No persist.

        Returns True only when pending was newly appended. Existing
        pending / processing / delivered / uncertain claims are left
        unchanged so a retry does not invent a second outbox row.
        """
        if not call_id:
            return False
        current = self.tasks.get(task_id)
        if current is None:
            return False
        if (
            call_id in current.pending_call_ids
            or call_id in current.processing_call_ids
            or call_id in current.delivered_call_ids
            or call_id in current.uncertain_call_ids
        ):
            return False
        self.tasks[task_id] = current.model_copy(
            update={"pending_call_ids": current.pending_call_ids + [call_id]}
        )
        return True

    def _stage_task_followup_prompt(
        self,
        task_id: str,
        message: str,
        call_id: Optional[str],
    ) -> bool:
        """Stage the TODO/QUEUED followup marker. No persist."""
        current = self.tasks.get(task_id)
        if current is None:
            return False
        if current.status not in (
            WorkspaceTaskStatus.TODO,
            WorkspaceTaskStatus.QUEUED,
        ):
            return False
        block = self._followup_prompt_block(message, call_id)
        if block in current.prompt:
            return False
        self.tasks[task_id] = current.model_copy(update={"prompt": f"{current.prompt}\n\n{block}"})
        return True

    def _resolve_actor_session_from_author_ref(
        self,
        workspace_id: str,
        author_ref: Optional[str],
    ) -> Optional[str]:
        """Resolve a Session id from a supervisor task id or legacy author ref."""

        if not author_ref:
            return None
        direct = self.tasks.get(author_ref)
        if direct is not None and direct.workspace_id == workspace_id and direct.session_id:
            return direct.session_id
        return None

    def _canonical_followup_payload(
        self,
        *,
        task_id: str,
        message: str,
        actor_role: TaskActorRole,
        actor_session_id: Optional[str],
        review_cycle: Optional[int],
        compat_author_run_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "message": message,
            "followup": True,
            "task_id": task_id,
            "actor_role": actor_role.value,
            "actor_session_id": actor_session_id,
            "review_cycle": review_cycle,
            "compat_author_run_id": compat_author_run_id,
            "correlation_id": correlation_id,
        }

    async def followup_task(
        self,
        workspace_id: str,
        task_id: str,
        message: str,
        call_id: str,
        *,
        actor_session_id: Optional[str] = None,
        actor_role: TaskActorRole = TaskActorRole.SUPERVISOR,
        compat_author_run_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> TaskEvent:
        """Write the TaskMailbox followup intent, then deliver after commit."""
        if actor_role not in _FOLLOWUP_ACTOR_ROLES:
            raise ValueError(
                f"followup_task actor_role must be supervisor or human, got {actor_role}"
            )
        async with self.workspace_mutation_lock(workspace_id):
            if workspace_id not in self.workspaces:
                raise KeyError(workspace_id)
            task = self.tasks.get(task_id)
            if task is None or task.workspace_id != workspace_id:
                raise KeyError(task_id)
            if task.status == WorkspaceTaskStatus.DONE:
                raise RuntimeError("Done tasks cannot receive followup")

            resolved_actor_session_id = actor_session_id
            if resolved_actor_session_id is None and compat_author_run_id:
                resolved_actor_session_id = self._resolve_actor_session_from_author_ref(
                    workspace_id, compat_author_run_id
                )
            existing = self.task_mailbox._call_record(workspace_id, call_id)
            existing_event = existing["event"] if existing is not None else None
            review_cycle = (
                existing_event.review_cycle if existing_event is not None else task.review_cycle
            )

            snapshot = self._snapshot_report_intake_workspace(workspace_id)
            event, _created = self.task_mailbox.append_event(
                workspace_id=workspace_id,
                task_id=task.id,
                actor_role=actor_role,
                event_type=TaskEventType.FOLLOWUP,
                call_id=call_id,
                action="followup",
                consumer_key=task_inbox_consumer_key(task),
                actor_session_id=resolved_actor_session_id,
                review_cycle=review_cycle,
                target=task.id,
                payload=self._canonical_followup_payload(
                    task_id=task.id,
                    message=message,
                    actor_role=actor_role,
                    actor_session_id=resolved_actor_session_id,
                    review_cycle=review_cycle,
                    compat_author_run_id=compat_author_run_id,
                    correlation_id=correlation_id,
                ),
                persist=False,
            )
            self._stage_task_followup_pending(task.id, call_id)
            self._stage_task_followup_prompt(task.id, message, call_id)
            try:
                self._save_state()
            except Exception:
                self._restore_report_intake_workspace(workspace_id, snapshot)
                raise
            self.task_mailbox._wake_compat_waiters(event)
            await self._followup_existing_task(task.id, message, call_id)
            return event

    def _canonical_abort_payload(
        self,
        *,
        task_id: str,
        reason: str,
        actor_role: TaskActorRole,
        actor_session_id: Optional[str],
        review_cycle: Optional[int],
        compat_author_run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "reason": reason,
            "abort": True,
            "task_id": task_id,
            "actor_role": actor_role.value,
            "actor_session_id": actor_session_id,
            "review_cycle": review_cycle,
            "compat_author_run_id": compat_author_run_id,
        }

    def _abort_report_id(
        self,
        workspace_id: str,
        call_id: str,
        existing: TaskEvent | None,
    ) -> str:
        if existing is not None and existing.action == "abort" and existing.report_id:
            return existing.report_id
        return str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"claude-hub:task-abort:{workspace_id}:{call_id}")
        )

    def _abort_fingerprint(
        self,
        task: WorkspaceTask,
        *,
        actor_role: TaskActorRole,
        actor_session_id: Optional[str],
        review_cycle: Optional[int],
        payload: dict[str, Any],
        report_id: Optional[str],
    ) -> str:
        consumer_key = task_inbox_consumer_key(task)
        return request_fingerprint(
            "abort",
            {
                "task_id": task.id,
                "actor_session_id": actor_session_id,
                "actor_role": actor_role.value,
                "review_cycle": review_cycle,
                "event_type": TaskEventType.ABORT.value,
                "target": task.id,
                "consumer_key": consumer_key,
                "payload": payload,
                "report_id": report_id,
            },
        )

    def _resolve_abort_actor_session_id(
        self,
        workspace_id: str,
        actor_session_id: Optional[str],
        actor_role: TaskActorRole,
        compat_author_run_id: Optional[str],
    ) -> Optional[str]:
        if actor_session_id:
            session = self.sessions.get(actor_session_id)
            if session is None or session.workspace_id != workspace_id:
                raise ValueError("actor_session_id must name a session in this workspace")
            return session.id
        if actor_role != TaskActorRole.SUPERVISOR:
            return None
        resolved = self._resolve_actor_session_from_author_ref(workspace_id, compat_author_run_id)
        if resolved is None:
            raise ValueError("supervisor abort requires a real actor session")
        return resolved

    def _preflight_abort_call(
        self,
        workspace_id: str,
        task: WorkspaceTask,
        call_id: str,
        fingerprint: str,
    ) -> TaskEvent | None:
        """Replay or reject a known abort call_id before status/report/interrupt."""
        existing = self.task_mailbox._call_record(workspace_id, call_id)
        if existing is None:
            return None
        if (
            existing["action"] != "abort"
            or existing["target"] != task.id
            or existing["fingerprint"] != fingerprint
        ):
            raise ValueError(
                f"call_id {call_id!r} already used for action="
                f"{existing['action']!r} target={existing['target']!r} "
                f"in workspace {workspace_id}; cannot reuse for "
                f"action='abort' target={task.id!r}"
            )
        event = existing["event"]
        return event if isinstance(event, TaskEvent) else None

    def _sessions_assigned_to_task(self, task: WorkspaceTask) -> list[ManagedSession]:
        sessions: list[ManagedSession] = []
        if task.session_id:
            worker = self.sessions.get(task.session_id)
            if worker and (worker.task_id == task.id or worker.current_task_id == task.id):
                sessions.append(worker)
        reviewer_ids: set[str] = set()
        if task.review_session_id:
            reviewer_ids.add(task.review_session_id)
        reviewer_ids.update(
            session.id
            for session in self.sessions.values()
            if session.role == WorkspaceSessionRole.REVIEWER
            and (session.task_id == task.id or session.current_task_id == task.id)
        )
        for session_id in reviewer_ids:
            reviewer = self.sessions.get(session_id)
            if (
                reviewer
                and reviewer.role == WorkspaceSessionRole.REVIEWER
                and (reviewer.task_id == task.id or reviewer.current_task_id == task.id)
            ):
                sessions.append(reviewer)
        return sessions

    def _stage_aborted_task(self, task: WorkspaceTask, reason: str, now: datetime) -> WorkspaceTask:
        is_feedback_summary = task.system_internal and task.internal_kind == "feedback_reaper"
        updated = task.model_copy(
            update={
                "status": (
                    WorkspaceTaskStatus.DONE if is_feedback_summary else WorkspaceTaskStatus.TODO
                ),
                "session_id": None,
                "clear_context": None,
                "dispatch_reason": f"Manually aborted: {reason}",
                "dispatch_pending": False,
                "review_session_id": None,
                "review_requested_at": None,
                "review_completed_at": None,
                "review_skipped_at": now if is_feedback_summary else None,
                "review_skip_reason": (
                    "Feedback Reaper was manually aborted; pending input was released."
                    if is_feedback_summary
                    else None
                ),
                "manual_aborted_at": now,
                "manual_abort_reason": reason,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "queued_at": None,
                "started_at": None,
                "reviewed_at": None,
                "completed_at": now if is_feedback_summary else None,
                "updated_at": now,
            }
        )
        self.tasks[task.id] = updated
        return updated

    async def abort_task(
        self,
        task_id: str,
        payload: ManualTaskControlRequest,
        *,
        workspace_id: Optional[str] = None,
        call_id: Optional[str] = None,
        actor_session_id: Optional[str] = None,
        actor_role: TaskActorRole = TaskActorRole.HUMAN,
        compat_author_run_id: Optional[str] = None,
    ) -> WorkspaceTask:
        """Abort a Task. This is the only work-lifecycle interrupt mutation."""
        if actor_role not in _ABORT_ACTOR_ROLES:
            raise ValueError(f"abort_task actor_role must be supervisor or human, got {actor_role}")
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        resolved_workspace_id = workspace_id or task.workspace_id
        if resolved_workspace_id not in self.workspaces:
            raise KeyError(resolved_workspace_id)
        if task.workspace_id != resolved_workspace_id:
            raise KeyError(task_id)

        reason = payload.reason.strip() if payload.reason else ""
        resolved_call_id = (call_id or payload.call_id or "").strip() or str(uuid.uuid4())
        resolved_actor_session_id = self._resolve_abort_actor_session_id(
            resolved_workspace_id,
            actor_session_id,
            actor_role,
            compat_author_run_id,
        )

        async with self.workspace_mutation_lock(resolved_workspace_id):
            live = self.tasks.get(task_id)
            if live is None or live.workspace_id != resolved_workspace_id:
                raise KeyError(task_id)

            existing_record = self.task_mailbox._call_record(
                resolved_workspace_id, resolved_call_id
            )
            existing_event = (
                existing_record["event"]
                if existing_record is not None
                and isinstance(existing_record.get("event"), TaskEvent)
                else None
            )
            review_cycle = (
                existing_event.review_cycle if existing_event is not None else live.review_cycle
            )
            report_id = self._abort_report_id(
                resolved_workspace_id, resolved_call_id, existing_event
            )
            abort_payload = self._canonical_abort_payload(
                task_id=live.id,
                reason=reason,
                actor_role=actor_role,
                actor_session_id=resolved_actor_session_id,
                review_cycle=review_cycle,
                compat_author_run_id=compat_author_run_id,
            )
            fingerprint = self._abort_fingerprint(
                live,
                actor_role=actor_role,
                actor_session_id=resolved_actor_session_id,
                review_cycle=review_cycle,
                payload=abort_payload,
                report_id=report_id,
            )
            replay = self._preflight_abort_call(
                resolved_workspace_id, live, resolved_call_id, fingerprint
            )
            if replay is not None:
                return self.tasks[live.id]

            if live.status not in _ABORT_STATUSES:
                raise RuntimeError("Only queued, working, or review tasks can be manually aborted")
            if not reason:
                raise RuntimeError("Manual abort requires a reason")

            snapshot = self._snapshot_report_intake_workspace(resolved_workspace_id)
            sessions_to_interrupt = self._sessions_assigned_to_task(live)
            is_feedback_summary = live.system_internal and live.internal_kind == "feedback_reaper"
            now = _wm._now()
            ephemeral_tab_ids: list[str] = []
            try:
                report_session_id = live.session_id or live.review_session_id or "manual-control"
                report = AgentReport(
                    id=report_id,
                    workspace_id=live.workspace_id,
                    task_id=live.id,
                    session_id=report_session_id,
                    state=AgentReportState.BLOCKED,
                    message=f"Task manually aborted by operator: {reason}",
                    message_en=f"Task manually aborted by operator: {reason}",
                    message_zh=f"操作员已手动终止任务：{reason}",
                    changed_files=[],
                    validation=None,
                    risks=(
                        "Task state was manually recovered; prior worker/reviewer "
                        "output may be incomplete."
                    ),
                    review_decision=ReviewDecision.SKIP,
                    review_reason=(
                        "Manual abort is an exceptional recovery action, not task completion."
                    ),
                    risk_level="manual_control",
                    review_cycle=live.review_cycle,
                    created_at=now,
                )
                self.reports[report.id] = report
                abort_event, _created = self.task_mailbox.append_event(
                    workspace_id=resolved_workspace_id,
                    task_id=live.id,
                    actor_role=actor_role,
                    event_type=TaskEventType.ABORT,
                    call_id=resolved_call_id,
                    action="abort",
                    consumer_key=task_inbox_consumer_key(live),
                    actor_session_id=resolved_actor_session_id,
                    review_cycle=review_cycle,
                    target=live.id,
                    payload=abort_payload,
                    report_id=report.id,
                    persist=False,
                )
                self._stage_aborted_task(live, reason, now)
                self._release_task_session(live)
                ephemeral_tab_ids = await self._cleanup_reviewer_for_terminal_task(
                    live, updated_at=now, delete_tabs=False
                )
                self._save_state()
            except Exception:
                self._restore_report_intake_workspace(resolved_workspace_id, snapshot)
                raise
            self.task_mailbox._wake_compat_waiters(abort_event)

            try:
                if sessions_to_interrupt:
                    await asyncio.gather(
                        *(self._interrupt_session(session) for session in sessions_to_interrupt)
                    )
            except Exception:
                logger.exception(
                    "Best-effort session interrupt failed after Task abort "
                    "workspace_id=%s task_id=%s",
                    resolved_workspace_id,
                    live.id,
                )
            for tab_id in ephemeral_tab_ids:
                try:
                    await ttyd_manager.delete_tab(tab_id)
                except Exception:
                    logger.exception(
                        "Failed to delete temporary reviewer tab after abort tab_id=%s",
                        tab_id,
                    )
            if is_feedback_summary:
                try:
                    self._feedback_store().abandon_summary_run(
                        live.workspace_id,
                        live.id,
                        reason="manually_aborted",
                        now=now,
                    )
                except Exception:
                    logger.exception(
                        "Failed to abandon Feedback Reaper summary run during manual abort "
                        "workspace_id=%s task_id=%s",
                        live.workspace_id,
                        live.id,
                    )
            await self.dispatch_workspace(live.workspace_id)
            return self.tasks[live.id]
