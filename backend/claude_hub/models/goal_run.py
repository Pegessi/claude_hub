"""Persistent contracts for Hub-managed and provider-native Chat Goals."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .chat_image import ChatImageAttachment

GOAL_OBJECTIVE_MAX_LENGTH = 4000
GOAL_STEER_INPUT_MAX_LENGTH = 32_000
GOAL_CHECKPOINT_HISTORY_LIMIT = 10
GOAL_RECENT_TURN_IDS_LIMIT = 100
GOAL_PLAN_STEPS_LIMIT = 30
GOAL_PLAN_HELPERS_LIMIT = 10


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GoalRunStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    USAGE_LIMITED = "usageLimited"
    BUDGET_LIMITED = "budgetLimited"
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


class GoalPlanStep(BaseModel):
    """One implementation step the user approves before a ``plan_first`` Goal runs."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=1000)
    acceptance: str | None = Field(default=None, max_length=1000)


class GoalPlanHelper(BaseModel):
    """One helper/sub-agent the plan proposes, with its provider and model."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=200)


class GoalPlanBudget(BaseModel):
    """The execution budget the user approves: who runs, how wide, how long."""

    model_config = ConfigDict(extra="forbid")

    helpers: list[GoalPlanHelper] = Field(default_factory=list, max_length=GOAL_PLAN_HELPERS_LIMIT)
    max_parallelism: int = Field(default=1, ge=1, le=32)
    duration_estimate: str | None = Field(default=None, max_length=200)


class GoalPlan(BaseModel):
    """Agent-authored plan for a ``plan_first`` Goal.

    It is validated control-plane data, not an instruction: the Hub renders it
    for approval and never executes anything it contains.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    steps: list[GoalPlanStep] = Field(default_factory=list, max_length=GOAL_PLAN_STEPS_LIMIT)
    budget: GoalPlanBudget = Field(default_factory=GoalPlanBudget)


class GoalRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tab_id: str
    execution_owner: Literal["hub_managed", "provider_native"] = "hub_managed"
    provider_thread_id: str | None = None
    provider_goal_created_at: int | None = None
    provider_updated_at: int | None = None
    provider_status: str | None = None
    provider_goal_absent: bool = False
    provider_stop_pending: bool = False
    provider_owned_turn_ids: list[str] = Field(default_factory=list)
    objective: str = Field(min_length=1, max_length=GOAL_OBJECTIVE_MAX_LENGTH)
    objective_revision: int = 0
    dispatched_objective_revision: int = 0
    pending_image_count: int = Field(default=0, ge=0, le=10)
    pending_image_fingerprint: str = ""
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
    # User text that the next dispatched Goal turn must carry. It is cleared
    # only after provider acceptance, so an uncertain dispatch keeps it.
    pending_input: str | None = Field(default=None, max_length=GOAL_STEER_INPUT_MAX_LENGTH)
    # Resumed while an ordinary Chat turn was running: dispatch once it ends.
    resume_after_turn: bool = False
    # Additive ``plan_first`` scope approval. The first Goal turn plans only and
    # parks on the existing needs_input pause; ``plan_pending`` says the next
    # dispatched turn is still the planning turn. ``plan_answered`` records that
    # a turn carrying the user's own message was dispatched after the plan
    # existed, which is what ends the approval request; it deliberately says
    # nothing about whether the user approved or asked for changes.
    plan_first: bool = False
    plan_pending: bool = False
    plan: GoalPlan | None = None
    plan_turn_id: str | None = None
    plan_answered: bool = False
    plan_warning: str | None = None
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
    attachments: list[ChatImageAttachment] = Field(default_factory=list, max_length=10)
    objective: str = Field(min_length=1, max_length=GOAL_OBJECTIVE_MAX_LENGTH)
    client_request_id: str = Field(min_length=1, max_length=128)
    # Additive and optional: absent callers keep the previous start-immediately
    # behaviour byte for byte.
    plan_first: bool = False

    @field_validator("objective", "client_request_id")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class GoalObjectiveUpdate(BaseModel):
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
