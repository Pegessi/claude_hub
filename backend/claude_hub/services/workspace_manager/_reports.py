"""Report intake, autonomous run, and review-request handling."""

import hashlib
import json

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ...models.agent_tree import AgentEventType
from ..agent_tree import _request_fingerprint
from ._constants import *  # noqa: F401,F403


class ReportCallIdConflict(ValueError):
    """Raised when a call_id is reused with a different payload fingerprint.

    Maps to HTTP 409 Conflict at the API layer. A call_id identifies a single
    durable report; reusing it with different content is a client error.

    Inherits from ValueError so existing tests that assert ``pytest.raises(
    ValueError)`` continue to pass; the API layer catches this specific
    subclass and returns 409 rather than 400.
    """


class _ReportsMixin:
    def _should_route_goal_packet_for_approval(
        self,
        task: WorkspaceTask | None,
        session: ManagedSession,
        payload: AgentReportCreate,
    ) -> bool:
        """Return true for the plan-only Goal Packet report on reviewed tasks."""

        if task is None or payload.goal_packet is None:
            return False
        if task.task_mode != WorkspaceTaskMode.REVIEWED:
            return False
        if session.role not in {
            WorkspaceSessionRole.ORCHESTRATOR,
            WorkspaceSessionRole.WORKER,
        }:
            return False
        if payload.state != AgentReportState.WORKING:
            return False
        if state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            return False
        current_status = task.goal_packet.status if task.goal_packet else None
        return current_status not in {
            GoalPacketStatus.PENDING_REVIEW,
            GoalPacketStatus.APPROVED,
            GoalPacketStatus.FROZEN,
        }

    def _reviewer_verdict_actionable(
        self,
        task: WorkspaceTask,
        report: AgentReport,
    ) -> bool:
        """Whether a reviewer terminal report should mutate task state.

        Two conditions must hold:

        1. **Cycle-fresh** — ``report_opens_review_round``: the report's
           ``review_cycle`` outranks the task's last-applied ``reviewed_cycle``,
           so it belongs to a round that has not yet been judged. A
           lower-or-equal cycle is a closed-round echo.
        2. **Review in flight** — ``review_in_flight``: a review was actually
           dispatched for this round (``review_requested_at`` set,
           ``review_completed_at`` cleared). A genuine verdict only ever arrives
           in response to a dispatch.

        The cycle check alone is insufficient because a reviewer report is
        stamped with the task's *current* ``review_cycle`` at intake — not the
        round it was "about". A stale/duplicate reviewer message that arrives
        after ``continue_task`` already bumped ``review_cycle`` (e.g. a second
        goal-packet ``review_passed`` echo arriving once the task was reopened
        for the implementation phase) would be stamped with the new, higher
        cycle and so spuriously pass ``report_opens_review_round``. Requiring a
        live review in flight rejects that echo: ``continue_task`` clears
        ``review_requested_at``, so no review is in flight until the worker
        resubmits and a fresh reviewer is dispatched.

        Both ``create_report`` call sites read the task **before** the verdict
        is applied (``self.tasks[...]`` is only model_copy'd after the gates),
        so both ``reviewed_cycle`` and the review-in-flight timestamps are still
        the pre-apply values here.
        """
        return state_policy.report_opens_review_round(
            report.review_cycle,
            task.reviewed_cycle,
        ) and state_policy.review_in_flight(
            task.review_requested_at,
            task.review_completed_at,
        )

    def _compute_report_fingerprint(
        self,
        report: AgentReportCreate | AgentReport,
    ) -> str:
        """Compute a durable canonical fingerprint of a report's content.

        Covers the fields that define the report's semantic content (state,
        message, changed files, validation, risks, acceptance checks, ACKed
        call_ids, review decision, etc.). Excludes bookkeeping fields
        (id, workspace_id, session_id, created_at, review_cycle, call_id)
        so that an incoming ``AgentReportCreate`` and the persisted
        ``AgentReport`` it produces yield the same fingerprint.

        Same content → same fingerprint regardless of field order. This is
        the source of truth for the report-intake idempotency invariant:
        a call_id's fingerprint is stored on first report and a retry with
        the same call_id + same fingerprint returns the existing report;
        same call_id + different fingerprint raises ValueError.
        """
        # Fields that carry report content. Both AgentReportCreate and
        # AgentReport share these; AgentReport adds bookkeeping fields
        # which we exclude.
        content_fields = (
            "state",
            "message",
            "message_en",
            "message_zh",
            "task_id",
            "changed_files",
            "validation",
            "risks",
            "acceptance_check",
            "goal_packet",
            "evaluation_report",
            "review_profiles",
            "profile_results",
            "artifact_refs",
            "confidence",
            "requires_human_judgment",
            "review_decision",
            "review_reason",
            "risk_level",
            "acked_call_ids",
        )
        data = report.model_dump(mode="json")
        content = {k: data.get(k) for k in content_fields}
        canonical = json.dumps(content, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
        call_id = (payload.call_id or "").strip()
        if not call_id:
            content_fp = self._compute_report_fingerprint(payload)
            call_id = f"legacy:{content_fp[:32]}"
            payload = payload.model_copy(update={"call_id": call_id})

        # ------------------------------------------------------------------
        # Atomic claim of (session_id, call_id).
        #
        # Two concurrent requests with the same call_id must not both pass
        # the "existing_report_id is None" check and create two reports.
        # A per-(session, call_id) asyncio lock serializes the claim so only
        # one request creates the report; the other sees the existing report
        # and either returns it (fingerprint match) or raises 409 (mismatch).
        # ------------------------------------------------------------------
        lock_key = f"{session_id}:{call_id}"
        lock = self._report_call_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._report_call_locks[lock_key] = lock

        async with lock:
            return await self._create_report_under_lock(session_id, payload)

    async def _create_report_under_lock(
        self, session_id: str, payload: AgentReportCreate
    ) -> AgentReport:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        # ------------------------------------------------------------------
        # Report intake idempotency (call_id + payload fingerprint).
        #
        # create_report durably persists the report (via _save_state) BEFORE
        # running post-commit side effects (_after_report_recorded, the
        # agent-tree bridge). If any of those side effects raises AFTER the
        # durable commit, the client receives an error and retries. Without
        # an idempotency key the retry creates a SECOND report, a second
        # task transition, and a second bridged event.
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
        new_fp = self._compute_report_fingerprint(payload)
        existing_report_id = session.report_call_ids.get(call_id)
        if existing_report_id is not None:
            existing = self.reports.get(existing_report_id)
            if existing is not None:
                # Prefer the persisted canonical fingerprint (stored at first
                # creation) over recomputing from the existing report. The
                # persisted fingerprint is the source of truth for the
                # call_id's content; recomputing from the report could drift
                # if the model's serialization changes. We still compute the
                # existing report's fingerprint as a cross-check.
                persisted_fp = session.report_call_fingerprints.get(call_id)
                existing_fp = persisted_fp or self._compute_report_fingerprint(existing)
                if new_fp == existing_fp:
                    # Idempotent retry: the report was durably committed
                    # in a previous attempt, but the response may have
                    # failed (e.g. a late exception in
                    # _after_report_recorded or the agent-tree bridge).
                    # Re-run the post-commit side effects against the
                    # EXISTING report. Both are idempotent:
                    #   * _after_report_recorded skips when a review is
                    #     already in flight or a verdict is already
                    #     recorded for this round.
                    #   * _bridge_report_to_agent_event uses
                    #     call_id=f"report:{existing.id}", so emit_event
                    #     deduplicates the bridged event.
                    # This guarantees exactly one report, one task
                    # transition, and one bridged event across retries.
                    existing_task = self.tasks.get(existing.task_id) if existing.task_id else None
                    existing_session = self.sessions.get(existing.session_id)
                    if existing_task is not None and existing_session is not None:
                        await self._after_report_recorded(existing_task, existing_session, existing)
                    if existing_session is not None:
                        self._bridge_report_to_agent_event(existing, existing_session)
                    return existing
                raise ReportCallIdConflict(
                    f"call_id {call_id!r} already used for a different "
                    f"report payload; refusing to overwrite."
                )

        # ------------------------------------------------------------------
        # Pre-commit rollback snapshots.
        #
        # ``_save_state`` is the durable commit point. If it fails (disk
        # full, permission error, fsync failure, etc.), the in-memory
        # mutations below must be rolled back so that a client retry with
        # the same call_id sees a clean state and can succeed. Without
        # rollback, a failed save would leave the report/session/task
        # mutated in memory but not on disk; the next call_id lookup would
        # find the in-memory report and return it, but a cold restart
        # would lose it — violating durability.
        #
        # We snapshot the session and task (if any) BEFORE any mutation
        # (including _rename_session_for_task) and remove the report from
        # self.reports on rollback. The snapshot is a reference to the
        # original object; since all mutations below replace the dict
        # entry with a new object (model_copy), the original is preserved.
        # ------------------------------------------------------------------
        _orig_session = self.sessions.get(session.id)
        task_id = payload.task_id or session.task_id or session.current_task_id
        _orig_task = self.tasks.get(task_id) if task_id else None

        now = _wm._now()
        task: WorkspaceTask | None = None
        if task_id:
            task = self.tasks.get(task_id)
            if not task or task.workspace_id != session.workspace_id:
                raise KeyError(task_id)
            if self._is_stale_report_for_aborted_task(task, session):
                raise RuntimeError(
                    "Task was manually aborted; restart or reassign it before accepting reports."
                )
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

        # ``_report_is_new`` tracks whether the report was just added to
        # ``self.reports`` so the rollback handler knows whether to pop it.
        _report_is_new = report.id not in self.reports

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
            # ------------------------------------------------------------------
            # REVIEWER terminal-review states: write all review fields and
            # release the reviewer binding BEFORE the first (and now only)
            # _save_state call. The previous pattern wrote the report in one
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
        # prevent future-ID poisoning.
        #
        # For Resident sessions (task_id is None) we still process
        # ``acked_call_ids``: the resident's event-batch delivery call_id
        # must be ACKed so the root run's ack_sequence cursor advances.
        # The implicit ``dispatch:{task_id}`` ACK only applies to task-bound
        # sessions.
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
        if ack_set:
            self._ack_call_ids(task_id, session.id, list(ack_set))

        # ------------------------------------------------------------------
        # Durable commit with rollback.
        #
        # ``_save_state`` writes the full workspace state to disk atomically.
        # If it fails, we roll back every in-memory mutation we made (report,
        # session, task) so the state is consistent with what's on disk. The
        # client can then retry with the same call_id; since the report was
        # never persisted, the retry creates it fresh.
        # ------------------------------------------------------------------
        try:
            self._save_state()
        except Exception:
            logger.exception(
                "create_report: _save_state failed; rolling back in-memory mutations "
                "workspace_id=%s task_id=%s session_id=%s report_id=%s call_id=%s",
                session.workspace_id,
                task_id,
                session.id,
                report.id,
                call_id,
            )
            if _report_is_new:
                self.reports.pop(report.id, None)
            if _orig_session is not None:
                self.sessions[session.id] = _orig_session
            if _orig_task is not None:
                self.tasks[task_id] = _orig_task
            raise

        if task_id and task_id in self.tasks:
            await self._after_report_recorded(
                self.tasks[task_id], self.sessions[session.id], report
            )
        # Bridge the report into the agent tree event stream so supervisors
        # can observe managed-task progress via the unified mailbox instead
        # of scanning global reports.
        self._bridge_report_to_agent_event(report, session)
        return report

    def _ack_call_ids(self, task_id: Optional[str], session_id: str, call_ids: list[str]) -> None:
        """Receiver-side commit: move call_ids from pending/processing/uncertain to delivered.

        This is the *commit* step of the durable delivery gate. The delivery
        (pending → processing) happens in the receiver pump
        (``_pump_session_messages``) which persists the intent BEFORE sending
        to tmux. This commit runs when the worker submits a report that
        includes the call_id in ``acked_call_ids`` — proving the worker
        processed the message.

        **Target verification (anti-forgery):**

        A call_id can only be ACKed by the session/task that it was targeted
        at. For each call_id we look up its call record
        (``agent_tree._call_record``) and verify that the record's target
        run corresponds to the current ``task_id`` / ``session_id``. A
        cross-task or cross-session ACK is rejected: the call_id is NOT
        moved to delivered and NO ``followup:delivered`` event is emitted.

        Call_ids in ``pending_call_ids``, ``processing_call_ids``, or
        ``uncertain_call_ids`` are all eligible for ACK (the worker may have
        processed the message even if the Hub's state machine hasn't caught
        up, e.g. a crash between send and persist). Unknown call_ids (not in
        any of the three) are ignored to prevent future-ID poisoning.

        After committing, the call_id's message body is removed from
        ``session.pending_messages`` (the durable inbox) since it is no
        longer needed for re-delivery.
        """
        if not call_ids:
            return
        acked = set(call_ids)

        task = self.tasks.get(task_id)
        session = self.sessions.get(session_id)

        # Determine which call_ids are actually present in this session's
        # pending/processing/uncertain lists. Only these can be committed.
        session_call_ids: set[str] = set()
        if session is not None:
            session_call_ids.update(session.pending_call_ids)
            session_call_ids.update(session.processing_call_ids)
            session_call_ids.update(session.uncertain_call_ids)

        # Target verification: for each call_id, check that its call record's
        # target run matches the current task/session. Reject cross-session
        # or cross-task ACKs.
        verified_acked: list[str] = []
        for call_id in acked:
            if call_id not in session_call_ids:
                # Not in this session's outbox — ignore (future-ID poisoning
                # or cross-session forgery).
                continue
            if not self._verify_call_target(call_id, task_id, session_id):
                logger.warning(
                    "Rejecting forged ACK: call_id=%s does not target task_id=%s session_id=%s",
                    call_id,
                    task_id,
                    session_id,
                )
                continue
            verified_acked.append(call_id)

        if not verified_acked:
            return

        verified_set = set(verified_acked)
        workspace_id = (
            session.workspace_id
            if session is not None
            else task.workspace_id if task is not None else None
        )

        # ---- Agent-tree lifecycle reconciliation (BEFORE delivered mutation) ----
        #
        # reconcile_followup_outcome and the followup:delivered event append
        # each call agent_tree._persist() -> self._save_state(), which writes
        # the FULL workspace state (sessions/tasks) to disk. If we ran these
        # AFTER moving the call_id to delivered (and clearing pending_messages),
        # a failure in the delivered-event append would leave disk committed
        # to delivered + no payload while memory rolls back to processing —
        # unrecoverable after restart.
        #
        # Therefore we run the lifecycle reconciliation FIRST, while the
        # call_id is still in processing and pending_messages still holds
        # the payload. Any persist failure here raises before the
        # session/task delivered mutation, so disk stays processing+payload
        # and the ACK can be retried.
        for call_id in verified_acked:
            # followup: reconcile outcome + emit delivered event.
            self._emit_followup_delivered_if_followup(workspace_id, call_id)
            # resident event-batch delivery: advance ack_sequence cursor.
            self._advance_resident_ack_on_delivery(call_id)

        # ---- Session/task delivered mutation (in-memory only) ----
        #
        # The lifecycle is now durably reconciled. Commit the delivery
        # state machine: move call_ids to delivered and clear their
        # payload from the durable inbox. This is in-memory only; the
        # caller (create_report) persists via _save_state.
        if task is not None:
            to_ack_pending = [c for c in task.pending_call_ids if c in verified_set]
            to_ack_processing = [c for c in task.processing_call_ids if c in verified_set]
            to_ack_uncertain = [c for c in task.uncertain_call_ids if c in verified_set]
            to_ack = to_ack_pending + to_ack_processing + to_ack_uncertain
            if to_ack:
                pending = [c for c in task.pending_call_ids if c not in verified_set]
                processing = [c for c in task.processing_call_ids if c not in verified_set]
                uncertain = [c for c in task.uncertain_call_ids if c not in verified_set]
                delivered = list(task.delivered_call_ids)
                for cid in to_ack:
                    if cid not in delivered:
                        delivered.append(cid)
                self.tasks[task_id] = task.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                        "uncertain_call_ids": uncertain,
                        "delivered_call_ids": delivered,
                    }
                )

        if session is not None:
            to_ack_pending = [c for c in session.pending_call_ids if c in verified_set]
            to_ack_processing = [c for c in session.processing_call_ids if c in verified_set]
            to_ack_uncertain = [c for c in session.uncertain_call_ids if c in verified_set]
            to_ack = to_ack_pending + to_ack_processing + to_ack_uncertain
            if to_ack:
                pending = [c for c in session.pending_call_ids if c not in verified_set]
                processing = [c for c in session.processing_call_ids if c not in verified_set]
                uncertain = [c for c in session.uncertain_call_ids if c not in verified_set]
                delivered = list(session.delivered_call_ids)
                for cid in to_ack:
                    if cid not in delivered:
                        delivered.append(cid)
                # Remove committed messages from the durable inbox and clear
                # their claim timestamps.
                pending_messages = {
                    cid: msg
                    for cid, msg in session.pending_messages.items()
                    if cid not in verified_set
                }
                pending_attachments = {
                    cid: atts
                    for cid, atts in session.pending_attachments.items()
                    if cid not in verified_set
                }
                processing_call_ids_at = {
                    cid: ts
                    for cid, ts in session.processing_call_ids_at.items()
                    if cid not in verified_set
                }
                self.sessions[session_id] = session.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                        "uncertain_call_ids": uncertain,
                        "delivered_call_ids": delivered,
                        "pending_messages": pending_messages,
                        "pending_attachments": pending_attachments,
                        "processing_call_ids_at": processing_call_ids_at,
                    }
                )

    def _verify_call_target(self, call_id: str, task_id: Optional[str], session_id: str) -> bool:
        """Verify that ``call_id``'s call record targets ``task_id``/``session_id``.

        Strict target binding: a tracked call record's target run must resolve
        to the exact task and session that is reporting the ACK. Cross-task
        or cross-session ACKs are rejected.

        Returns True only for:
          * Untracked call_ids (no call record) — legacy dispatch/internal
            call_ids that are verified by being in the session's outbox
            (the ``session_call_ids`` membership check performed by the
            caller before invoking this method).

        Returns False (fail closed) for:
          * A tracked call record whose target run is missing or unresolvable.
          * A tracked call record whose target task differs from ``task_id``.
          * A tracked call record whose target task's session differs from
            ``session_id``.
        """
        workspace_id = None
        session = self.sessions.get(session_id)
        if session is not None:
            workspace_id = session.workspace_id
        else:
            task = self.tasks.get(task_id) if task_id else None
            if task is not None:
                workspace_id = task.workspace_id

        if workspace_id is None:
            return False

        record = self.agent_tree._call_record(workspace_id, call_id)
        if record is None:
            # No call record — legacy untracked call (dispatch/internal).
            # Verified by the caller's session_call_ids membership check.
            return True

        target_run_id = record.get("target")
        if not target_run_id:
            # Tracked call record but missing target — fail closed.
            return False

        target_run = self.agent_tree._runs.get(target_run_id)
        if target_run is None:
            return False

        # The target run's context_ref is either a task id (for managed task
        # runs) or a session id (for the resident root run).
        target_context_ref = target_run.context_ref

        # Case 1: managed task run — context_ref is the task id.
        if target_context_ref and target_context_ref in self.tasks:
            target_task_id = target_context_ref
            if task_id and target_task_id != task_id:
                return False
            target_task = self.tasks.get(target_task_id)
            if target_task is not None:
                if target_task.session_id and target_task.session_id != session_id:
                    # Cross-session ACK: the call was delivered to a
                    # different session. Reject — the reporting session
                    # cannot ACK a call it did not receive.
                    return False
            return True

        # Case 2: resident root run — context_ref is the resident session id.
        # Bind the ACK to the exact resident session: the reporting session
        # must be the one the resident run is linked to.
        if target_context_ref and target_context_ref == session_id:
            return True

        # Case 3: context_ref is neither a known task nor the reporting
        # session — fail closed.
        return False

    def _advance_resident_ack_on_delivery(self, call_id: str) -> None:
        """If ``call_id`` is a resident event-batch delivery, advance ack_sequence.

        The resident's child-event batch is delivered with a call_id that
        has a call record with action="resident_delivery". The record's
        event payload stores the ``max_sequence`` of the delivered events.
        When the resident worker ACKs that call_id, we advance the resident
        root run's ``ack_sequence`` cursor to ``max_sequence``.

        This ensures the cursor only advances AFTER the resident worker
        proves it processed the events — not when the Hub sends the prompt.

        Fail-closed: any exception (including ``agent_tree.ack`` persistence
        failures) propagates to the caller. Because this runs BEFORE the
        session/task delivered-state mutation, a failure here simply prevents
        the delivered commit — the call_id stays in processing (with payload
        intact) and the ACK can be retried.
        """
        # Find the workspace for this call_id by scanning call records.
        workspace_id = None
        for ws_id, records in self.agent_tree._call_index.items():
            if call_id in records:
                workspace_id = ws_id
                break
        if workspace_id is None:
            return

        record = self.agent_tree._call_record(workspace_id, call_id)
        if record is None:
            return
        if record.get("action") != "resident_delivery":
            return

        event = record.get("event")
        if event is None:
            return
        max_sequence = (event.payload or {}).get("max_sequence")
        if max_sequence is None:
            return

        target_run_id = record.get("target")
        if not target_run_id:
            return
        # Do NOT catch exceptions here. If agent_tree.ack fails (e.g.
        # persistence error), the exception propagates before the
        # session/task delivered-state mutation, so the call_id stays in
        # processing (payload intact) and the ACK can be retried.
        self.agent_tree.ack(workspace_id, target_run_id, max_sequence)

    def _emit_followup_delivered_if_followup(
        self, workspace_id: Optional[str], call_id: str
    ) -> None:
        """If ``call_id`` is a followup call_id, reconcile lifecycle + emit delivered.

        This runs BEFORE the session/task delivered-state mutation in
        ``_ack_call_ids``. Both ``reconcile_followup_outcome`` and the
        delivered-event append call ``agent_tree._persist()`` →
        ``_save_state()``, which writes the full workspace state. Running
        them before the delivered mutation ensures a persist failure leaves
        disk at ``processing`` + intact payload, so the ACK is retryable.

        Order:

        1. ``reconcile_followup_outcome`` — emit the ``followup:outcome``
           event (``delivered: False``) and move the run to ``RUNNING``.
           This bridges the delivery state (processing/delivered) back to
           the agent-tree lifecycle. Idempotent.
        2. Append the ``followup:delivered`` event (``delivered: True``)
           to record that the worker actually received and processed the
           followup.

        Persistence exceptions are NOT swallowed: they propagate to
        ``_ack_call_ids`` (and then ``create_report``), which never reaches
        the session/task delivered mutation or its final ``_save_state``.
        The call_id stays in ``processing_call_ids`` (with
        ``pending_messages`` payload intact) so the ACK or an operator
        retry can re-attempt reconciliation. The tmux receipt is already
        set, so no re-paste occurs.
        """
        if not workspace_id:
            return
        record = self.agent_tree._call_record(workspace_id, call_id)
        if record is None:
            return
        if record.get("action") != "followup":
            return
        followup_event = record["event"]
        recipient_id = followup_event.agent_run_id
        author_id = followup_event.author

        # 1. Reconcile the followup lifecycle (outcome event + RUNNING).
        # Must run before the delivered event so the run reflects the
        # settled delivery state. Idempotent — no-op if the outcome
        # event already exists.
        self.agent_tree.reconcile_followup_outcome(workspace_id=workspace_id, call_id=call_id)

        # 2. Emit the delivered event. Idempotent on call_id=f"{call_id}:delivered".
        # Do NOT swallow exceptions: let _ack_call_ids roll back the
        # session/task delivered mutation so the call_id stays retryable.
        self.agent_tree._append_event(
            workspace_id=workspace_id,
            agent_run_id=recipient_id,
            event_type=AgentEventType.PROGRESS,
            author=recipient_id,
            recipient=author_id,
            call_id=f"{call_id}:delivered",
            action="followup:delivered",
            target=recipient_id,
            fingerprint=_request_fingerprint(
                "followup:delivered",
                {"followup_call_id": call_id},
            ),
            payload={"delivered": True, "followup_call_id": call_id},
            rollback_on_error=False,
        )

    def _rollback_processing_to_pending(
        self, task_id: Optional[str], session_id: str, call_ids: list[str]
    ) -> None:
        """Roll back call_ids from ``processing`` back to ``pending``.

        This is the inverse of the pump's intent-persist step. It is used
        when a tmux send fails: the call_id was moved to processing
        (in-flight) before the send, and on failure we move it back to
        pending so the next pump cycle can retry.

        The message body stays in ``pending_messages`` (it was never
        removed) so re-delivery has the payload.
        """
        if not call_ids:
            return
        rollback = set(call_ids)

        task = self.tasks.get(task_id)
        if task is not None:
            to_rollback = [c for c in task.processing_call_ids if c in rollback]
            if to_rollback:
                processing = [c for c in task.processing_call_ids if c not in rollback]
                pending = list(task.pending_call_ids)
                for cid in to_rollback:
                    if cid not in pending:
                        pending.append(cid)
                self.tasks[task_id] = task.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                    }
                )

        session = self.sessions.get(session_id)
        if session is not None:
            to_rollback = [c for c in session.processing_call_ids if c in rollback]
            if to_rollback:
                processing = [c for c in session.processing_call_ids if c not in rollback]
                pending = list(session.pending_call_ids)
                for cid in to_rollback:
                    if cid not in pending:
                        pending.append(cid)
                processing_call_ids_at = {
                    cid: ts
                    for cid, ts in session.processing_call_ids_at.items()
                    if cid not in rollback
                }
                self.sessions[session_id] = session.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                        "processing_call_ids_at": processing_call_ids_at,
                    }
                )

    def _bridge_report_to_agent_event(
        self,
        report: AgentReport,
        session: ManagedSession,
    ) -> None:
        """Translate a managed-task report into an agent tree event.

        Finds the agent run whose ``context_ref`` matches the report's task
        id and emits the corresponding event type. Reports from sessions
        that are not part of an agent tree (e.g. direct human-driven tasks)
        are silently skipped.
        """
        if not report.task_id:
            return
        run = self.agent_tree.get_run_by_context_ref(report.workspace_id, report.task_id)
        if run is None:
            return

        from claude_hub.models.agent_tree import AgentEventType
        from claude_hub.models.schemas import WorkspaceTaskMode

        task = self.tasks.get(report.task_id)
        is_reviewed = task is not None and task.task_mode == WorkspaceTaskMode.REVIEWED

        state_map = {
            AgentReportState.STARTED: AgentEventType.STARTED,
            AgentReportState.WORKING: AgentEventType.PROGRESS,
            AgentReportState.BLOCKED: AgentEventType.BLOCKED,
            AgentReportState.NEEDS_INPUT: AgentEventType.APPROVAL_REQUIRED,
            AgentReportState.READY_FOR_REVIEW: AgentEventType.PROGRESS,
            # For REVIEWED tasks, the worker's COMPLETED report does NOT
            # terminate the run — the task moves to REVIEW status and waits
            # for the reviewer. Only REVIEW_PASSED (from the reviewer) emits
            # the terminal COMPLETED event. For DIRECT tasks, the worker's
            # COMPLETED is the terminal event.
            AgentReportState.COMPLETED: (
                AgentEventType.PROGRESS if is_reviewed else AgentEventType.COMPLETED
            ),
            AgentReportState.REVIEW_STARTED: AgentEventType.PROGRESS,
            AgentReportState.REVIEW_PASSED: AgentEventType.COMPLETED,
            # REVIEW_FAILED does NOT mean the run failed: the task is sent
            # back to WORKING for revisions. Map to PROGRESS so the run
            # status is reconciled to RUNNING (see emit_event's report_state
            # handling).
            AgentReportState.REVIEW_FAILED: AgentEventType.PROGRESS,
            AgentReportState.REVIEW_NEEDS_INPUT: AgentEventType.BLOCKED,
        }
        event_type = state_map.get(report.state, AgentEventType.PROGRESS)

        # The author is the run itself (the executor); the recipient is the
        # run's supervisor so the directed mailbox delivers it.
        self.agent_tree.emit_event(
            workspace_id=report.workspace_id,
            agent_run_id=run.id,
            event_type=event_type,
            author=run.id,
            recipient=run.supervisor_id or run.id,
            call_id=f"report:{report.id}",
            payload={
                "message": report.message,
                "report_id": report.id,
                "report_state": report.state.value,
                "task_id": report.task_id,
            },
        )

    async def _after_report_recorded(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
    ) -> None:
        if session.role == WorkspaceSessionRole.REVIEWER:
            await self._handle_review_report(task, session, report)
            return
        if session.role not in {
            WorkspaceSessionRole.ORCHESTRATOR,
            WorkspaceSessionRole.WORKER,
        }:
            return
        if task.system_internal:
            await self._handle_internal_task_report(task, session, report)
            return
        if self._is_goal_packet_approval_review(task, report):
            await self._request_task_review(task, report)
            return
        if not state_policy.is_review_gate_state(report.state):
            return
        # ------------------------------------------------------------------
        # After-review short-circuit: once the current work round already
        # carries a reviewer verdict (reviewed_cycle >= review_cycle), late
        # orchestrator gate-state reports must not trigger a second reviewer
        # dispatch. Returning here keeps the previous reviewer binding,
        # timestamps, and verdict intact; the fast-path in create_report also
        # already prevented any status/timestamp overwrite on the task row.
        #
        # NOTE: a legitimate reopen (review_failed → continue_task, or
        # goal-packet review_passed → implementation-phase reopen) bumps
        # review_cycle past reviewed_cycle, so current_round_has_verdict is
        # False and the new phase's gate report is correctly routed to a fresh
        # reviewer. This is the cycle-based replacement for the old
        # status==REVIEW + review_completed_at timestamp heuristic.
        # ------------------------------------------------------------------
        if state_policy.current_round_has_verdict(task.review_cycle, task.reviewed_cycle):
            logger.info(
                "Skipping re-review dispatch after recorded reviewer verdict "
                "workspace_id=%s task_id=%s session_id=%s report_state=%s "
                "review_cycle=%s reviewed_cycle=%s",
                task.workspace_id,
                task.id,
                session.id,
                report.state.value,
                task.review_cycle,
                task.reviewed_cycle,
            )
            return
        if state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            # Allow a new gate report to recover a stuck review: only skip if
            # the previously-assigned reviewer session is still actively
            # working on the review.
            if self._reviewer_is_active(task):
                return
            logger.info(
                "Recovering stuck review via agent report task_id=%s "
                "report_state=%s review_session_id=%s",
                task.id,
                report.state.value,
                task.review_session_id,
            )
            self._release_stale_reviewer_for_task(task, updated_at=_wm._now())
        if task.task_mode == WorkspaceTaskMode.DIRECT:
            if report.review_decision == ReviewDecision.REQUEST:
                await self._request_task_review(task, report)
                return
            if report.state in {
                AgentReportState.COMPLETED,
                AgentReportState.READY_FOR_REVIEW,
            }:
                self._mark_task_review_skipped(task, report)
            return
        evidence_gaps = self._completion_evidence_gaps(task, report)
        if report.review_decision == ReviewDecision.SKIP and evidence_gaps:
            await self._request_goal_packet_supplement(task, session, report, evidence_gaps)
            return
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            await self._request_task_review(task, report)
            return
        should_review = await self._should_request_task_review(
            task,
            report,
            trigger_kind="agent_report",
        )
        if should_review:
            await self._request_task_review(task, report)
            return
        self._mark_task_review_skipped(task, report)

    def _is_goal_packet_approval_review(
        self,
        task: WorkspaceTask,
        report: AgentReport,
    ) -> bool:
        return (
            task.task_mode == WorkspaceTaskMode.REVIEWED
            and report.state == AgentReportState.WORKING
            and task.goal_packet is not None
            and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
        )

    async def _handle_internal_task_report(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
    ) -> None:
        if task.internal_kind == "feedback_reaper" and report.state in {
            AgentReportState.BLOCKED,
            AgentReportState.NEEDS_INPUT,
        }:
            updated = self._mark_feedback_summary_retryable(
                task,
                reason=f"Feedback Reaper reported {report.state.value}; ready to retry",
                validation=(
                    f"report_id={report.id}; report_state={report.state.value}; "
                    "pending_input_preserved=true"
                ),
            )
            await self.dispatch_workspace(updated.workspace_id)
            return
        if report.state not in {
            AgentReportState.COMPLETED,
            AgentReportState.READY_FOR_REVIEW,
        }:
            return
        now = _wm._now()
        updated = self.tasks[task.id].model_copy(
            update={
                "status": WorkspaceTaskStatus.DONE,
                "review_skipped_at": now,
                "review_skip_reason": "System-internal task completed without human review.",
                "completed_at": now,
                "human_accepted_at": None,
                "updated_at": now,
            }
        )
        self.tasks[task.id] = updated
        summary_run = None
        processed_count = None
        if updated.internal_kind == "feedback_reaper":
            store = self._feedback_store()
            pending_run = store.summary_run_for_task(updated.workspace_id, updated.id)
            try:
                if pending_run is not None:
                    processed_count = store.commit_staged_summary_input(
                        updated.workspace_id,
                        pending_run.id,
                    )
                summary_run = store.complete_summary_run(
                    updated.workspace_id,
                    updated.id,
                    report,
                    now=now,
                )
                if pending_run is not None:
                    store.discard_staged_summary_input(
                        updated.workspace_id,
                        pending_run.id,
                    )
            except Exception as exc:
                error_text = str(exc).replace("\n", " ")[:400]
                retryable = self._mark_feedback_summary_retryable(
                    updated,
                    reason="Feedback Reaper completion persistence failed; ready to retry",
                    validation=(
                        f"completion_report_id={report.id}; pending_input_preserved=true; "
                        f"error_type={type(exc).__name__}; error={error_text}"
                    ),
                )
                await self.dispatch_workspace(retryable.workspace_id)
                return
        self._record_system_task_audit(
            task=updated,
            message=(
                "Internal Feedback Reaper completed and was archived without ordinary review."
            ),
            message_zh="内部 Feedback Reaper 已完成，并已跳过普通 review 归档。",
            validation=(
                f"completion_report_id={report.id}; session_id={session.id}; "
                "review_gate=skipped_internal"
                + (
                    f"; summary_run_id={summary_run.id}; "
                    f"created_lesson_ids={json.dumps(summary_run.created_lesson_ids)}; "
                    f"merged_lesson_ids={json.dumps(summary_run.merged_lesson_ids)}; "
                    f"skipped_reason={summary_run.skipped_reason}; "
                    f"processed_count={processed_count}"
                    if summary_run
                    else ""
                )
            ),
            session_id=session.id,
        )
        self._write_task_record(updated)
        self._release_task_session(updated)
        if updated.feedback_lesson_ids:
            self._feedback_store().increment_lesson_usage(
                updated.workspace_id,
                list(updated.feedback_lesson_ids),
                success=True,
                now=now,
            )
        self._save_state()
        await self.dispatch_workspace(updated.workspace_id)

    async def _should_request_task_review(
        self,
        task: WorkspaceTask,
        report: AgentReport,
        *,
        trigger_kind: str,
    ) -> bool:
        if trigger_kind != "agent_report":
            return True
        can_skip_review = False
        if (
            report.review_decision == ReviewDecision.SKIP
            and report.state == AgentReportState.COMPLETED
        ):
            can_skip_review = await self._can_skip_task_review(task, report)
        return state_policy.should_request_task_review(
            trigger_kind=trigger_kind,
            report_state=report.state,
            review_decision=report.review_decision,
            can_skip_review=can_skip_review,
        )

    async def _can_skip_task_review(self, task: WorkspaceTask, report: AgentReport) -> bool:
        return state_policy.can_skip_task_review(
            state_policy.ReviewSkipContext(
                report_state=report.state,
                evidence_gaps=self._completion_evidence_gaps(task, report),
                changed_files=report.changed_files,
                risk_level=report.risk_level,
                latest_review_state=self._latest_review_report_state(task.id),
                workspace_has_tracked_changes=await self._workspace_has_tracked_changes(
                    task.workspace_id
                ),
            )
        )

    def _completion_evidence_gaps(
        self,
        task: WorkspaceTask,
        report: AgentReport,
    ) -> list[str]:
        return state_policy.completion_evidence_gaps(
            report.state,
            has_goal_packet=task.goal_packet is not None,
            has_acceptance_check=bool(report.acceptance_check),
        )

    def _autonomous_run_after_worker_report(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
        *,
        now: datetime,
    ) -> AutonomousRun | None:
        phase = state_policy.autonomous_phase_after_worker_report(report.state)
        if phase is None:
            return None
        run = task.autonomous_run or self._default_autonomous_run(
            task.id,
            task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
        )
        # ------------------------------------------------------------------
        # Post-PASS idempotency guard.
        #
        # Once the autonomous run has reached PASSED for the current verdict
        # round, further worker completed/working/ready_for_review reports are
        # stale echoes — the worker re-emitting after human acceptance was
        # already requested. Re-running evaluation here would flip the phase
        # off PASSED (PASSED → EVALUATING/WORKING); once a later review_cycle
        # bump makes ``current_round_has_verdict`` lapse, those stale reports
        # go on to reopen review and clear ``human_acceptance_requested_at``,
        # stranding the task in REVIEW with no usable Done button. Ignore them
        # so the run stays PASSED and acceptance-able.
        #
        # A genuine reopen (continue_task after a FAIL/REVISING verdict) sets
        # phase = WORKING and bumps review_cycle *before* the worker reports
        # again, so report_opens_review_round is True there and legitimate
        # revision rounds proceed untouched.
        # ------------------------------------------------------------------
        if run.phase == AutonomousRunPhase.PASSED and not state_policy.report_opens_review_round(
            report.review_cycle, task.reviewed_cycle
        ):
            logger.info(
                "Ignoring stale post-PASS worker report in _autonomous_run_after_worker_report "
                "workspace_id=%s task_id=%s session_id=%s report_state=%s "
                "report_cycle=%s review_cycle=%s reviewed_cycle=%s",
                task.workspace_id,
                task.id,
                session.id,
                report.state.value,
                report.review_cycle,
                task.review_cycle,
                task.reviewed_cycle,
            )
            return None
        active_session_ids = list(dict.fromkeys([*run.active_session_ids, session.id]))
        iterations = list(run.iterations)
        if report.state in {AgentReportState.READY_FOR_REVIEW, AgentReportState.COMPLETED}:
            iterations.append(
                AutonomousIteration(
                    iteration=run.iteration,
                    worker_session_id=session.id,
                    worker_report_id=report.id,
                    controller_decision="worker_ready_for_evaluation",
                    started_at=run.iterations[-1].started_at if run.iterations else task.started_at,
                    completed_at=now,
                )
            )
        return run.model_copy(
            update={
                "phase": phase,
                "status_summary": self._autonomous_phase_label(phase),
                "active_session_ids": active_session_ids,
                "next_action": self._autonomous_next_action(phase),
                "iterations": iterations,
            }
        )

    def _autonomous_phase_label(self, phase: AutonomousRunPhase) -> str:
        return phase.value.replace("_", " ").title()

    def _autonomous_next_action(self, phase: AutonomousRunPhase) -> str:
        return {
            AutonomousRunPhase.INTAKE: "Derive Goal Packet and begin work",
            AutonomousRunPhase.DISPATCHING: "Select or queue a workspace agent",
            AutonomousRunPhase.WORKING: "Worker is executing the current iteration",
            AutonomousRunPhase.EVALUATING: "Evaluator is reviewing the latest worker output",
            AutonomousRunPhase.REVISING: "Send targeted revision feedback to the worker",
            AutonomousRunPhase.WAITING_FOR_HUMAN: "Waiting for human input or product judgment",
            AutonomousRunPhase.PASSED: "Autonomous evaluation passed; awaiting human acceptance",
            AutonomousRunPhase.EXHAUSTED: "Iteration budget exhausted; awaiting human review",
            AutonomousRunPhase.FAILED: "Autonomous run failed; awaiting human review",
            AutonomousRunPhase.CANCELLED: "Autonomous run cancelled",
        }.get(phase, "Continue autonomous run")

    def _autonomous_run_after_evaluation(
        self,
        task: WorkspaceTask,
        evaluator: ManagedSession,
        report: AgentReport,
        *,
        now: datetime,
    ) -> tuple[AutonomousRun | None, AutonomousRunPhase | None]:
        decision = state_policy.autonomous_decision_from_review_state(report.state)
        if decision is None:
            return task.autonomous_run, None
        run = task.autonomous_run or self._default_autonomous_run(
            task.id,
            task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
        )
        evaluation_report = self._evaluation_report_from_review(
            task=task,
            run=run,
            evaluator=evaluator,
            report=report,
            decision=decision,
            now=now,
        )
        next_phase = state_policy.autonomous_phase_from_evaluation_decision(
            decision=decision,
            current_iteration=run.iteration,
            max_iterations=run.max_iterations,
        )
        next_iteration = run.iteration
        if next_phase == AutonomousRunPhase.REVISING:
            next_iteration += 1
        iterations = list(run.iterations)
        iterations.append(
            AutonomousIteration(
                iteration=run.iteration,
                worker_session_id=task.session_id,
                evaluator_session_id=evaluator.id,
                evaluation_report_id=evaluation_report.id,
                controller_decision=next_phase.value,
                completed_at=now,
            )
        )
        return (
            run.model_copy(
                update={
                    "phase": next_phase,
                    "iteration": next_iteration,
                    "status_summary": self._autonomous_phase_label(next_phase),
                    "active_session_ids": list(
                        dict.fromkeys([*run.active_session_ids, evaluator.id])
                    ),
                    "current_score": evaluation_report.overall_score,
                    "next_action": self._autonomous_next_action(next_phase),
                    "exhausted_at": now if next_phase == AutonomousRunPhase.EXHAUSTED else None,
                    "completed_at": now if next_phase == AutonomousRunPhase.PASSED else None,
                    "evaluation_reports": [*run.evaluation_reports, evaluation_report],
                    "iterations": iterations,
                }
            ),
            next_phase,
        )

    def _evaluation_report_from_review(
        self,
        *,
        task: WorkspaceTask,
        run: AutonomousRun,
        evaluator: ManagedSession,
        report: AgentReport,
        decision: EvaluationDecision,
        now: datetime,
    ) -> EvaluationReport:
        if report.evaluation_report is not None:
            return report.evaluation_report.model_copy(
                update={
                    "run_id": report.evaluation_report.run_id or run.id,
                    "task_id": report.evaluation_report.task_id or task.id,
                    "iteration": report.evaluation_report.iteration or run.iteration,
                    "evaluator_session_id": (
                        report.evaluation_report.evaluator_session_id or evaluator.id
                    ),
                    "created_at": report.evaluation_report.created_at or now,
                }
            )
        score = 1.0 if decision == EvaluationDecision.PASS else None
        blocking_issues = [report.message] if decision == EvaluationDecision.REVISE else []
        suggested_fixes = [report.message] if decision == EvaluationDecision.REVISE else []
        return EvaluationReport(
            id=str(uuid.uuid4()),
            run_id=run.id,
            task_id=task.id,
            iteration=run.iteration,
            evaluator_session_id=evaluator.id,
            overall_score=score,
            decision=decision,
            profile_results=report.profile_results,
            blocking_issues=blocking_issues,
            suggested_fixes=suggested_fixes,
            artifact_refs=report.artifact_refs,
            validation_reviewed=report.validation,
            risks=report.risks,
            confidence=report.confidence,
            requires_human_judgment=report.requires_human_judgment,
            created_at=now,
        )

    async def _request_goal_packet_supplement(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
        gaps: list[str],
    ) -> None:
        now = _wm._now()
        gap_text = ", ".join(gaps)
        # Reopen-to-worker: this hands the task back for reviewable rework, so
        # open the next review round. The worker's subsequent gate report will
        # be stamped with the bumped cycle and thus outranks the prior verdict.
        next_cycle = task.review_cycle + 1
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "review_cycle": next_cycle,
                "reviewed_at": None,
                "updated_at": now,
            }
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "task_id": task.id,
                "current_task_id": task.id,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        self._save_state()
        message = (
            "Your latest completion-style workspace report is missing required Goal Packet "
            f"audit evidence: {gap_text}.\n\n"
            "Please supplement the task before review or review-skip can proceed. If a Goal "
            "Packet has not been stored yet, include goal_packet with objective, "
            "acceptance_criteria, validation_plan, assumptions, out_of_scope, and "
            "handoff_requirements. Include acceptance_check mapping each acceptance criterion "
            "to status passed, failed, partial, or not_checked with evidence. Then POST a new "
            "ready_for_review or completed report.\n\n"
            "Supplement report example:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"completed",'
            '"message":"Supplemented Goal Packet evidence.",'
            '"message_en":"Supplemented Goal Packet evidence.",'
            '"message_zh":"已补充目标包验收证据。",'
            f'"call_id":"{task.id}-gp-supplement-1",'
            '"goal_packet":{"objective":"Concrete task objective.",'
            '"acceptance_criteria":["Reviewer-checkable criterion."],'
            '"validation_plan":["Command or manual check."],'
            '"assumptions":[],"out_of_scope":[],"handoff_requirements":[]},'
            '"acceptance_check":[{"criterion":"Reviewer-checkable criterion.",'
            '"status":"passed","evidence":"Command, file, or manual check evidence."}],'
            '"changed_files":[],"validation":"Checks run.",'
            '"risks":"Residual risk or none",'
            '"review_decision":"request","review_reason":"Goal Packet evidence supplemented.",'
            '"risk_level":"low"}\''
        )
        logger.info(
            "Requesting Goal Packet supplement session_id=%s task_id=%s report_id=%s gaps=%s",
            session.id,
            task.id,
            report.id,
            gap_text,
        )
        await self.send_session_message(session.id, message)

    async def _workspace_has_tracked_changes(self, workspace_id: str) -> bool:
        workspace = self.workspaces.get(workspace_id)
        if not workspace or workspace.target != ExecutionTarget.LOCAL:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                workspace.path,
                "status",
                "--porcelain",
                "--untracked-files=no",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return True
        except OSError:
            return True
        if proc.returncode != 0:
            return False
        return bool(stdout.strip())

    def _mark_task_review_skipped(self, task: WorkspaceTask, report: AgentReport) -> None:
        now = _wm._now()
        reason = report.review_reason or "Agent completed the task without requesting review."
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.REVIEW,
                "review_session_id": None,
                "review_requested_at": None,
                "review_completed_at": None,
                "review_skipped_at": now,
                "review_skip_reason": reason,
                "reviewed_at": now,
                "completed_at": None,
                "human_acceptance_requested_at": now,
                "human_accepted_at": None,
                "updated_at": now,
            }
        )
        self._save_state()

    async def _request_task_review(
        self,
        task: WorkspaceTask,
        trigger_report: AgentReport,
    ) -> None:
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        reviewer = await self._select_or_create_reviewer(workspace, task)
        now = _wm._now()
        lesson_context = self._lesson_context_payload(
            workspace,
            f"{task.title}\n{task.prompt}\n{trigger_report.message}",
        )
        reviewer = await self._rename_session_for_task(reviewer, task, updated_at=now)
        autonomous_run = task.autonomous_run
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            autonomous_run = autonomous_run or self._default_autonomous_run(
                task.id,
                task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
            )
            phase = AutonomousRunPhase.EVALUATING
            autonomous_run = autonomous_run.model_copy(
                update={
                    "phase": phase,
                    "status_summary": self._autonomous_phase_label(phase),
                    "active_session_ids": list(
                        dict.fromkeys([*autonomous_run.active_session_ids, reviewer.id])
                    ),
                    "next_action": self._autonomous_next_action(phase),
                }
            )
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.REVIEW,
                "review_session_id": reviewer.id,
                "review_attempts": task.review_attempts + 1,
                "review_requested_at": now,
                "review_completed_at": None,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "completed_at": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "autonomous_run": autonomous_run,
                "updated_at": now,
            }
        )
        # Capture the reviewer's previously-reviewed task before we overwrite it
        # below. The cross-task /clear decision keys off this session-local value
        # rather than scanning other tasks' review_session_id (which abort/skip/
        # stale-release paths null out, silently dropping the prior-history
        # signal and letting an unrelated review start without /clear).
        previous_review_task_id = reviewer.last_review_task_id
        self.sessions[reviewer.id] = reviewer.model_copy(
            update={
                "task_id": task.id,
                "current_task_id": task.id,
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "prompt_retry_task_id": None,
                "prompt_retry_attempted_at": None,
                "last_review_task_id": task.id,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        self._save_state()
        # Two independent reasons to clear the reviewer's context:
        #   1. The user's "Clear context" choice (task.clear_context) — the same
        #      per-task flag the worker honors in _dispatch_task_to_session. It was
        #      previously only wired to the worker, so ticking the box cleared the
        #      worker but never the reviewer (the reviewer ran with stale context).
        #   2. The reviewer last reviewed a *different* task. Same-task re-review
        #      cycles (review_failed -> fix -> completed, or goal-packet then
        #      implementation review) keep their context; a brand-new reviewer with
        #      no prior review (previous_review_task_id is None) pays no /clear
        #      round-trip.
        # OR them so the user override adds to, rather than replaces, the heuristic.
        user_requested_clear = bool(task.clear_context)
        unrelated_prior_review = (
            previous_review_task_id is not None and previous_review_task_id != task.id
        )
        should_clear_context = user_requested_clear or unrelated_prior_review
        if should_clear_context:
            logger.info(
                "Clearing reviewer context task_id=%s reviewer_id=%s "
                "user_requested=%s unrelated_prior_review=%s",
                task.id,
                reviewer.id,
                user_requested_clear,
                unrelated_prior_review,
            )
            await self.send_session_message(reviewer.id, "/clear")
            await asyncio.sleep(0.5)
        try:
            await self.send_session_message(
                reviewer.id,
                self._build_review_prompt(
                    workspace,
                    self.tasks[task.id],
                    self.sessions[reviewer.id],
                    trigger_report,
                    lesson_context=lesson_context,
                ),
            )
        except Exception as exc:
            logger.exception(
                "Failed to dispatch workspace review prompt task_id=%s reviewer_id=%s",
                task.id,
                reviewer.id,
            )
            self._mark_prompt_dispatch_stalled(
                task_id=task.id,
                session_id=reviewer.id,
                message=(
                    "Reviewer prompt could not be submitted to the terminal; "
                    f"manual recovery is required. Error: {exc}"
                ),
                message_zh=("Reviewer prompt 未能提交到终端；需要手动恢复。" f"错误：{exc}"),
                report_state=AgentReportState.REVIEW_NEEDS_INPUT,
                sampled_at=_wm._now(),
            )
