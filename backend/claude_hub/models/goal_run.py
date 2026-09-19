"""Persistent contracts for Hub-managed Chat Goals."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

GOAL_OBJECTIVE_MAX_LENGTH = 4000
GOAL_CHECKPOINT_HISTORY_LIMIT = 10
GOAL_RECENT_TURN_IDS_LIMIT = 100


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GoalRunStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_GOAL_STATUSES = frozenset(
    {GoalRunStatus.COMPLETE, GoalRunStatus.CANCELLED, GoalRunStatus.FAILED}
)


class GoalUsageQuality(str, Enum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class GoalDispatchState(str, Enum):
    IDLE = "idle"
    PENDING = "pending"
    DISPATCHED = "dispatched"
    UNCERTAIN = "uncertain"


class GoalVerifiedProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=1000)


class GoalCheckpoint(BaseModel):
    """Bounded agent-authored working memory; never replaces the objective."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=128)
    verified_progress: list[GoalVerifiedProgress] = Field(default_factory=list, max_length=20)
    decisions: list[str] = Field(default_factory=list, max_length=20)
    remaining: list[str] = Field(default_factory=list, max_length=30)
    blocker: str | None = Field(default=None, max_length=1000)
    next_step: str | None = Field(default=None, max_length=1000)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("decisions", "remaining")
    @classmethod
    def validate_bounded_items(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("checkpoint items must not be blank")
            if len(item) > 1000:
                raise ValueError("checkpoint items must be at most 1000 characters")
            normalized.append(item)
        return normalized


class GoalRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tab_id: str
    objective: str = Field(min_length=1, max_length=GOAL_OBJECTIVE_MAX_LENGTH)
    status: GoalRunStatus = GoalRunStatus.ACTIVE
    token_usage: int | None = Field(default=None, ge=0)
    usage_quality: GoalUsageQuality = GoalUsageQuality.UNAVAILABLE
    turns_completed: int = Field(default=0, ge=0)
    dispatch_state: GoalDispatchState = GoalDispatchState.IDLE
    pending_step_id: str | None = None
    current_turn_id: str | None = None
    completed_turn_ids: list[str] = Field(default_factory=list)
    checkpoint: GoalCheckpoint | None = None
    checkpoint_history: list[GoalCheckpoint] = Field(default_factory=list)
    checkpoint_warning: str | None = None
    status_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    paused_at: datetime | None = None
    completed_at: datetime | None = None
    idempotency: dict[str, str] = Field(default_factory=dict, exclude=True)

    @field_validator("objective")
    @classmethod
    def normalize_objective(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("objective must not be blank")
        return value


class GoalRunCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=GOAL_OBJECTIVE_MAX_LENGTH)
    client_request_id: str = Field(min_length=1, max_length=128)

    @field_validator("objective", "client_request_id")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class GoalMutationRequest(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=128)


class GoalTurnUsage(BaseModel):
    total_tokens: int | None = Field(default=None, ge=0)
    quality: GoalUsageQuality = GoalUsageQuality.UNAVAILABLE
    raw: dict[str, Any] | None = None
