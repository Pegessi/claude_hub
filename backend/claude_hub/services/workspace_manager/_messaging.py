"""Managed session messaging and terminal output capture.

Managed Chat uses the same durable envelope, payload fingerprint and report
ACK contract. Native provider handoff evidence replaces tmux receipts: known
unsent calls may retry, accepted calls await ACK, and ambiguous calls block
further input. Chat pumps one turn at a time and preserves paused/queued work.
The terminal-specific receipt and recovery details below remain unchanged.

Delivery semantics: **at-least-once with fail-closed uncertainty**.

The Hub does NOT own a verifiable durable receiver for tmux stdin. Bytes
written to tmux may be consumed by the agent and then lost if the agent
process crashes, and the Hub cannot read back what the model actually
processed. Therefore we cannot guarantee exactly-once delivery.

Instead we guarantee **fail-closed**: a call_id is always in exactly one of
four states:

  pending ──persist intent──▶ processing (in-flight) ──worker ACK──▶ delivered
     │                              │
     │         send failed          │  STOPPED session / tmux gone
     └──────────────────────────────┤  (receipt unqueryable)
                                    ▼
                              uncertain (fail-closed)

  For LIVE (WORKING) sessions, ``processing`` call_ids are reconciled
  against the tmux-server receipt on cold start / monitor tick:
    * receipt present  → stay in ``processing`` (no repaste, await ACK)
    * receipt absent   → move back to ``pending`` (one safe re-delivery)
    * session gone     → move to ``uncertain`` (fail-closed)

* ``pending_call_ids``: message persisted in ``pending_messages``, not yet
  sent to tmux. Always re-deliverable on restart.
* ``processing_call_ids``: the intent to deliver was persisted (call_id
  moved out of pending) and the pump is sending / has sent the message to
  tmux, but the worker has not ACKed. "Maybe delivered" — we cannot prove
  the message reached the tmux input buffer. On cold restart, LIVE sessions
  keep the call_id here for receipt-based reconciliation; STOPPED sessions
  move it to ``uncertain``.
* ``uncertain_call_ids``: the call_id was in-flight when the Hub crashed or
  the tmux session disappeared (STOPPED/gone, receipt unqueryable).
  Fail-closed: we do NOT auto-resend (could duplicate) and do NOT silently
  mark delivered (could lose). A ``delivery:uncertain`` event is emitted to
  the supervisor. Moves to delivered on worker ACK, or back to pending on
  explicit operator retry (``retry_uncertain_delivery``) only if the tmux
  session is alive and the receipt is absent.
* ``delivered_call_ids``: the worker ACKed the call_id (listed it in
  ``acked_call_ids`` of a report). Only the worker's ACK moves a call_id
  here.

The ``[call_id:<id>]`` marker is embedded in the message body so the worker
can correlate its ACK. It is NOT used by the Hub as a receipt or dedup
basis — tmux pane history is not durable (markers roll out of the scroll
buffer), so relying on it for cold-recovery dedup would produce duplicate
deliveries.

**Duplicate delivery and platform-provided durable dedupe:**

The Hub provides durable dedupe through three mechanisms; the worker does
NOT need to keep a conversational or scratch-file processed-call list:

1. **Sender-side state machine.** A call_id is always in exactly one of
   ``pending``, ``processing``, ``delivered``, or ``uncertain``. The
   sender skips call_ids already in ``processing``, ``delivered``, or
   ``uncertain`` — they are never re-enqueued. This is durable across Hub
   restarts because the state is persisted in ``state.json``.

2. **tmux-server receipt (sender-side fence for a live session).** The
   receipt-aware delivery primitive (``_send_tmux_message_with_receipt``)
   records a tmux session user option atomically with the paste+submit. A
   later replay of the same call_id on the *same* tmux session sees the
   receipt and skips the paste — at-most-once paste per call_id per tmux
   session lifetime. The receipt is scoped to the tmux session; it does
   not survive a destroyed/recreated session.

3. **Receipt-gated re-delivery for LIVE sessions; fail-closed for gone
   sessions.** For a LIVE (WORKING) session, ``_recover_processing_via_receipt``
   queries the tmux-server receipt:
     * receipt present → stay in ``processing``, no repaste (await ACK).
     * receipt absent → move to ``pending`` for **one** safe re-delivery.
   If the tmux session is gone / unqueryable (or STOPPED) when a call_id
   is in ``processing`` or ``uncertain``, the call_id moves to (or stays
   in) ``uncertain`` and is NOT automatically re-delivered. Only the
   explicit operator-facing ``retry_uncertain_delivery`` endpoint may
   move it back to ``pending``, and only if the tmux session is alive
   and the receipt is absent. If the receipt is present (the paste
   already happened), the retry does NOT re-paste — it just ensures the
   already-pasted input is submitted and waits for the worker's ACK.

**Worker ACK is the durable commit.** A call_id only leaves
``processing_call_ids`` (or ``uncertain_call_ids`` after a receipt-present
retry) when the worker ACKs it by listing it in ``acked_call_ids`` of a
report. The ACK moves the call_id to ``delivered_call_ids``, from which it
is never re-sent. The ``[call_id:<id>]`` marker embedded in the message
body lets the worker correlate its ACK; it is NOT used by the Hub as a
receipt or dedup basis.

Because tmux stdin is not a verifiable durable receiver, an explicit
operator retry of an ``uncertain`` call_id MAY deliver the same message
to the worker twice (if the first ambiguous send actually succeeded and
the tmux session was recreated). This is an explicitly documented,
human-authorized consequence of the fail-closed uncertainty model — we
do NOT claim exactly-once effects. The Hub's guarantee is at-least-once
delivery with fail-closed uncertainty and platform-provided dedupe within
a single tmux session lifetime; exactly-once *effects* across session
recreation require the operator to verify the worker state before
retrying.
"""

import hashlib
import time

from claude_hub.services.session_seat import SessionSeatMismatch, validate_session_seat
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403
from claude_hub.services.workspace_manager.messaging.delivery import _MessageDeliveryMixin
from claude_hub.services.workspace_manager.messaging.recovery import _MessageRecoveryMixin
from claude_hub.services.workspace_manager.messaging.retry import _MessageRetryMixin


class _MessagingMixin(
    _MessageDeliveryMixin, _MessageRecoveryMixin, _MessageRetryMixin, _WorkspaceManagerBase
):
    def _assert_session_seat(self, session: ManagedSession) -> str:
        return validate_session_seat(session, processes=ttyd_manager.processes)

    async def _ensure_session_ready_for_send(self, session: ManagedSession) -> None:
        self._assert_session_seat(session)
        if session.session_kind == SessionKind.CHAT:
            return

        from claude_hub.services.ttyd_manager import _tmux_session_exists_async

        # Send paths must not create a same-name pane and pour into it.
        # Missing seats fail closed so /clear cannot land on a freshly
        # spawned empty Claude that reused the expected tmux name.
        process = ttyd_manager.processes.get(session.tab_id)
        if process is None:
            raise SessionSeatMismatch(
                f"session {session.id} tab {session.tab_id} is not live; "
                "refusing to create a seat and send"
            )
        if not await _tmux_session_exists_async(session.tmux_session):
            raise SessionSeatMismatch(
                f"session {session.id} tmux {session.tmux_session} is gone; "
                "refusing to recreate and send"
            )

    async def _capture_tmux_output(self, tmux_session: str) -> str:
        from claude_hub.services.runtime_isolation import tmux_command

        proc = await asyncio.create_subprocess_exec(
            *tmux_command(
                "capture-pane",
                "-p",
                "-S",
                "-120",
                "-t",
                tmux_session,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux capture-pane failed with code {proc.returncode}")
        return stdout.decode("utf-8", errors="ignore")

    def _agent_update_dialog(self, output: str) -> bool:
        """True when Codex is asking to self-update instead of showing a prompt."""
        lower = output.lower()
        return "update available" in lower and "skip" in lower

    def _agent_input_ready(self, output: str) -> bool:
        if self._agent_update_dialog(output):
            return False
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

    async def _dismiss_codex_update_dialog(self, session: ManagedSession) -> None:
        """Move off 'Update now' (npm install -g) onto Skip, then confirm.

        The dialog highlights option 1. A bootstrap Enter would install.
        """
        await self._run_tmux("send-keys", "-t", session.tmux_session, "Down")
        await self._prompt_wait_sleep(AGENT_UPDATE_DIALOG_SETTLE_SECONDS)
        await self._run_tmux("send-keys", "-t", session.tmux_session, "Enter")

    async def _prompt_wait_sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _wait_for_agent_prompt(self, session: ManagedSession) -> None:
        """Wait until the agent TUI can accept a pasted bootstrap prompt.

        Dismiss a Codex update dialog if it appears. If that dialog is still
        showing when the wait expires, fail closed so bootstrap Enter cannot
        confirm ``npm install -g``.
        """
        if session.session_kind == SessionKind.CHAT:
            self._assert_session_seat(session)
            return
        deadline = time.monotonic() + AGENT_PROMPT_WAIT_SECONDS
        saw_update_dialog = False
        last_output = ""
        while time.monotonic() < deadline:
            try:
                last_output = await self._capture_tmux_output(session.tmux_session)
            except RuntimeError:
                await self._prompt_wait_sleep(AGENT_PROMPT_POLL_SECONDS)
                continue
            if self._agent_update_dialog(last_output):
                saw_update_dialog = True
                await self._dismiss_codex_update_dialog(session)
                await self._prompt_wait_sleep(AGENT_UPDATE_DIALOG_SETTLE_SECONDS)
                continue
            if self._agent_input_ready(last_output):
                return
            await self._prompt_wait_sleep(AGENT_PROMPT_POLL_SECONDS)
        if saw_update_dialog and self._agent_update_dialog(last_output):
            raise WorkspaceAgentInitializationError(
                "Codex update dialog is still blocking the prompt; "
                "bootstrap was not sent to avoid confirming npm install -g"
            )
        logger.warning(
            "agent prompt not confirmed before bootstrap session_id=%s",
            session.id,
        )

    def _compute_payload_fingerprint(
        self,
        message: str,
        attachments: list[WorkspaceAttachmentCreate],
    ) -> str:
        """Compute a durable canonical fingerprint of a (message, attachments) payload.

        The fingerprint is sha256 over a canonical encoding of:
          * the message text
          * for each attachment: filename, normalized (stripped/lowercased)
            mime_type, and sha256 of the base64-decoded bytes.

        Same message + same attachment content always yields the same
        fingerprint regardless of base64 padding or whitespace. This is the
        source of truth for the immutable-call_id invariant: a call_id's
        fingerprint is stored on first send and kept forever (even after
        ACK), so a re-send with the same payload is idempotent and a re-send
        with a different payload is rejected.
        """
        h = hashlib.sha256()
        h.update(message.encode("utf-8"))
        for att in attachments:
            h.update(b"\x00")
            h.update(att.filename.encode("utf-8"))
            h.update(b"\x00")
            h.update(att.mime_type.strip().lower().encode("utf-8"))
            h.update(b"\x00")
            # Decode the attachment bytes and hash their content. Same-size
            # different bytes produce different fingerprints.
            try:
                raw = base64.b64decode(att.data_url.split(",", 1)[1], validate=True)
            except (binascii.Error, ValueError, IndexError):
                raw = b""
            h.update(hashlib.sha256(raw).digest())
        return h.hexdigest()

    def _compute_persisted_payload_fingerprint(
        self,
        message: str,
        attachments: list[WorkspaceAttachment],
    ) -> str:
        """Compute the canonical fingerprint of a PERSISTED (message, attachments)
        envelope.

        Unlike :meth:`_compute_payload_fingerprint`, which operates on
        :class:`WorkspaceAttachmentCreate` (base64 ``data_url``), this reads
        the persisted file bytes from each :class:`WorkspaceAttachment`'s
        ``path`` and hashes them. Used for legacy backfill: a call_id present
        in a state list but without a stored fingerprint can have its
        fingerprint derived from the persisted envelope so the
        immutable-payload invariant applies from then on.

        Fail-closed: if any attachment file is missing or unreadable, raise
        rather than silently producing a wrong fingerprint (which could let a
        conflicting payload slip through).
        """
        h = hashlib.sha256()
        h.update(message.encode("utf-8"))
        for att in attachments:
            h.update(b"\x00")
            h.update(att.filename.encode("utf-8"))
            h.update(b"\x00")
            h.update(att.mime_type.strip().lower().encode("utf-8"))
            h.update(b"\x00")
            try:
                raw = Path(att.path).read_bytes()
            except (OSError, FileNotFoundError) as exc:
                raise RuntimeError(
                    f"cannot fingerprint persisted attachment {att.filename} "
                    f"(id={att.id}): file at {att.path} is missing/unreadable"
                ) from exc
            h.update(hashlib.sha256(raw).digest())
        return h.hexdigest()

    def _safe_attachment_owner_id(self, session_id: str, call_id: str) -> str:
        """Return a filesystem-safe owner id for a call_id's attachments.

        The raw call_id is external input and may contain "/" or "..", which
        would escape the workspace attachments directory. We derive a safe
        owner id from sha256(session_id + call_id) so it is unique per
        (session, call_id) and contains only hex characters.
        """
        h = hashlib.sha256()
        h.update(session_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(call_id.encode("utf-8"))
        return h.hexdigest()
