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
        """Send a message to a managed session's terminal.

        ``call_id`` enables exactly-once delivery at the executor boundary:
        - If ``call_id`` is already in ``session.delivered_call_ids``, the
          executor has acknowledged it; skip the send.
        - Otherwise, persist ``call_id`` to ``session.pending_call_ids``
          (phase 1 / outbox), send the message, then move it to
          ``session.delivered_call_ids`` (phase 2 / ACK). A crash between
          send and phase-2 persist leaves the call_id in pending so recovery
          re-delivers; the executor must tolerate at-least-once delivery.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        if call_id:
            if call_id in session.delivered_call_ids:
                logger.debug(
                    "send_session_message call_id=%s already delivered to session %s; skipping",
                    call_id,
                    session_id,
                )
                return
            # Phase 1: persist call_id as pending at the executor boundary
            # before sending. This is the outbox entry.
            if call_id not in session.pending_call_ids:
                self.sessions[session_id] = session.model_copy(
                    update={"pending_call_ids": session.pending_call_ids + [call_id]}
                )
                self._save_state()
                session = self.sessions[session_id]

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

        if call_id:
            # Phase 2: move call_id from pending to delivered. This is the
            # executor's ACK record. Persist immediately so a crash after
            # this point does not cause a re-delivery.
            current = self.sessions.get(session_id)
            if current is not None:
                pending = [c for c in current.pending_call_ids if c != call_id]
                delivered = current.delivered_call_ids
                if call_id not in delivered:
                    delivered = delivered + [call_id]
                self.sessions[session_id] = current.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "delivered_call_ids": delivered,
                    }
                )
                self._save_state()

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
