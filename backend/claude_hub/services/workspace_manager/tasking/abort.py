"""Task domain implementation split from the compatibility mixin."""

from claude_hub.services.task_transitions import apply_task_update

from ....models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from ...request_fingerprint import request_fingerprint
from ...task_graph import task_inbox_consumer_key
from .._constants import *  # noqa: F401,F403
from .followup import _TaskFollowupMixin

_ABORT_ACTOR_ROLES = frozenset({TaskActorRole.SUPERVISOR, TaskActorRole.HUMAN})
_ABORT_STATUSES = frozenset(
    {
        WorkspaceTaskStatus.QUEUED,
        WorkspaceTaskStatus.WORKING,
        WorkspaceTaskStatus.REVIEW,
    }
)


class _TaskAbortMixin(_TaskFollowupMixin):
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
        updated = apply_task_update(
            task,
            {
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
            },
            cause="abort",
        )
        self.tasks[task.id] = updated
        return updated

    async def _abort_task_workspace(
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
            now = self._runtime_now()
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
                self._save_workspace_state(resolved_workspace_id)
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
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        async with self._workspace_execution_operation(task):
            return await self._abort_task_workspace(
                task_id,
                payload,
                workspace_id=workspace_id,
                call_id=call_id,
                actor_session_id=actor_session_id,
                actor_role=actor_role,
                compat_author_run_id=compat_author_run_id,
            )
