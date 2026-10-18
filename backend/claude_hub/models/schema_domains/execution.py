"""Task execution protocol schemas."""

from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from claude_hub.models.schema_domains.runtime import AgentType, SessionKind


class TaskExecutionControl(str, Enum):
    WORKSPACE = "workspace"
    INITIATOR = "initiator"


class TaskProgressState(str, Enum):
    STARTED = "started"
    WORKING = "working"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    COMPLETED = "completed"
    FAILED = "failed"
    RELEASED = "released"


class TaskSourceReference(BaseModel):
    model_config = {"extra": "forbid"}

    kind: Literal["human", "chat", "agent"]
    tab_id: Optional[str] = Field(default=None, max_length=256)
    agent_id: Optional[str] = Field(default=None, max_length=256)


class TaskExecutionReference(BaseModel):
    model_config = {"extra": "forbid"}

    provider: Optional[str] = Field(default=None, max_length=64)
    session_id: Optional[str] = Field(default=None, max_length=256)
    thread_id: Optional[str] = Field(default=None, max_length=256)
    turn_id: Optional[str] = Field(default=None, max_length=256)
    run_epoch: Optional[int] = Field(default=None, ge=0, strict=True)


class TaskProgressSnapshot(BaseModel):
    state: TaskProgressState
    summary: str
    validation: Optional[str] = None
    risks: Optional[str] = None
    artifact_refs: List[str] = Field(default_factory=list)
    call_id: str
    execution_epoch: int
    reported_at: datetime


class TaskRuntimeObservation(BaseModel):
    model_config = {"extra": "forbid"}

    execution_epoch: int = Field(ge=1, strict=True)
    stream_sequence: int = Field(ge=0, strict=True)
    status: Literal["active", "idle", "error", "disconnected", "unknown"]
    observed_at: datetime
    turn_id: Optional[str] = Field(default=None, max_length=256)
    detail: Optional[str] = Field(default=None, max_length=512)


class TaskProgressRequest(BaseModel):
    model_config = {"extra": "forbid"}

    call_id: str = Field(min_length=1, max_length=128)
    expected_execution_epoch: int = Field(ge=1, strict=True)
    expected_progress_revision: int = Field(ge=0, strict=True)
    state: TaskProgressState
    summary: str = Field(min_length=1, max_length=2000)
    validation: Optional[str] = Field(default=None, max_length=4000)
    risks: Optional[str] = Field(default=None, max_length=2000)
    artifact_refs: List[str] = Field(default_factory=list, max_length=32)
    execution_ref: Optional[TaskExecutionReference] = None

    @field_validator("call_id", "summary")
    @classmethod
    def _nonblank_progress_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("progress text must not be blank")
        return value

    @field_validator("artifact_refs")
    @classmethod
    def _bounded_artifact_refs(cls, values: List[str]) -> List[str]:
        if any(not value.strip() or len(value) > 2048 for value in values):
            raise ValueError("invalid artifact reference")
        return values


class TaskManualProgressRequest(TaskProgressRequest):
    @model_validator(mode="after")
    def _manual_progress_has_no_execution_authority(
        self,
    ) -> "TaskManualProgressRequest":
        if self.state == TaskProgressState.RELEASED or "execution_ref" in self.model_fields_set:
            raise ValueError("manual progress cannot release or bind an executor")
        return self


class TaskExecutionHandoffRequest(BaseModel):
    model_config = {"extra": "forbid"}

    call_id: str = Field(min_length=1, max_length=128)
    expected_execution_epoch: int = Field(ge=1, strict=True)
    expected_progress_revision: int = Field(ge=0, strict=True)
    execution_control: TaskExecutionControl
    new_reporter_key: Optional[str] = Field(
        default=None, min_length=32, max_length=256, exclude=True, repr=False
    )
    execution_ref: Optional[TaskExecutionReference] = None

    @field_validator("call_id")
    @classmethod
    def _nonblank_handoff_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("call_id must not be blank")
        return value


def validate_workspace_session_surface(agent_type: AgentType, session_kind: SessionKind) -> None:
    """Keep task creation, start overrides, and managed admission on the same surface rules."""
    if agent_type == AgentType.TRAEX:
        raise ValueError("TraeX is not supported for workspace agents")
    if agent_type == AgentType.MASO and session_kind != SessionKind.CHAT:
        raise ValueError("MASO is an HTTP Chat provider; select a Chat session")
    if session_kind == SessionKind.CHAT and agent_type not in {
        AgentType.CLAUDE,
        AgentType.CODEX,
        AgentType.CURSOR,
        AgentType.MASO,
    }:
        raise ValueError("Managed Chat supports only local Claude, Codex, Cursor, or MASO agents")
