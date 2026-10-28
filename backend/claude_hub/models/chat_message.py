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
    kind: Literal["message", "question_answer"] = "message"
    question_ref: str | None = None
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


class QuestionMirror(BaseModel):
    bot_id: str
    app_id: str
    tenant_key: str
    pairing_id: str
    generation: int
    revision: int
    chat_id: str
    sender_open_id: str
    send_uuid: str
    message_id: str | None = None
    state: Literal["queued", "sending", "sent", "uncertain", "disabled"] = "queued"
    displayed_state: str | None = None
    retry_at: datetime | None = None
    attempts: int = 0
    action_digests: dict[str, dict[str, str]] = Field(default_factory=dict)


class QuestionCardIdentity(BaseModel):
    """Content-free correlation tombstone for a superseded remote card."""

    bot_id: str
    app_id: str
    chat_id: str
    message_id: str


class ChatQuestion(BaseModel):
    question_ref: str
    tab_id: str
    session_id: str
    incarnation: str
    owner_created_at: str = ""
    owner_workspace_id: str = ""
    turn_id: str
    call_id: str
    run_epoch: int
    question_id: str
    prompt: str
    options: list[str]
    mode: Literal["native", "followup"]
    created_at: datetime
    expires_at: datetime
    state: Literal["open", "claimed", "resolved", "stale", "uncertain"] = "open"
    answer_id: str | None = None
    reply_state: Literal["pending", "sending", "sent", "uncertain"] | None = None
    reply_text: str = ""
    mirror: QuestionMirror | None = None
    mirror_checked: bool = False
    retired_cards: list[QuestionCardIdentity] = Field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json", exclude={"mirror", "incarnation", "reply_text", "retired_cards"}
        )


class QuestionAnswerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: str = Field(min_length=1, max_length=256)
    selected: list[str] = Field(min_length=1, max_length=1)

    @field_validator("call_id")
    @classmethod
    def nonblank_call_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("call_id must be nonblank")
        return value

    @field_validator("selected")
    @classmethod
    def bounded_answer(cls, values: list[str]) -> list[str]:
        if not values[0].strip() or len(values[0].encode("utf-8")) > 65536:
            raise ValueError("answer must be nonblank and at most 64 KiB")
        return values
