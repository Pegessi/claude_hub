"""Workspace task and attachment schemas."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from claude_hub.models.schema_domains.evaluation import (
    AutonomousRun,
    AutonomyPolicy,
    GoalPacket,
)
from claude_hub.models.schema_domains.execution import (
    TaskExecutionControl,
    TaskExecutionReference,
    TaskProgressSnapshot,
    TaskProgressState,
    TaskRuntimeObservation,
    TaskSourceReference,
    validate_workspace_session_surface,
)
from claude_hub.models.schema_domains.lifecycle import TaskLifecycleMark
from claude_hub.models.schema_domains.runtime import (
    AgentType,
    ReviewProfile,
    SessionKind,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskOrigin,
    WorkspaceTaskStatus,
    normalize_agent_tag,
)
from claude_hub.models.task_mailbox import TaskEvent


class WorkspaceTaskCreate(BaseModel):
    """Payload for creating a workspace task."""

    model_config = {"extra": "forbid"}

    request_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    execution_control: TaskExecutionControl = TaskExecutionControl.WORKSPACE
    source: Optional[TaskSourceReference] = None
    execution_ref: Optional[TaskExecutionReference] = None
    reporter_key: Optional[str] = Field(
        default=None, min_length=32, max_length=256, exclude=True, repr=False
    )
    title: str
    prompt: str
    agent_type: AgentType = AgentType.CODEX
    session_kind: SessionKind = SessionKind.TERMINAL
    task_mode: WorkspaceTaskMode = WorkspaceTaskMode.REVIEWED
    execution_complexity: WorkspaceTaskExecutionComplexity = WorkspaceTaskExecutionComplexity.AUTO
    origin: WorkspaceTaskOrigin = WorkspaceTaskOrigin.HUMAN
    related_task_id: Optional[str] = None
    attachments: List["WorkspaceAttachmentCreate"] = Field(default_factory=list)
    goal_packet: Optional[GoalPacket] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    autonomy_policy: Optional[AutonomyPolicy] = None
    session_id: Optional[str] = None
    clear_context: Optional[bool] = None
    timeout_seconds: Optional[int] = None
    # Task Graph parent. Independent of related_task_id (session-affinity).
    parent_task_id: Optional[str] = None
    # Explicit execution prerequisites, separate from supervision/session affinity.
    depends_on_task_ids: List[str] = Field(default_factory=list)
    agent_tag: Optional[str] = None

    @field_validator("request_key")
    @classmethod
    def _nonblank_request_key(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("request_key must not be blank")
        return value

    @field_validator("agent_tag", mode="before")
    @classmethod
    def _validate_agent_tag_create(cls, value: Any) -> Optional[str]:
        return normalize_agent_tag(value, allow_clear=False)

    @model_validator(mode="after")
    def _validate_session_surface(self) -> "WorkspaceTaskCreate":
        validate_workspace_session_surface(self.agent_type, self.session_kind)
        return self


class WorkspaceAttachmentCreate(BaseModel):
    """Browser-provided task attachment data."""

    filename: str
    mime_type: str
    data_url: str


class WorkspaceAttachment(BaseModel):
    """Persisted workspace task attachment."""

    id: str
    filename: str
    mime_type: str
    path: str
    size_bytes: int


class WorkspaceTaskUpdate(BaseModel):
    """Payload for updating a workspace task."""

    model_config = {"extra": "forbid"}

    title: Optional[str] = None
    prompt: Optional[str] = None
    status: Optional[WorkspaceTaskStatus] = None
    goal_packet: Optional[GoalPacket] = None
    task_mode: Optional[WorkspaceTaskMode] = None
    session_kind: Optional[SessionKind] = None
    execution_complexity: Optional[WorkspaceTaskExecutionComplexity] = None
    review_profiles: Optional[List[ReviewProfile]] = None
    autonomy_policy: Optional[AutonomyPolicy] = None
    autonomous_run: Optional[AutonomousRun] = None
    # Todo-task edit fields
    add_attachments: Optional[List["WorkspaceAttachmentCreate"]] = None
    removed_attachment_ids: Optional[List[str]] = None
    related_task_id: Optional[str] = None
    clear_context: Optional[bool] = None
    session_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    # Explicit execution prerequisites, separate from supervision/session affinity.
    depends_on_task_ids: Optional[List[str]] = None
    agent_tag: Optional[str] = None

    @field_validator("agent_tag", mode="before")
    @classmethod
    def _validate_agent_tag_update(cls, value: Any) -> Optional[str]:
        return normalize_agent_tag(value, allow_clear=True)


class WorkspaceTask(BaseModel):
    """Task tracked by Agent Workspace mode."""

    model_config = {"extra": "forbid"}

    creation_request_key: Optional[str] = Field(default=None, exclude=True)
    creation_actor_key: Optional[str] = Field(default=None, exclude=True)
    creation_fingerprint: Optional[str] = Field(default=None, exclude=True)
    execution_control: TaskExecutionControl = TaskExecutionControl.WORKSPACE
    source: Optional[TaskSourceReference] = None
    execution_ref: Optional[TaskExecutionReference] = None
    execution_epoch: int = Field(default=1, ge=1)
    progress_revision: int = Field(default=0, ge=0)
    execution_released: bool = False
    latest_progress: Optional[TaskProgressSnapshot] = None
    runtime_observation: Optional[TaskRuntimeObservation] = None
    # Last observed lifecycle phase and its notification state (Hub-maintained).
    lifecycle_mark: Optional[TaskLifecycleMark] = None
    # Explicitly included by the state codec, never by public model serialization.
    reporter_key_hash: Optional[str] = Field(default=None, exclude=True, repr=False)
    execution_call_fingerprints: Dict[str, str] = Field(
        default_factory=dict, exclude=True, repr=False
    )
    id: str
    workspace_id: str
    title: str
    prompt: str
    attachments: List[WorkspaceAttachment] = Field(default_factory=list)
    goal_packet: Optional[GoalPacket] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    agent_type: AgentType
    session_kind: SessionKind = SessionKind.TERMINAL
    task_mode: WorkspaceTaskMode = WorkspaceTaskMode.REVIEWED
    execution_complexity: WorkspaceTaskExecutionComplexity = WorkspaceTaskExecutionComplexity.AUTO
    origin: WorkspaceTaskOrigin = WorkspaceTaskOrigin.HUMAN
    agent_tag: Optional[str] = None
    autonomy_policy: Optional[AutonomyPolicy] = None
    autonomous_run: Optional[AutonomousRun] = None
    status: WorkspaceTaskStatus
    session_id: Optional[str] = None
    related_task_id: Optional[str] = None
    clear_context: Optional[bool] = None
    # Task Graph: parent/root/path. related_task_id stays a dispatch hint.
    parent_task_id: Optional[str] = None
    # Explicit execution prerequisites, separate from supervision/session affinity.
    depends_on_task_ids: List[str] = Field(default_factory=list)
    root_task_id: Optional[str] = None
    path: str = ""
    # Consumer cursor for this Task when it waits on its subtree. Not a Session field.
    consumer_ack_sequence: int = 0
    # Call ids of followup messages already ACKed (processed) by the worker.
    # Used for sender-side dedup: a followup with a call_id already in this
    # list is a no-op. Persisted with the task so delivery survives restarts.
    # NOTE: delivered_call_ids == "processed by the worker", not "sent by the
    # Hub". A call_id stays in pending_call_ids until the worker ACKs it.
    delivered_call_ids: List[str] = Field(default_factory=list)
    # Call ids persisted in the outbox (sender side) whose message sits in the
    # session's pending_messages inbox awaiting pump delivery. The sender only
    # ever appends here; it never sends to tmux directly. On restart, every
    # call_id here is re-deliverable.
    pending_call_ids: List[str] = Field(default_factory=list)
    # Call ids the receiver pump has moved to ``in-flight``: the intent to
    # deliver was persisted (call_id moved out of pending_call_ids) and the
    # pump is about to send / has sent the message to tmux, but the worker
    # has not yet ACKed it. The Hub CANNOT prove the message reached the tmux
    # input buffer (tmux stdin is not a durable, verifiable receiver), so a
    # call_id here is "maybe delivered, not yet confirmed". On cold restart
    # these move to ``uncertain_call_ids`` (fail-closed: not re-sent, not
    # silently marked delivered).
    processing_call_ids: List[str] = Field(default_factory=list)
    # Call ids whose delivery state is **uncertain**: they were in
    # ``processing_call_ids`` (in-flight) when the Hub crashed or the tmux
    # session disappeared. We cannot prove the message was NOT delivered to
    # the worker's tmux input buffer, so we fail closed: do NOT auto-resend
    # (could duplicate) and do NOT silently mark delivered (could lose). The
    # call_id stays here until either (a) the worker ACKs it (moves to
    # delivered), or (b) an explicit human/operator retry moves it back to
    # pending_call_ids for re-delivery. A ``delivery:uncertain`` event is
    # emitted to the supervisor so the condition is visible.
    uncertain_call_ids: List[str] = Field(default_factory=list)
    dispatch_reason: Optional[str] = None
    dispatch_pending: bool = False
    system_internal: bool = False
    internal_kind: Optional[str] = None
    feedback_lesson_ids: List[str] = Field(default_factory=list)
    review_session_id: Optional[str] = None
    review_attempts: int = 0
    # Monotonic counter incremented each time the task is (re-)dispatched to a
    # worker via start_task. Used to build a per-attempt dispatch call_id
    # (``dispatch:{task.id}:{dispatch_attempt}``) so that followups — which
    # may carry a different assignment prompt — do not collide with the
    # immutable-call_id payload invariant. Crash recovery reuses the stored
    # attempt, so the same call_id no-ops correctly.
    dispatch_attempt: int = 0
    # Review-cycle ordinals. ``review_cycle`` is the current work round (a task is
    # born in round 1; each reopen-to-worker opens the next round).
    # ``reviewed_cycle`` is the round number of the most recently applied reviewer
    # verdict (0 = none judged yet). A gate report opens a new review when its
    # stamped cycle exceeds ``reviewed_cycle``; the current round is already
    # judged when ``reviewed_cycle >= review_cycle``.
    review_cycle: int = 1
    reviewed_cycle: int = 0
    review_requested_at: Optional[datetime] = None
    # One delivery reminder per exact review dispatch, durable across restarts.
    review_delivery_round: Optional[str] = None
    review_delivery_message: Optional[str] = None
    review_delivery_nudged_at: Optional[datetime] = None
    review_delivery_escalated_at: Optional[datetime] = None
    review_completed_at: Optional[datetime] = None
    review_skipped_at: Optional[datetime] = None
    review_skip_reason: Optional[str] = None
    manual_aborted_at: Optional[datetime] = None
    manual_abort_reason: Optional[str] = None
    # Failure tracking. ``timeout_seconds`` bounds how long a task may stay in
    # WORKING without a fresh report before the monitor marks it FAILED.
    # ``failure_reason`` and ``failed_at`` are set when the task transitions to
    # FAILED (worker-reported failure, session death, or timeout).
    timeout_seconds: Optional[int] = None
    failure_reason: Optional[str] = None
    failed_at: Optional[datetime] = None
    human_acceptance_requested_at: Optional[datetime] = None
    human_accepted_at: Optional[datetime] = None
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class TaskExecutionMutationResult(BaseModel):
    task: WorkspaceTask
    event: TaskEvent
    replayed: bool


class TaskExecutionCapabilities(BaseModel):
    supported_execution_controls: List[str] = ["workspace", "initiator"]
    progress_states: List[str] = [state.value for state in TaskProgressState]
    record_only_requires_reporter_key: bool = True
    handoff_requires_release: bool = True
