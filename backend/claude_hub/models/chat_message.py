"""Strict public contracts for ordinary Chat inbox messages."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ChatMessageState = Literal[
    "queued", "waiting", "dispatching", "accepted", "completed", "failed", "cancelled", "uncertain"
]
TERMINAL_STATES = {"completed", "failed", "cancelled"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChatMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=65536)
    call_id: str = Field(min_length=1, max_length=256)
    source_tab_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("message", "call_id", "source_tab_id")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("message")
    @classmethod
    def bounded_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("message exceeds 64 KiB UTF-8")
        return value


class ChatMessageReceipt(BaseModel):
    message_id: str
    call_id: str
    tab_id: str
    turn_id: str
    state: ChatMessageState
    waiting_reason: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    status_url: str


class ChatMessageEnvelope(ChatMessageReceipt):
    principal: str
    principal_email: str | None = None
    actor: str
    incarnation: str
    fingerprint: str
    message: str
    source_tab_id: str | None = None
    sequence: int
    attempts: int = 0
    submission_started: bool = False
    expired: bool = False

    # Trusted internal fields; absent from the strict public create contract.
    scheduled_run_id: str | None = None
    scheduled_task_id: str | None = None
    turn_metadata: dict[str, Any] = Field(default_factory=dict)
    legacy_recovery: bool = False
    recovery_checked_at: datetime | None = None

    @property
    def blocks_queue(self) -> bool:
        return self.state not in TERMINAL_STATES and not (
            self.scheduled_run_id is not None and self.state == "uncertain"
        )

    def receipt(self) -> ChatMessageReceipt:
        return ChatMessageReceipt.model_validate(self.model_dump())
