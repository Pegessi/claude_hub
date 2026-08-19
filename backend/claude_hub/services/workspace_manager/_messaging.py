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
        """Send a message to a managed session's terminal with at-least-once
        delivery semantics at the executor boundary.

        Delivery guarantees — **at-least-once, not exactly-once**:

        1. A durable outbox (``session.pending_call_ids``) is persisted before
           the send, so a crash before the send does not lose the message.
        2. After the send completes, the call_id **stays** in
           ``pending_call_ids``. It is only moved to ``delivered_call_ids``
           when the worker submits a report whose ``acked_call_ids`` includes
           it (the dispatch call_id is ACKed automatically on any report).
           This means ``delivered_call_ids`` == "processed by the worker",
           not merely "sent by the Hub".
        3. Sender-side dedup: ``send_session_message`` skips any call_id
           already in ``delivered_call_ids`` (i.e. already ACKed). A call_id
           still in ``pending_call_ids`` (sent but not yet ACKed) **will** be
           re-sent if ``send_session_message`` is invoked again — this is the
           at-least-once crash-recovery path. The receiving executor dedupes
           any duplicate via the ``[call_id:<id>]`` marker.
        4. Crash recovery: if a call_id is left in ``pending_call_ids`` by a
           crash after the send but before the worker ACKs it, the message is
           **re-sent** (e.g. by ``_recover_queued_task_ownership`` for the
           dispatch call_id). The worker dedupes via the marker.

        We do NOT claim exactly-once. tmux pane output is not a durable
        delivery record (it can be cleared, rolled off, or capture can fail),
        so inferring delivery from pane text is unreliable. The sender
        guarantees at-least-once; the receiver is responsible for dedup via
        the call_id marker. The Hub's sender-side dedup only suppresses
        re-sends of call_ids the worker has **ACKed** (processed), not merely
        sent.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        if call_id:
            # Sender-side dedup: skip call_ids the worker has already ACKed
            # (processed). A call_id still in pending_call_ids was sent but
            # not yet ACKed; re-sending it is the at-least-once path.
            if call_id in session.delivered_call_ids:
                logger.debug(
                    "send_session_message call_id=%s already ACKed by session %s; skipping",
                    call_id,
                    session_id,
                )
                return

            # Phase 1: persist call_id as pending at the executor boundary
            # before sending. This is the durable outbox entry. If we crash
            # after this point, recovery will re-send (at-least-once). The
            # call_id stays in pending_call_ids until the worker ACKs it.
            if call_id not in session.pending_call_ids:
                self.sessions[session_id] = session.model_copy(
                    update={"pending_call_ids": session.pending_call_ids + [call_id]}
                )
                self._save_state()
                session = self.sessions[session_id]

        # Build the outbound message. When a call_id is present, prefix the
        # message with a [call_id:<id>] marker. This marker lets the receiving
        # executor dedupe any duplicate delivery caused by the at-least-once
        # crash-recovery re-send.
        full_message = message
        if call_id:
            full_message = f"[call_id:{call_id}]\n{message}"

        persisted = self._persist_attachments(
            session.workspace_id,
            f"{session.id}-message-{uuid.uuid4().hex[:8]}",
            attachments or [],
        )
        await self._ensure_session_ready_for_send(session)
        await self._send_tmux_message(
            session.tmux_session,
            self._append_attachment_block(full_message, persisted),
        )

        # NOTE: We intentionally do NOT move the call_id to delivered_call_ids
        # here. delivered_call_ids only contains call_ids the worker has
        # ACKed (processed). Moving it here would suppress the at-least-once
        # re-send if the worker crashed after receiving but before processing.
        # The worker ACKs by listing the call_id in acked_call_ids of its
        # report (the dispatch call_id is ACKed automatically).

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
