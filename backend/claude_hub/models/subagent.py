"""Public contracts for Hub subagents (``claude-hub agent run``).

A subagent is one native Chat tab started for another agent. It has no Task,
board entry, Goal Packet, review or acceptance; each turn has a small result
record that survives the request which started it.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .schema_domains.runtime import AgentType

SUBAGENT_PROMPT_MAX_BYTES = 256 * 1024
SUBAGENT_DEFAULT_TIMEOUT_SECONDS = 30 * 60
SUBAGENT_MAX_TIMEOUT_SECONDS = 24 * 60 * 60
SUBAGENT_MAX_WAIT_SECONDS = 60.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SubagentTurnStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


TERMINAL_SUBAGENT_TURN_STATUSES = frozenset(
    {
        SubagentTurnStatus.COMPLETED,
        SubagentTurnStatus.FAILED,
        SubagentTurnStatus.TIMEOUT,
        SubagentTurnStatus.CANCELLED,
    }
)


def _nonblank(value: Optional[str]) -> Optional[str]:
    if value is not None and not value.strip():
        raise ValueError("must not be blank")
    return value


def _bounded_prompt(value: str) -> str:
    if not value.strip():
        raise ValueError("prompt must not be blank")
    if len(value.encode("utf-8")) > SUBAGENT_PROMPT_MAX_BYTES:
        raise ValueError(f"prompt exceeds {SUBAGENT_PROMPT_MAX_BYTES // 1024} KiB UTF-8")
    return value


class SubagentTurnCreate(BaseModel):
    """Follow-up turn on a kept subagent (same provider conversation)."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    call_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Idempotency key: the same call_id never starts a second turn.",
    )
    timeout_seconds: float = Field(
        default=SUBAGENT_DEFAULT_TIMEOUT_SECONDS, gt=0, le=SUBAGENT_MAX_TIMEOUT_SECONDS
    )

    _prompt = field_validator("prompt")(_bounded_prompt)
    _call_id = field_validator("call_id")(_nonblank)


class SubagentCreate(SubagentTurnCreate):
    """Create a subagent Chat tab and start its first turn."""

    agent_type: AgentType
    model: Optional[str] = Field(default=None, min_length=1, max_length=200)
    effort: Optional[str] = Field(default=None, min_length=1, max_length=32)
    cwd: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    env_preset: Optional[str] = Field(default=None, min_length=1, max_length=200)
    env: Dict[str, str] = Field(default_factory=dict)
    parent_tab_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    keep: bool = Field(
        default=False,
        description="Keep the Chat open for follow-up turns; default archives it after its turn.",
    )
    solo_mode: bool = Field(
        default=True,
        description="Run without interactive approvals; nobody is watching to approve them.",
    )

    _fields = field_validator("model", "effort", "cwd", "env_preset", "parent_tab_id", "name")(
        _nonblank
    )


class SubagentTurn(BaseModel):
    turn_id: str
    call_id: Optional[str] = None
    status: SubagentTurnStatus
    final_text: Optional[str] = None
    error: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    timeout_seconds: float
    request_digest: str = ""


class Subagent(BaseModel):
    id: str
    tab_id: Optional[str] = None
    parent_tab_id: Optional[str] = None
    agent_type: AgentType
    model: Optional[str] = None
    effort: Optional[str] = None
    cwd: str
    env_preset: Optional[str] = None
    name: str
    keep: bool = False
    created_at: datetime
    created_by: Optional[str] = None
    call_id: Optional[str] = None
    request_digest: str = ""
    closed: bool = False
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None
    turns: List[SubagentTurn] = Field(default_factory=list)


class SubagentTurnResult(BaseModel):
    """What a caller needs from one turn: identity, status and the final answer."""

    subagent_id: str
    tab_id: Optional[str]
    turn_id: str
    status: SubagentTurnStatus
    final_text: Optional[str] = None
    error: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    keep: bool
    closed: bool

    @classmethod
    def of(cls, subagent: Subagent, turn: SubagentTurn) -> "SubagentTurnResult":
        return cls(
            subagent_id=subagent.id,
            tab_id=subagent.tab_id,
            turn_id=turn.turn_id,
            status=turn.status,
            final_text=turn.final_text,
            error=turn.error,
            started_at=turn.started_at,
            completed_at=turn.completed_at,
            keep=subagent.keep,
            closed=subagent.closed,
        )


class SubagentSummary(BaseModel):
    id: str
    tab_id: Optional[str]
    parent_tab_id: Optional[str]
    agent_type: AgentType
    model: Optional[str]
    name: str
    keep: bool
    closed: bool
    close_reason: Optional[str]
    created_at: datetime
    turn_count: int
    last_turn_id: Optional[str]
    last_status: Optional[SubagentTurnStatus]

    @classmethod
    def of(cls, subagent: Subagent) -> "SubagentSummary":
        last = subagent.turns[-1] if subagent.turns else None
        return cls(
            id=subagent.id,
            tab_id=subagent.tab_id,
            parent_tab_id=subagent.parent_tab_id,
            agent_type=subagent.agent_type,
            model=subagent.model,
            name=subagent.name,
            keep=subagent.keep,
            closed=subagent.closed,
            close_reason=subagent.close_reason,
            created_at=subagent.created_at,
            turn_count=len(subagent.turns),
            last_turn_id=last.turn_id if last else None,
            last_status=last.status if last else None,
        )
