"""Persistent contracts for Hub-managed Chat Goals."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

GOAL_OBJECTIVE_MAX_LENGTH = 4000
DEFAULT_GOAL_MAX_TURNS = 20
HARD_GOAL_MAX_TURNS = 100


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GoalRunStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    BUDGET_LIMITED = "budget_limited"
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


class GoalRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tab_id: str
    objective: str = Field(min_length=1, max_length=GOAL_OBJECTIVE_MAX_LENGTH)
    status: GoalRunStatus = GoalRunStatus.ACTIVE
    token_budget: int | None = Field(default=None, ge=1)
    token_usage: int | None = Field(default=None, ge=0)
    usage_quality: GoalUsageQuality = GoalUsageQuality.UNAVAILABLE
    max_turns: int = Field(default=DEFAULT_GOAL_MAX_TURNS, ge=1, le=HARD_GOAL_MAX_TURNS)
    turns_completed: int = Field(default=0, ge=0)
    dispatch_state: GoalDispatchState = GoalDispatchState.IDLE
    pending_step_id: str | None = None
    current_turn_id: str | None = None
    completed_turn_ids: list[str] = Field(default_factory=list)
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
    token_budget: int | None = Field(default=None, ge=1)
    max_turns: int = Field(default=DEFAULT_GOAL_MAX_TURNS, ge=1, le=HARD_GOAL_MAX_TURNS)
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


class GoalBudgetUpdate(GoalMutationRequest):
    token_budget: int | None = Field(default=None, ge=1)


class GoalTurnUsage(BaseModel):
    total_tokens: int | None = Field(default=None, ge=0)
    quality: GoalUsageQuality = GoalUsageQuality.UNAVAILABLE
    raw: dict[str, Any] | None = None
