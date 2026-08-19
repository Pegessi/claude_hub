"""Session messaging and tmux output capture."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _MessagingMixin:
    async def send_session_message(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
    ) -> None:
        """Enqueue a message for a managed session's terminal.

        Sender-side semantics: **at-least-once transport via a durable inbox**.

        The sender does NOT write to tmux directly. Instead it:
          1. Skips call_ids already in ``delivered_call_ids`` (committed by the
             worker's ACK) or ``processing_call_ids`` (claimed by the receiver
             pump — a delivery is already in flight).
          2. Persists the message body in ``session.pending_messages[call_id]``
             (the durable inbox) and appends ``call_id`` to
             ``pending_call_ids``.
          3. Triggers the receiver pump (``_pump_session_messages``).

        The pump performs the actual CLAIM (pending → processing) and the tmux
        send. Because the claim happens before the tmux write, a duplicate
        ``send_session_message`` for the same call_id while the pump holds the
        claim is a no-op — the Hub-managed side effect (the tmux prompt) is
        applied exactly once per call_id until the worker ACKs (or the lease
        expires).

        A call_id stays in ``pending_call_ids`` (or ``processing_call_ids``)
        until the worker ACKs it via ``acked_call_ids`` in a report. On Hub
        restart, any call_id still in ``pending_call_ids`` is re-deliverable;
        any call_id in ``processing_call_ids`` whose lease has expired is moved
        back to ``pending_call_ids`` for re-delivery.

        NOTE: This guarantees exactly-once of the **Hub-managed** effect (the
        tmux prompt). It does NOT guarantee exactly-once of arbitrary external
        tool side effects the model may invoke — those require the call_id to
        be propagated as an idempotency key by the model itself.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        if call_id:
            # Sender-side gate: skip call_ids that the receiver pump has
            # already claimed (processing) or that the worker has committed
            # (delivered). In both cases, delivery is already in flight or
            # done, so the sender must NOT enqueue again.
            if call_id in session.delivered_call_ids:
                logger.debug(
                    "send_session_message call_id=%s already committed by session %s; skipping",
                    call_id,
                    session_id,
                )
                return
            if call_id in session.processing_call_ids:
                logger.debug(
                    "send_session_message call_id=%s already claimed by session %s pump; skipping",
                    call_id,
                    session_id,
                )
                return

            # Persist the message in the durable inbox and record the call_id
            # as pending. If already pending, do not duplicate.
            pending_messages = dict(session.pending_messages)
            pending_messages[call_id] = message
            pending_call_ids = list(session.pending_call_ids)
            if call_id not in pending_call_ids:
                pending_call_ids.append(call_id)
            self.sessions[session_id] = session.model_copy(
                update={
                    "pending_messages": pending_messages,
                    "pending_call_ids": pending_call_ids,
                }
            )
            self._save_state()
            session = self.sessions[session_id]

            # Kick the receiver pump to claim and deliver the message.
            await self._pump_session_messages(session_id)
            return

        # No call_id: fire-and-forget (no delivery guarantee). Used for
        # one-shot prompts that don't need at-least-once semantics.
        persisted = self._persist_attachments(
            session.workspace_id,
            f"{session.id}-message-{uuid.uuid4().hex[:8]}",
            attachments or [],
        )
        await self._ensure_session_ready_for_send(session)
        await self._send_tmux_message(
            session.tmux_session,
            self._append_attachment_block(message, persisted),
        )

    async def _pump_session_messages(self, session_id: str) -> None:
        """Receiver pump: claim pending call_ids and deliver them to tmux.

        Lifecycle of a call_id through the durable receiver gate:

          pending_call_ids  ──claim──▶  processing_call_ids  ──tmux-send──▶  delivered_call_ids

        1. **Claim**: atomically move the call_id from ``pending_call_ids`` to
           ``processing_call_ids``. The claim happens BEFORE the tmux write so
           a concurrent ``send_session_message`` for the same call_id sees it
           in ``processing_call_ids`` and skips — the tmux prompt is applied
           exactly once.

        2. **Deliver**: send the message to tmux. On success, the call_id is
           moved to ``delivered_call_ids`` — the **receiver-owned durable
           receipt**. A call_id in ``delivered_call_ids`` is NEVER re-delivered
           by the Hub, even across a crash. This guarantees one call_id cannot
           produce two tmux prompts.

           If the tmux send fails, the call_id stays in ``processing_call_ids``;
           the lease-expiry step (``_expire_processing_leases``) moves it back
           to ``pending_call_ids`` so a fresh pump cycle can retry.

        3. **Worker ACK** (``_ack_call_ids``): the worker confirms it processed
           the message. Since the call_id is already in ``delivered_call_ids``
           (moved there on tmux send), the ACK is a no-op for the call_id's
           lifecycle. The ACK still serves as the worker's signal that it has
           finished processing and is ready for the next message.

        The tmux session persists across Hub restarts, so a message that was
        successfully sent to tmux remains visible to the worker even if the Hub
        crashes before the worker ACKs. The Hub does NOT re-deliver it.
        """
        session = self.sessions.get(session_id)
        if not session:
            return

        # Snapshot the pending call_ids to deliver. We iterate over a copy
        # because we mutate the session during the loop.
        pending = list(session.pending_call_ids)
        for call_id in pending:
            # Re-fetch the session each iteration: a previous iteration may
            # have mutated it.
            session = self.sessions.get(session_id)
            if not session:
                return
            if call_id not in session.pending_call_ids:
                # Already claimed by a concurrent pump cycle.
                continue

            message = session.pending_messages.get(call_id)
            if message is None:
                # Inbox entry missing — should not happen, but guard against
                # it. Move to delivered to avoid an infinite loop.
                logger.warning(
                    "pump: call_id=%s in pending_call_ids but missing from pending_messages; "
                    "marking delivered",
                    call_id,
                )
                self._ack_call_ids(session.task_id, session_id, [call_id])
                continue

            # ---- CLAIM: pending → processing ----
            # Move the call_id from pending to processing on BOTH the session
            # and its task (if any) so the two stay in sync. The claim is
            # atomic at the session level; the task mirror follows.
            now = datetime.now(timezone.utc)
            pending_call_ids = [c for c in session.pending_call_ids if c != call_id]
            processing_call_ids = list(session.processing_call_ids)
            if call_id not in processing_call_ids:
                processing_call_ids.append(call_id)
            processing_call_ids_at = dict(session.processing_call_ids_at)
            processing_call_ids_at[call_id] = now
            session_update = {
                "pending_call_ids": pending_call_ids,
                "processing_call_ids": processing_call_ids,
                "processing_call_ids_at": processing_call_ids_at,
            }
            task_update: dict[str, Any] = {}
            task = self.tasks.get(session.task_id) if session.task_id else None
            if task is not None and call_id in task.pending_call_ids:
                task_pending = [c for c in task.pending_call_ids if c != call_id]
                task_processing = list(task.processing_call_ids)
                if call_id not in task_processing:
                    task_processing.append(call_id)
                task_update = {
                    "pending_call_ids": task_pending,
                    "processing_call_ids": task_processing,
                }
            self.sessions[session_id] = session.model_copy(update=session_update)
            if task is not None and task_update:
                self.tasks[task.id] = task.model_copy(update=task_update)
            self._save_state()

            # ---- DELIVER: send to tmux ----
            try:
                persisted = self._persist_attachments(
                    session.workspace_id,
                    f"{session.id}-message-{uuid.uuid4().hex[:8]}",
                    [],
                )
                await self._ensure_session_ready_for_send(self.sessions[session_id])
                full_message = f"[call_id:{call_id}]\n{message}"
                await self._send_tmux_message(
                    self.sessions[session_id].tmux_session,
                    self._append_attachment_block(full_message, persisted),
                )
            except Exception:
                # Delivery failed. The call_id stays in processing_call_ids;
                # lease expiry will move it back to pending for retry.
                logger.exception(
                    "pump: failed to deliver call_id=%s to session %s; " "lease expiry will retry",
                    call_id,
                    session_id,
                )
                # Re-raise so callers know delivery failed, but the claim is
                # already persisted so we don't double-send on retry.
                raise

            # ---- RECEIPT: processing → delivered ----
            # The tmux send succeeded. Move the call_id to delivered_call_ids
            # — the receiver-owned durable receipt. The Hub will NEVER
            # re-deliver this call_id, even across a crash. This guarantees
            # one call_id cannot produce two tmux prompts.
            #
            # Pass the session's task_id so the task's call_id lists stay in
            # sync with the session's (the claim step updated both).
            self._ack_call_ids(session.task_id, session_id, [call_id])

    def _expire_processing_leases(self, session_id: str, max_age_seconds: float = 300.0) -> int:
        """Move stale ``processing_call_ids`` back to ``pending_call_ids``.

        A call_id in ``processing_call_ids`` means the pump claimed it and
        sent it to tmux, but the worker has not yet ACKed. If the Hub crashed
        after the claim (or the worker died), the call_id would be stuck in
        ``processing`` forever and never re-delivered.

        This method moves any call_id whose claim timestamp
        (``processing_call_ids_at[call_id]``) is older than
        ``max_age_seconds`` back to ``pending_call_ids`` so the pump can
        re-claim and re-deliver it.

        Returns the number of call_ids moved back to pending.
        """
        session = self.sessions.get(session_id)
        if not session:
            return 0
        if not session.processing_call_ids:
            return 0

        now = datetime.now(timezone.utc)
        expired: list[str] = []
        for call_id in session.processing_call_ids:
            claimed_at = session.processing_call_ids_at.get(call_id)
            if claimed_at is None:
                # No timestamp — treat as expired (conservative: re-deliver).
                expired.append(call_id)
                continue
            age = (now - claimed_at).total_seconds()
            if age >= max_age_seconds:
                expired.append(call_id)

        if not expired:
            return 0

        expired_set = set(expired)
        pending_call_ids = list(session.pending_call_ids)
        processing_call_ids = [c for c in session.processing_call_ids if c not in expired_set]
        processing_call_ids_at = {
            cid: ts for cid, ts in session.processing_call_ids_at.items() if cid not in expired_set
        }
        for call_id in expired:
            if call_id not in pending_call_ids:
                pending_call_ids.append(call_id)
        self.sessions[session_id] = session.model_copy(
            update={
                "pending_call_ids": pending_call_ids,
                "processing_call_ids": processing_call_ids,
                "processing_call_ids_at": processing_call_ids_at,
            }
        )

        # Mirror on the task: move expired call_ids back to pending.
        task = self.tasks.get(session.task_id) if session.task_id else None
        if task is not None:
            task_pending = list(task.pending_call_ids)
            task_processing = [c for c in task.processing_call_ids if c not in expired_set]
            for call_id in expired:
                if call_id in task.processing_call_ids and call_id not in task_pending:
                    task_pending.append(call_id)
            self.tasks[task.id] = task.model_copy(
                update={
                    "pending_call_ids": task_pending,
                    "processing_call_ids": task_processing,
                }
            )

        self._save_state()
        logger.info(
            "pump: expired %d processing call_ids for session %s",
            len(expired),
            session_id,
        )
        return len(expired)

    async def _ensure_session_ready_for_send(self, session: ManagedSession) -> None:
        created = await ttyd_manager.ensure_tab_tmux_session(session.tab_id)
        if not created:
            return

        deadline = asyncio.get_running_loop().time() + 12
        last_output = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                last_output = await self._capture_tmux_output(session.tmux_session)
            except RuntimeError:
                await asyncio.sleep(0.3)
                continue
            if self._agent_input_ready(last_output):
                return
            await asyncio.sleep(0.5)

        logger.warning(
            "Timed out waiting for workspace agent input prompt before sending to %s. "
            "Sending anyway. Last output tail: %s",
            session.id,
            last_output[-200:],
        )

    async def _capture_tmux_output(self, tmux_session: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-p",
            "-S",
            "-120",
            "-t",
            tmux_session,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux capture-pane failed with code {proc.returncode}")
        return stdout.decode("utf-8", errors="ignore")

    def _agent_input_ready(self, output: str) -> bool:
        lower = output.lower()
        return any(
            marker in lower
            for marker in (
                "implement {feature}",
                "? for shortcuts",
                "/ for commands",
                "openai codex",
                "claude code",
                "permissions:",
                "cursor agent",
                "/auto-run",
            )
        )
