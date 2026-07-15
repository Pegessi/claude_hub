"""Status refresh, stall detection, auto-continue, reconcile."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _MonitorMixin:
    async def _refresh_session_statuses(
        self,
        workspace_id: Optional[str] = None,
        *,
        run_auto_continue: bool = False,
    ) -> None:
        sessions = [
            session
            for session in self.sessions.values()
            if workspace_id is None or session.workspace_id == workspace_id
        ]
        tab_ids = [session.tab_id for session in sessions]
        statuses = {
            status.tab_id: status
            for status in await ttyd_manager.list_tab_agent_statuses(tab_ids=tab_ids)
        }
        changed = False
        for session in sessions:
            session_id = session.id
            if session.status in {ManagedSessionStatus.DONE, ManagedSessionStatus.ERROR}:
                continue
            status = statuses.get(session.tab_id)
            if not status:
                continue
            next_status = self._map_runtime_status(status)
            runtime_status = status.status
            if next_status == ManagedSessionStatus.STOPPED and self._is_spawn_grace_period(session):
                continue

            current_task_id = session.current_task_id or session.task_id
            update: dict[str, Any] = {
                "status": next_status,
                "runtime_status": runtime_status,
                "current_task_id": current_task_id,
                "updated_at": status.sampled_at,
            }
            if status.last_changed_at:
                update["last_activity_at"] = status.last_changed_at

            if current_task_id:
                task = self.tasks.get(current_task_id)
                if task and task.status in {
                    WorkspaceTaskStatus.WORKING,
                    WorkspaceTaskStatus.REVIEW,
                }:
                    stalled_update = await self._detect_prompt_dispatch_stall(
                        session,
                        task,
                        status.sampled_at,
                    )
                    if stalled_update:
                        update.update(stalled_update)
                        next_status = update["status"]
                        runtime_status = update["runtime_status"]
                        task = self.tasks.get(current_task_id)
                        changed = True
                if (
                    run_auto_continue
                    and runtime_status == AgentRuntimeStatus.IDLE
                    and task
                    and task.status in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}
                ):
                    auto_continue_update = await self._auto_continue_stopped_task(
                        session,
                        task,
                        status.sampled_at,
                    )
                    if auto_continue_update:
                        update.update(auto_continue_update)
                        next_status = update["status"]
                        runtime_status = update["runtime_status"]
                        changed = True
                if (
                    runtime_status == AgentRuntimeStatus.ATTENTION
                    and task
                    and task.status == WorkspaceTaskStatus.WORKING
                ):
                    # If the latest report is already a prompt-dispatch-stall
                    # marker, the stall detector handled the state transition
                    # (session NEEDS_INPUT, risk-level report recorded) and we
                    # must NOT fire _request_task_review here — that would
                    # incorrectly move the task to REVIEW when the worker never
                    # finished implementation, losing the work phase. Prompt
                    # stall is a delivery failure on the worker's own prompt,
                    # not a worker request for review; nudge/auto-recovery
                    # revives it, not a reviewer.
                    if not self._latest_report_has_risk(
                        task.id, PROMPT_STUCK_RISK_LEVEL
                    ) and not state_policy.review_in_flight(
                        task.review_requested_at, task.review_completed_at
                    ):
                        report = AgentReport(
                            id=str(uuid.uuid4()),
                            workspace_id=task.workspace_id,
                            task_id=task.id,
                            session_id=session.id,
                            state=AgentReportState.NEEDS_INPUT,
                            message=status.detail
                            or "Agent runtime is waiting for input; reviewer diagnosis requested.",
                            changed_files=[],
                            validation=None,
                            risks=None,
                            review_cycle=task.review_cycle,
                            created_at=status.sampled_at,
                        )
                        self.reports[report.id] = report
                        await self._request_task_review(task, report)
                    update["task_id"] = current_task_id
                    changed = True

            self.sessions[session_id] = session.model_copy(update=update)
            changed = True
        if changed:
            self._save_state()

    async def _detect_prompt_dispatch_stall(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> dict[str, Any] | None:
        if self._prompt_dispatch_still_in_grace_period(session, task, sampled_at):
            return None
        try:
            output = await self._capture_tmux_output(session.tmux_session)
        except RuntimeError as exc:
            logger.warning(
                "Could not inspect workspace prompt dispatch for session_id=%s: %s",
                session.id,
                exc,
            )
            return None

        # Check ALL known prompt prefixes rather than a single one. The
        # continue_task and hard-recovery paths send prompts that start with
        # different first lines than the initial-dispatch / review prompts;
        # previously the detector only looked for "New workspace task assigned."
        # / "Review workspace task." and silently missed stalled continue /
        # hard-recovery pastes, so a stuck Enter key on the continue prompt
        # was never retried and the worker sat idle indefinitely.
        for prefix in self._expected_pending_prompt_prefixes(session, task):
            if self._message_still_in_input(output, prefix):
                break
        else:
            return None
        if self._latest_report_has_risk(task.id, PROMPT_STUCK_RISK_LEVEL):
            return {
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "updated_at": sampled_at,
            }
        if session.prompt_retry_task_id != task.id:
            await self._retry_pending_prompt_submit(session, task, sampled_at)
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "prompt_retry_task_id": task.id,
                "prompt_retry_attempted_at": sampled_at,
                "updated_at": sampled_at,
                "last_activity_at": sampled_at,
            }
        if self._prompt_dispatch_retry_still_in_grace_period(session, sampled_at):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "updated_at": sampled_at,
            }

        if session.role == WorkspaceSessionRole.REVIEWER:
            message = (
                "Reviewer prompt appears to still be sitting in the terminal input box; "
                "review did not start and manual recovery is required."
            )
            message_zh = "Reviewer prompt 似乎仍停留在终端输入框；评审未启动，需要手动恢复。"
            report_state = AgentReportState.REVIEW_NEEDS_INPUT
        else:
            message = (
                "Workspace task prompt appears to still be sitting in the terminal input box; "
                "the agent did not start executing and manual recovery is required."
            )
            message_zh = (
                "Workspace task prompt 似乎仍停留在终端输入框；Agent 未开始执行，需要手动恢复。"
            )
            report_state = AgentReportState.NEEDS_INPUT

        self._mark_prompt_dispatch_stalled(
            task_id=task.id,
            session_id=session.id,
            message=message,
            message_zh=message_zh,
            report_state=report_state,
            sampled_at=sampled_at,
        )
        return {
            "status": ManagedSessionStatus.NEEDS_INPUT,
            "runtime_status": AgentRuntimeStatus.ATTENTION,
            "updated_at": sampled_at,
        }

    # Prompt prefixes that can appear in a terminal input box. The stall
    # detector checks for ALL of these so that continue / hard-recovery
    # prompts — which start with different first lines than initial dispatch
    # or review prompts — are not silently missed when Enter fails to submit.
    _PROMPT_PREFIX_INITIAL_DISPATCH = "New workspace task assigned."
    _PROMPT_PREFIX_REVIEW = "Review workspace task."
    _PROMPT_PREFIX_CONTINUE = "Continue workspace task from review."
    # Both hard-recovery messages share the first ~60 chars; use enough to
    # disambiguate but not enough to diverge on the "agent" vs "reviewer" word.
    _PROMPT_PREFIX_HARD_RECOVERY = "⚠️  Your previous context was automatically cleared because the"

    def _expected_pending_prompt_prefixes(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> list[str]:
        """Return prompt first-lines that could legitimately be in the input box."""
        if session.role == WorkspaceSessionRole.REVIEWER:
            prefixes = [self._PROMPT_PREFIX_REVIEW]
            # A reviewer that just underwent hard recovery will have the
            # hard-recovery reviewer prompt sitting in the input box.
            if (
                session.hard_recovery_task_id == task.id
                and session.last_hard_recovery_at
                and task.review_requested_at
                and session.last_hard_recovery_at >= task.review_requested_at
            ):
                prefixes.append(self._PROMPT_PREFIX_HARD_RECOVERY)
            return prefixes
        prefixes = [self._PROMPT_PREFIX_INITIAL_DISPATCH, self._PROMPT_PREFIX_CONTINUE]
        if (
            session.hard_recovery_task_id == task.id
            and session.last_hard_recovery_at
            and task.started_at
            and session.last_hard_recovery_at >= task.started_at
        ):
            prefixes.append(self._PROMPT_PREFIX_HARD_RECOVERY)
        return prefixes

    def _prompt_dispatch_still_in_grace_period(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> bool:
        dispatch_started_at = (
            task.review_requested_at
            if session.role == WorkspaceSessionRole.REVIEWER
            else task.started_at
        )
        if not dispatch_started_at:
            return False
        return (
            sampled_at - dispatch_started_at
        ).total_seconds() < PROMPT_DISPATCH_STALL_GRACE_SECONDS

    def _prompt_dispatch_retry_still_in_grace_period(
        self,
        session: ManagedSession,
        sampled_at: datetime,
    ) -> bool:
        if not session.prompt_retry_attempted_at:
            return False
        return (
            sampled_at - session.prompt_retry_attempted_at
        ).total_seconds() < PROMPT_DISPATCH_RETRY_GRACE_SECONDS

    async def _retry_pending_prompt_submit(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> None:
        logger.warning(
            "Workspace prompt appears pending; retrying Enter submit session_id=%s task_id=%s role=%s",
            session.id,
            task.id,
            session.role,
        )
        await self._run_tmux("send-keys", "-t", session.tmux_session, "C-m")
        self.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "prompt_retry_task_id": task.id,
                "prompt_retry_attempted_at": sampled_at,
                "last_activity_at": sampled_at,
                "updated_at": sampled_at,
            }
        )
        self._save_state()

    def _mark_prompt_dispatch_stalled(
        self,
        *,
        task_id: str,
        session_id: str,
        message: str,
        message_zh: str,
        report_state: AgentReportState,
        sampled_at: datetime,
    ) -> None:
        task = self.tasks.get(task_id)
        session = self.sessions.get(session_id)
        if not task or not session:
            return

        # When the prompt is stuck for a REVIEWER, the task was already in
        # REVIEW state (reviewer binding completed before send). Keep it there
        # and mark the session NEEDS_INPUT so the human sees attention needed.
        # When the prompt is stuck for any other role (worker/orchestrator/
        # dispatcher — i.e. an implementation-phase session), the task is
        # already in WORKING state; do NOT demote it to REVIEW — that would
        # incorrectly close the work phase and confuse the dispatch loop.
        # Instead, keep the task in WORKING and mark the session NEEDS_INPUT;
        # the monitor's auto-continue / stall-detector will nudge the worker
        # and retry the prompt.
        is_reviewer = session.role == WorkspaceSessionRole.REVIEWER
        updated_task_status = WorkspaceTaskStatus.REVIEW if is_reviewer else task.status
        updated_session_status = ManagedSessionStatus.NEEDS_INPUT
        updated_runtime_status = AgentRuntimeStatus.ATTENTION

        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session.id,
            state=report_state,
            message=message,
            message_en=message,
            message_zh=message_zh,
            changed_files=[],
            validation=None,
            risks="Prompt dispatch stalled before execution; terminal input needs manual inspection.",
            review_decision=ReviewDecision.SKIP,
            review_reason="Prompt dispatch did not reach execution; independent review cannot run until recovered.",
            risk_level=PROMPT_STUCK_RISK_LEVEL,
            review_cycle=task.review_cycle,
            created_at=sampled_at,
        )
        self.reports[report.id] = report
        self.tasks[task.id] = task.model_copy(
            update={
                "status": updated_task_status,
                "reviewed_at": task.reviewed_at or sampled_at if is_reviewer else task.reviewed_at,
                "updated_at": sampled_at,
            }
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "status": updated_session_status,
                "runtime_status": updated_runtime_status,
                "task_id": task.id,
                "current_task_id": task.id,
                "last_activity_at": sampled_at,
                "updated_at": sampled_at,
            }
        )
        self._save_state()

    async def _auto_continue_stopped_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> dict[str, Any] | None:
        # Only the agent that owns the current work phase may be auto-continued:
        # - During WORKING: the worker (task.session_id). After a ``review_failed``
        #   reopen the reviewer session stays bound via ``current_task_id`` but is
        #   NOT the worker; without this guard the monitor would treat the idle
        #   reviewer as a worker owing a report and endlessly auto-prompt it.
        # - During REVIEW: the reviewer (task.review_session_id). The worker may
        #   also be idle but should not be auto-continued while review is in flight.
        is_worker = task.session_id and task.session_id == session.id
        is_reviewer = task.review_session_id and task.review_session_id == session.id
        if task.status == WorkspaceTaskStatus.WORKING:
            if not is_worker:
                return None
        elif task.status == WorkspaceTaskStatus.REVIEW:
            if not is_reviewer:
                return None
        else:
            return None
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
            self.tasks[task.id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": sampled_at,
                    "updated_at": sampled_at,
                }
            )
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
            self.tasks[task.id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": sampled_at,
                    "updated_at": sampled_at,
                }
            )
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
        message = f"{message}\n\n{self._report_endpoint_curl(session, task.id)}"
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
    ) -> dict[str, Any]:
        """Hard-recover a stuck agent: interrupt, /clear, re-inject task prompt.

        This is the escalation path when soft auto-continue prompts fail to
        revive a Claude agent that hit a persistent API error. The conversation
        ID is preserved (same tmux pane, same --session-id), but the in-context
        window is wiped via /clear so the agent can continue without a corrupt
        error state in its context.
        """
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
        # error dialog and return to the Claude prompt.
        await self._interrupt_session(session)
        await asyncio.sleep(INTERRUPT_SETTLE_SECONDS)

        # Step 2: Send /clear to wipe the context window.
        await self.send_session_message(session.id, "/clear")
        await asyncio.sleep(CLEAR_CONTEXT_SETTLE_SECONDS)

        # Step 3: Re-inject the appropriate recovery prompt.
        if session.role == WorkspaceSessionRole.REVIEWER:
            trigger_report = self._latest_report_for_task(task.id)
            if trigger_report:
                prompt = self._build_hard_recovery_reviewer_prompt(
                    workspace, task, session, trigger_report, interruption_reason
                )
            else:
                # Should not happen (reviewer only exists when there is a trigger report),
                # but fall back to a simpler message.
                prompt = (
                    f"{HARD_RECOVERY_REVIEWER_MESSAGE}\n\n"
                    f"Error detected: {interruption_reason}\n\n"
                    f"Task ID: {task.id}\nTask title: {task.title}\n\n"
                    f"{self._report_endpoint_curl(session, task.id)}"
                )
        else:
            prompt = self._build_hard_recovery_worker_prompt(
                workspace, task, session, interruption_reason
            )

        await self.send_session_message(session.id, prompt)

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

    def _latest_report_for_task(self, task_id: str) -> AgentReport | None:
        """Return the most recent report for a task (any state)."""
        reports = sorted(
            [report for report in self.reports.values() if report.task_id == task_id],
            key=lambda report: report.created_at,
        )
        return reports[-1] if reports else None

    def _auto_continue_completion_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_completion_reason(output)

    def _auto_continue_interruption_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_interruption_reason(output)

    def _auto_continue_recent_output_segment(self, output: str) -> str:
        return state_policy.auto_continue_recent_output_segment(output)

    def _auto_continue_output_looks_busy(self, output: str) -> bool:
        return state_policy.auto_continue_output_looks_busy(output)

    def _reconcile_task_report_statuses(self, workspace_id: str) -> None:
        changed = False
        reports_by_task: dict[str, AgentReport] = {}
        for report in self.reports_for_workspace(workspace_id):
            if report.task_id:
                reports_by_task[report.task_id] = report

        for task_id, report in reports_by_task.items():
            task = self.tasks.get(task_id)
            if (
                not task
                or task.workspace_id != workspace_id
                or task.status == WorkspaceTaskStatus.DONE
            ):
                continue
            # Cycle gate: never reconcile task status from a report belonging to
            # a different work round. A prior-round verdict (e.g. an old
            # REVIEW_PASSED stamped at review_cycle=1) must not resurrect itself
            # after continue_task opened round 2; legacy reports (cycle 0) are
            # likewise ignored once the task has advanced past round 0.
            if report.review_cycle != task.review_cycle:
                continue
            if report.state not in {
                AgentReportState.READY_FOR_REVIEW,
                AgentReportState.COMPLETED,
            }:
                continue
            if task.status == WorkspaceTaskStatus.REVIEW:
                continue
            if task.reviewed_at != report.created_at:
                continue
            if (
                task.updated_at > report.created_at
                and task.started_at
                and task.started_at > report.created_at
            ):
                continue

            self.tasks[task_id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": report.created_at,
                    "updated_at": report.created_at,
                }
            )
            changed = True

        if changed:
            self._save_state()

    def _latest_report_state(self, task_id: str) -> AgentReportState | None:
        reports = sorted(
            [report for report in self.reports.values() if report.task_id == task_id],
            key=lambda report: report.created_at,
        )
        return reports[-1].state if reports else None

    def _latest_report_has_risk(self, task_id: str, risk_level: str) -> bool:
        reports = sorted(
            [report for report in self.reports.values() if report.task_id == task_id],
            key=lambda report: report.created_at,
        )
        return bool(reports and reports[-1].risk_level == risk_level)

    def _latest_review_report_state(self, task_id: str) -> AgentReportState | None:
        reports = sorted(
            [
                report
                for report in self.reports.values()
                if report.task_id == task_id and report.state.value.startswith("review_")
            ],
            key=lambda report: report.created_at,
        )
        return reports[-1].state if reports else None

    def _latest_review_report_for_task(self, task_id: str) -> AgentReport | None:
        """Return the most recent REVIEW_* AgentReport on the task (or None)."""

        reports = sorted(
            [
                report
                for report in self.reports.values()
                if report.task_id == task_id and report.state.value.startswith("review_")
            ],
            key=lambda report: report.created_at,
        )
        return reports[-1] if reports else None

    def _map_runtime_status(self, status: TerminalAgentStatus) -> ManagedSessionStatus:
        return state_policy.managed_status_from_runtime(status.status)

    def _is_spawn_grace_period(self, session: ManagedSession) -> bool:
        if session.status != ManagedSessionStatus.SPAWNING:
            return False
        return (_wm._now() - session.created_at).total_seconds() < 90

    def _status_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> ManagedSessionStatus:
        return state_policy.managed_status_from_report(state, session.role, session.status)

    def _runtime_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> AgentRuntimeStatus:
        return state_policy.runtime_status_from_report(state, session.runtime_status)

    def _task_status_from_report(self, state: AgentReportState) -> Optional[WorkspaceTaskStatus]:
        return state_policy.task_status_from_report(state)

    def _is_stale_report_for_aborted_task(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> bool:
        if not task.manual_aborted_at:
            return False
        return task.session_id != session.id and task.review_session_id != session.id

    def _release_task_session(self, task: WorkspaceTask) -> None:
        if not task.session_id:
            return
        session = self.sessions.get(task.session_id)
        if not session:
            return
        if session.task_id == task.id or session.current_task_id == task.id:
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
                    "updated_at": _wm._now(),
                }
            )

    def _release_reviewer_session(
        self,
        task: WorkspaceTask,
        *,
        status: ManagedSessionStatus,
        runtime_status: AgentRuntimeStatus,
        updated_at: datetime,
        include_stale_assignments: bool = False,
    ) -> None:
        session_ids: set[str] = set()
        if task.review_session_id:
            session_ids.add(task.review_session_id)
        if include_stale_assignments:
            session_ids.update(
                session.id
                for session in self.sessions.values()
                if session.role == WorkspaceSessionRole.REVIEWER
                and (session.task_id == task.id or session.current_task_id == task.id)
            )

        for session_id in session_ids:
            session = self.sessions.get(session_id)
            if (
                not session
                or session.role != WorkspaceSessionRole.REVIEWER
                or (session.task_id != task.id and session.current_task_id != task.id)
            ):
                continue
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": status,
                    "runtime_status": runtime_status,
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "last_auto_continue_at": None,
                    "hard_recovery_task_id": None,
                    "hard_recovery_attempts": 0,
                    "last_hard_recovery_at": None,
                    "updated_at": updated_at,
                    "last_activity_at": updated_at,
                }
            )

    async def _cleanup_reviewer_for_terminal_task(
        self,
        task: WorkspaceTask,
        *,
        updated_at: datetime,
    ) -> None:
        """Release persistent reviewers and delete task-scoped temporary reviewers."""

        session_ids: set[str] = set()
        if task.review_session_id:
            session_ids.add(task.review_session_id)
        session_ids.update(
            session.id
            for session in self.sessions.values()
            if session.role == WorkspaceSessionRole.REVIEWER
            and (session.task_id == task.id or session.current_task_id == task.id)
        )

        for session_id in session_ids:
            session = self.sessions.get(session_id)
            if not session or session.role != WorkspaceSessionRole.REVIEWER:
                continue
            if session.ephemeral:
                self.sessions.pop(session.id, None)
                try:
                    await ttyd_manager.delete_tab(session.tab_id)
                except Exception:
                    logger.exception(
                        "Failed to delete temporary reviewer tab session_id=%s tab_id=%s",
                        session.id,
                        session.tab_id,
                    )
                continue
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
                    "updated_at": updated_at,
                    "last_activity_at": updated_at,
                }
            )

    def _assign_current_task(self, session_id: str, task_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return
        task = self.tasks.get(task_id)
        title = task.title.strip() if task and task.title.strip() else session.title
        if task:
            renamed = ttyd_manager.rename_tab(session.tab_id, title)
            if not renamed:
                logger.warning(
                    "Could not rename workspace session tab for task session_id=%s tab_id=%s task_id=%s",
                    session.id,
                    session.tab_id,
                    task.id,
                )
        self.sessions[session_id] = session.model_copy(
            update={
                "task_id": task_id,
                "current_task_id": task_id,
                "title": title,
                "auto_continue_task_id": task_id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "updated_at": _wm._now(),
            }
        )
