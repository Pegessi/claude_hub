"""Managed-session schemas and public redaction helpers."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from claude_hub.models.schema_domains.runtime import (
    AgentRuntimeStatus,
    AgentType,
    ChatMode,
    ExecutionTarget,
    ManagedSessionStatus,
    SessionKind,
    StreamCapabilities,
    WorkspaceSessionRole,
)
from claude_hub.models.schema_domains.tasks import WorkspaceAttachment


class ManagedSession(BaseModel):
    """Managed agent session backed by a terminal tab."""

    id: str
    workspace_id: str
    task_id: Optional[str] = None
    tab_id: str
    role: WorkspaceSessionRole
    agent_type: AgentType
    session_kind: SessionKind = SessionKind.TERMINAL
    chat_mode: ChatMode = ChatMode.DEFAULT
    archived: bool = False
    archived_at: Optional[datetime] = None
    # A successful archive may retain delivery evidence requiring proof that
    # its provider was stopped. Valid only while this incarnation is archived.
    archive_provider_fenced: bool = False
    chat_paused: bool = False
    status: ManagedSessionStatus
    runtime_status: AgentRuntimeStatus = AgentRuntimeStatus.IDLE
    current_task_id: Optional[str] = None
    queued_count: int = 0
    title: str
    branch: Optional[str] = None
    workspace_path: str
    tmux_session: str
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    solo_mode: bool = True
    ephemeral: bool = False
    caller_owned_ephemeral: bool = False
    env: Dict[str, str] = Field(default_factory=dict)
    # Non-secret identity of the env preset the session was launched with (the
    # raw name/id passed to EnsureWorkspaceAgentRequest.env_preset). The merged
    # KEY=VALUE pairs live in ``env`` (redacted on public surfaces); this only
    # records *which preset* was used so that follow-on sessions — notably
    # auto-created reviewers — can inherit the same preset instead of launching
    # on a default/broken env. None means no preset was requested. Not
    # backfilled for sessions created before this field existed (loads as None).
    env_preset: Optional[str] = None
    remote_forward_port: Optional[int] = None
    auto_continue_task_id: Optional[str] = None
    auto_continue_attempts: int = 0
    last_auto_continue_at: Optional[datetime] = None
    # Hard recovery: interrupt + /clear + re-inject prompt after repeated API errors.
    # Tracked per task_id so counter resets on new task dispatch.
    hard_recovery_task_id: Optional[str] = None
    hard_recovery_attempts: int = 0
    last_hard_recovery_at: Optional[datetime] = None
    prompt_retry_task_id: Optional[str] = None
    prompt_retry_attempted_at: Optional[datetime] = None
    # For reviewer sessions: the id of the task this session was last dispatched
    # a review prompt for. Drives the cross-task /clear decision independently of
    # any task's mutable review_session_id (which abort/skip/stale-release null).
    last_review_task_id: Optional[str] = None
    # Executor-boundary call_id tracking for at-least-once / fail-closed
    # delivery to the tmux inbox. The Hub does NOT own a verifiable durable
    # receiver: tmux stdin can accept bytes that are then lost if the agent
    # process dies, and the Hub cannot read back what the model consumed.
    # Therefore we cannot guarantee exactly-once; we guarantee fail-closed:
    # a message is either pending, in-flight (maybe sent), uncertain (crash
    # while in-flight), or delivered (worker ACKed).
    #
    # Lifecycle (see _messaging.py and _reports.py):
    #
    #   pending_call_ids ──persist intent──▶ processing_call_ids ──ACK──▶ delivered_call_ids
    #         │                                      │
    #         │         send failed                  │  crash / tmux gone
    #         └──────────────────────────────────────┤
    #                                                ▼
    #                                       uncertain_call_ids
    #                                       (fail-closed: not re-sent,
    #                                        not silently delivered;
    #                                        emit delivery:uncertain)
    #
    # pending_call_ids: call_ids persisted in the outbox (sender side) whose
    #   message sits in ``pending_messages`` awaiting delivery. The sender
    #   (``send_session_message``) only ever appends here; it never sends to
    #   tmux directly. On restart, every call_id here is pumped.
    # processing_call_ids: call_ids the pump has moved to in-flight: the
    #   intent was persisted (call_id moved out of pending) and the pump is
    #   sending / has sent the message to tmux, but the worker has not ACKed.
    #   The Hub CANNOT prove the message reached the tmux input buffer, so
    #   this is "maybe delivered". On send failure the call_id rolls back to
    #   pending. On crash while here, it moves to uncertain.
    # uncertain_call_ids: call_ids whose delivery state is unknown (crash
    #   while in-flight, or tmux session gone). Fail-closed: we do NOT
    #   auto-resend (could duplicate) and do NOT silently mark delivered
    #   (could lose). A ``delivery:uncertain`` event is emitted to the
    #   supervisor. Moves to delivered on worker ACK, or back to pending on
    #   explicit operator retry.
    # delivered_call_ids: call_ids the worker has ACKed (processed) via
    #   ``acked_call_ids`` in a report. Only the worker's ACK moves a call_id
    #   here. ``send_session_message`` skips any call_id already here.
    #
    # NOTE: delivered_call_ids == "processed by the worker", not "sent by the
    # Hub". The Hub never moves a call_id to delivered_call_ids on its own;
    # only an ACK from the worker does.
    pending_call_ids: List[str] = Field(default_factory=list)
    processing_call_ids: List[str] = Field(default_factory=list)
    # Call ids whose delivery state is **uncertain**: they were in
    # ``processing_call_ids`` (in-flight) when the Hub crashed or the tmux
    # session disappeared. Fail-closed: not auto-resent (could duplicate),
    # not silently marked delivered (could lose). Moves to delivered on
    # worker ACK, or back to pending on explicit retry.
    uncertain_call_ids: List[str] = Field(default_factory=list)
    delivered_call_ids: List[str] = Field(default_factory=list)
    # Per-call_id claim timestamps. Keyed by call_id, value is the UTC datetime
    # when the receiver pump moved the call_id from pending to processing.
    # Used by ``_expire_processing_leases`` to decide whether a claimed but
    # unACKed call_id should be moved back to pending for re-delivery (only
    # for STOPPED sessions whose tmux inbox is gone).
    processing_call_ids_at: Dict[str, datetime] = Field(default_factory=dict)
    # Durable inbox: call_id → message body. Populated by
    # ``send_session_message``; consumed (and deleted) by the receiver pump
    # after a successful claim. Persisted so a crash between outbox write and
    # pump claim does not lose the message.
    pending_messages: Dict[str, str] = Field(default_factory=dict)
    # Durable inbox attachments: call_id → list of persisted attachments.
    # Populated by ``send_session_message`` when a call_id send carries
    # attachments; consumed (and deleted) by the receiver pump alongside the
    # message body. Kept in lockstep with ``pending_messages`` so a reload
    # preserves both the message and its attachments.
    pending_attachments: Dict[str, list[WorkspaceAttachment]] = Field(default_factory=dict)
    # Durable call payload fingerprints: call_id → sha256 hex digest of the
    # canonical (message, attachments) payload. Computed on send and kept
    # forever (even after ACK) so that:
    #   * same call_id + same payload is idempotent (no-op) at any state
    #     (pending / processing / delivered / uncertain).
    #   * same call_id + different payload is rejected (ValueError) without
    #     mutation — a call_id identifies a single durable delivery.
    # The fingerprint covers the message text and, for each attachment, the
    # filename, normalized mime type, and sha256 digest of the decoded
    # bytes. Same-size-but-different-content attachments are therefore
    # detected as conflicting. Raw bytes are NOT stored in the fingerprint
    # (only the digest), so the JSON state stays small.
    call_payload_fingerprints: Dict[str, str] = Field(default_factory=dict)
    # Exact [turn_id, tool_call_id] JSON key -> answer client call_id. Persisted
    # with the mailbox envelope, not provider completion, so a failed handoff
    # cannot admit a competing answer. Retained for this session's lifetime,
    # like fingerprints; legacy sessions start without claims.
    chat_question_answer_calls: Dict[str, str] = Field(default_factory=dict)
    # Report intake idempotency: call_id → report_id. Populated by
    # ``create_report`` when the payload carries a call_id. Lets a retry
    # after an error-response (report was durably committed but the
    # response failed) return the existing report instead of duplicating.
    # Kept forever (like call_payload_fingerprints) so same call_id + same
    # fingerprint is always idempotent; same call_id + different fingerprint
    # raises ValueError.
    report_call_ids: Dict[str, str] = Field(default_factory=dict)
    # Durable canonical report fingerprints: call_id → sha256 hex digest of
    # the report's content (including the exact Goal Packet). Computed on
    # first report creation and persisted so that:
    #   * a retry with the same call_id compares against the stored
    #     fingerprint directly (no reliance on recomputing from the
    #     persisted report, which could drift if the model changes),
    #   * same call_id + same fingerprint returns the existing report,
    #   * same call_id + different fingerprint raises ReportCallIdConflict.
    # The fingerprint covers every content field (state, message,
    # changed_files, validation, risks, acceptance_check, goal_packet,
    # evaluation_report, review_profiles, profile_results, artifact_refs,
    # confidence, requires_human_judgment, review_decision, review_reason,
    # risk_level, acked_call_ids) and excludes only bookkeeping fields
    # (id, workspace_id, session_id, created_at, review_cycle, call_id).
    report_call_fingerprints: Dict[str, str] = Field(default_factory=dict)
    # None means structured capability has not been negotiated yet.
    stream_capabilities: Optional[StreamCapabilities] = None
    # Persistent provider conversation id.  Its exact use remains provider
    # specific; Codex resume is the currently supported caller-owned path.
    agent_session_id: Optional[str] = None
    # Whether ``agent_session_id`` has been verified by the provider (i.e. the
    # provider emitted it in a system/init record). Only a verified id may be
    # passed to ``--resume``; an unverified (constructive) id must use
    # ``--session-id``. Persisted alongside ``agent_session_id`` so a cold
    # restart does not infer verification merely from the presence of a UUID.
    agent_session_id_verified: bool = False
    cursor_transport: str = "terminal"
    # Cursor CLI transcript provenance. All five must validate exactly for the
    # structured adapter to return structured=True; otherwise fail-closed to
    # the raw terminal.
    cursor_data_dir: Optional[str] = None
    cursor_cli_version: Optional[str] = None
    cursor_transcript_schema: Optional[str] = None
    cursor_transcript_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_activity_at: Optional[datetime] = None


PUBLIC_REDACTED_ENV_VALUE = "[redacted]"


def redact_env_for_public_response(env: Dict[str, str]) -> Dict[str, str]:
    """Return env keys with redacted values for API/CLI/log surfaces."""
    return {key: PUBLIC_REDACTED_ENV_VALUE for key in env}


def redact_managed_session_env(session: ManagedSession) -> ManagedSession:
    """Return a copy safe for API/CLI responses (never leak env secret values)."""
    if not session.env:
        return session
    return session.model_copy(update={"env": redact_env_for_public_response(session.env)})


def redact_managed_sessions_for_public(
    sessions: List[ManagedSession],
) -> List[ManagedSession]:
    """Redact env values on every session in a public list response."""
    return [redact_managed_session_env(session) for session in sessions]


def redact_session_json_payload(data: Any) -> Any:
    """Redact env values on a session-shaped JSON dict (CLI/API escape hatches)."""
    if isinstance(data, dict) and isinstance(data.get("env"), dict):
        redacted = dict(data)
        redacted["env"] = redact_env_for_public_response(data["env"])
        return redacted
    return data


class ManagedSessionPublic(ManagedSession):
    """Bounded API/CLI session response; env values are never literal secrets."""

    @model_validator(mode="after")
    def _redact_env_values(self) -> "ManagedSessionPublic":
        if not self.env:
            return self
        if all(value == PUBLIC_REDACTED_ENV_VALUE for value in self.env.values()):
            return self
        return self.model_copy(update={"env": redact_env_for_public_response(self.env)})

    @classmethod
    def from_managed_session(cls, session: ManagedSession) -> "ManagedSessionPublic":
        """Build a public response from an internal managed session."""
        return cls.model_validate(redact_managed_session_env(session).model_dump(mode="json"))
