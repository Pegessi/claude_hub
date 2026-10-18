"""Workspace task and session request schemas."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from claude_hub.models.schema_domains.execution import (
    validate_workspace_session_surface,
)
from claude_hub.models.schema_domains.runtime import (
    CHAT_REMOTE_UNSUPPORTED,
    AgentType,
    ExecutionTarget,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.models.schema_domains.tasks import WorkspaceAttachmentCreate


class SpawnWorkerRequest(BaseModel):
    """Payload for spawning a worker session for a task."""

    agent_type: Optional[AgentType] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_chat_surface_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and ({"session_kind", "chat_mode"} & value.keys()):
            raise ValueError("Workspace managed sessions always use Terminal")
        return value


class EnsureWorkspaceAgentRequest(BaseModel):
    """Payload for ensuring a resident workspace agent session."""

    agent_type: AgentType = AgentType.CODEX
    session_kind: SessionKind = SessionKind.TERMINAL
    title: Optional[str] = None
    role: WorkspaceSessionRole = WorkspaceSessionRole.ORCHESTRATOR
    reuse_existing: bool = Field(
        default=False,
        description=(
            "Best-effort reuse of a compatible idle session already visible at request time. "
            "This is advisory and does not collapse overlapping create requests."
        ),
    )
    cwd: Optional[str] = None
    solo_mode: bool = True
    target: Optional[ExecutionTarget] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None
    ephemeral: bool = False
    caller_owned_ephemeral: bool = Field(
        default=False,
        description=(
            "Explicit CLI provenance: True only when the caller owns a task-scoped "
            "ephemeral session (--ephemeral on claude-hub agent create). Internal "
            "review/dispatch/resident paths must leave this False."
        ),
    )
    env: Dict[str, str] = Field(default_factory=dict)
    env_preset: Optional[str] = None
    agent_session_id: Optional[str] = Field(
        default=None,
        description=("Existing Codex conversation id to resume for a new local managed session."),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_managed_chat_mode(cls, value: Any) -> Any:
        if isinstance(value, dict) and "chat_mode" in value:
            raise ValueError("Managed Chat mode is controlled by task execution")
        return value

    @model_validator(mode="after")
    def _validate_managed_session_admission(self) -> "EnsureWorkspaceAgentRequest":
        validate_workspace_session_surface(self.agent_type, self.session_kind)
        if self.session_kind == SessionKind.CHAT:
            if (
                self.target == ExecutionTarget.REMOTE
                or self.remote_profile_id is not None
                or self.remote_cwd is not None
                or self.remote_reconnect is not None
            ):
                raise ValueError(CHAT_REMOTE_UNSUPPORTED)
        if self.caller_owned_ephemeral and not self.ephemeral:
            raise ValueError("caller_owned_ephemeral requires ephemeral=True")
        if self.agent_session_id is not None:
            if not self.agent_session_id.strip():
                raise ValueError("agent_session_id must not be empty")
            if self.agent_type != AgentType.CODEX:
                raise ValueError("agent_session_id is supported only for Codex agents")
            if (
                self.target == ExecutionTarget.REMOTE
                or self.remote_profile_id is not None
                or self.remote_cwd is not None
                or self.remote_reconnect is not None
            ):
                raise ValueError("agent_session_id is supported only for local Codex agents")
            if self.reuse_existing:
                raise ValueError("agent_session_id cannot be combined with reuse_existing")
        return self


class TaskCleanupResult(BaseModel):
    """Structured result for safe task-scoped session cleanup."""

    task_id: str
    session_id: Optional[str] = None
    action: str
    reason: Optional[str] = None


class StartTaskRequest(BaseModel):
    """Payload for queueing a task and optionally overriding dispatch."""

    agent_type: Optional[AgentType] = None
    target_session_id: Optional[str] = None
    clear_context: Optional[bool] = None
    related_task_id: Optional[str] = None


class ContinueTaskRequest(BaseModel):
    """Payload for continuing a task from review with its original agent."""

    message: Optional[str] = None
    attachments: List[WorkspaceAttachmentCreate] = Field(default_factory=list)


class TaskFollowupRequest(BaseModel):
    """Task-first follow-up. ``call_id`` is optional; the API mints one if omitted."""

    message: str = Field(..., min_length=1)
    call_id: Optional[str] = Field(default=None, min_length=1)


class TaskMailboxAckRequest(BaseModel):
    """Advance a Task-owned mailbox cursor (``consumer_ack_sequence``)."""

    sequence: int = Field(..., ge=0)


class RequestTaskReviewRequest(BaseModel):
    """Payload for manually requesting reviewer checks."""

    message: Optional[str] = None


class ManualTaskControlRequest(BaseModel):
    """Payload for exceptional manual task state control."""

    reason: str
    call_id: Optional[str] = None


class DispatchDecisionRequest(BaseModel):
    """Structured dispatch decision produced by a dispatcher agent."""

    target_session_id: str
    clear_context: bool = False
    reason: Optional[str] = None


class SendSessionMessageRequest(BaseModel):
    """Payload for sending a message to a managed session."""

    message: str
    call_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    attachments: List[WorkspaceAttachmentCreate] = Field(default_factory=list)

    @field_validator("call_id")
    @classmethod
    def _nonblank_call_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("call_id must not be blank")
        return value


class RetryUncertainDeliveryRequest(BaseModel):
    """Payload for retrying an uncertain delivery.

    An operator explicitly requests that a call_id currently in
    ``uncertain_call_ids`` be moved back to ``pending_call_ids`` so the
    normal pump path can re-deliver it. The original payload is preserved.

    The actor identity is NOT client-supplied; it is derived from the
    authenticated ``current_user`` so the audit trail cannot be forged.
    """

    call_id: str
    reason: str
