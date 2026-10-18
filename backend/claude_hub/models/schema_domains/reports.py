"""Agent report and workspace artifact schemas."""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from claude_hub.models.schema_domains.evaluation import (
    AcceptanceCheck,
    EvaluationReport,
    GoalPacket,
    ReviewProfileResult,
)
from claude_hub.models.schema_domains.runtime import (
    AgentReportState,
    ReviewDecision,
    ReviewProfile,
)


class AgentReportCreate(BaseModel):
    """Payload for appending a worker progress report."""

    state: AgentReportState
    message: str
    message_en: Optional[str] = None
    message_zh: Optional[str] = None
    task_id: Optional[str] = None
    # Idempotency key for report intake. When provided, a second report with
    # the same call_id (and identical payload fingerprint) returns the
    # previously-persisted report instead of creating a duplicate. This makes
    # report/result intake safe under error-after-commit retries: if the Hub
    # durably persists the report but fails before returning the response
    # (e.g. a late exception in _after_report_recorded or the TaskMailbox
    # bridge), the client's retry with the same call_id is a no-op that
    # returns the existing report, task transition, and bridged event.
    call_id: Optional[str] = None
    changed_files: List[str] = Field(default_factory=list)
    validation: Optional[str] = None
    risks: Optional[str] = None
    acceptance_check: List[AcceptanceCheck] = Field(default_factory=list)
    goal_packet: Optional[GoalPacket] = None
    evaluation_report: Optional[EvaluationReport] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    profile_results: List[ReviewProfileResult] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    requires_human_judgment: bool = False
    review_decision: ReviewDecision = ReviewDecision.AUTO
    review_reason: Optional[str] = None
    risk_level: Optional[str] = None
    # Call-specific ACK: the call_ids this report acknowledges as **processed**
    # by the worker. Only call_ids currently in the task/session
    # pending_call_ids or processing_call_ids are moved to delivered_call_ids.
    # Unknown or future call_ids (not in pending or processing) are silently
    # ignored — this prevents a malicious or buggy report from poisoning the
    # delivered set and suppressing a real future delivery.
    #
    # Production contract:
    # - The dispatch call_id (f"dispatch:{task_id}:{dispatch_attempt}") is
    #   ACKed automatically by the Hub on every report submission; the worker
    #   does NOT need to list it. (Legacy dispatch:{task_id} ACKs are still
    #   accepted for backward compatibility.)
    # - For any other call_id the worker has processed (e.g. a followup
    #   message carrying [call_id:<id>]), the worker MUST include it in
    #   acked_call_ids so the Hub can move it to delivered_call_ids and
    #   release it. The worker ACK is the durable commit to delivered.
    # - A call_id not listed in acked_call_ids stays in processing_call_ids.
    #   The Hub does NOT guarantee exactly-once delivery: a processing
    #   call_id is not re-sent to a LIVE tmux session (at-most-once paste
    #   per call_id per tmux session lifetime, enforced by the tmux
    #   @receipt_<sha16(call_id)> session option). On cold restart the
    #   monitor reconciles processing call_ids against the tmux receipt:
    #     * receipt present on a LIVE session -> keep processing (no repaste).
    #     * receipt absent on a LIVE session -> move back to pending for one
    #       re-delivery (the paste definitely did not happen).
    #     * session STOPPED/gone/unqueryable -> move to uncertain (fail
    #       closed; explicit operator retry required via
    #       retry_uncertain_delivery).
    acked_call_ids: List[str] = Field(default_factory=list)


class AgentReport(BaseModel):
    """Progress report recorded against a managed session."""

    id: str
    workspace_id: str
    task_id: Optional[str] = None
    session_id: str
    # Idempotency key for report intake. Mirrors AgentReportCreate.call_id.
    # When set, a retry with the same call_id returns this report instead of
    # creating a duplicate. See AgentReportCreate.call_id for the contract.
    call_id: Optional[str] = None
    state: AgentReportState
    message: str
    message_en: Optional[str] = None
    message_zh: Optional[str] = None
    changed_files: List[str] = Field(default_factory=list)
    validation: Optional[str] = None
    risks: Optional[str] = None
    acceptance_check: List[AcceptanceCheck] = Field(default_factory=list)
    evaluation_report: Optional[EvaluationReport] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    profile_results: List[ReviewProfileResult] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    requires_human_judgment: bool = False
    review_decision: ReviewDecision = ReviewDecision.AUTO
    review_reason: Optional[str] = None
    risk_level: Optional[str] = None
    # Call-specific ACK: the call_ids this report acknowledges as processed
    # by the worker. Mirrors AgentReportCreate.acked_call_ids.
    acked_call_ids: List[str] = Field(default_factory=list)
    # The owning task's ``review_cycle`` at the moment this report was created.
    # Used to rank gate/verdict reports against the task's ``reviewed_cycle``.
    # Defaults to 0 so legacy on-disk reports rank below any post-migration round.
    review_cycle: int = 0
    created_at: datetime


class WorkspaceArtifactPreview(BaseModel):
    """Previewable Markdown artifact content from a workspace report."""

    path: str
    filename: str
    content: str
    size_bytes: int
    truncated: bool = False


class WorkspaceMarkdownDocumentSource(str, Enum):
    """Where a discoverable Markdown document came from."""

    ARTIFACT = "artifact"
    CHANGED_FILE = "changed_file"
    SNAPSHOT = "snapshot"
    DISCOVERED = "discovered"


class WorkspaceMarkdownDocument(BaseModel):
    """Discoverable local Markdown document for workspace tasks."""

    id: str
    path: str
    label: str
    source: WorkspaceMarkdownDocumentSource
    task_id: Optional[str] = None
    report_id: Optional[str] = None
    session_id: Optional[str] = None
    size_bytes: Optional[int] = None
    updated_at: Optional[datetime] = None
