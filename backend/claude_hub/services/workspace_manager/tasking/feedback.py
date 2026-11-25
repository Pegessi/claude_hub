"""Task domain implementation split from the compatibility mixin."""

from claude_hub.services.feedback.reaper import (
    apply_reaper_output,
    default_reaper_model_caller,
    parse_reaper_response,
)
from claude_hub.services.feedback_automation import FeedbackAutomationStore
from claude_hub.services.task_transitions import apply_task_update

from .._constants import *  # noqa: F401,F403
from .core import _TaskCoreMixin


class _TaskFeedbackSummaryMixin(_TaskCoreMixin):
    def _reaper_jobs(self) -> dict[str, asyncio.Task[FeedbackSummaryRun]]:
        jobs = getattr(self, "_feedback_reaper_jobs", None)
        if jobs is None:
            jobs = {}
            self._feedback_reaper_jobs = jobs
        return jobs

    def _reaper_active_runs(self) -> dict[str, FeedbackSummaryRun]:
        runs = getattr(self, "_feedback_reaper_active_runs", None)
        if runs is None:
            runs = {}
            self._feedback_reaper_active_runs = runs
        return runs

    def _has_active_feedback_reaper_job(self, workspace_id: str | None = None) -> bool:
        jobs = self._reaper_jobs()
        if workspace_id is not None:
            job = jobs.get(workspace_id)
            return job is not None and not job.done()
        return any(not job.done() for job in jobs.values())

    async def _stop_feedback_reaper_jobs(self) -> None:
        jobs = list(self._reaper_jobs().values())
        self._reaper_jobs().clear()
        self._reaper_active_runs().clear()
        for job in jobs:
            if not job.done():
                job.cancel()
        for job in jobs:
            try:
                await job
            except BaseException:
                pass

    async def wait_for_feedback_reaper(self, workspace_id: str) -> FeedbackSummaryRun | None:
        """Await any currently running silent background Feedback Reaper job."""
        job = self._reaper_jobs().get(workspace_id)
        if job is None:
            return None
        return await job

    def _update_feedback_summary_status(
        self,
        workspace_id: str,
        *,
        running: bool,
        last_run_id: str | None = None,
        last_outcome: str | None = None,
        last_summarized_at: datetime | None = None,
        last_error: str | None = None,
        clear_error: bool = False,
    ) -> None:
        """Persist silent reaper status onto automation state while preserving settings."""
        try:
            controls = FeedbackAutomationStore(self._feedback_store().state_root)
            now = self._runtime_now()
            state = controls.state(workspace_id, getattr(self, "_feedback_started_at", now))
            state.running = running
            if last_run_id is not None:
                state.last_run_id = last_run_id
            if last_outcome is not None:
                state.last_outcome = last_outcome
            if last_summarized_at is not None:
                state.last_summarized_at = last_summarized_at
            if clear_error:
                state.last_error = None
            elif last_error is not None:
                state.last_error = last_error
            controls.save(workspace_id, state)
        except Exception:
            logger.debug(
                "Skipping silent feedback status update workspace_id=%s",
                workspace_id,
                exc_info=True,
            )

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
        """Legacy helper: detect any manually-injected unfinished feedback_reaper task."""
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
        """Legacy compatibility handler for pre-existing feedback_reaper tasks."""
        current = self.tasks.get(task.id, task)
        self._release_task_session(current)
        now = self._runtime_now()
        updated = apply_task_update(
            current,
            {
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
            },
            cause="feedback_reaper",
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

    async def _execute_silent_feedback_reaper_job(
        self,
        workspace_id: str,
        run: FeedbackSummaryRun,
        prompt: str,
    ) -> FeedbackSummaryRun:
        store = self._feedback_store()
        current_task = asyncio.current_task()
        try:
            caller = getattr(self, "_feedback_reaper_caller", None) or default_reaper_model_caller
            raw_output = await caller(workspace_id, prompt)
            parsed = parse_reaper_response(raw_output)
            done_now = self._runtime_now()
            applied = apply_reaper_output(store, workspace_id, parsed, now=done_now)
            store.commit_staged_summary_input(workspace_id, run.id)
            store.discard_staged_summary_input(workspace_id, run.id)
            completed_run = run.model_copy(
                update={
                    "created_lesson_ids": applied.created_lesson_ids,
                    "merged_lesson_ids": applied.merged_lesson_ids,
                    "skipped_reason": applied.skipped_reason,
                    "running": False,
                    "error": None,
                    "completed_at": done_now,
                }
            )
            store.write_summary_run(workspace_id, completed_run)
            self._update_feedback_summary_status(
                workspace_id,
                running=False,
                last_run_id=completed_run.id,
                last_outcome=(
                    "completed"
                    if (applied.created_lesson_ids or applied.merged_lesson_ids)
                    else (applied.skipped_reason or "no_new_lessons")
                ),
                last_summarized_at=done_now,
                clear_error=True,
            )
            return completed_run
        except asyncio.CancelledError:
            cancel_now = self._runtime_now()
            try:
                store.discard_staged_summary_input(workspace_id, run.id)
                cancelled_run = run.model_copy(
                    update={
                        "running": False,
                        "skipped_reason": "cancelled",
                        "completed_at": cancel_now,
                    }
                )
                store.write_summary_run(workspace_id, cancelled_run)
            except OSError:
                pass
            self._update_feedback_summary_status(
                workspace_id,
                running=False,
                last_run_id=run.id,
                last_outcome="cancelled",
            )
            raise
        except Exception as exc:
            fail_now = self._runtime_now()
            error_text = str(exc).replace("\n", " ")[:400] or type(exc).__name__
            logger.warning(
                "Silent Feedback Reaper failed workspace_id=%s run_id=%s error=%s",
                workspace_id,
                run.id,
                error_text,
                exc_info=True,
            )
            try:
                store.discard_staged_summary_input(workspace_id, run.id)
            except OSError:
                logger.debug(
                    "Failed to discard staged feedback input workspace_id=%s run_id=%s",
                    workspace_id,
                    run.id,
                    exc_info=True,
                )
            failed_run = run.model_copy(
                update={
                    "running": False,
                    "error": error_text,
                    "skipped_reason": f"error:{type(exc).__name__}",
                    "completed_at": fail_now,
                }
            )
            try:
                store.write_summary_run(workspace_id, failed_run)
            except OSError:
                logger.debug(
                    "Failed to persist failed feedback summary run workspace_id=%s run_id=%s",
                    workspace_id,
                    run.id,
                    exc_info=True,
                )
            self._update_feedback_summary_status(
                workspace_id,
                running=False,
                last_run_id=run.id,
                last_outcome="failed",
                last_error=error_text,
            )
            return failed_run
        finally:
            jobs = self._reaper_jobs()
            if jobs.get(workspace_id) is current_task:
                jobs.pop(workspace_id, None)
                self._reaper_active_runs().pop(workspace_id, None)

    async def _summarize_workspace_feedback_locked(
        self,
        workspace_id: str,
        payload: FeedbackSummaryRequest,
        *,
        summary_input: dict[str, Any] | None = None,
    ) -> FeedbackSummaryRun:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)
        now = self._runtime_now()
        store = self._feedback_store()

        # Single-flight per workspace: coalesce overlapping triggers cleanly.
        existing_job = self._reaper_jobs().get(workspace_id)
        if existing_job is not None and not existing_job.done():
            active_run = self._reaper_active_runs().get(workspace_id)
            if active_run is not None:
                return active_run

        if summary_input is None:
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
                running=False,
                error=None,
                created_at=now,
                completed_at=now,
            )
            store.write_summary_run(workspace_id, run)
            return run

        run_id = str(summary_input["run_id"])
        try:
            prompt, committed_task_ids, committed_paths = (
                self._build_workspace_feedback_summary_prompt(workspace, summary_input)
            )
            run = FeedbackSummaryRun(
                id=run_id,
                workspace_id=workspace_id,
                task_id=None,
                mode=payload.mode,
                input_record_ids=committed_task_ids,
                cache_hit=False,
                prompt_version=summary_input["prompt_version"],
                running=True,
                error=None,
                created_at=now,
                completed_at=None,
            )
            store.write_summary_run(workspace_id, run)
            store.stage_summary_input(summary_input, committed_paths)
        except Exception as exc:
            try:
                store.discard_staged_summary_input(workspace_id, run_id)
            except OSError:
                pass
            error_text = str(exc).replace("\n", " ")[:400] or type(exc).__name__
            logger.warning(
                "Silent Feedback Reaper prompt preparation failed workspace_id=%s run_id=%s error=%s",
                workspace_id,
                run_id,
                error_text,
                exc_info=True,
            )
            failed_run = FeedbackSummaryRun(
                id=run_id,
                workspace_id=workspace_id,
                task_id=None,
                mode=payload.mode,
                input_record_ids=[],
                cache_hit=False,
                prompt_version=summary_input["prompt_version"],
                skipped_reason=f"error:{type(exc).__name__}",
                running=False,
                error=error_text,
                created_at=now,
                completed_at=now,
            )
            store.write_summary_run(workspace_id, failed_run)
            self._update_feedback_summary_status(
                workspace_id,
                running=False,
                last_run_id=failed_run.id,
                last_outcome="failed",
                last_error=error_text,
            )
            return failed_run

        self._update_feedback_summary_status(
            workspace_id,
            running=True,
            last_run_id=run.id,
            last_outcome="running",
            clear_error=True,
        )
        job = asyncio.create_task(
            self._execute_silent_feedback_reaper_job(workspace_id, run, prompt),
            name=f"silent-feedback-reaper-{workspace_id[:8]}-{run.id[:8]}",
        )
        self._reaper_jobs()[workspace_id] = job
        self._reaper_active_runs()[workspace_id] = run
        return run
