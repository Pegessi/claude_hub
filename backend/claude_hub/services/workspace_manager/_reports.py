"""Report intake, autonomous run, and review-request handling."""

import copy
import hashlib
import json

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ...models.task_mailbox import TaskActorRole, TaskEventType
from ..request_fingerprint import request_fingerprint
from ..task_graph import task_supervisor_consumer_key
from ._constants import *  # noqa: F401,F403

_REPORT_STATE_TO_TASK_EVENT: dict[AgentReportState, TaskEventType] = {
    AgentReportState.STARTED: TaskEventType.STARTED,
    AgentReportState.WORKING: TaskEventType.PROGRESS,
    AgentReportState.BLOCKED: TaskEventType.NEEDS_INPUT,
    AgentReportState.NEEDS_INPUT: TaskEventType.NEEDS_INPUT,
    AgentReportState.READY_FOR_REVIEW: TaskEventType.REPORT,
    AgentReportState.COMPLETED: TaskEventType.COMPLETED,
    AgentReportState.FAILED: TaskEventType.FAILED,
    AgentReportState.REVIEW_STARTED: TaskEventType.REVIEW_STARTED,
    AgentReportState.REVIEW_PASSED: TaskEventType.REVIEW_PASSED,
    AgentReportState.REVIEW_FAILED: TaskEventType.REVIEW_FAILED,
    AgentReportState.REVIEW_NEEDS_INPUT: TaskEventType.REVIEW_NEEDS_INPUT,
}


class ReportCallIdConflict(ValueError):
    """Raised when a call_id is reused with a different payload fingerprint.

    Maps to HTTP 409 Conflict at the API layer. A call_id identifies a single
    durable report; reusing it with different content is a client error.

    Inherits from ValueError so existing tests that assert ``pytest.raises(
    ValueError)`` continue to pass; the API layer catches this specific
    subclass and returns 409 rather than 400.
    """


class _ReportsMixin:
    def _snapshot_report_intake_workspace(self, workspace_id: str) -> dict[str, Any]:
        """Deep snapshot every durable domain mutated by report intake."""

        mailbox = self.task_mailbox
        workspace = self.workspaces.get(workspace_id)
        return {
            "sessions": {
                key: value.model_copy(deep=True)
                for key, value in self.sessions.items()
                if value.workspace_id == workspace_id
            },
            "tasks": {
                key: value.model_copy(deep=True)
                for key, value in self.tasks.items()
                if value.workspace_id == workspace_id
            },
            "reports": {
                key: value.model_copy(deep=True)
                for key, value in self.reports.items()
                if value.workspace_id == workspace_id
            },
            "workspace": workspace.model_copy(deep=True) if workspace is not None else None,
            "task_events_present": workspace_id in mailbox._events,
            "task_events": copy.deepcopy(mailbox._events.get(workspace_id, [])),
            "task_call_index_present": workspace_id in mailbox._call_index,
            "task_call_index": copy.deepcopy(mailbox._call_index.get(workspace_id, {})),
            "task_next_seq_present": workspace_id in mailbox._next_seq,
            "task_next_seq": mailbox._next_seq.get(workspace_id),
        }

    def _restore_report_intake_workspace(self, workspace_id: str, snapshot: dict[str, Any]) -> None:
        """Restore a snapshot made by :meth:`_snapshot_report_intake_workspace`."""

        for collection_name in ("sessions", "tasks", "reports"):
            collection = getattr(self, collection_name)
            for key, value in list(collection.items()):
                if value.workspace_id == workspace_id:
                    collection.pop(key, None)
            collection.update(snapshot[collection_name])

        if snapshot.get("workspace") is not None:
            self.workspaces[workspace_id] = snapshot["workspace"]

        mailbox = self.task_mailbox
        if snapshot.get("task_events_present"):
            mailbox._events[workspace_id] = snapshot["task_events"]
        else:
            mailbox._events.pop(workspace_id, None)
        if snapshot.get("task_call_index_present"):
            mailbox._call_index[workspace_id] = snapshot["task_call_index"]
        else:
            mailbox._call_index.pop(workspace_id, None)
        if snapshot.get("task_next_seq_present"):
            mailbox._next_seq[workspace_id] = snapshot["task_next_seq"]
        else:
            mailbox._next_seq.pop(workspace_id, None)

    def _wake_report_intake_runs(self, wake_targets: set[tuple[str, str]]) -> None:
        """Wake TaskMailbox consumers after the outer commit succeeds."""

        for consumer_key, _author in wake_targets:
            if consumer_key.startswith("task:"):
                self.task_mailbox._waiters.wake(consumer_key)

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

    def _canonical_report_call_id(self, call_id: str | None) -> str:
        """Strip leading/trailing whitespace so padded public IDs alias."""
        return (call_id or "").strip()

    def _report_intake_commit_token(
        self, workspace_id: str, session_id: str, call_id: str | None
    ) -> str:
        """Stable commit marker; always uses the canonical call_id."""
        return f"{workspace_id}\0{session_id}\0{self._canonical_report_call_id(call_id)}"

    def _stored_report_call_key(self, session: ManagedSession, call_id: str) -> str | None:
        """Match a canonical call_id to the key actually stored on the session."""
        if call_id in session.report_call_ids:
            return call_id
        for key in session.report_call_ids:
            if self._canonical_report_call_id(key) == call_id:
                return key
        return None

    def _existing_report_for_call_id(
        self, session: ManagedSession, payload: AgentReportCreate
    ) -> AgentReport | None:
        """Return the durable report for this call_id, or raise on conflict.

        Must run before any tab rename. A late retry after the session was
        reused for another task must not restore the previous title.
        Public call_ids are compared after stripping leading/trailing
        whitespace so a padded retry cannot bypass the preflight.
        """
        call_id = self._canonical_report_call_id(payload.call_id)
        if not call_id:
            return None
        stored_key = self._stored_report_call_key(session, call_id)
        if stored_key is None:
            return None
        existing_report_id = session.report_call_ids.get(stored_key)
        if existing_report_id is None:
            return None
        existing = self.reports.get(existing_report_id)
        if existing is None:
            return None
        new_fp = self._compute_report_fingerprint(payload)
        persisted_fp = session.report_call_fingerprints.get(
            stored_key
        ) or session.report_call_fingerprints.get(call_id)
        existing_fp = persisted_fp or self._compute_report_fingerprint(existing)
        if new_fp == existing_fp:
            return existing
        raise ReportCallIdConflict(
            f"call_id {call_id!r} already used for a different "
            f"report payload; refusing to overwrite."
        )

    def _session_still_bound_to_report(self, session: ManagedSession, report: AgentReport) -> bool:
        if not report.task_id:
            return True
        bound = session.current_task_id or session.task_id
        return bound == report.task_id

    def _ensure_session_may_report_task(self, session: ManagedSession, task: WorkspaceTask) -> None:
        """Reject reports from a session that is not the Task's current assignee.

        Reviewers must match ``task.review_session_id``. Workers must match
        ``task.session_id``. An unassigned Task (``session_id is None``) cannot
        be claimed by a worker report; callers must fail closed.
        """
        if session.workspace_id != task.workspace_id:
            raise RuntimeError("Session belongs to a different workspace")
        if session.role == WorkspaceSessionRole.REVIEWER:
            if task.review_session_id != session.id:
                raise RuntimeError("Session is not the current reviewer for this task")
            return
        if task.session_id is None:
            raise RuntimeError("Task has no assigned worker session")
        if task.session_id != session.id:
            raise RuntimeError("Session is not the current worker for this task")

    def _replay_existing_report_intake(
        self, session: ManagedSession, existing: AgentReport
    ) -> AgentReport:
        """Idempotent retry of a known matching call_id. Does not rename."""
        existing_task = self.tasks.get(existing.task_id) if existing.task_id else None
        existing_session = self.sessions.get(existing.session_id)
        retry_wake_targets: set[tuple[str, str]] = set()
        if existing_session is not None:
            retry_ack_set: set[str] = set(existing.acked_call_ids)
            if existing_task is not None:
                retry_ack_set.add(f"dispatch:{existing_task.id}:{existing_task.dispatch_attempt}")
                retry_ack_set.add(f"dispatch:{existing_task.id}")
            if retry_ack_set:
                retry_wake_targets = self._ack_call_ids(
                    existing_task.id if existing_task else None,
                    existing_session.id,
                    list(retry_ack_set),
                )
            bridge_wake_target = self._bridge_report_to_agent_event(existing, existing_session)
            if bridge_wake_target is not None:
                retry_wake_targets.add(bridge_wake_target)
        self._save_state()
        self._report_intake_committed.add(
            self._report_intake_commit_token(session.workspace_id, session.id, existing.call_id)
        )
        self._wake_report_intake_runs(retry_wake_targets)
        return existing

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
                                    live, rename_task, updated_at=_wm._now()
                                )
                    snapshot = self._snapshot_report_intake_workspace(live.workspace_id)
                    commit_token = self._report_intake_commit_token(
                        live.workspace_id, session_id, call_id
                    )
                    self._report_intake_committed.discard(commit_token)
                    persistence_token = self._report_intake_workspace.set(live.workspace_id)
                    try:
                        report = await self._create_report_under_lock(session_id, payload)
                    except Exception:
                        if commit_token not in self._report_intake_committed:
                            self._restore_report_intake_workspace(live.workspace_id, snapshot)
                        raise
                    finally:
                        self._report_intake_workspace.reset(persistence_token)
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
        # together (via _save_state) BEFORE running message-sending
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

        # Single durable commit for report/session/task plus mailbox ACK and
        # TaskMailbox cursor reconciliation. The ACK helpers above only mutate
        # in-memory state (persist=False); if this save fails the workspace
        # workspace snapshot owned by create_report restores every domain.
        self._save_state()
        self._report_intake_committed.add(
            self._report_intake_commit_token(session.workspace_id, session_id, call_id)
        )
        self._wake_report_intake_runs(wake_targets)

        return report

    def _ack_call_ids(
        self, task_id: Optional[str], session_id: str, call_ids: list[str]
    ) -> set[tuple[str, str]]:
        """Receiver-side commit: move call_ids from pending/processing/uncertain to delivered.

        This is the *commit* step of the durable delivery gate. The delivery
        (pending → processing) happens in the receiver pump
        (``_pump_session_messages``) which persists the intent BEFORE sending
        to tmux. This commit runs when the worker submits a report that
        includes the call_id in ``acked_call_ids`` — proving the worker
        processed the message.

        **Target verification (anti-forgery):**

        TaskMailbox followups are verified Task-first: the call_id must sit
        on the Task outbox, the mailbox call record target must be exactly
        this Task, and the reporting session must be the Task's current
        worker assignment. Session outbox membership is not required for
        that path; Session delivered is updated only when the same call_id
        is also present on the Session.

        Legacy dispatch/internal call_ids (no TaskMailbox record) still
        require Session outbox membership plus mailbox call-record target
        binding. Cross-task and cross-session ACKs fail closed.

        Call_ids in ``pending_call_ids``, ``processing_call_ids``, or
        ``uncertain_call_ids`` are all eligible for ACK (the worker may have
        processed the message even if the Hub's state machine hasn't caught
        up, e.g. a crash between send and persist). Unknown call_ids (not in
        any of the three) are ignored to prevent future-ID poisoning.

        After committing, the call_id's message body is removed from
        ``session.pending_messages`` (the durable inbox) since it is no
        longer needed for re-delivery.
        """
        wake_targets: set[tuple[str, str]] = set()
        if not call_ids:
            return wake_targets
        acked = set(call_ids)

        task = self.tasks.get(task_id)
        session = self.sessions.get(session_id)

        session_call_ids: set[str] = set()
        if session is not None:
            session_call_ids.update(session.pending_call_ids)
            session_call_ids.update(session.processing_call_ids)
            session_call_ids.update(session.uncertain_call_ids)

        verified_acked: list[str] = []
        for call_id in acked:
            mailbox_record = (
                self.task_mailbox._call_record(task.workspace_id, call_id)
                if task is not None
                else None
            )
            mailbox_target = mailbox_record.get("target") if mailbox_record else None
            if mailbox_record is not None and task is not None and mailbox_target == task.id:
                if not self._can_ack_task_mailbox_call(call_id, task, session, mailbox_record):
                    logger.warning(
                        "Rejecting TaskMailbox ACK: call_id=%s task_id=%s session_id=%s",
                        call_id,
                        task_id,
                        session_id,
                    )
                    continue
                verified_acked.append(call_id)
                continue
            if mailbox_target in self.tasks:
                logger.warning(
                    "Rejecting cross-task TaskMailbox ACK: call_id=%s target=%s task_id=%s",
                    call_id,
                    mailbox_target,
                    task_id,
                )
                continue
            if call_id not in session_call_ids:
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
            return wake_targets

        verified_set = set(verified_acked)
        workspace_id = (
            session.workspace_id
            if session is not None
            else task.workspace_id if task is not None else None
        )

        # ---- TaskMailbox followup reconciliation (BEFORE delivered mutation) ----
        #
        # reconcile_followup_outcome and the followup:delivered event append
        # each TaskMailbox persist -> self._save_state(), which writes
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
            wake_target = self._emit_followup_delivered_if_followup(workspace_id, call_id)
            if wake_target is not None:
                wake_targets.add(wake_target)

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
        return wake_targets

    def _can_ack_task_mailbox_call(
        self,
        call_id: str,
        task: Optional[WorkspaceTask],
        session: Optional[ManagedSession],
        record: dict[str, Any],
    ) -> bool:
        """Allow ACK for a TaskMailbox-tracked call_id on the assigned worker."""
        if task is None or session is None:
            return False
        if call_id not in (
            set(task.pending_call_ids)
            | set(task.processing_call_ids)
            | set(task.uncertain_call_ids)
        ):
            return False
        if record.get("target") != task.id:
            return False
        if task.session_id != session.id or session.workspace_id != task.workspace_id:
            return False
        assigned = {session.task_id, session.current_task_id}
        if task.id not in assigned:
            return False
        return True

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

        record = self.task_mailbox._call_record(workspace_id, call_id)
        if record is None:
            # No call record — legacy untracked call (dispatch/internal).
            return True

        target_task_id = record.get("target")
        if not target_task_id or target_task_id not in self.tasks:
            return False

        if task_id and target_task_id != task_id:
            return False
        target_task = self.tasks.get(target_task_id)
        if (
            target_task is not None
            and target_task.session_id
            and target_task.session_id != session_id
        ):
            return False
        return True

    def _emit_followup_delivered_if_followup(
        self, workspace_id: Optional[str], call_id: str
    ) -> tuple[str, str] | None:
        """TaskMailbox followups do not emit separate delivered events."""

        return None

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

    def _task_actor_role_for_session(self, session: ManagedSession) -> TaskActorRole:
        if session.role == WorkspaceSessionRole.REVIEWER:
            return TaskActorRole.REVIEWER
        if session.role == WorkspaceSessionRole.RESIDENT:
            return TaskActorRole.SUPERVISOR
        return TaskActorRole.WORKER

    def _task_event_type_for_report(
        self, state: AgentReportState, task: WorkspaceTask | None = None
    ) -> TaskEventType:
        """Map a report state to a Task event. REVIEWED worker COMPLETED is REPORT.

        Compat AgentEventType.COMPLETED is reserved for REVIEW_PASSED / a
        DIRECT-mode terminal completion. A REVIEWED worker completion waits
        for the reviewer and must project as PROGRESS.
        """
        if (
            state == AgentReportState.COMPLETED
            and task is not None
            and task.task_mode == WorkspaceTaskMode.REVIEWED
        ):
            return TaskEventType.REPORT
        return _REPORT_STATE_TO_TASK_EVENT.get(state, TaskEventType.PROGRESS)

    def _task_actor_for_report(self, report: AgentReport) -> tuple[str | None, TaskActorRole]:
        session = self.sessions.get(report.session_id)
        if session is not None and session.workspace_id == report.workspace_id:
            return session.id, self._task_actor_role_for_session(session)
        review_states = {
            AgentReportState.REVIEW_STARTED,
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }
        if report.state in review_states:
            return report.session_id or None, TaskActorRole.REVIEWER
        return report.session_id or None, TaskActorRole.WORKER

    def _canonical_report_event_payload(
        self,
        report: AgentReport,
        task: WorkspaceTask,
        *,
        actor_session_id: str | None,
        actor_role: TaskActorRole,
    ) -> dict[str, object]:
        return {
            "message": report.message,
            "report_id": report.id,
            "report_state": report.state.value,
            "task_id": task.id,
            "actor_role": actor_role.value,
            "actor_session_id": actor_session_id,
            "review_cycle": report.review_cycle,
        }

    def _canonical_report_bridge_payload(
        self, report: AgentReport, session: ManagedSession, task: WorkspaceTask
    ) -> dict[str, object]:
        """Authoritative TaskMailbox payload for a persisted report rewrite."""

        actor_role = self._task_actor_role_for_session(session)
        return self._canonical_report_event_payload(
            report, task, actor_session_id=session.id, actor_role=actor_role
        )

    def _bridge_report_to_agent_event(
        self,
        report: AgentReport,
        session: ManagedSession,
    ) -> tuple[str, str] | None:
        """Append a TaskMailbox event for a persisted report.

        All task-linked reports write a TaskEvent. Run-tree status, cursor, and
        context are not written here. Persist is deferred to the outer
        report-intake ``_save_state``.
        """
        if not report.task_id:
            return None
        task = self.tasks.get(report.task_id)
        if task is None or task.workspace_id != report.workspace_id:
            return None

        actor_role = self._task_actor_role_for_session(session)
        event_type = self._task_event_type_for_report(report.state, task)
        call_id = f"report:{report.id}"
        self.task_mailbox.append_event(
            workspace_id=report.workspace_id,
            task_id=task.id,
            actor_role=actor_role,
            event_type=event_type,
            call_id=call_id,
            action="report",
            consumer_key=task_supervisor_consumer_key(task),
            actor_session_id=session.id,
            review_cycle=report.review_cycle,
            payload=self._canonical_report_bridge_payload(report, session, task),
            report_id=report.id,
            persist=False,
        )

        return task_supervisor_consumer_key(task), task_supervisor_consumer_key(task)

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
        if task.task_mode == WorkspaceTaskMode.SUBAGENT:
            # Subagent mode: the calling agent judges results. Never route to
            # AI review regardless of review_decision. On completion, mark
            # review-skipped so the task waits for the caller's acceptance.
            if report.state in {
                AgentReportState.COMPLETED,
                AgentReportState.READY_FOR_REVIEW,
            }:
                self._mark_task_review_skipped(task, report)
            return
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
        # A retry of the same durable report must reopen the same round, not
        # advance it again. The report carries the cycle at which the logical
        # supplement was requested, so report.review_cycle + 1 is the stable
        # target even when a post-send failure replays this method.
        next_cycle = max(task.review_cycle, report.review_cycle + 1)
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
        supplement_call_id = self._report_prompt_call_id(
            task.id,
            "goal-packet-supplement",
            attempt=report.id,
            cycle=next_cycle,
        )
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
            f'"call_id":"{supplement_call_id}",'
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
