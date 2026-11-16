"""Validated configuration and bounded durable patrol evidence."""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PatrolChannel = Literal["all", "feishu", "daily"]


class PatrolNotifier(Protocol):
    def is_set_up(self) -> bool: ...

    async def notify(self, text: str, *, title: str | None = None, tone: str = "info") -> str: ...


class PatrolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    feishu_enabled: bool = True
    feishu_interval_minutes: int = Field(default=20, ge=1, le=1440)
    feishu_window_start: str = "09:30"
    feishu_window_end: str = "22:30"
    daily_enabled: bool = True
    daily_slots: list[str] = Field(default_factory=lambda: ["11:00", "17:30"], max_length=24)
    stalled_hours: float = Field(default=48.0, gt=0, le=8760, allow_inf_nan=False)
    pending_top_n: int = Field(default=5, ge=1, le=50)
    max_triage_per_run: int = Field(default=3, ge=1, le=20)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("feishu_window_start", "feishu_window_end")
    @classmethod
    def valid_window_time(cls, value: str) -> str:
        return validate_clock_time(value)

    @field_validator("daily_slots")
    @classmethod
    def valid_daily_slots(cls, values: list[str]) -> list[str]:
        return sorted({validate_clock_time(value) for value in values})

    @model_validator(mode="after")
    def ordered_window(self) -> PatrolConfig:
        if self.feishu_window_start > self.feishu_window_end:
            raise ValueError("feishu_window_start must not follow feishu_window_end")
        return self


def validate_clock_time(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must use HH:MM (00:00 through 23:59)")
    return value


class MRStateSnapshot(BaseModel):
    url: str
    state: str = "unknown"
    ci_status: str = "unknown"
    updated_at: str = ""
    title: str = ""


class PatrolRunRecord(BaseModel):
    channel: Literal["feishu", "daily"]
    started_at: str
    dry_run: bool = False
    candidates: int = 0
    llm_invoked: int = 0
    notified: int = 0
    markdown: str = ""
    errors: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class PatrolState(BaseModel):
    config: PatrolConfig = Field(default_factory=PatrolConfig)
    feishu_watermark_iso: str | None = None
    processed_feishu_ids: list[str] = Field(default_factory=list)
    fired_daily_slots: list[str] = Field(default_factory=list)
    mr_snapshots: dict[str, MRStateSnapshot] = Field(default_factory=dict)
    recent_runs: list[PatrolRunRecord] = Field(default_factory=list)

    def bound_history(self) -> None:
        # Keep the most recent occurrence when a recovery overlap revisits a message.
        self.processed_feishu_ids = list(dict.fromkeys(reversed(self.processed_feishu_ids)))[:500][
            ::-1
        ]
        self.fired_daily_slots = list(dict.fromkeys(reversed(self.fired_daily_slots)))[:60][::-1]
        self.recent_runs = self.recent_runs[-20:]
        self.mr_snapshots = dict(list(self.mr_snapshots.items())[-500:])


class PatrolRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: PatrolChannel = "all"
    dry_run: bool = False
    notify: bool = True
