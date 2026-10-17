"""Processing-lease recovery and fail-closed uncertain-delivery state."""

import claude_hub.services.managed_chat as managed_chat
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _MessageRecoveryMixin(_WorkspaceManagerBase):
    def _chat_delivery_state(
        self, session: ManagedSession, call_id: str
    ) -> managed_chat.DeliveryState:
        try:
            return managed_chat.delivery_state(session, call_id)
        except RuntimeError:
            logger.exception(
                "Cannot read Chat delivery evidence for call_id=%s session=%s",
                call_id,
                session.id,
            )
            return "uncertain"

    def _rollback_chat_processing_to_pending(self, session_id: str, call_ids: list[str]) -> None:
        """Retry proven-unsent envelopes ahead of later queued Chat input."""
        session = self.sessions.get(session_id)
        if session is None:
            return
        retry_ids = [call_id for call_id in session.processing_call_ids if call_id in call_ids]
        self._rollback_processing_to_pending(session.task_id, session_id, retry_ids)
        session = self.sessions[session_id]
        self.sessions[session_id] = session.model_copy(
            update={
                "pending_call_ids": retry_ids
                + [call_id for call_id in session.pending_call_ids if call_id not in retry_ids]
            }
        )
        task = self.tasks.get(session.task_id) if session.task_id else None
        if task is not None:
            task_retry_ids = [call_id for call_id in retry_ids if call_id in task.pending_call_ids]
            self.tasks[task.id] = task.model_copy(
                update={
                    "pending_call_ids": task_retry_ids
                    + [
                        call_id
                        for call_id in task.pending_call_ids
                        if call_id not in task_retry_ids
                    ]
                }
            )
        self._save_state()

    def _expire_processing_leases(self, session_id: str, max_age_seconds: float = 300.0) -> int:
        """Move stale ``processing_call_ids`` to ``uncertain_call_ids``.

        A call_id in ``processing_call_ids`` means the pump persisted the
        intent to deliver and sent (or was about to send) the message to
        tmux. The Hub CANNOT prove the message reached the tmux input
        buffer, so we cannot safely re-deliver it (that could duplicate the
        model turn).

        Therefore, for **live** sessions we NEVER move processing call_ids
        back to pending. They stay in processing until the worker ACKs them
        (→ delivered) or the session is stopped (→ uncertain).

        For sessions whose tmux session is gone
        (``ManagedSessionStatus.STOPPED``), the input buffer may have been
        destroyed, but we still cannot prove the message was not delivered
        before the session died. So we move them to ``uncertain_call_ids``
        (fail-closed) rather than back to pending. A ``delivery:uncertain``
        event is emitted to the supervisor.

        Returns the number of call_ids moved to uncertain.
        """
        session = self.sessions.get(session_id)
        if not session:
            return 0
        if not session.processing_call_ids:
            return 0

        # Only expire for sessions whose tmux inbox is gone. For live
        # sessions the message may already be in the tmux input buffer;
        # re-delivering it would risk a duplicate turn.
        if session.status != ManagedSessionStatus.STOPPED:
            return 0

        now = datetime.now(timezone.utc)
        expired: list[str] = []
        for call_id in session.processing_call_ids:
            claimed_at = session.processing_call_ids_at.get(call_id)
            if claimed_at is None:
                # No timestamp — treat as expired (conservative).
                expired.append(call_id)
                continue
            age = (now - claimed_at).total_seconds()
            if age >= max_age_seconds:
                expired.append(call_id)

        if not expired:
            return 0

        self._mark_processing_as_uncertain(session_id, expired)
        return len(expired)

    async def _recover_processing_via_receipt(self, session_id: str) -> int:
        """Reconcile ``processing_call_ids`` against the tmux-server receipt.

        After a Hub restart (or on each monitor tick), a call_id in
        ``processing_call_ids`` may or may not have actually been pasted into
        the tmux input buffer. The tmux-server receipt (a session user option
        set atomically with the paste) lets us distinguish:

        * **receipt present** — the paste happened. Keep the call_id in
          ``processing`` and wait for the worker's ACK. Do NOT re-deliver.
        * **receipt absent, tmux session alive** — the paste did NOT happen
          (the Hub died before the tmux command ran, or the command failed
          before setting the receipt). Safe to move the call_id back to
          ``pending`` so the pump re-delivers it.
        * **tmux session gone / unqueryable** — we cannot prove either way.
          Fail closed: move to ``uncertain`` (no automatic resend; operator
          retry required).

        Returns the number of call_ids whose state changed.
        """
        session = self.sessions.get(session_id)
        if not session or not session.processing_call_ids:
            return 0
        if session.session_kind == SessionKind.CHAT:
            lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                return self._recover_chat_processing_via_receipt(session_id)

        to_pending: list[str] = []
        to_uncertain: list[str] = []

        for call_id in list(session.processing_call_ids):
            try:
                receipt_set = await self._query_tmux_receipt(session.tmux_session, call_id)
            except RuntimeError:
                # Session gone or tmux query failed — fail closed.
                to_uncertain.append(call_id)
                continue
            if receipt_set:
                # Paste happened. The message may still be sitting in the
                # input box if the Hub died between the atomic paste+first
                # Enter and the submit verification loop. Nudge Enter (no
                # re-paste) so the TUI accepts it, then keep the call_id
                # in processing to await the worker's ACK.
                message = session.pending_messages.get(call_id)
                if not message:
                    # Fail closed: without the original message body we
                    # cannot verify whether the input is still pending,
                    # and a blind Enter could submit an unrelated line.
                    # Move to uncertain so an operator can decide.
                    to_uncertain.append(call_id)
                    continue
                try:
                    await self._ensure_submitted_without_repaste(session.tmux_session, message)
                except Exception:
                    # Cannot verify submit state (capture failed or input
                    # still pending after retries). Fail closed: move to
                    # uncertain so an operator can decide. The receipt
                    # remains set so no future replay re-pastes.
                    logger.exception(
                        "pump: submit-nudge failed for receipt-present call_id=%s "
                        "session %s; moving to uncertain",
                        call_id,
                        session_id,
                    )
                    to_uncertain.append(call_id)
                continue
            # Receipt absent on a live session: the paste never ran. Safe to
            # return to pending for re-delivery.
            to_pending.append(call_id)

        changed = 0
        if to_pending:
            self._rollback_processing_to_pending(session.task_id, session_id, to_pending)
            changed += len(to_pending)
        if to_uncertain:
            self._mark_processing_as_uncertain(session_id, to_uncertain)
            changed += len(to_uncertain)
        return changed

    def _recover_chat_processing_via_receipt(self, session_id: str) -> int:
        """Reconcile provider handoff evidence without using UI or tmux history."""
        session = self.sessions.get(session_id)
        if session is None:
            return 0
        to_pending: list[str] = []
        to_uncertain: list[str] = []
        for call_id in session.processing_call_ids:
            state = self._chat_delivery_state(session, call_id)
            if state == "not_sent":
                to_pending.append(call_id)
            elif state != "accepted":
                # The pump lock excludes live sends. Persisted in-flight
                # evidence after a crash cannot prove provider acceptance.
                to_uncertain.append(call_id)
        if to_pending:
            self._rollback_chat_processing_to_pending(session_id, to_pending)
        if to_uncertain:
            self._mark_processing_as_uncertain(session_id, to_uncertain)
        return len(to_pending) + len(to_uncertain)

    def _mark_processing_as_uncertain(self, session_id: str, call_ids: list[str]) -> None:
        """Move call_ids from ``processing_call_ids`` to ``uncertain_call_ids``.

        Fail-closed: we cannot prove the message was not delivered, so we do
        NOT re-send it and do NOT silently mark it delivered. Emit a
        ``delivery:uncertain`` event to the supervisor so the condition is
        visible and an operator can decide whether to retry.
        """
        if not call_ids:
            return
        uncertain_set = set(call_ids)

        session = self.sessions.get(session_id)
        if session is None:
            return

        processing = [c for c in session.processing_call_ids if c not in uncertain_set]
        uncertain = list(session.uncertain_call_ids)
        for cid in call_ids:
            if cid not in uncertain:
                uncertain.append(cid)
        processing_call_ids_at = {
            cid: ts
            for cid, ts in session.processing_call_ids_at.items()
            if cid not in uncertain_set
        }
        self.sessions[session_id] = session.model_copy(
            update={
                "processing_call_ids": processing,
                "uncertain_call_ids": uncertain,
                "processing_call_ids_at": processing_call_ids_at,
            }
        )

        # Mirror on the task.
        task = self.tasks.get(session.task_id) if session.task_id else None
        if task is not None:
            task_processing = [c for c in task.processing_call_ids if c not in uncertain_set]
            task_uncertain = list(task.uncertain_call_ids)
            for cid in call_ids:
                if cid not in task_uncertain:
                    task_uncertain.append(cid)
            self.tasks[task.id] = task.model_copy(
                update={
                    "processing_call_ids": task_processing,
                    "uncertain_call_ids": task_uncertain,
                }
            )

        self._save_state()

        # Emit delivery:uncertain on TaskMailbox when the session is assigned to
        # a Task; standalone sessions keep fail-closed state on the session only.
        for call_id in call_ids:
            self._emit_delivery_uncertain(session.workspace_id, session_id, call_id)

        logger.info(
            "pump: moved %d processing call_ids to uncertain for session %s (fail-closed)",
            len(call_ids),
            session_id,
        )

    def _emit_delivery_uncertain(self, workspace_id: str, session_id: str, call_id: str) -> None:
        """Record ``delivery:uncertain`` on TaskMailbox for Task-assigned sessions.

        Standalone Resident/legacy sessions without a Task assignment keep
        fail-closed delivery state on the session (and bound task mirror) only.
        No legacy supervisor orchestration events are emitted.
        """

        from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
        from claude_hub.services.task_graph import make_task_consumer_key

        session = self.sessions.get(session_id)
        if session is None or session.workspace_id != workspace_id:
            return

        task_id = session.current_task_id or session.task_id
        if not task_id:
            return

        task = self.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            return
        if session_id not in {task.session_id, task.review_session_id}:
            return

        actor_role = (
            TaskActorRole.REVIEWER if session_id == task.review_session_id else TaskActorRole.WORKER
        )
        uncertain_call_id = f"delivery:uncertain:{call_id}"
        try:
            self.task_mailbox.append_event(
                workspace_id=workspace_id,
                task_id=task.id,
                actor_session_id=session_id,
                actor_role=actor_role,
                event_type=TaskEventType.PROGRESS,
                call_id=uncertain_call_id,
                action="delivery:uncertain",
                target=session_id,
                consumer_key=make_task_consumer_key(task.id),
                payload={
                    "call_id": call_id,
                    "session_id": session_id,
                    "reason": "in-flight call_id lost on session stop / crash; "
                    "cannot prove delivery. Operator retry required.",
                },
                persist=True,
                wake=False,
            )
        except Exception:
            logger.exception(
                "Failed to append delivery:uncertain Task event workspace_id=%s "
                "session_id=%s call_id=%s task_id=%s",
                workspace_id,
                session_id,
                call_id,
                task.id,
            )
