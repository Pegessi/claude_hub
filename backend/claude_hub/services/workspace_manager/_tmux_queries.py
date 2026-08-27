"""Tmux send/run, session queries, reaper, and board."""

import hashlib

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _TmuxQueriesMixin:
    def reports_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        return sorted(
            [report for report in self.reports.values() if report.workspace_id == workspace_id],
            key=lambda report: report.created_at,
        )

    def latest_reports_per_task_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        """Latest report per ``task_id`` for the board.

        The board only renders the most recent report per task card; the full
        per-task history is fetched on demand by the detail panel. Trimming here
        keeps the board payload an order of magnitude smaller (the full history
        can be thousands of reports) without changing what any card shows.
        """
        latest: dict[Optional[str], AgentReport] = {}
        for report in self.reports_for_workspace(workspace_id):  # asc by created_at
            latest[report.task_id] = report  # later (newer) overwrites
        return sorted(latest.values(), key=lambda report: report.created_at)

    def reports_for_task(self, workspace_id: str, task_id: str) -> list[AgentReport]:
        """Full report history for a single task, sorted ascending by created_at."""
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        return [
            report
            for report in self.reports_for_workspace(workspace_id)
            if report.task_id == task_id
        ]

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
            await self._run_tmux("send-keys", "-t", tmux_session, "C-m")
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
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
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
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
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
             pastes the named buffer, submits it (first ``C-m``), and sets
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
           sitting in the input box, send additional ``C-m`` retries.
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
            #   else-cmd  = send-keys C-u; paste-buffer ...; send-keys C-m;
            #               set-option @receipt_<hash> 1
            #
            # The else branch is a tmux command list separated by ';'.
            # Because it is passed as a single argument, the ';' reaches
            # tmux and is parsed as a command separator inside the else
            # branch. The first C-m is part of the atomic transaction so
            # the receipt is set only after the paste+submit attempt.
            else_commands = (
                f"send-keys -t {tmux_session} C-u"
                f"; paste-buffer -b {buffer_name} -p -r -t {tmux_session}"
                f"; send-keys -t {tmux_session} C-m"
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
        send ``C-m`` and re-check, up to ``TMUX_SUBMIT_ATTEMPTS`` times.

        Used both after the atomic paste transaction (the receipt
        guarantees at-most-once paste; this only nudges Enter) and on
        cold recovery / retry when the receipt is present.
        """
        if not message:
            # Fail closed: without the original message body we cannot
            # verify whether the input is still pending, and sending a
            # blind C-m could submit an unrelated line. Surface this to
            # the caller so it can quarantine the call_id.
            raise RuntimeError(
                f"cannot verify submit for tmux_session={tmux_session}: "
                "message body is empty; refusing to send blind C-m"
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
                        "Already-pasted message accepted after %s C-m nudge(s) " "tmux_session=%s",
                        attempt - 1,
                        tmux_session,
                    )
                return
            # Message is still pending: send one C-m and re-check.
            await self._run_tmux("send-keys", "-t", tmux_session, "C-m")
            await asyncio.sleep(TMUX_SUBMIT_SETTLE_SECONDS)

        raise RuntimeError(
            f"Already-pasted message still pending after {TMUX_SUBMIT_ATTEMPTS} "
            f"C-m nudges on tmux_session={tmux_session}"
        )

    async def _interrupt_session(self, session: ManagedSession) -> None:
        """Send Escape then a single Ctrl-C to interrupt a running Claude Code process.

        Sequence: Escape dismisses any open dialog/prompt, a short settle allows the
        TUI to return to its main loop, then one Ctrl-C raises KeyboardInterrupt in
        the agent.  We deliberately send exactly one Ctrl-C to avoid the double-tap
        that exits Claude Code entirely.  Errors are logged but not raised so that
        bookkeeping abort can still proceed.
        """
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
                "updated_at": updated_at or _wm._now(),
            }
        )
        self.sessions[session.id] = updated_session
        return updated_session

    def sessions_for_workspace(self, workspace_id: str) -> list[ManagedSession]:
        sessions = self._sessions_for_workspace_raw(workspace_id)
        return [self._with_assignment_summary(session) for session in sessions]

    def _sessions_for_workspace_raw(self, workspace_id: str) -> list[ManagedSession]:
        return [
            session for session in self.sessions.values() if session.workspace_id == workspace_id
        ]

    def _workspace_agents(
        self,
        workspace_id: str,
        include_stopped: bool = False,
    ) -> list[ManagedSession]:
        return [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.ORCHESTRATOR
            and (include_stopped or session.status != ManagedSessionStatus.STOPPED)
        ]

    def _dispatcher_session(self, workspace: Workspace) -> Optional[ManagedSession]:
        """Resolve the stored dispatcher pointer only.

        Does not scan for arbitrary dispatcher sessions and must not be used
        as a CLI reuse shortcut — agent create reuse goes through
        ``_find_compatible_workspace_agent`` strict matching.
        """
        if not workspace.dispatcher_session_id:
            return None
        session = self.sessions.get(workspace.dispatcher_session_id)
        if session and session.status != ManagedSessionStatus.STOPPED:
            return session
        self.workspaces[workspace.id] = workspace.model_copy(
            update={"dispatcher_session_id": None, "updated_at": _wm._now()}
        )
        self._save_state()
        return None

    _TERMINAL_TASK_STATUSES_FOR_REUSE = frozenset({WorkspaceTaskStatus.DONE})

    def _session_has_active_canonical_ownership(self, session: ManagedSession) -> bool:
        """True when a non-terminal task canonically owns this session via task.session_id."""
        for task in self.tasks.values():
            if task.workspace_id != session.workspace_id:
                continue
            if task.session_id != session.id:
                continue
            if task.status not in self._TERMINAL_TASK_STATUSES_FOR_REUSE:
                return True
        return False

    def _orchestrator_assignment_is_orphaned(
        self, session: ManagedSession, candidate_id: str
    ) -> bool:
        """Visible orphan while idle: task terminal or canonically owned by another live session."""
        if session.runtime_status != AgentRuntimeStatus.IDLE:
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        task = self.tasks.get(candidate_id)
        if task is None or task.workspace_id != session.workspace_id:
            return True
        if task.status in self._TERMINAL_TASK_STATUSES_FOR_REUSE:
            return True
        canonical_session_id = task.session_id
        if not canonical_session_id:
            # Active dispatch/binding window before task.session_id is set.
            return False
        if canonical_session_id == session.id:
            return False
        owner = self.sessions.get(canonical_session_id)
        if owner is None:
            return False
        if owner.workspace_id != task.workspace_id:
            return False
        if owner.status == ManagedSessionStatus.STOPPED:
            return False
        return True

    def _non_terminal_tasks_referencing_session(
        self,
        session_id: str,
        *,
        exclude_task_id: str | None = None,
    ) -> list[WorkspaceTask]:
        return [
            other
            for other in self.tasks.values()
            if other.id != exclude_task_id
            and (other.session_id == session_id or other.review_session_id == session_id)
            and other.status not in self._TERMINAL_TASK_STATUSES_FOR_REUSE
        ]

    def _session_raw_task_bindings_block_cleanup(
        self,
        session: ManagedSession,
        *,
        exclude_task_id: str | None = None,
    ) -> bool:
        """True when raw task_id/current_task_id bind a non-orphaned non-terminal task."""
        for candidate_id in {session.task_id, session.current_task_id}:
            if not candidate_id:
                continue
            if exclude_task_id and candidate_id == exclude_task_id:
                continue
            if self._orchestrator_assignment_is_orphaned(session, candidate_id):
                continue
            other = self.tasks.get(candidate_id)
            if other is not None and other.status not in self._TERMINAL_TASK_STATUSES_FOR_REUSE:
                return True
        return False

    def _orchestrator_assignment_should_clear(self, session: ManagedSession) -> bool:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return False
        if not session.task_id and not session.current_task_id:
            return False
        candidate_ids = {value for value in (session.task_id, session.current_task_id) if value}
        if not any(
            self._orchestrator_assignment_is_orphaned(session, candidate_id)
            for candidate_id in candidate_ids
        ):
            return False
        return not self._session_has_active_canonical_ownership(session)

    def _cleared_orchestrator_assignment(
        self, session: ManagedSession, now: datetime
    ) -> ManagedSession:
        return session.model_copy(
            update={
                "task_id": None,
                "current_task_id": None,
                "status": ManagedSessionStatus.IDLE,
                "runtime_status": AgentRuntimeStatus.IDLE,
                "auto_continue_task_id": None,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "prompt_retry_task_id": None,
                "prompt_retry_attempted_at": None,
                "updated_at": now,
                "last_activity_at": now,
            }
        )

    def _reconcile_orchestrator_session_assignment(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None or not self._orchestrator_assignment_should_clear(session):
            return False
        now = _wm._now()
        logger.info(
            "Cleaning stale orchestrator assignment session_id=%s task_ids=%s",
            session.id,
            sorted({value for value in (session.task_id, session.current_task_id) if value}),
        )
        self.sessions[session_id] = self._cleared_orchestrator_assignment(session, now)
        return True

    def _cleanup_stale_orchestrator_assignments(self, workspace_id: str) -> bool:
        """Clear visibly orphaned orchestrator task_id/current_task_id under save.

        An assignment is orphaned when the session is idle and the referenced
        task is terminal or its canonical session_id points at another live
        session in the same workspace. Unassigned canonical bindings
        (session_id unset) and missing/stopped canonical owners are never cleared.
        """
        changed = False
        for session in self._sessions_for_workspace_raw(workspace_id):
            if session.role != WorkspaceSessionRole.ORCHESTRATOR:
                continue
            if self._reconcile_orchestrator_session_assignment(session.id):
                changed = True
        if changed:
            self._save_state()
        return changed

    def _session_blocks_agent_reuse(self, session: ManagedSession) -> bool:
        if self._queued_count(session.id) > 0:
            return True
        if session.role == WorkspaceSessionRole.REVIEWER:
            return self._reviewer_has_active_task_binding(session)
        if session.role == WorkspaceSessionRole.ORCHESTRATOR:
            # Never block on raw task_id/current_task_id alone; stale fields are
            # reconciled before reuse. Block only on active canonical ownership.
            return self._session_has_active_canonical_ownership(session)
        return False

    def _find_compatible_workspace_agent(
        self,
        workspace: Workspace,
        payload: EnsureWorkspaceAgentRequest,
    ) -> Optional[ManagedSession]:
        from ..env_preset_resolver import effective_launch_envs_match
        from ..workspace_identity import (
            effective_local_agent_cwd,
            local_agent_cwd_allowed_for_workspace,
            normalize_remote_cwd,
            validate_local_agent_cwd_for_workspace,
        )

        self._cleanup_stale_orchestrator_assignments(workspace.id)

        requested_target = self._effective_agent_target(workspace, payload)
        requested_cwd = payload.cwd or workspace.path
        if requested_target == ExecutionTarget.LOCAL:
            requested_local_cwd = validate_local_agent_cwd_for_workspace(
                workspace.path, requested_cwd
            )
        else:
            requested_local_cwd = effective_local_agent_cwd(requested_cwd)
        requested_remote_cwd = self._resolve_remote_cwd(
            profile_id=payload.remote_profile_id or workspace.remote_profile_id,
            requested_cwd=payload.remote_cwd,
            workspace_cwd=workspace.remote_cwd,
        )

        compatible: list[ManagedSession] = []
        reconcile_changed = False
        for session in self._sessions_for_workspace_raw(workspace.id):
            if session.role != payload.role:
                continue
            if session.agent_type != payload.agent_type:
                continue
            if session.solo_mode != payload.solo_mode:
                continue
            if session.status == ManagedSessionStatus.STOPPED:
                continue
            if session.runtime_status != AgentRuntimeStatus.IDLE:
                continue
            if session.role == WorkspaceSessionRole.ORCHESTRATOR:
                if self._reconcile_orchestrator_session_assignment(session.id):
                    reconcile_changed = True
                session = self.sessions[session.id]
            if self._session_blocks_agent_reuse(session):
                continue
            if session.ephemeral or session.caller_owned_ephemeral:
                continue
            if session.target != requested_target:
                continue
            if requested_target == ExecutionTarget.REMOTE:
                session_profile = session.remote_profile_id or workspace.remote_profile_id
                requested_profile = payload.remote_profile_id or workspace.remote_profile_id
                if session_profile != requested_profile:
                    continue
                session_remote_cwd = normalize_remote_cwd(
                    session.remote_cwd or workspace.remote_cwd
                )
                if session_remote_cwd != normalize_remote_cwd(requested_remote_cwd):
                    continue
            elif not local_agent_cwd_allowed_for_workspace(workspace.path, session.workspace_path):
                continue
            elif effective_local_agent_cwd(session.workspace_path) != requested_local_cwd:
                continue
            if not effective_launch_envs_match(session.env, payload.env):
                continue
            compatible.append(session)

        if reconcile_changed:
            self._save_state()

        if not compatible:
            return None
        return sorted(compatible, key=lambda session: session.created_at)[0]

    def _first_available_workspace_agent(self, workspace_id: str) -> Optional[ManagedSession]:
        agents = self._workspace_agents(workspace_id)
        return agents[0] if agents else None

    def _reviewer_is_active(self, task: WorkspaceTask) -> bool:
        """Return True when the task has a reviewer session that is actively
        working on its review. A missing, idle, or stopped reviewer means the
        prior dispatch got stuck and re-dispatch is allowed."""
        if not task.review_session_id:
            return False
        reviewer = self.sessions.get(task.review_session_id)
        if not reviewer:
            return False
        if reviewer.status == ManagedSessionStatus.STOPPED:
            return False
        if reviewer.runtime_status == AgentRuntimeStatus.IDLE:
            return False
        if reviewer.task_id != task.id and reviewer.current_task_id != task.id:
            return False
        return True

    def _reviewer_dispatch_stuck(self, task: WorkspaceTask) -> bool:
        """Reaper-only test: return True when the task's review dispatch has
        positively failed and a fresh dispatch is warranted.

        This is deliberately stricter than ``not _reviewer_is_active``. The
        fallback reaper must NOT re-dispatch a reviewer that is simply slow:
        the terminal classifier reports ``runtime_status == IDLE`` between
        bursts of model output, and a reviewer reading/thinking over a large
        review prompt emits no terminal frame change for minutes. Treating that
        transient IDLE as "stuck" (the old behaviour) made the reaper re-send
        the review prompt to a reviewer that was actively working, producing a
        duplicate "fallback reaper" review report and a confusing status.

        A dispatch is considered stuck only when the reviewer is genuinely
        unavailable:

        - the task has no ``review_session_id``;
        - the referenced reviewer session no longer exists;
        - the reviewer session is STOPPED;
        - the reviewer is bound to a *different* task.

        A reviewer that is bound to THIS task and not stopped is presumed
        mid-review and is never reaped here regardless of IDLE duration. The
        genuine "bound but the prompt never landed" failure is handled
        separately by the input-box backstop in ``_reap_stuck_reviews``.
        """
        if not task.review_session_id:
            return True
        reviewer = self.sessions.get(task.review_session_id)
        if not reviewer:
            return True
        if reviewer.status == ManagedSessionStatus.STOPPED:
            return True
        if reviewer.task_id != task.id and reviewer.current_task_id != task.id:
            return True
        return False

    async def _reviewer_prompt_still_pending(self, task: WorkspaceTask) -> bool:
        """Backstop for the genuine "bound but the prompt never landed" failure.

        ``_reviewer_dispatch_stuck`` deliberately refuses to reap a reviewer that
        is bound to THIS task and not stopped, because a slow/thinking reviewer
        is reported IDLE by the terminal classifier. But if ``_request_task_review``
        bound the reviewer and the review prompt never actually left the tmux
        input box (e.g. the send raised after binding), the reviewer would sit
        bound + IDLE forever and would never be recovered.

        Return True only when the reviewer's review prompt is *still verifiably
        sitting in the terminal input box* — the same positive signal the
        monitor's ``_detect_prompt_dispatch_stall`` already trusts. A reviewer
        that is genuinely working has already submitted the prompt, so its input
        box is empty and this returns False.
        """
        if not task.review_session_id:
            return False
        reviewer = self.sessions.get(task.review_session_id)
        if not reviewer or not reviewer.tmux_session:
            return False
        try:
            output = await self._capture_tmux_output(reviewer.tmux_session)
        except RuntimeError as exc:
            logger.warning(
                "Could not inspect reviewer prompt for reaper task_id=%s session_id=%s: %s",
                task.id,
                reviewer.id,
                exc,
            )
            return False
        # Check every prefix that could legitimately be in the reviewer's
        # input box (review prompt or hard-recovery reviewer prompt). If none
        # match, the prompt was submitted and the reviewer is genuinely idle.
        for prefix in self._expected_pending_prompt_prefixes(reviewer, task):
            if self._message_still_in_input(output, prefix):
                return True
        return False

    async def _review_dispatch_failed(self, task: WorkspaceTask) -> bool:
        """Reaper gate: True when this task's review dispatch should be re-sent.

        Combines the strict reviewer-availability predicate with the input-box
        backstop so the fallback reaper only re-dispatches on positive evidence
        of a failed dispatch, never on transient reviewer IDLE."""
        if self._reviewer_dispatch_stuck(task):
            return True
        return await self._reviewer_prompt_still_pending(task)

    def _reviewer_has_active_task_binding(self, session: ManagedSession) -> bool:
        return any(
            task.workspace_id == session.workspace_id
            and task.review_session_id == session.id
            and task.status in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}
            for task in self.tasks.values()
        )

    def _release_stale_reviewer_for_task(
        self, task: WorkspaceTask, *, updated_at: datetime
    ) -> None:
        """Clear the task's stale review_session_id reference and reset any
        reviewer sessions whose task_id/current_task_id still point at this
        task but are not actively working on it. Used to unstick review
        dispatch after a prompt send failure or reviewer crash."""
        # Release any reviewer session that still carries this task's id.
        self._release_reviewer_session(
            task,
            status=ManagedSessionStatus.IDLE,
            runtime_status=AgentRuntimeStatus.IDLE,
            updated_at=updated_at,
            include_stale_assignments=True,
        )
        # Clear the task's own stale reference so future dispatch succeeds.
        current = self.tasks.get(task.id)
        if not current:
            return
        if current.review_session_id and not self._reviewer_is_active(current):
            self.tasks[current.id] = current.model_copy(
                update={
                    "review_session_id": None,
                    "updated_at": updated_at,
                }
            )

    def _cleanup_stale_reviewer_assignments(self, workspace_id: str) -> bool:
        """Drop stale task_id/current_task_id values from REVIEWER sessions
        where the referenced task no longer exists, is not awaiting review,
        or does not list the session as its review_session_id. Returns True
        if any session was modified."""
        changed = False
        now = _wm._now()
        for session in self._sessions_for_workspace_raw(workspace_id):
            if session.role != WorkspaceSessionRole.REVIEWER:
                continue
            if not session.task_id and not session.current_task_id:
                continue
            candidate_id = session.task_id or session.current_task_id
            if not candidate_id:
                continue
            task = self.tasks.get(candidate_id)
            should_reset = False
            if not task or task.workspace_id != workspace_id:
                should_reset = True
            elif task.status == WorkspaceTaskStatus.DONE:
                should_reset = True
            elif task.review_session_id != session.id:
                # Session claims the task but the task does not reference this
                # session as its reviewer — the assignment is stale.
                should_reset = True
            elif (
                session.runtime_status == AgentRuntimeStatus.IDLE
                and session.status == ManagedSessionStatus.IDLE
            ):
                # An idle reviewer may still be intentionally bound to a task
                # after a terminal verdict. Only clear the fields when the task
                # itself is no longer in a protected working/review phase.
                should_reset = task.status not in {
                    WorkspaceTaskStatus.WORKING,
                    WorkspaceTaskStatus.REVIEW,
                }
            if not should_reset:
                continue
            logger.info(
                "Cleaning stale reviewer assignment session_id=%s task_id=%s",
                session.id,
                candidate_id,
            )
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "last_auto_continue_at": None,
                    "hard_recovery_task_id": None,
                    "hard_recovery_attempts": 0,
                    "last_hard_recovery_at": None,
                    "prompt_retry_task_id": None,
                    "prompt_retry_attempted_at": None,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            changed = True
        return changed

    async def _prune_orphan_workspace_tabs(self, workspace_id: str) -> int:
        """Delete managed terminal tabs for this workspace that have no backing
        ManagedSession.

        Terminal tabs (ttyd_manager) and managed sessions (workspace_manager)
        persist to separate state files. When a session is removed without its
        tab — e.g. historical temporary-reviewer lifecycle desync — the tab is
        left behind with ``workspace_id``/``workspace_role`` set but no session
        row. Such orphan tabs show up in the terminal tab bar yet are absent
        from the "Manage Agents" board (which lists sessions), so they cannot
        respond to dispatch and cannot be deleted from that UI.

        This reconciler removes those orphans. It is deliberately conservative:

        * Only tabs that carry this ``workspace_id`` are considered, so manual
          terminal tabs (no ``workspace_id``) are never touched.
        * A tab whose id backs a live session is kept.
        * A tab created within ``ORPHAN_TAB_PRUNE_GRACE_SECONDS`` is kept, to
          avoid racing the gap between ``create_tab`` and session registration
          in ``_create_managed_session``.

        Returns the number of orphan tabs pruned.
        """
        live_tab_ids = {
            session.tab_id
            for session in self.sessions.values()
            if session.workspace_id == workspace_id
        }
        now = _wm._now()
        orphan_tab_ids: list[str] = []
        for tab in ttyd_manager.list_tabs():
            if tab.workspace_id != workspace_id:
                continue
            if tab.id in live_tab_ids:
                continue
            created_at = tab.created_at
            if created_at is not None:
                # Tab timestamps and _now() are both naive local datetimes.
                age_seconds = (now - created_at).total_seconds()
                if age_seconds < ORPHAN_TAB_PRUNE_GRACE_SECONDS:
                    continue
            orphan_tab_ids.append(tab.id)

        pruned = 0
        for tab_id in orphan_tab_ids:
            logger.warning(
                "Pruning orphan managed terminal tab with no backing session "
                "workspace_id=%s tab_id=%s",
                workspace_id,
                tab_id,
            )
            try:
                await ttyd_manager.delete_tab(tab_id)
                pruned += 1
            except Exception:
                logger.exception(
                    "Failed to prune orphan managed tab workspace_id=%s tab_id=%s",
                    workspace_id,
                    tab_id,
                )
        return pruned

    def _review_dispatch_in_reaper_grace(self, task: WorkspaceTask, *, now: datetime) -> bool:
        """Return True when a review dispatch is recent enough that the
        reviewer may simply be slow to emit first tokens. The fallback
        reaper should not redispatch in this window even when
        ``_reviewer_is_active`` reports False, because the terminal
        classifier briefly reports IDLE between bursts of model output.

        We grant the grace based on whichever of the timestamps is
        more recent: when the review was last requested, when the goal
        packet was submitted (for pending-GP cases where review_requested_at
        was never set), or when the assigned reviewer last had terminal
        activity recorded.
        """
        candidates: list[datetime] = []
        if task.review_requested_at:
            candidates.append(task.review_requested_at)
        if (
            task.goal_packet is not None
            and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
            and task.goal_packet.updated_at
        ):
            candidates.append(task.goal_packet.updated_at)
        if task.review_session_id:
            reviewer = self.sessions.get(task.review_session_id)
            if reviewer and reviewer.last_activity_at:
                candidates.append(reviewer.last_activity_at)
        if not candidates:
            return False
        latest = max(candidates)
        return (now - latest).total_seconds() < REVIEW_REAPER_DISPATCH_GRACE_SECONDS

    def _is_fallback_reaper_report(self, report: AgentReport) -> bool:
        if report.review_decision != ReviewDecision.REQUEST:
            return False
        blob = " ".join(
            part
            for part in (report.message, report.message_en, report.review_reason)
            if isinstance(part, str)
        ).lower()
        return "fallback reaper" in blob or "background dispatcher" in blob

    def _review_cycle_has_reviewer_activity(self, task_id: str, review_cycle: int) -> bool:
        """True when review_started or a terminal reviewer verdict exists for the cycle."""

        reviewer_states = {
            AgentReportState.REVIEW_STARTED,
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }
        for report in self.reports.values():
            if report.task_id != task_id:
                continue
            if int(report.review_cycle or 0) != review_cycle:
                continue
            if report.state in reviewer_states:
                return True
        return False

    async def _reap_stuck_reviews(self, workspace_id: str) -> int:
        """Fallback reaper: find tasks whose review dispatch appears stuck
        (review-requested or REVIEW status with no active reviewer) and
        trigger a fresh review dispatch. Called from the periodic
        dispatch_workspace loop so transient failures do not permanently
        strand tasks in "Awaiting AI review".

        Returns the number of tasks that were re-dispatched."""
        reaped = 0
        for task in list(self.tasks.values()):
            if task.workspace_id != workspace_id:
                continue
            if task.status == WorkspaceTaskStatus.DONE:
                continue
            if task.system_internal:
                continue
            # Sealed-round guard: once this work round already has a reviewer
            # verdict (``reviewed_cycle >= review_cycle``) the round is closed.
            # It only moves forward via a reopen (continue_task / review_failed,
            # which BUMP review_cycle) or human acceptance — never via re-review.
            # Re-dispatching here is the infinite-loop bug: _request_task_review
            # clears review_completed_at / human_acceptance_requested_at without
            # bumping review_cycle, so the reviewer's re-emitted verdict is
            # stamped at the already-judged cycle and dropped as a closed-round
            # echo by _reviewer_verdict_actionable. review_completed_at is never
            # rewritten, review_in_flight stays true, and the reaper re-fires
            # every loop forever (observed: review_passed applied 14×, zero diff).
            # A genuinely-stuck *unjudged* round has reviewed_cycle < review_cycle
            # and is unaffected, so legitimate crashed-reviewer recovery still
            # runs.
            if state_policy.current_round_has_verdict(task.review_cycle, task.reviewed_cycle):
                logger.debug(
                    "Skipping fallback reaper for task_id=%s: current round already "
                    "has a verdict (review_cycle=%s reviewed_cycle=%s)",
                    task.id,
                    task.review_cycle,
                    task.reviewed_cycle,
                )
                continue
            needs_review_dispatch = False
            if state_policy.review_in_flight(
                task.review_requested_at, task.review_completed_at
            ) and await self._review_dispatch_failed(task):
                # Review was requested but the dispatch positively failed: the
                # reviewer is missing/stopped/cross-bound, or the review prompt
                # is still verifiably sitting unsent in the tmux input box. A
                # reviewer that is bound to this task and merely IDLE (reading or
                # thinking over a large prompt) is NOT reaped here.
                needs_review_dispatch = True
            elif (
                task.status == WorkspaceTaskStatus.REVIEW
                and not task.review_completed_at
                and not task.human_acceptance_requested_at
                and await self._review_dispatch_failed(task)
            ):
                # Task is in REVIEW state with no reviewer progress — a
                # reconciler or manual status transition set REVIEW without
                # actually dispatching a reviewer.
                needs_review_dispatch = True
            elif (
                task.task_mode == WorkspaceTaskMode.REVIEWED
                and task.status == WorkspaceTaskStatus.WORKING
                and task.goal_packet is not None
                and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
                and not state_policy.review_in_flight(
                    task.review_requested_at, task.review_completed_at
                )
            ):
                # Goal packet was marked PENDING_REVIEW (the worker submitted
                # its GP working report) but _request_task_review never fired
                # or threw before persisting review_requested_at — e.g.
                # reviewer creation failed between the goal-packet status
                # write and the review dispatch save. The task appears in
                # WORKING state with a pending-review GP but no reviewer
                # bound; recover by re-dispatching a GP review. The grace
                # check below uses goal_packet.updated_at so a freshly-submitted
                # GP gets the same 60s dispatch window as a normal review.
                needs_review_dispatch = True
            if not needs_review_dispatch:
                continue
            if not task.session_id:
                continue
            now = _wm._now()
            if self._review_dispatch_in_reaper_grace(task, now=now):
                # Reviewer was just dispatched (or has very recent terminal
                # activity). The terminal classifier can briefly report IDLE
                # while the model produces its first tokens — wait the
                # configured grace before declaring the dispatch stuck.
                logger.debug(
                    "Skipping fallback reaper for task_id=%s within dispatch grace",
                    task.id,
                )
                continue
            latest_state = self._latest_report_state(task.id)
            # Detect the pending-goal-packet case: the task carries a GP in
            # PENDING_REVIEW but review was never dispatched (review_requested_at
            # is None). The trigger report must be WORKING state so that
            # _build_review_prompt produces the Goal Packet approval review
            # instructions rather than implementation review instructions.
            is_pending_gp_recovery = (
                task.task_mode == WorkspaceTaskMode.REVIEWED
                and task.goal_packet is not None
                and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
                and not state_policy.review_in_flight(
                    task.review_requested_at, task.review_completed_at
                )
            )
            if is_pending_gp_recovery:
                trigger_state = AgentReportState.WORKING
                trigger_message = (
                    "Re-dispatching pending Goal Packet review (fallback reaper); "
                    "initial GP review dispatch did not complete."
                )
                trigger_message_en = (
                    "Re-dispatching pending Goal Packet review (fallback reaper); "
                    "initial GP review dispatch did not complete."
                )
                trigger_message_zh = (
                    "重新分派等待中的 Goal Packet 评审（fallback reaper）；"
                    "初始 GP 评审分派未完成。"
                )
                trigger_reason = "Pending Goal Packet review recovered by background dispatcher."
            else:
                trigger_state = (
                    latest_state
                    if latest_state
                    in {
                        AgentReportState.READY_FOR_REVIEW,
                        AgentReportState.COMPLETED,
                        AgentReportState.BLOCKED,
                        AgentReportState.NEEDS_INPUT,
                    }
                    else AgentReportState.READY_FOR_REVIEW
                )
                trigger_message = (
                    "Re-dispatching stuck review task (fallback reaper); "
                    "prior reviewer dispatch did not complete."
                )
                trigger_message_en = (
                    "Re-dispatching stuck review task (fallback reaper); "
                    "prior reviewer dispatch did not complete."
                )
                trigger_message_zh = (
                    "重新分派卡住的 review 任务（fallback reaper）；" "之前的 reviewer 分派未完成。"
                )
                trigger_reason = "Stuck review recovered by background dispatcher."
            self._release_stale_reviewer_for_task(task, updated_at=now)
            trigger_report = AgentReport(
                id=str(uuid.uuid4()),
                workspace_id=task.workspace_id,
                task_id=task.id,
                session_id=task.session_id,
                state=trigger_state,
                message=trigger_message,
                message_en=trigger_message_en,
                message_zh=trigger_message_zh,
                changed_files=[],
                validation=None,
                risks=None,
                review_decision=ReviewDecision.REQUEST,
                review_reason=trigger_reason,
                risk_level=None,
                review_cycle=task.review_cycle,
                created_at=now,
            )
            self.reports[trigger_report.id] = trigger_report
            logger.info(
                "Reaping stuck review task_id=%s trigger_state=%s",
                task.id,
                trigger_state.value,
            )
            try:
                await self._request_task_review(self.tasks[task.id], trigger_report)
                reaped += 1
            except Exception:
                logger.exception("Failed to reap stuck review task_id=%s", task.id)
        return reaped

    def _first_available_reviewer(self, workspace_id: str) -> Optional[ManagedSession]:
        """Internal review dispatch only — not CLI agent-create reuse.

        Hub review routing may pick any idle reviewer; CLI ``agent create`` with
        ``reuse_existing`` must use ``_find_compatible_workspace_agent`` instead.
        """
        reviewers = [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.REVIEWER
            and session.status != ManagedSessionStatus.STOPPED
            and session.runtime_status == AgentRuntimeStatus.IDLE
            and not session.task_id
            and not session.current_task_id
            and not self._reviewer_has_active_task_binding(session)
        ]
        if not reviewers:
            return None
        return sorted(reviewers, key=lambda session: (session.ephemeral, session.created_at))[0]

    def _queued_count(self, session_id: str) -> int:
        return len(
            [
                task
                for task in self.tasks.values()
                if task.session_id == session_id and task.status == WorkspaceTaskStatus.QUEUED
            ]
        )

    def _with_assignment_summary(self, session: ManagedSession) -> ManagedSession:
        current_task_id = session.current_task_id or session.task_id
        return session.model_copy(
            update={
                "task_id": session.task_id,
                "current_task_id": current_task_id,
                "queued_count": self._queued_count(session.id),
            }
        )

    async def get_board(
        self,
        workspace_id: str,
        *,
        tasks_limit: int | None = None,
        tasks_cursor: str | None = None,
    ) -> WorkspaceBoard:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        await self._refresh_session_statuses(workspace_id)
        self._reconcile_task_report_statuses(workspace_id)
        self._reconcile_workspace_session_pointers(workspace_id)
        self._cleanup_stale_orchestrator_assignments(workspace_id)
        await self._prune_orphan_workspace_tabs(workspace_id)
        self._sync_workspace_tab_metadata(workspace_id)
        tasks = [
            (
                task.model_copy(
                    update={
                        "prompt": (
                            "System-managed Feedback Reaper task. Inspect its reports and "
                            "summary-run audit for lifecycle details."
                        )
                    }
                )
                if task.system_internal and task.internal_kind == "feedback_reaper"
                else task
            )
            for task in self.tasks.values()
            if task.workspace_id == workspace_id
        ]
        from ..board_pagination import paginate_board_tasks

        page_tasks, tasks_pagination = paginate_board_tasks(
            tasks,
            limit=tasks_limit,
            cursor=tasks_cursor,
        )
        task_ids = {task.id for task in page_tasks}
        reports = [
            report
            for report in self.latest_reports_per_task_for_workspace(workspace_id)
            if report.task_id in task_ids
        ]
        sessions = self.sessions_for_workspace(workspace_id)
        return WorkspaceBoard(
            workspace=self.workspaces[workspace_id],
            tasks=page_tasks,
            sessions=sessions,
            reports=reports,
            markdown_documents=self.markdown_documents_for_workspace(workspace_id),
            snapshot_path=str(self.snapshot_path(workspace_id)),
            tasks_pagination=tasks_pagination,
        )

    def _sync_workspace_tab_metadata(self, workspace_id: str) -> None:
        for session in self._sessions_for_workspace_raw(workspace_id):
            self._sync_session_tab_metadata(session)

    def _sync_session_tab_metadata(self, session: ManagedSession) -> None:
        workspace = self.workspaces.get(session.workspace_id)
        if not workspace:
            return
        ttyd_manager.set_tab_workspace_metadata(
            tab_id=session.tab_id,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=session.role,
        )
