"""Report processing split from the compatibility mixin."""

from .._constants import *  # noqa: F401,F403
from .events import _ReportEventsMixin
from .identity import ReportCallIdConflict


class _ReportIntakeMixin(_ReportEventsMixin):
    async def create_report(self, session_id: str, payload: AgentReportCreate) -> AgentReport:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        # ------------------------------------------------------------------
        # call_id requirement + legacy compatibility adapter.
        #
        # Every report must carry a stable non-empty call_id so that retries
        # (error-after-commit, context reload, network blip) are idempotent.
        # New clients (worker/reviewer/resident prompts) always emit one.
        # Legacy callers that omit call_id get a deterministic adapter call_id
        # derived from the payload fingerprint: same content → same call_id
        # → same report. This preserves idempotency for legacy paths without
        # requiring them to track a call_id.
        # ------------------------------------------------------------------
        call_id = self._canonical_report_call_id(payload.call_id)
        if not call_id:
            content_fp = self._compute_report_fingerprint(payload)
            call_id = f"legacy:{content_fp[:32]}"
        if payload.call_id != call_id:
            payload = payload.model_copy(update={"call_id": call_id})

        # ------------------------------------------------------------------
        # Atomic full-workspace report transaction.
        #
        # Two concurrent requests with the same call_id must not both pass
        # the "existing_report_id is None" check and create two reports.
        # Report intake replaces whole session/task models and may ACK mailbox
        # cursors shared with TaskMailbox waiters. Hold the workspace mutation
        # lock so concurrent task followup/ack/wait cannot persist between
        # snapshot and rollback restore.  Different call_ids (and
        # worker/reviewer sessions for the same task) also cannot overwrite
        # each other's derived state.
        # ------------------------------------------------------------------
        async with self.workspace_mutation_lock(session.workspace_id):
            # The mailbox pump also replaces the full ManagedSession while it
            # moves pending -> processing.  Hold the same per-session pump lock
            # across the only pre-commit await (_rename_session_for_task) and
            # the transaction so neither side derives from a stale session.
            pump_lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
            async with pump_lock:
                live = self.sessions.get(session_id)
                if not live:
                    raise KeyError(session_id)
                # Resolve same/conflicting call_ids BEFORE any tab rename.
                existing = self._existing_report_for_call_id(live, payload)
                if existing is not None and not self._session_still_bound_to_report(live, existing):
                    report = existing
                else:
                    if existing is None:
                        # New reports only: assignment must fail closed before
                        # rename, snapshot, or any report write. Matching
                        # existing reports replay above even after the
                        # session was rebound to another task.
                        rename_task_id = payload.task_id or live.task_id or live.current_task_id
                        if rename_task_id:
                            rename_task = self.tasks.get(rename_task_id)
                            if (
                                rename_task is not None
                                and rename_task.workspace_id == live.workspace_id
                            ):
                                if self._is_stale_report_for_aborted_task(rename_task, live):
                                    raise RuntimeError(
                                        "Task was manually aborted; restart or reassign "
                                        "it before accepting reports."
                                    )
                                self._ensure_session_may_report_task(live, rename_task)
                                await self._rename_session_for_task(
                                    live, rename_task, updated_at=self._runtime_now()
                                )
                    snapshot = self._snapshot_report_intake_workspace(live.workspace_id)
                    commit_token = self._report_intake_commit_token(
                        live.workspace_id, session_id, call_id
                    )
                    self._report_intake_committed.discard(commit_token)
                    try:
                        report = await self._create_report_under_lock(session_id, payload)
                    except Exception:
                        if commit_token not in self._report_intake_committed:
                            self._restore_report_intake_workspace(live.workspace_id, snapshot)
                        raise
                    finally:
                        self._report_intake_committed.discard(commit_token)

        # Post-commit task/reviewer effects deliberately run after both locks
        # are released. They can send/pump messages and therefore must not
        # execute while the reporting session's pump lock is held. The Agent
        # Tree report bridge itself is already part of the transaction above.
        committed_session = self.sessions.get(report.session_id)
        committed_task = self.tasks.get(report.task_id) if report.task_id else None
        if (
            committed_task is not None
            and committed_session is not None
            and self._session_still_bound_to_report(committed_session, report)
        ):
            await self._after_report_recorded(committed_task, committed_session, report)
        return report

    async def _create_report_under_lock(
        self, session_id: str, payload: AgentReportCreate
    ) -> AgentReport:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        # ------------------------------------------------------------------
        # Report intake idempotency (call_id + payload fingerprint).
        #
        # create_report durably persists the report and TaskMailbox bridge
        # together (via _save_workspace_state) BEFORE running message-sending
        # post-commit effects. If one of those effects raises AFTER the
        # durable commit, the client receives an error and retries. Without
        # an idempotency key the retry creates a SECOND report and task
        # transition.
        #
        # When the client supplies a call_id, we look up the previously
        # persisted report by call_id (stored on the session). If the
        # payload fingerprint matches, we return the existing report
        # verbatim — no duplicate, no double transition, no double event.
        # If the call_id is reused with a DIFFERENT payload, we raise
        # ReportCallIdConflict (HTTP 409): a call_id identifies a single
        # durable report.
        # ------------------------------------------------------------------
        call_id = payload.call_id
        assert call_id  # guaranteed non-empty by create_report's adapter
        existing = self._existing_report_for_call_id(session, payload)
        if existing is not None:
            return self._replay_existing_report_intake(session, existing)
        new_fp = self._compute_report_fingerprint(payload)

        task_id = payload.task_id or session.task_id or session.current_task_id

        now = self._runtime_now()
        task: WorkspaceTask | None = None
        if task_id:
            task = self.tasks.get(task_id)
            if not task or task.workspace_id != session.workspace_id:
                raise KeyError(task_id)
            if self._is_stale_report_for_aborted_task(task, session):
                raise RuntimeError(
                    "Task was manually aborted; restart or reassign it before accepting reports."
                )
            self._ensure_session_may_report_task(session, task)
            # Title already matches after the outer pre-snapshot rename, so
            # this is a synchronous no-op and does not yield the event loop.
            session = await self._rename_session_for_task(session, task, updated_at=now)
        goal_packet_for_task = payload.goal_packet
        if self._should_route_goal_packet_for_approval(task, session, payload):
            # _should_route_goal_packet_for_approval only returns True when the
            # payload carries a goal packet; assert it so mypy can narrow None.
            assert payload.goal_packet is not None
            created_at = (
                payload.goal_packet.created_at
                or (task.goal_packet.created_at if task and task.goal_packet else None)
                or now
            )
            goal_packet_for_task = payload.goal_packet.model_copy(
                update={
                    "status": GoalPacketStatus.PENDING_REVIEW,
                    "created_at": created_at,
                    "updated_at": now,
                }
            )

        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=session.workspace_id,
            task_id=task_id,
            session_id=session.id,
            call_id=payload.call_id,
            state=payload.state,
            message=payload.message,
            message_en=payload.message_en,
            message_zh=payload.message_zh,
            changed_files=payload.changed_files,
            validation=payload.validation,
            risks=payload.risks,
            acceptance_check=payload.acceptance_check,
            evaluation_report=payload.evaluation_report,
            review_profiles=payload.review_profiles,
            profile_results=payload.profile_results,
            artifact_refs=payload.artifact_refs,
            confidence=payload.confidence,
            requires_human_judgment=payload.requires_human_judgment,
            review_decision=payload.review_decision,
            review_reason=payload.review_reason,
            risk_level=payload.risk_level,
            acked_call_ids=payload.acked_call_ids,
            review_cycle=task.review_cycle if task else 0,
            created_at=now,
        )

        self.reports[report.id] = report

        session_status = self._status_from_report(payload.state, session)
        session_update: dict[str, Any] = {
            "status": session_status,
            "runtime_status": self._runtime_from_report(payload.state, session),
            "last_activity_at": now,
            "updated_at": now,
        }
        if task_id:
            session_update["task_id"] = task_id
            session_update["current_task_id"] = task_id
        if task:
            session_update["title"] = session.title
        # Persist the call_id → report_id mapping so a retry with the same
        # call_id returns this report (idempotent intake). Also persist the
        # canonical fingerprint so a retry compares against the stored
        # fingerprint directly (no reliance on recomputing from the report).
        if payload.call_id:
            report_call_ids = dict(session.report_call_ids)
            report_call_ids[payload.call_id] = report.id
            session_update["report_call_ids"] = report_call_ids
            report_call_fingerprints = dict(session.report_call_fingerprints)
            report_call_fingerprints[payload.call_id] = new_fp
            session_update["report_call_fingerprints"] = report_call_fingerprints
        self.sessions[session.id] = session.model_copy(update=session_update)

        if task_id and task_id in self.tasks:
            task_status = self._task_status_from_report(payload.state)
            task_update: dict[str, Any] = {}
            if goal_packet_for_task is not None:
                task_update["goal_packet"] = goal_packet_for_task
            current_task = self.tasks[task_id]
            # ------------------------------------------------------------------
            # Stale / duplicate reviewer verdict suppression.
            #
            # A reviewer terminal report (review_passed / review_failed /
            # review_needs_input) is only authoritative while a review is
            # genuinely in flight (see _reviewer_verdict_actionable). When it
            # is not — e.g. a second goal-packet review_passed arriving after
            # continue_task already reopened the task to WORKING for the
            # implementation phase — the generic status block below would
            # otherwise write status=REVIEW / reviewed_at from
            # _task_status_from_report, seeding a phantom verdict. Zero out
            # task_status so the verdict is recorded for audit but mutates no
            # task state; the reviewer fast-path is likewise gated below.
            # ------------------------------------------------------------------
            if (
                session.role == WorkspaceSessionRole.REVIEWER
                and payload.state
                in {
                    AgentReportState.REVIEW_PASSED,
                    AgentReportState.REVIEW_FAILED,
                    AgentReportState.REVIEW_NEEDS_INPUT,
                }
                and not self._reviewer_verdict_actionable(current_task, report)
            ):
                logger.info(
                    "Ignoring stale/duplicate reviewer verdict with no active review "
                    "workspace_id=%s task_id=%s reviewer=%s decision=%s status=%s "
                    "review_requested_at=%s review_completed_at=%s",
                    session.workspace_id,
                    task_id,
                    session.id,
                    payload.state.value,
                    current_task.status.value,
                    current_task.review_requested_at,
                    current_task.review_completed_at,
                )
                task_status = None
            if current_task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                autonomous_run = self._autonomous_run_after_worker_report(
                    current_task,
                    session,
                    report,
                    now=now,
                )
                if autonomous_run is not None:
                    task_update["autonomous_run"] = autonomous_run
            # ------------------------------------------------------------------
            # ORCHESTRATOR post-review late-report guard.
            #
            # Once a reviewer verdict has been recorded (review_completed_at
            # is set AND task.status is still REVIEW — i.e. the verdict is
            # "still authoritative" and no continue_task / reopen happened),
            # late/follow-up orchestrator reports MUST NOT:
            #   1. overwrite task.status back from REVIEW → WORKING (which
            #      would be invisible in "AI Reviewing" for minutes until
            #      reconcile repairs, or worse permanently lose the verdict
            #      if status becomes WORKING),
            #   2. re-request review by writing review_requested_at /
            #      reviewed_at for a second time (which triggers another
            #      reviewer dispatch cycle against an already-judged task).
            #
            # The key-of-truth is review_completed_at (atomically written
            # in the reviewer-decision fast-path below) TOGETHER with
            # task.status still being REVIEW. Legitimate task reopens
            # (continue_task after review_failed, or goal-packet approval
            #  → implementation-phase reopen) transition status back to
            #  WORKING; in those cases review_completed_at records the
            #  PREVIOUS phase's verdict and a NEW implementation-phase
            #  review MUST be allowed to fire. Requiring status=REVIEW
            #  correctly distinguishes the two cases.
            #
            # We guard both buckets:
            #   • WORKING-family states (STARTED/WORKING/BLOCKED/NEEDS_INPUT)
            #     → drop the status write and started_at overwrite entirely.
            #   • REVIEW-family gate states (READY_FOR_REVIEW/COMPLETED)
            #     → keep status=REVIEW if not already REVIEW, but NEVER
            #       touch review_requested_at / reviewed_at / reviewer
            #       binding fields, since that would trigger the re-review
            #       path.
            # ------------------------------------------------------------------
            _verdict_still_authoritative = state_policy.current_round_has_verdict(
                current_task.review_cycle,
                current_task.reviewed_cycle,
            )
            if (
                session.role == WorkspaceSessionRole.ORCHESTRATOR
                and _verdict_still_authoritative
                and task_status is not None
            ):
                # Drop WORKING/BLOCKED/NEEDS_INPUT/STARTED status write
                # entirely — the verdict is still terminal.
                if task_status == WorkspaceTaskStatus.WORKING:
                    task_update["updated_at"] = now
                    task_update.pop("status", None)
                    task_update.pop("started_at", None)
                    logger.info(
                        "Late orchestrator working-family report after reviewer verdict "
                        "suppressed status overwrite workspace_id=%s task_id=%s "
                        "session_id=%s report_state=%s review_completed_at=%s",
                        session.workspace_id,
                        task_id,
                        session.id,
                        payload.state.value,
                        current_task.review_completed_at,
                    )
                # Keep REVIEW column (so a legitimate new human-acceptance
                # request still surfaces the card in the REVIEW list), but
                # never re-dispatch review or overwrite the reviewer
                # decision timestamp.
                elif task_status == WorkspaceTaskStatus.REVIEW:
                    if current_task.status != WorkspaceTaskStatus.REVIEW:
                        task_update["status"] = WorkspaceTaskStatus.REVIEW
                    task_update["updated_at"] = now
                    # Intentionally do NOT set reviewed_at /
                    # review_requested_at — _request_task_review is also
                    # guarded in _after_report_recorded.
                    logger.info(
                        "Late orchestrator completion/review report after reviewer verdict "
                        "suppressed re-review fields workspace_id=%s task_id=%s "
                        "session_id=%s report_state=%s review_completed_at=%s",
                        session.workspace_id,
                        task_id,
                        session.id,
                        payload.state.value,
                        current_task.review_completed_at,
                    )
                _status_block_applied = True
            else:
                _status_block_applied = False

            if task_status and not _status_block_applied:
                if (
                    session.role == WorkspaceSessionRole.ORCHESTRATOR
                    and task_status == WorkspaceTaskStatus.WORKING
                    and self.tasks[task_id].status == WorkspaceTaskStatus.REVIEW
                    and self.tasks[task_id].review_requested_at
                    and not self.tasks[task_id].review_completed_at
                ):
                    task_update["updated_at"] = now
                else:
                    task_update.update({"status": task_status, "updated_at": now})
                    if task_status == WorkspaceTaskStatus.WORKING:
                        task_update["started_at"] = self.tasks[task_id].started_at or now
                    if task_status == WorkspaceTaskStatus.REVIEW:
                        task_update["reviewed_at"] = now
                    if task_status == WorkspaceTaskStatus.FAILED:
                        task_update["failed_at"] = now
                        task_update["failure_reason"] = payload.message or "agent reported failure"
                        task_update["human_acceptance_requested_at"] = now
            # ------------------------------------------------------------------
            # REVIEWER terminal-review states: write all review fields and
            # release the reviewer binding BEFORE the first (and now only)
            # _save_workspace_state call. The previous pattern wrote the report in one
            # save, then relied on _after_report_recorded → _handle_review_report
            # to do a SECOND save with review_completed_at / human_acceptance_requested_at
            # set. That two-phase gap meant every board poll between them saw
            # review_{passed,failed,needs_input} on the report but not on the
            # task, so the frontend kept rendering "AI Reviewing" until the
            # reconcile repair ran — which matches the user-reported symptom
            # "reviewer passed but dashboard stuck in AI Reviewing for a long
            # time, then resolves". Writing fields here makes reconciliation
            # idempotent and removes the race.
            # ------------------------------------------------------------------
            if (
                session.role == WorkspaceSessionRole.REVIEWER
                and payload.state
                in {
                    AgentReportState.REVIEW_PASSED,
                    AgentReportState.REVIEW_FAILED,
                    AgentReportState.REVIEW_NEEDS_INPUT,
                }
                and self._reviewer_verdict_actionable(self.tasks[task_id], report)
            ):
                current_task = self.tasks[task_id]
                # reviewed_at is intentionally overwritten with ``now`` (not
                # preserved) so the board timeline matches when the reviewer
                # rendered a decision; review_completed_at and (for passed)
                # human_acceptance_requested_at keep any existing value.
                #
                # Goal Packet reviews auto-continue back to the worker and must
                # NEVER set human_acceptance_requested_at — that flag is for
                # implementation-phase passes that legitimately park on human
                # acceptance. Passing human_acceptance_for_passed=False (and
                # clearing any pre-existing value) keeps the UI from showing
                # "Awaiting human acceptance" on GP approvals, and also ensures
                # the auto-recovery predicate can recognise stranded GP verdicts.
                is_gp_review = (
                    current_task.task_mode == WorkspaceTaskMode.REVIEWED
                    and current_task.goal_packet is not None
                    and current_task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
                )
                task_update.update(
                    state_policy.compute_reviewer_verdict_task_update(
                        report_state=payload.state,
                        reviewer_session_id=session.id,
                        now=now,
                        report_review_cycle=report.review_cycle,
                        existing_review_completed_at=current_task.review_completed_at,
                        existing_reviewed_at=current_task.reviewed_at,
                        existing_human_acceptance_requested_at=(
                            current_task.human_acceptance_requested_at
                        ),
                        preserve_existing_review_completed_at=True,
                        preserve_existing_reviewed_at=False,
                        preserve_existing_human_acceptance_requested_at=(not is_gp_review),
                        human_acceptance_for_passed=(not is_gp_review),
                    )
                )
                if is_gp_review:
                    # Explicitly clear any stale human-acceptance flag so the
                    # fast-path state is consistent with "GP auto-continues".
                    task_update["human_acceptance_requested_at"] = None
                # Autonomous mode: recompute next phase so the task remains
                # in sync with _autonomous_run_after_evaluation.
                if current_task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
                    autonomous_run, autonomous_next_phase = self._autonomous_run_after_evaluation(
                        current_task, session, report, now=now
                    )
                    if autonomous_run is not None:
                        task_update["autonomous_run"] = autonomous_run
                        task_update["human_acceptance_requested_at"] = (
                            now if autonomous_next_phase == AutonomousRunPhase.PASSED else None
                        )
                # Goal Packet review transition: move packet status to
                # APPROVED / REJECTED so frontend detail view shows the
                # verdict immediately (and so _handle_goal_packet_review_report
                # can bail out if already transitioned).
                if (
                    current_task.task_mode == WorkspaceTaskMode.REVIEWED
                    and current_task.goal_packet is not None
                    and current_task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
                ):
                    if payload.state == AgentReportState.REVIEW_PASSED:
                        new_packet_status = GoalPacketStatus.APPROVED
                    elif payload.state == AgentReportState.REVIEW_FAILED:
                        new_packet_status = GoalPacketStatus.REJECTED
                    else:
                        new_packet_status = GoalPacketStatus.PENDING_REVIEW
                    if new_packet_status != GoalPacketStatus.PENDING_REVIEW:
                        task_update["goal_packet"] = current_task.goal_packet.model_copy(
                            update={
                                "status": new_packet_status,
                                "updated_at": now,
                            }
                        )
                logger.info(
                    "Reviewer terminal decision recorded in create_report "
                    "workspace_id=%s task_id=%s reviewer=%s decision=%s "
                    "review_completed_at=%s",
                    session.workspace_id,
                    task_id,
                    session.id,
                    payload.state.value,
                    now,
                )
            elif task_update:
                task_update["updated_at"] = now
            if task_update:
                self.tasks[task_id] = self.tasks[task_id].model_copy(update=task_update)
            if session.role == WorkspaceSessionRole.REVIEWER and payload.state in {
                AgentReportState.REVIEW_PASSED,
                AgentReportState.REVIEW_FAILED,
                AgentReportState.REVIEW_NEEDS_INPUT,
            }:
                self._cleanup_stale_reviewer_assignments(session.workspace_id)

        # ------------------------------------------------------------------
        # Durable receiver gate: commit.
        #
        # The receiver (worker) signals completion by including call_ids in
        # ``payload.acked_call_ids`` (plus the implicit dispatch call_id for
        # task-bound sessions).
        # This is the *commit* step: the call-id-scoped effect (the model's
        # turn) has been applied, so the Hub moves the call_id from
        # ``processing_call_ids`` to ``delivered_call_ids``.
        #
        # The *claim* step (pending → processing) does NOT happen here. It
        # happens in the receiver pump (``_pump_session_messages``) BEFORE
        # the message is delivered to the model. See that method for the
        # full claim → deliver → commit lifecycle.
        #
        # Call_ids still in ``pending_call_ids`` at commit time (the receiver
        # ACKed before the pump recorded the claim — e.g. a crash between
        # claim and persist) are moved straight to ``delivered``: the
        # receiver's ACK is authoritative.
        #
        # Unknown call_ids (not in pending or processing) are ignored to
        # prevent future-ID poisoning. ACK authorization follows ordinary
        # Task assignment only; sessions without a linked Task cannot ACK
        # via legacy run context_ref shortcuts.
        # ------------------------------------------------------------------
        ack_set: set[str] = set(payload.acked_call_ids)
        if task_id and task_id in self.tasks:
            # The implicit dispatch ACK covers the call_id of the *current*
            # dispatch attempt: ``dispatch:{task_id}:{dispatch_attempt}``.
            # Each (re-)dispatch increments dispatch_attempt, so the call_id
            # is unique per attempt and matches what _dispatch_task_to_session
            # actually sent.
            task = self.tasks[task_id]
            ack_set.add(f"dispatch:{task_id}:{task.dispatch_attempt}")
            # Legacy compatibility: states persisted before the attempt-scoped
            # call_id change carry ``dispatch:{task_id}`` (no attempt suffix)
            # in pending/processing. ACK it too so a legacy in-flight dispatch
            # is not stranded when the worker reports. The ack path ignores
            # call_ids not present in pending/processing, so adding the legacy
            # form is harmless on new state.
            ack_set.add(f"dispatch:{task_id}")
        wake_targets: set[tuple[str, str]] = set()
        if ack_set:
            wake_targets = self._ack_call_ids(task_id, session.id, list(ack_set))

        # Stage the TaskMailbox event in the same transaction as the report
        # and ACK/cursor mutations. A pre-commit failure restores all of them
        # from the outer workspace snapshot. Wake stays after the outer save.
        bridge_wake_target = self._bridge_report_to_agent_event(
            report, self.sessions.get(session.id, session)
        )
        if bridge_wake_target is not None:
            wake_targets.add(bridge_wake_target)

        # Commit report/session/task and mailbox reconciliation together.
        # ACK helpers only stage memory (persist=False); create_report owns
        # rollback. The marker preserves committed state if a later wake fails.
        self._save_workspace_state(session.workspace_id)
        self._report_intake_committed.add(
            self._report_intake_commit_token(session.workspace_id, session_id, call_id)
        )
        self._wake_report_intake_runs(wake_targets)

        return report
