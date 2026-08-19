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

        Sender-side semantics: **exactly-once delivery to the tmux inbox**.

        The sender does NOT write to tmux directly. Instead it:
          1. Skips call_ids already in ``delivered_call_ids`` (committed by the
             worker's ACK) or ``processing_call_ids`` (already delivered to
             the tmux inbox by the receiver pump).
          2. Persists the message body in ``session.pending_messages[call_id]``
             (the durable inbox) and appends ``call_id`` to
             ``pending_call_ids``.
          3. Triggers the receiver pump (``_pump_session_messages``).

        The pump performs the actual delivery: it checks the tmux pane for the
        ``[call_id:<id>]`` marker (receiver-verifiable receipt), sends the
        message if the marker is absent, and THEN moves the call_id to
        ``processing_call_ids``. Because the call_id stays in
        ``pending_call_ids`` until the tmux send succeeds, a duplicate
        ``send_session_message`` for the same call_id simply overwrites the
        pending message body (a no-op for identical payloads).

        A call_id stays in ``processing_call_ids`` until the worker ACKs it
        via ``acked_call_ids`` in a report. The tmux input buffer is the
        durable receiver: once a message is sent to tmux, the worker will
        read it exactly once. Therefore the Hub NEVER re-delivers a
        ``processing`` call_id to a live session — that would append it to
        the tmux buffer a second time and produce a duplicate model turn.

        On Hub restart, only ``pending_call_ids`` (never sent to tmux, or
        sent but not yet persisted as processing) are re-deliverable. The
        pump's pane-marker check ensures that a call_id which was sent but
        whose processing state was not persisted is NOT re-sent (the marker
        is already in the pane). ``processing_call_ids`` were already written
        to the tmux inbox and are left for the worker to ACK. The only
        exception is a session whose tmux session itself is gone
        (``ManagedSessionStatus.STOPPED``): the input buffer was destroyed,
        so its processing call_ids are moved back to pending for re-delivery.

        NOTE: This guarantees exactly-once delivery of the **Hub-managed**
        effect (the tmux prompt). It does NOT guarantee exactly-once of
        arbitrary external tool side effects the model may invoke — those
        require the call_id to be propagated as an idempotency key by the
        model itself.
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
        """Receiver pump: deliver pending call_ids to the tmux inbox exactly once.

        Lifecycle of a call_id through the durable receiver gate:

          pending_call_ids  ──deliver──▶  processing_call_ids  ──worker-ACK──▶  delivered_call_ids

        The tmux input buffer IS the durable receiver: once a message is sent
        to tmux, the worker reads it exactly once. Therefore the Hub must
        guarantee that each call_id is written to the tmux buffer exactly
        once — no loss, no duplicate.

        **Receiver-verifiable delivery (no loss, no duplicate):**

        For each pending call_id the pump performs the following steps:

        1. **Pane check**: capture the tmux pane and look for the
           ``[call_id:<id>]`` marker. If the marker is present, the message
           was already sent to tmux (a previous pump cycle sent it but
           crashed before persisting the ``processing`` state). In that case
           move the call_id to ``processing_call_ids`` WITHOUT re-sending
           (avoids duplicate).

        2. **Send**: if the marker is NOT present, send the message to tmux.
           The send is synchronous and verified (``_submit_tmux_message``
           confirms the input was submitted).

        3. **Persist processing**: after a successful send (or after
           confirming the marker was already present), move the call_id from
           ``pending_call_ids`` to ``processing_call_ids`` and persist.

        **Crash windows covered:**

        * Crash before send: marker not in pane → re-send on next cycle (no loss).
        * Crash after send, before persist: marker in pane → move to
          processing without re-sending (no duplicate).
        * Crash after persist: call_id in ``processing_call_ids`` → never
          re-sent to a live session (no duplicate).

        **Worker ACK** (``_ack_call_ids``): the worker confirms it processed
        the message by including the call_id in ``acked_call_ids``. Only the
        worker's ACK moves the call_id to ``delivered_call_ids`` and removes
        the message from ``pending_messages``.

        **Lease expiry**: for live sessions the message sits in the tmux
        input buffer awaiting the worker; we never expire processing
        call_ids. Only when the tmux session itself is gone
        (``ManagedSessionStatus.STOPPED``) does ``_expire_processing_leases``
        move stranded call_ids back to ``pending_call_ids`` for re-delivery.
        """
        session = self.sessions.get(session_id)
        if not session:
            return

        # Serialize pump cycles per session so two concurrent calls cannot
        # both send the same pending call_id to tmux. The pane-marker check
        # below is the receiver-verifiable dedup; the lock closes the race
        # window between two concurrent pane checks.
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._pump_session_messages_locked(session_id)

    async def _pump_session_messages_locked(self, session_id: str) -> None:
        """Inner pump logic — caller must hold ``self._pump_locks[session_id]``."""
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

            # ---- RECEIVER-VERIFIABLE DELIVERY ----
            # First, check whether the [call_id:<id>] marker is already in
            # the tmux pane. If it is, the message was sent by a previous
            # pump cycle that crashed before persisting the processing
            # state. We must NOT re-send (that would duplicate the model
            # turn); instead we move the call_id straight to processing.
            marker = f"[call_id:{call_id}]"
            already_sent = False
            try:
                pane_output = await self._capture_tmux_output(session.tmux_session)
                already_sent = marker in pane_output
            except RuntimeError:
                # Can't inspect the pane (e.g. tmux session gone). Treat as
                # not-yet-sent and attempt delivery; if the session is dead
                # the send will fail and we'll roll back.
                already_sent = False

            if not already_sent:
                # ---- SEND to tmux ----
                try:
                    persisted = self._persist_attachments(
                        session.workspace_id,
                        f"{session.id}-message-{uuid.uuid4().hex[:8]}",
                        [],
                    )
                    await self._ensure_session_ready_for_send(self.sessions[session_id])
                    full_message = f"{marker}\n{message}"
                    await self._send_tmux_message(
                        self.sessions[session_id].tmux_session,
                        self._append_attachment_block(full_message, persisted),
                    )
                except Exception:
                    # Delivery failed. Leave the call_id in pending so the
                    # next pump cycle can retry the same call_id (transient
                    # retry resumes delivery). The message body stays in
                    # pending_messages.
                    logger.exception(
                        "pump: failed to deliver call_id=%s to session %s; "
                        "leaving in pending for retry",
                        call_id,
                        session_id,
                    )
                    continue

            # ---- PERSIST processing ----
            # The message is now in the tmux input buffer (either we just
            # sent it, or we confirmed it was already there from a prior
            # crashed cycle). Move the call_id from pending to processing
            # on BOTH the session and its task (if any) so the two stay in
            # sync.
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

            # The call_id stays in processing_call_ids, awaiting the worker's
            # ACK. Only the worker's ACK (_ack_call_ids) moves it to
            # delivered_call_ids and removes the message from
            # pending_messages. This preserves the message until the worker
            # proves receipt.

    def _expire_processing_leases(self, session_id: str, max_age_seconds: float = 300.0) -> int:
        """Move stale ``processing_call_ids`` back to ``pending_call_ids``.

        A call_id in ``processing_call_ids`` means the pump claimed it and
        successfully sent it to the worker's tmux inbox. The tmux input
        buffer is the durable receiver: the worker will read that message
        exactly once when it next reads stdin. Re-sending the same call_id
        would append it to the tmux buffer a second time and produce a
        duplicate model turn / side effect.

        Therefore, for **live** sessions (tmux session still exists) we
        NEVER expire processing call_ids — they are sitting in the tmux
        input buffer awaiting the worker. The worker's ACK
        (``_ack_call_ids``) is what moves them to ``delivered_call_ids``.

        Expiry only applies when the tmux session itself is gone
        (``ManagedSessionStatus.STOPPED``): the input buffer has been
        destroyed, so the message was never received and it is safe to
        re-claim and re-deliver it.

        Returns the number of call_ids moved back to pending.
        """
        session = self.sessions.get(session_id)
        if not session:
            return 0
        if not session.processing_call_ids:
            return 0

        # Only expire for sessions whose tmux inbox is gone. For live
        # sessions the message is already in the tmux input buffer;
        # re-delivering it would cause a duplicate turn.
        if session.status != ManagedSessionStatus.STOPPED:
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
