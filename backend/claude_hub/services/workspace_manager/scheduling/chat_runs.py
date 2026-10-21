"""Timer-owned intent policy and legacy run-history projection.

Delivery, FIFO, submission fences and recovery live in ChatMessageInbox.
"""

from typing import Any, Callable, cast

from ....models.chat_message import ChatMessageEnvelope, ChatMessageState
from ...chat_message_inbox import (
    ChatMessageInbox,
    InboxError,
    InboxEvidence,
    InboxTarget,
    inspect_chat_delivery,
)
from .._constants import (
    Optional,
    ScheduledChatLiveness,
    ScheduledTask,
    ScheduledTaskRun,
    ScheduledTaskRunStatus,
    SessionKind,
    asyncio,
    datetime,
    logger,
    ttyd_manager,
    uuid,
)
from ._contract import _SchedulingHost


class _ScheduledChatRunsMixin(_SchedulingHost):
    _SCHEDULED_CHAT_ACTIVE_RUN_LIMIT = 100
    _SCHEDULED_CHAT_COLD_MAX_ATTEMPTS = 12

    @staticmethod
    def _chat_run_active(run: ScheduledTaskRun) -> bool:
        return run.status in {
            ScheduledTaskRunStatus.QUEUED,
            ScheduledTaskRunStatus.WAITING,
            ScheduledTaskRunStatus.DISPATCHING,
            ScheduledTaskRunStatus.RUNNING,
        }

    def configure_chat_delivery(self, inbox: Callable[[], ChatMessageInbox]) -> None:
        self._chat_delivery_getter = inbox

    def _delivery_inbox(self) -> ChatMessageInbox:
        getter = self._chat_delivery_getter
        inbox: ChatMessageInbox | None
        if getter is not None:
            inbox = getter()
        else:
            # Standalone manager/test embedding uses the same core with injected
            # transport, never a second scheduler drain or submission mechanism.
            inbox = self._chat_delivery_instance
            if inbox is None:

                async def submit(item: ChatMessageEnvelope, before: Callable[[], None]) -> None:
                    run = self.scheduled_task_runs[item.message_id]
                    task = self.scheduled_tasks[run.scheduled_task_id]
                    callback = self._scheduled_chat_dispatch
                    if callback is None:
                        raise InboxError(409, "Chat dispatcher is starting")
                    await callback(run, task)

                async def evidence(item: ChatMessageEnvelope) -> InboxEvidence:
                    run = self.scheduled_task_runs[item.message_id]
                    started, status = await self._scheduled_turn_lifecycle(run)
                    if status:
                        return InboxEvidence(
                            "completed"
                            if status in {"completed", "success", "ok"}
                            else ("cancelled" if status == "cancelled" else "failed")
                        )
                    return InboxEvidence("started" if started else "absent")

                inbox = ChatMessageInbox(
                    self._runtime_scheduled_tasks_file.with_name("chat_messages.json"),
                    lambda tab: InboxTarget("scheduled-test-adapter"),
                    submit,
                    evidence,
                )
                self._chat_delivery_instance = inbox
        assert inbox is not None
        inbox.scheduled_prepare = self._prepare_scheduled_intent
        inbox.scheduled_changed = self._project_scheduled_intent
        inbox.scheduled_liveness = self._scheduled_intent_liveness
        return inbox

    def _queue_scheduled_chat_run(
        self, task: ScheduledTask, scheduled_for: datetime, now: datetime
    ) -> ScheduledTaskRun:
        """Create one durable Chat run for one schedule occurrence."""
        assert task.tab_id is not None
        assert task.message is not None
        run_uuid = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"claude-hub:scheduled-chat:{task.id}:{scheduled_for.isoformat()}",
        )
        existing = self.scheduled_task_runs.get(str(run_uuid))
        if existing is not None:
            # A deterministic id makes replaying the exact same occurrence
            # idempotent without collapsing distinct future occurrences.
            task.last_run_id = existing.id
            task.last_status = existing.status.value
            task.last_error = existing.error or existing.waiting_reason
            return existing
        # Supersede blocked occurrences: a target Chat accepts one turn at a
        # time, so while it is busy/archived every interval occurrence just
        # accumulates in the FIFO and would replay as a burst of identical,
        # stale prompts once it frees up. Mark every earlier occurrence of the
        # SAME task that was never dispatched (queued/waiting) as auditable
        # SKIPPED and keep only the newest. A DISPATCHING/RUNNING occurrence
        # (or another task's run on the same tab) is always retained.
        new_key = (scheduled_for, now, str(run_uuid))
        superseded = [
            candidate
            for candidate in self.scheduled_task_runs.values()
            if candidate.scheduled_task_id == task.id
            and candidate.dispatched_at is None
            and candidate.status in {ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING}
            and (candidate.scheduled_for, candidate.queued_at, candidate.id) < new_key
        ]
        for older in superseded:
            older.status = ScheduledTaskRunStatus.SKIPPED
            older.completed_at = now
            older.waiting_reason = None
            older.error = "superseded by a newer occurrence of this schedule"
            # Set the policy projection first: cancellation callbacks must not
            # persist the advanced timer before its new occurrence exists.
            self._delivery_inbox().cancel_scheduled(older.id)
        run = ScheduledTaskRun(
            id=str(run_uuid),
            scheduled_task_id=task.id,
            tab_id=task.tab_id,
            client_turn_id=f"scheduled-{run_uuid}",
            message=task.message,
            scheduled_for=scheduled_for,
            queued_at=now,
        )
        active_ids = {
            candidate.id
            for candidate in self.scheduled_task_runs.values()
            if candidate.tab_id == task.tab_id and self._chat_run_active(candidate)
        }
        active_ids.update(
            item.message_id
            for item in self._delivery_inbox().messages.values()
            if item.tab_id == task.tab_id and item.blocks_queue
        )
        active_for_tab = len(active_ids)
        if active_for_tab >= self._SCHEDULED_CHAT_ACTIVE_RUN_LIMIT:
            run.status = ScheduledTaskRunStatus.SKIPPED
            run.completed_at = now
            run.error = (
                "target Chat backlog limit reached "
                f"({self._SCHEDULED_CHAT_ACTIVE_RUN_LIMIT} active runs)"
            )
        self.scheduled_task_runs[run.id] = run
        task.last_run_id = run.id
        task.last_status = run.status.value
        task.last_error = run.error
        return run

    async def _scheduled_turn_lifecycle(self, run: ScheduledTaskRun) -> tuple[bool, Optional[str]]:
        proof = await inspect_chat_delivery(
            run.tab_id, run.client_turn_id, legacy_errors_terminal=True
        )
        return proof.state != "absent", (
            proof.state if proof.state in {"completed", "cancelled", "failed"} else None
        )

    def _scheduled_intent_metadata(
        self, run: ScheduledTaskRun, task: ScheduledTask | None
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "origin": "scheduled_task",
            "scheduled_task_id": run.scheduled_task_id,
            "scheduled_task_run_id": run.id,
            "scheduled_task_name": task.name if task else "",
        }
        from ...dot import get_dot_service

        if get_dot_service().is_system_schedule(run.scheduled_task_id):
            metadata["yields_to_user"] = True
        return metadata

    def _admit_scheduled_intents(self) -> ChatMessageInbox:
        inbox = self._delivery_inbox()
        for run in sorted(
            list(self.scheduled_task_runs.values()),
            key=lambda r: (r.scheduled_for, r.queued_at, r.id),
        ):
            if not self._chat_run_active(run):
                continue
            existing = inbox.messages.get(run.id)
            if existing is not None:
                self._project_scheduled_intent(existing)
                continue
            task = self.scheduled_tasks.get(run.scheduled_task_id)
            metadata = self._scheduled_intent_metadata(run, task)
            try:
                incarnation = inbox._target(run.tab_id).incarnation
            except InboxError:
                incarnation = ""  # policy diagnoses and disables before admission
            states = {"running": "accepted"}
            item = ChatMessageEnvelope(
                message_id=run.id,
                call_id=run.id,
                tab_id=run.tab_id,
                turn_id=run.client_turn_id,
                state=cast(ChatMessageState, states.get(run.status.value, run.status.value)),
                created_at=run.queued_at,
                updated_at=run.dispatched_at or run.queued_at,
                status_url="",
                principal="scheduler",
                actor="scheduler",
                incarnation=incarnation,
                fingerprint=run.id,
                message=run.message,
                sequence=0,
                attempts=run.dispatch_attempts,
                scheduled_run_id=run.id,
                scheduled_task_id=run.scheduled_task_id,
                turn_metadata=metadata,
                legacy_recovery=run.status
                in {ScheduledTaskRunStatus.DISPATCHING, ScheduledTaskRunStatus.RUNNING},
            )
            try:
                inbox.admit_scheduled(
                    item, enforce_limit=run.id not in self._scheduled_legacy_run_ids
                )
            except InboxError as exc:
                if exc.status != 429:
                    raise
                run.status = ScheduledTaskRunStatus.SKIPPED
                run.error = "target Chat backlog limit reached (100 active runs)"
                run.completed_at = self._runtime_now()
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
        return inbox

    def _project_scheduled_intent(self, item: ChatMessageEnvelope) -> None:
        run = self.scheduled_task_runs.get(item.message_id)
        if run is None or not self._chat_run_active(run):
            return
        status = ScheduledTaskRunStatus("running" if item.state == "accepted" else item.state)
        error = item.error
        if status == ScheduledTaskRunStatus.FAILED and error is None:
            error = "Chat turn completed with status: failed"
        if status == ScheduledTaskRunStatus.CANCELLED and error is None:
            error = "Chat turn was cancelled or its backend runtime was lost"
        attempts = (
            item.attempts
            if self._chat_delivery_getter
            and item.state == "waiting"
            and "runtime" in (item.waiting_reason or "").lower()
            else run.dispatch_attempts
        )
        if (run.status, run.waiting_reason, run.error, run.dispatch_attempts) == (
            status,
            item.waiting_reason,
            error,
            attempts,
        ):
            return
        run.status, run.waiting_reason, run.error = status, item.waiting_reason, error
        run.dispatch_attempts = attempts
        if item.state == "accepted" and run.dispatched_at is None:
            run.dispatched_at = self._runtime_now()
        if not self._chat_run_active(run):
            run.completed_at = self._runtime_now()
        self._sync_task_from_run(run)
        self._save_scheduled_tasks()

    async def _prepare_scheduled_intent(self, item: ChatMessageEnvelope) -> str | None:
        run = self.scheduled_task_runs.get(item.message_id)
        task = self.scheduled_tasks.get(item.scheduled_task_id or "")
        if run is None or task is None or not self._chat_run_active(run):
            self._delivery_inbox().cancel_scheduled(item.message_id)
            return None
        # Exhausted one-shot disables future ticks, not its already recorded turn.
        consumed_one_shot = (
            task.run_at is not None and task.last_run_at is not None and task.next_run_at is None
        )
        if not task.enabled and not consumed_one_shot:
            self._cancel_scheduled_task_runs(
                task.id,
                statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
                reason="automation is disabled",
                now=self._runtime_now(),
            )
            self._save_scheduled_tasks()
            return None
        tab = ttyd_manager.get_tab(run.tab_id)
        reason = None
        if tab is None:
            reason = "target Chat was deleted"
        elif tab.session_kind != SessionKind.CHAT or tab.workspace_role is not None:
            reason = "target is no longer a top-level Chat session"
        elif tab.agent_type != task.agent_type:
            reason = "target Chat backend changed; edit the automation to confirm it"
        if reason:
            run.status = ScheduledTaskRunStatus.SKIPPED
            run.error, run.completed_at = reason, self._runtime_now()
            task.enabled = False
            self._delivery_inbox().cancel_scheduled(item.message_id)
            self._cancel_scheduled_task_runs(
                task.id,
                statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
                reason=reason,
                now=self._runtime_now(),
            )
            self._sync_task_from_run(run)
            self._save_scheduled_tasks()
            return None
        assert tab is not None
        if tab.archived:
            return "target Chat is archived"
        from ...goal_run import get_goal_manager

        goal = get_goal_manager().current(run.tab_id)
        if goal is not None and (
            goal.status.value == "active" or goal.dispatch_state.value != "idle"
        ):
            return "waiting for the active Goal"
        # Injected standalone transports retain their readiness contract. Native
        # production readiness is performed by the one shared inbox adapter.
        if self._chat_delivery_getter is None:
            if self._scheduled_chat_dispatch is None:
                return "Chat dispatcher is starting"
            if not await self._ensure_scheduled_chat_runtime_ready(run.tab_id):
                if run.dispatch_attempts >= self._SCHEDULED_CHAT_COLD_MAX_ATTEMPTS:
                    self._delivery_inbox()._change(
                        item.message_id,
                        "failed",
                        f"Chat runtime did not become ready after {self._SCHEDULED_CHAT_COLD_MAX_ATTEMPTS} cold-start attempts",
                    )
                    return None
                run.dispatch_attempts += 1
                self._schedule_chat_redrain(run.tab_id)
                return "waiting for the Chat runtime to start"
        return None

    async def _scheduled_intent_liveness(self, item: ChatMessageEnvelope) -> str:
        if self._scheduled_chat_liveness is None:
            return "unknown"
        try:
            result = await self._scheduled_chat_liveness(item.tab_id, item.turn_id)
            return result if result in {"active", "dead", "terminalized"} else "unknown"
        except Exception:
            logger.exception("Scheduled run liveness probe failed")
            return "unknown"

    def configure_scheduled_chat_liveness(self, callback: ScheduledChatLiveness) -> None:
        self._scheduled_chat_liveness = callback

    async def _recover_scheduled_chat_runs(self) -> None:
        inbox = self._admit_scheduled_intents()
        await inbox.reconcile(restarting=True)

    async def _reap_stale_scheduled_chat_runs(self, now: datetime) -> None:
        inbox = self._admit_scheduled_intents()
        if inbox._runner is not None:
            inbox.wake()
            return
        # Legacy API/tests may adjust history timestamps; the projection stamp
        # remains the scheduler's externally persisted dispatch time.
        for run in self.scheduled_task_runs.values():
            item = inbox.messages.get(run.id)
            if item and run.dispatched_at and item.state in {"dispatching", "accepted"}:
                item.updated_at = run.dispatched_at
        await inbox.reconcile(now=now)
        await self._drain_scheduled_chat_runs()

    def _cancel_scheduled_task_runs(
        self,
        task_id: str,
        *,
        statuses: set[ScheduledTaskRunStatus],
        reason: str,
        now: datetime,
        tab_id: Optional[str] = None,
    ) -> int:
        count = 0
        for run in self.scheduled_task_runs.values():
            if (
                run.scheduled_task_id == task_id
                and run.status in statuses
                and (tab_id is None or run.tab_id == tab_id)
            ):
                run.status = ScheduledTaskRunStatus.CANCELLED
                run.error, run.waiting_reason, run.completed_at = reason, None, now
                self._delivery_inbox().cancel_scheduled(run.id, release=True)
                count += 1
        return count

    def _sync_task_from_run(self, run: ScheduledTaskRun) -> None:
        task = self.scheduled_tasks.get(run.scheduled_task_id)
        if task is None:
            return
        current = (
            self.scheduled_task_runs.get(task.last_run_id) if task.last_run_id is not None else None
        )
        if current is not None and current.id != run.id:
            current_key = (current.scheduled_for, current.queued_at, current.id)
            run_key = (run.scheduled_for, run.queued_at, run.id)
            if current_key > run_key:
                # An older FIFO entry can keep transitioning after a newer
                # occurrence is queued. Keep task-level "last" fields tied
                # to the newest occurrence; its own run record still captures
                # every transition.
                return
        task.last_run_id = run.id
        task.last_status = run.status.value
        task.last_error = run.error or run.waiting_reason
        task.updated_at = self._runtime_now()

    async def on_scheduled_chat_turn_completed(
        self, tab_id: str, turn_id: str, status: str
    ) -> None:
        inbox = self._admit_scheduled_intents()
        await inbox.completed(tab_id, turn_id, status)
        if not inbox._pump_locks.setdefault(tab_id, asyncio.Lock()).locked():
            await self._drain_scheduled_chat_runs(tab_id)

    async def _drain_scheduled_chat_runs(self, tab_id: Optional[str] = None) -> None:
        inbox = self._admit_scheduled_intents()
        inbox.wake()
        if inbox._runner is not None:
            return
        tabs = {m.tab_id for m in inbox.messages.values() if m.blocks_queue}
        for current in ([tab_id] if tab_id else sorted(tabs)):
            await inbox.pump(current)
