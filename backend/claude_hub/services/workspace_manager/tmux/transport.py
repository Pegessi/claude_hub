"""Tmux transport, receipts, interrupts, and tab renames."""

import hashlib

import claude_hub.services.managed_chat as managed_chat
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _TmuxTransportMixin(_WorkspaceManagerBase):
    async def _send_tmux_message(self, tmux_session: str, message: str) -> None:
        logger.info(
            "Sending workspace message to tmux_session=%s message_length=%s",
            tmux_session,
            len(message),
        )
        await self._run_tmux("send-keys", "-t", tmux_session, "C-u")
        await asyncio.sleep(0.2)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(message)
            tmp_path = tmp.name
        try:
            await self._run_tmux("load-buffer", tmp_path)
            # -p wraps the paste in bracketed-paste control codes
            # (ESC[200~ … ESC[201~); -r disables tmux's default LF->CR
            # replacement so embedded newlines stay newlines. Without these, a
            # multi-line prompt is delivered as bare CRs with no bracket markers,
            # and a TUI in bracketed-paste mode (codex) treats each CR as Enter —
            # submitting every prompt line separately and piling up "Queued
            # follow-up inputs" so the agent never starts. The pairing is
            # required: -p alone still converts LF->CR; -r alone omits the
            # markers a non-bracketed reader needs to not submit on the LFs.
            await self._run_tmux("paste-buffer", "-p", "-r", "-t", tmux_session)
            await asyncio.sleep(TMUX_PASTE_SETTLE_SECONDS)
            await self._submit_tmux_message(tmux_session, message)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def _submit_tmux_message(self, tmux_session: str, message: str) -> None:
        for attempt in range(1, TMUX_SUBMIT_ATTEMPTS + 1):
            # Use tmux's semantic Enter key. With extended-keys/csi-u enabled,
            # C-m is encoded as Ctrl-M and Claude Code leaves multi-line pasted
            # input pending instead of treating it as a submit action.
            await self._run_tmux("send-keys", "-t", tmux_session, "Enter")
            await asyncio.sleep(TMUX_SUBMIT_SETTLE_SECONDS)
            try:
                output = await self._capture_tmux_output(tmux_session)
            except RuntimeError as exc:
                logger.warning(
                    "Could not verify workspace message submit for tmux_session=%s: %s",
                    tmux_session,
                    exc,
                )
                return
            if not self._message_still_in_input(output, message):
                if attempt > 1:
                    logger.info(
                        "Workspace message submit succeeded after retry tmux_session=%s attempts=%s",
                        tmux_session,
                        attempt,
                    )
                return
            logger.warning(
                "Workspace message still appears pending after submit attempt %s/%s "
                "tmux_session=%s output_tail=%r",
                attempt,
                TMUX_SUBMIT_ATTEMPTS,
                tmux_session,
                output[-240:],
            )

        raise RuntimeError("Failed to submit workspace agent message; input still appears pending")

    def _message_still_in_input(self, output: str, message: str) -> bool:
        lines = [line.rstrip() for line in output.splitlines()]
        while lines and not lines[-1].strip():
            lines.pop()
        first_line = message.strip().splitlines()[0][:80] if message.strip() else ""
        is_slash_command = message.strip().startswith("/")
        prompt_markers = ("›", ">", "❯", "→")
        tail_start = max(0, len(lines) - 16)
        tail = lines[tail_start:]
        for index, line in enumerate(tail):
            stripped = line.strip()
            if not stripped.startswith(prompt_markers):
                continue
            has_pasted_placeholder = "[Pasted Content" in stripped or "[Pasted text" in stripped
            has_message_prefix = bool(first_line and first_line in stripped)
            if not has_pasted_placeholder and not has_message_prefix:
                continue
            following_lines = tail[index + 1 :]
            if any(next_line.strip().startswith(prompt_markers) for next_line in following_lines):
                continue
            following = "\n".join(following_lines[:5]).lstrip()
            if following.startswith(("•", "⏺", "●")):
                continue
            if following.startswith("⎿") and (is_slash_command or not has_pasted_placeholder):
                continue
            return True
        return False

    async def _run_tmux(self, *args: str) -> None:
        from claude_hub.services.runtime_isolation import tmux_command

        proc = await asyncio.create_subprocess_exec(
            *tmux_command(*args),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux {' '.join(args)} failed with code {proc.returncode}")

    async def _run_tmux_capture(self, *args: str) -> str:
        """Run a tmux command and return its stdout.

        Used for receipt queries (``show-option -v``) where we need the
        option value. Raises ``RuntimeError`` on non-zero exit (including
        the case where the target session no longer exists).
        """

        from claude_hub.services.runtime_isolation import tmux_command

        proc = await asyncio.create_subprocess_exec(
            *tmux_command(*args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux {' '.join(args)} failed with code {proc.returncode}")
        return stdout.decode("utf-8", errors="ignore")

    @staticmethod
    def _receipt_key(call_id: str) -> str:
        """Derive a tmux session user-option name for the delivery receipt.

        The key is ``@receipt_<sha256(call_id)[:16]>``. Using a hash keeps
        the option name short, filesystem-safe, and free of shell-special
        characters. The first 16 hex chars (64 bits) are enough to avoid
        collisions within a single tmux server.
        """
        digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:16]
        return f"@receipt_{digest}"

    @staticmethod
    def _buffer_name(call_id: str, tmux_session: str) -> str:
        """Derive a tmux named-buffer name for the message payload.

        Named buffers are **server-global** (not scoped to a session), so
        the same ``call_id`` delivered concurrently to two sessions would
        otherwise share a buffer and one payload could overwrite the other.
        We hash ``call_id`` *and* ``tmux_session`` together so each
        (session, call_id) pair gets its own buffer.

        Same hash scheme as ``_receipt_key`` but with a ``buf_`` prefix so
        the buffer and the receipt option never collide.
        """
        digest = hashlib.sha256(f"{call_id}\x00{tmux_session}".encode("utf-8")).hexdigest()[:16]
        return f"buf_{digest}"

    async def _query_tmux_receipt(self, tmux_session: str, call_id: str) -> bool:
        """Return True if the tmux server has recorded a delivery receipt
        for ``call_id`` on ``tmux_session``.

        The receipt is a session user option set by the atomic
        check-and-paste command list in ``_send_tmux_message_with_receipt``.

        We use ``show-options -qv`` (quiet, value-only) because a *missing*
        user option returns rc=0 with empty stdout, while a *nonexistent
        session* returns rc=1 with an error on stderr. This lets us
        distinguish "receipt absent" (False) from "session gone /
        unqueryable" (RuntimeError → caller treats as uncertain).
        ``show-option -v`` (singular) returns rc=1 for missing options,
        which would incorrectly classify absent receipts as unqueryable.
        """
        key = self._receipt_key(call_id)
        value = await self._run_tmux_capture(
            "show-options",
            "-qv",
            "-t",
            tmux_session,
            key,
        )
        return value.strip() != ""

    async def _send_tmux_message_with_receipt(
        self,
        tmux_session: str,
        message: str,
        call_id: str,
    ) -> None:
        """Deliver ``message`` to ``tmux_session`` with a tmux-server-side
        receipt that prevents duplicate paste on replay.

        Design
        ------

        1. **Pre-side-effect load.** Write the message to a temp file and
           ``load-buffer`` it into a named buffer derived from
           ``sha256(call_id + tmux_session)``. ``load-buffer`` is idempotent
           (reloading the same content is harmless) and happens *before*
           any side effect on the target pane. The session is included in
           the hash because named buffers are server-global.

        2. **Atomic check-and-paste.** Enqueue a *single* tmux command
           list that:

           * checks the session user option ``@receipt_<hash>`` (the
             receipt);
           * if the receipt is **absent**, clears the input line (``C-u``),
             pastes the named buffer, submits it (first ``Enter``), and sets
             the receipt option — all in one tmux command list;
           * if the receipt is **present**, does nothing.

           Because the whole list is one tmux server command, once the
           Hub process has enqueued it (the ``tmux`` subprocess returns
           successfully), the tmux server will execute it atomically
           regardless of whether the Hub dies mid-way. A later replay
           sees the receipt and skips the paste.

        3. **Submit verification (no re-paste).** After the transaction,
           we run the same ``_submit_tmux_message`` verification loop as
           the legacy path: capture the pane and, if the message is still
           sitting in the input box, send additional ``Enter`` retries.
           This never re-pastes — the receipt guarantees the paste
           happened at most once — it only ensures the already-pasted
           input is accepted by the TUI.

        4. **Buffer cleanup.** Best-effort ``delete-buffer`` of the named
           buffer after the transaction. Because the receipt gates the
           paste (a second same-call transaction is a no-op once the
           receipt is set), deleting the buffer after the transaction
           cannot race a concurrent same-call paste: either the receipt
           is already set (paste skipped) or the buffer is reloaded by
           the concurrent ``load-buffer`` before its own transaction.

        5. **No shell interpolation, no pane text.** We use
           ``if-shell -F`` with a format string (not a shell command) and
           a named buffer (not pane capture) so the decision is made by
           the tmux server against its own option store, not by parsing
           scrollback.

        This gives **at-most-once paste per call_id per tmux session
        lifetime**. It is NOT global exactly-once: if the tmux session
        is destroyed and recreated (even with the same name), the receipt
        is lost. On cold restart the monitor reconciles processing
        call_ids against the tmux receipt:

        * receipt present on a LIVE session → keep processing (the paste
          definitely ran there; no repaste).
        * receipt absent on a LIVE session (e.g. the session was
          destroyed and recreated with the same name) → move the call_id
          back to ``pending`` for **one** re-delivery.
        * session gone / unqueryable / STOPPED → move to
          ``uncertain_call_ids`` (fail closed; no auto-resend; explicit
          operator retry required via ``retry_uncertain_delivery``).

        The worker does NOT need to keep a scratch-file processed-call
        list — the Hub's persisted state machine
        (``pending``/``processing``/``delivered``/``uncertain``) plus the
        tmux receipt provide the durable dedupe boundary.
        """
        receipt_key = self._receipt_key(call_id)
        buffer_name = self._buffer_name(call_id, tmux_session)

        # 1. Pre-side-effect: load the message into a named buffer.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(message)
            tmp_path = tmp.name
        try:
            await self._run_tmux("load-buffer", "-b", buffer_name, tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        try:
            # 2. Atomic check-and-paste command list.
            #
            # if-shell -F <format> <then-cmd> <else-cmd>
            #   format = #{@receipt_<hash>}  -> non-empty if receipt set
            #   then-cmd  = "" (do nothing)
            #   else-cmd  = send-keys C-u; paste-buffer ...; send-keys Enter;
            #               set-option @receipt_<hash> 1
            #
            # The else branch is a tmux command list separated by ';'.
            # Because it is passed as a single argument, the ';' reaches
            # tmux and is parsed as a command separator inside the else
            # branch. The first Enter is part of the atomic transaction so
            # the receipt is set only after the paste+submit attempt.
            else_commands = (
                f"send-keys -t {tmux_session} C-u"
                f"; paste-buffer -b {buffer_name} -p -r -t {tmux_session}"
                f"; send-keys -t {tmux_session} Enter"
                f"; set-option -t {tmux_session} {receipt_key} 1"
            )
            await self._run_tmux(
                "if-shell",
                "-F",
                "-t",
                tmux_session,
                f"#{{{receipt_key}}}",
                "",
                else_commands,
            )
            await asyncio.sleep(TMUX_PASTE_SETTLE_SECONDS)

            # 3. Submit verification (capture-first, no re-paste). The
            #    receipt guarantees the paste happened at most once. We
            #    only nudge Enter if the message is verifiably still
            #    sitting in the input box; otherwise we do nothing so
            #    we never submit an unrelated/blank line.
            await self._ensure_submitted_without_repaste(tmux_session, message)
        finally:
            # 4. Best-effort buffer cleanup. Safe because the receipt
            #    gates the paste: once set, a same-call replay skips the
            #    paste regardless of whether the buffer still exists.
            try:
                await self._run_tmux("delete-buffer", "-b", buffer_name)
            except Exception:
                logger.debug(
                    "Failed to delete tmux buffer %s (ignored)", buffer_name, exc_info=True
                )

    async def _ensure_submitted_without_repaste(
        self,
        tmux_session: str,
        message: str,
    ) -> None:
        """Ensure an already-pasted message is accepted by the TUI without
        ever re-pasting.

        Capture-first: we inspect the pane *before* sending any keys. If
        the message is no longer sitting in the input box (the TUI
        already accepted it), we return without sending anything — this
        avoids submitting an unrelated/blank line that happens to be on
        the prompt. Only while the message is verifiably pending do we
        send ``Enter`` and re-check, up to ``TMUX_SUBMIT_ATTEMPTS`` times.

        Used both after the atomic paste transaction (the receipt
        guarantees at-most-once paste; this only nudges Enter) and on
        cold recovery / retry when the receipt is present.
        """
        if not message:
            # Fail closed: without the original message body we cannot
            # verify whether the input is still pending, and sending a
            # blind Enter could submit an unrelated line. Surface this to
            # the caller so it can quarantine the call_id.
            raise RuntimeError(
                f"cannot verify submit for tmux_session={tmux_session}: "
                "message body is empty; refusing to send blind Enter"
            )

        for attempt in range(1, TMUX_SUBMIT_ATTEMPTS + 1):
            try:
                output = await self._capture_tmux_output(tmux_session)
            except RuntimeError as exc:
                # Cannot verify submit state. Fail closed: raise so the
                # caller can move the call_id to uncertain rather than
                # silently declaring success.
                raise RuntimeError(
                    f"Could not capture pane for submit verification "
                    f"tmux_session={tmux_session} attempt={attempt}: {exc}"
                ) from exc
            if not self._message_still_in_input(output, message):
                if attempt > 1:
                    logger.info(
                        "Already-pasted message accepted after %s Enter nudge(s) "
                        "tmux_session=%s",
                        attempt - 1,
                        tmux_session,
                    )
                return
            # Message is still pending: send one semantic Enter and re-check.
            await self._run_tmux("send-keys", "-t", tmux_session, "Enter")
            await asyncio.sleep(TMUX_SUBMIT_SETTLE_SECONDS)

        raise RuntimeError(
            f"Already-pasted message still pending after {TMUX_SUBMIT_ATTEMPTS} "
            f"Enter nudges on tmux_session={tmux_session}"
        )

    async def _interrupt_session(self, session: ManagedSession) -> None:
        """Send Escape then a single Ctrl-C to interrupt a running Claude Code process.

        Sequence: Escape dismisses any open dialog/prompt, a short settle allows the
        TUI to return to its main loop, then one Ctrl-C raises KeyboardInterrupt in
        the agent.  We deliberately send exactly one Ctrl-C to avoid the double-tap
        that exits Claude Code entirely.  Errors are logged but not raised so that
        bookkeeping abort can still proceed.
        """
        if session.session_kind == SessionKind.CHAT:

            await managed_chat.cancel(session)
            return
        tmux_name = session.tmux_session
        try:
            await self._run_tmux("send-keys", "-t", tmux_name, "Escape")
            await asyncio.sleep(0.3)
            await self._run_tmux("send-keys", "-t", tmux_name, "C-c")
            logger.info(
                "Sent interrupt (Escape + single C-c) to session_id=%s tmux=%s role=%s",
                session.id,
                tmux_name,
                session.role.value,
            )
        except Exception:
            logger.exception(
                "Failed to send interrupt keys to session_id=%s tmux=%s",
                session.id,
                tmux_name,
            )

    async def _rename_session_for_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        *,
        updated_at: datetime | None = None,
    ) -> ManagedSession:
        title = task.title
        if session.title == title:
            return session

        try:
            updated_tab = await ttyd_manager.update_tab(session.tab_id, name=title)
        except Exception:
            logger.exception(
                "Failed to rename workspace terminal tab_id=%s session_id=%s task_id=%s",
                session.tab_id,
                session.id,
                task.id,
            )
        else:
            if not updated_tab:
                logger.warning(
                    "Could not rename missing workspace terminal tab_id=%s session_id=%s task_id=%s",
                    session.tab_id,
                    session.id,
                    task.id,
                )

        updated_session = session.model_copy(
            update={
                "title": title,
                "updated_at": updated_at or self._runtime_now(),
            }
        )
        self.sessions[session.id] = updated_session
        return updated_session
