"""Stopped-task continuation and bounded hard recovery."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _AutomaticRecoveryMixin(_WorkspaceManagerBase):
    async def _auto_continue_stopped_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> dict[str, Any] | None:
        if session.session_kind == SessionKind.CHAT:
            # Native transport failures have explicit delivery state. Terminal
            # heuristics must never trigger a second, potentially duplicate turn.
            return None
        # Only the agent that owns the current work phase may be auto-continued:
        # - During WORKING: the worker (task.session_id). After a ``review_failed``
        #   reopen the reviewer session stays bound via ``current_task_id`` but is
        #   NOT the worker; without this guard the monitor would treat the idle
        #   reviewer as a worker owing a report and endlessly auto-prompt it.
        # - During REVIEW: the reviewer (task.review_session_id). The worker may
        #   also be idle but should not be auto-continued while review is in flight.
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None
        session, task = current
        is_worker = bool(task.session_id and task.session_id == session.id)
        is_reviewer = bool(task.review_session_id and task.review_session_id == session.id)
        sealed_round_awaiting_continue = False
        if task.status == WorkspaceTaskStatus.WORKING:
            if not is_worker:
                return None
        elif task.status == WorkspaceTaskStatus.REVIEW:
            # Normally during REVIEW only the reviewer is auto-continued
            # (the worker waits). Exception: a verdict has sealed the round
            # (reviewed_cycle == review_cycle), no human acceptance is being
            # waited on, no next-cycle WORKING report exists, and the sealing
            # verdict is a terminal auto-continuable state (REVIEW_PASSED or
            # REVIEW_FAILED — NOT review_needs_input, which must wait for a
            # human). This catches pre-existing stranded GP-approval /
            # GP-rejected / impl-review-failed tasks as well as any future
            # strandings from cold-resume races.
            round_has_verdict = state_policy.current_round_has_verdict(
                task.review_cycle, task.reviewed_cycle
            )
            sealed_verdict_state = self._sealed_cycle_verdict_state(task.id, task.review_cycle)
            sealed_round_awaiting_continue = (
                is_worker
                and round_has_verdict
                and task.human_acceptance_requested_at is None
                and not self._task_has_post_cycle_report(task.id, task.review_cycle)
                and sealed_verdict_state
                in {AgentReportState.REVIEW_PASSED, AgentReportState.REVIEW_FAILED}
            )
            # Sealed current round: reviewer duty is complete — never auto-continue
            # the reviewer (avoids duplicate review_started / verdict noise).
            # Worker sealed-verdict recovery remains via sealed_round_awaiting_continue.
            if is_reviewer and round_has_verdict:
                return None
            if not is_reviewer and not sealed_round_awaiting_continue:
                return None
        else:
            return None
        # Sealed-verdict recovery: dispatch the proper continue transition
        # rather than a generic "inspect state" soft-nudge. Continue_task will
        # move the task to WORKING, write the next-cycle report, and deliver
        # the saved verdict feedback; if the session is still unable to receive
        # input the new try/except wrappers in _review.py will mark the
        # dispatch stalled and the next monitor tick will retry.
        if sealed_round_awaiting_continue:
            try:
                await self.continue_task(
                    task.id,
                    ContinueTaskRequest(
                        message=(
                            "Continue workspace task from review "
                            "(auto-recovered sealed verdict)."
                        )
                    ),
                )
                logger.info(
                    "Auto-recovered sealed review verdict for task_id=%s session_id=%s",
                    task.id,
                    session.id,
                )
            except Exception as exc:
                logger.warning(
                    "Auto-recovery continue_task still failing for sealed verdict "
                    "task_id=%s session_id=%s: %s",
                    task.id,
                    session.id,
                    exc,
                )
                # Do NOT mark the session WORKING on failure — let downstream
                # stall detection / next monitor tick retry; forcing WORKING
                # here would mask persistent failures (e.g. the worker really
                # is STOPPED and can't receive input).
                return None
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "updated_at": sampled_at,
                "last_activity_at": sampled_at,
            }
        if state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            # For workers during WORKING, review_in_flight means a reviewer is active
            # and the worker should not be prodded. For reviewers during REVIEW,
            # review_in_flight is True while they are reviewing, which is exactly
            # when we want to auto-continue them if they get stuck on errors.
            if task.status == WorkspaceTaskStatus.WORKING:
                return None
        latest_state = self._latest_report_state(task.id)
        if latest_state in {
            AgentReportState.READY_FOR_REVIEW,
            AgentReportState.COMPLETED,
            AgentReportState.BLOCKED,
            AgentReportState.NEEDS_INPUT,
        }:
            return None
        try:
            output = await self._capture_tmux_output(session.tmux_session)
        except RuntimeError as exc:
            logger.warning(
                "Could not inspect workspace agent output for auto-continue session_id=%s: %s",
                session.id,
                exc,
            )
            return None
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None
        session, task = current

        if self._auto_continue_output_looks_busy(output):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": (
                    session.auto_continue_attempts
                    if session.auto_continue_task_id == task.id
                    else 0
                ),
                "updated_at": sampled_at,
            }

        last_activity_at = session.last_activity_at
        is_reviewer_session = (
            task.review_session_id is not None and task.review_session_id == session.id
        )
        if last_activity_at:
            idle_seconds = (sampled_at - last_activity_at).total_seconds()
            # Pre-compute pattern checks so we can choose the right grace.
            _interruption_check = self._auto_continue_interruption_reason(output)
            _completion_check = (
                None if is_reviewer_session else self._auto_continue_completion_reason(output)
            )
            # Use a longer grace for the clean-idle case (no error/completion
            # patterns) because agents legitimately sit at a clean prompt while
            # reading or thinking; nudging too early creates spam. Error and
            # completion patterns use the shorter standard grace.
            effective_grace = (
                AUTO_CONTINUE_CLEAN_IDLE_GRACE_SECONDS
                if not _interruption_check and not _completion_check
                else AUTO_CONTINUE_IDLE_GRACE_SECONDS
            )
            if idle_seconds < effective_grace:
                return None

        interruption_reason = self._auto_continue_interruption_reason(output)
        completion_reason = None
        # Completion patterns (e.g. "ready_for_review", "changed_files") only apply
        # to workers posting final reports. Reviewers post review_passed/review_failed
        # and don't need completion-nudge prompts.
        if not interruption_reason and not is_reviewer_session:
            completion_reason = self._auto_continue_completion_reason(output)
        # Idle-clean-prompt nudge: when a worker is past the clean-idle grace
        # period, not showing busy output, not showing an API error, and not
        # showing completion patterns, it is likely sitting at a fresh input
        # prompt without having received (or having lost) its task/continue
        # prompt. Previously this case returned None and the task stayed stuck
        # in WORKING with an IDLE worker forever; send the inspect-state nudge
        # so the agent reads the snapshot and resumes. Reviewers are NOT nudged
        # here — the stuck-review reaper handles them with its own grace and
        # dispatch-retry logic.
        idle_clean_prompt_reason = None
        if not interruption_reason and not completion_reason and not is_reviewer_session:
            idle_clean_prompt_reason = "idle_clean_prompt"
        if not interruption_reason and not completion_reason and not idle_clean_prompt_reason:
            return None

        attempts = session.auto_continue_attempts if session.auto_continue_task_id == task.id else 0
        hard_attempts = (
            session.hard_recovery_attempts if session.hard_recovery_task_id == task.id else 0
        )
        # Hard recovery cooldown: after a hard recovery, do not immediately fire
        # another one or another soft prompt; wait for the agent to produce output.
        if (
            session.hard_recovery_task_id == task.id
            and session.last_hard_recovery_at
            and (sampled_at - session.last_hard_recovery_at).total_seconds()
            < AUTO_CONTINUE_MIN_INTERVAL_SECONDS * 2
        ):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "hard_recovery_task_id": task.id,
                "hard_recovery_attempts": hard_attempts,
                "updated_at": sampled_at,
            }
        if (
            session.auto_continue_task_id == task.id
            and session.last_auto_continue_at
            and (sampled_at - session.last_auto_continue_at).total_seconds()
            < AUTO_CONTINUE_MIN_INTERVAL_SECONDS
        ):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "updated_at": sampled_at,
            }

        # --- Hard recovery escalation for persistent API errors ---
        # When soft auto-continue prompts fail to revive a Claude agent stuck on an
        # API error (4xx/5xx/overloaded/etc.), escalate: interrupt the agent, clear
        # its context with /clear, wait for settle, then re-inject the task prompt
        # so it can resume within the same conversation. Only Claude supports /clear;
        # Codex/Cursor fall through to the existing soft-prompt path.
        if (
            interruption_reason
            and session.agent_type == AgentType.CLAUDE
            and attempts >= AUTO_CONTINUE_SOFT_ATTEMPTS_BEFORE_HARD_RECOVERY
            and hard_attempts < AUTO_CONTINUE_MAX_HARD_RECOVERIES
        ):
            return await self._perform_hard_recovery(
                session, task, sampled_at, interruption_reason, attempts, hard_attempts
            )

        # If we have exhausted all hard recoveries and are still seeing errors,
        # give up: for workers mark NEEDS_INPUT for manual intervention; for
        # reviewers release the stuck binding so the reaper can re-dispatch.
        if interruption_reason and hard_attempts >= AUTO_CONTINUE_MAX_HARD_RECOVERIES:
            if is_reviewer_session:
                self._release_stale_reviewer_for_task(task, updated_at=sampled_at)
                logger.warning(
                    "Workspace reviewer hard recovery exhausted; releasing for re-dispatch "
                    "session_id=%s task_id=%s soft_attempts=%s hard_attempts=%s reason=%s",
                    session.id,
                    task.id,
                    attempts,
                    hard_attempts,
                    interruption_reason,
                )
                return {
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "hard_recovery_task_id": None,
                    "hard_recovery_attempts": 0,
                    "task_id": None,
                    "current_task_id": None,
                    "updated_at": sampled_at,
                }
            # Keep the durable task in WORKING until the caller records the
            # NEEDS_INPUT report and dispatches its reviewer. Mutating it here
            # would make the caller's fresh revalidation skip that transition.
            logger.warning(
                "Workspace agent hard recovery exhausted session_id=%s task_id=%s "
                "soft_attempts=%s hard_attempts=%s reason=%s",
                session.id,
                task.id,
                attempts,
                hard_attempts,
                interruption_reason,
            )
            return {
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "hard_recovery_task_id": task.id,
                "hard_recovery_attempts": hard_attempts,
                "updated_at": sampled_at,
            }

        # If max soft attempts reached (completion patterns or non-Claude agents),
        # fall through to NEEDS_INPUT / REVIEW. For reviewers, release for re-dispatch.
        if attempts >= AUTO_CONTINUE_MAX_ATTEMPTS:
            if is_reviewer_session:
                self._release_stale_reviewer_for_task(task, updated_at=sampled_at)
                logger.warning(
                    "Workspace reviewer auto-continue limit reached; releasing for re-dispatch "
                    "session_id=%s task_id=%s attempts=%s hard_attempts=%s",
                    session.id,
                    task.id,
                    attempts,
                    hard_attempts,
                )
                return {
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "hard_recovery_task_id": None,
                    "hard_recovery_attempts": 0,
                    "task_id": None,
                    "current_task_id": None,
                    "updated_at": sampled_at,
                }
            # Keep the durable task in WORKING until the caller records the
            # NEEDS_INPUT report and dispatches its reviewer. Mutating it here
            # would make the caller's fresh revalidation skip that transition.
            logger.warning(
                "Workspace agent auto-continue limit reached session_id=%s task_id=%s attempts=%s hard_attempts=%s",
                session.id,
                task.id,
                attempts,
                hard_attempts,
            )
            return {
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "hard_recovery_task_id": task.id,
                "hard_recovery_attempts": hard_attempts,
                "updated_at": sampled_at,
            }

        if interruption_reason:
            message = (
                AUTO_CONTINUE_REVIEWER_MESSAGE if is_reviewer_session else AUTO_CONTINUE_MESSAGE
            )
        elif idle_clean_prompt_reason:
            message = AUTO_CONTINUE_IDLE_PROMPT_MESSAGE
        else:
            message = AUTO_REPORT_MISSING_MESSAGE
        # The agent's context may have been cleared since it learned the report
        # endpoint from its bootstrap/assignment prompt. Restate the endpoint so
        # a cleared agent always has a curl target to POST its report to.
        # Durable attempt is the reminder being sent (attempts+1) so a retry of
        # this paste keeps the same call_id; a later reminder gets a new one.
        reminder_attempt = attempts + 1
        message = (
            f"{message}\n\n"
            f"{self._report_endpoint_curl(session, task.id, purpose='monitor-reminder', attempt=reminder_attempt)}"
        )
        await self._send_tmux_message(session.tmux_session, message)
        attempts += 1
        logger.info(
            "Auto-prompted idle workspace agent session_id=%s task_id=%s "
            "attempt=%s/%s action=%s reason=%s",
            session.id,
            task.id,
            attempts,
            AUTO_CONTINUE_MAX_ATTEMPTS,
            (
                "continue"
                if interruption_reason
                else ("idle_prompt_nudge" if idle_clean_prompt_reason else "report_missing")
            ),
            interruption_reason or idle_clean_prompt_reason or completion_reason,
        )
        return {
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
            "auto_continue_task_id": task.id,
            "auto_continue_attempts": attempts,
            "last_auto_continue_at": sampled_at,
            "last_activity_at": sampled_at,
            "updated_at": sampled_at,
        }

    async def _perform_hard_recovery(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
        interruption_reason: str,
        soft_attempts: int,
        hard_attempts: int,
    ) -> dict[str, Any] | None:
        """Hard-recover a stuck agent: interrupt, /clear, re-inject task prompt.

        This is the escalation path when soft auto-continue prompts fail to
        revive a Claude agent that hit a persistent API error. The conversation
        ID is preserved (same tmux pane, same --session-id), but the in-context
        window is wiped via /clear so the agent can continue without a corrupt
        error state in its context.
        """
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None
        session, task = current
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            logger.warning(
                "Hard recovery could not find workspace for task_id=%s session_id=%s",
                task.id,
                session.id,
            )
            return {
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": soft_attempts,
                "updated_at": sampled_at,
            }

        new_hard_attempts = hard_attempts + 1
        agent_session_id = None
        try:
            tab = ttyd_manager.get_tab(session.tab_id)
            if tab:
                agent_session_id = tab.agent_session_id
        except Exception:
            pass

        logger.warning(
            "Performing hard recovery for stuck agent session_id=%s task_id=%s "
            "reason=%s soft_attempts=%s hard_attempt=%s/%s agent_session_id=%s",
            session.id,
            task.id,
            interruption_reason,
            soft_attempts,
            new_hard_attempts,
            AUTO_CONTINUE_MAX_HARD_RECOVERIES,
            agent_session_id,
        )

        # Step 1: Interrupt the agent (Escape + single Ctrl-C) to dismiss any
        # error dialog and return to the Claude prompt. Serialize this side
        # effect with deletion, but do not hold the mutation lock across the
        # later settle sleeps.
        async with self.workspace_mutation_lock(session.workspace_id):
            current = self._current_monitor_entities(session, task)
            if current is None:
                return None
            session, task = current
            await self._interrupt_session(session)
        await asyncio.sleep(INTERRUPT_SETTLE_SECONDS)
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None
        session, task = current

        # Step 2: Reset through the execution transport, never as Chat text.
        try:
            await self._reset_session_context(session)
        except KeyError:
            return None
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None
        session, task = current
        if session.session_kind != SessionKind.CHAT:
            await asyncio.sleep(max(0.0, CLEAR_CONTEXT_SETTLE_SECONDS - 0.5))
            current = self._current_monitor_entities(session, task)
            if current is None:
                return None
            session, task = current

        # Step 3: Re-inject the appropriate recovery prompt.
        if session.role == WorkspaceSessionRole.REVIEWER:
            trigger_report = self._latest_report_for_task(task.id)
            if trigger_report:
                prompt = self._build_hard_recovery_reviewer_prompt(
                    workspace,
                    task,
                    session,
                    trigger_report,
                    interruption_reason,
                    recovery_attempt=new_hard_attempts,
                )
            else:
                # Should not happen (reviewer only exists when there is a trigger report),
                # but fall back to a simpler message that still uses the same
                # verdict-specific durable recovery call_ids.
                prompt = self._build_hard_recovery_reviewer_fallback_prompt(
                    task,
                    session,
                    interruption_reason,
                    recovery_attempt=new_hard_attempts,
                )
        else:
            prompt = self._build_hard_recovery_worker_prompt(
                workspace,
                task,
                session,
                interruption_reason,
                recovery_attempt=new_hard_attempts,
            )

        try:
            await self.send_session_message(session.id, prompt)
        except KeyError:
            return None
        current = self._current_monitor_entities(session, task)
        if current is None:
            return None

        logger.info(
            "Hard recovery complete for session_id=%s task_id=%s hard_attempt=%s",
            session.id,
            task.id,
            new_hard_attempts,
        )
        return {
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
            "auto_continue_task_id": task.id,
            "auto_continue_attempts": 0,  # Reset soft attempts after hard recovery
            "last_auto_continue_at": None,
            "hard_recovery_task_id": task.id,
            "hard_recovery_attempts": new_hard_attempts,
            "last_hard_recovery_at": sampled_at,
            "last_activity_at": sampled_at,
            "updated_at": sampled_at,
        }
