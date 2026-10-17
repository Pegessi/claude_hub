"""Report processing split from the compatibility mixin."""

import hashlib
import json
from abc import abstractmethod
from collections.abc import Mapping

from ...request_fingerprint import request_fingerprint
from .._base import _WorkspaceManagerBase
from .._constants import *  # noqa: F401,F403


class ReportCallIdConflict(ValueError):
    """Raised when a call ID is reused with a different payload fingerprint."""


class _ReportIdentityMixin(_WorkspaceManagerBase):
    @abstractmethod
    def _ack_call_ids(
        self, task_id: Optional[str], session_id: str, call_ids: list[str]
    ) -> set[tuple[str, str]]: ...

    def _snapshot_report_intake_workspace(self, workspace_id: str) -> dict[str, Any]:
        """Snapshot the domains mutated by report and Task transactions."""
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
            "mailbox": self.task_mailbox.snapshot_workspace(workspace_id),
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
        self.task_mailbox.restore_workspace(workspace_id, snapshot["mailbox"])

    def _wake_report_intake_runs(self, wake_targets: set[tuple[str, str]]) -> None:
        """Wake TaskMailbox consumers after the outer commit succeeds."""

        for consumer_key, _author in wake_targets:
            if consumer_key.startswith("task:"):
                self.task_mailbox.wake_consumer(consumer_key)

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
        task = self._require_workspace_execution(task)
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
        self._save_workspace_state(session.workspace_id)
        self._report_intake_committed.add(
            self._report_intake_commit_token(session.workspace_id, session.id, existing.call_id)
        )
        self._wake_report_intake_runs(retry_wake_targets)
        return existing
