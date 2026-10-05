"""Bounded feedback controls and explicitly selected Chat evidence.

These files are feedback metadata, not a second task state machine. They never
dispatch an agent themselves. Source discovery reads a bounded tail of the
authoritative Chat event stream and excludes machine-generated turns.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TAIL_BYTES = 256 * 1024
_HUMAN_TURN_ORIGINS = frozenset({"web", "feishu"})


class FeedbackAutomationSettings(BaseModel):
    enabled: bool = True
    cooldown_seconds: int = Field(default=3600, ge=3600, le=604800)
    max_records: int = Field(default=5, ge=1, le=10)


class FeedbackAutomationState(BaseModel):
    settings: FeedbackAutomationSettings = Field(default_factory=FeedbackAutomationSettings)
    initialized_at: datetime
    last_checked_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_run_id: str | None = None
    last_outcome: str = "initialized_fresh_only"
    scan_cursor: str = ""


class ChatCorrectionCreate(BaseModel):
    tab_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=8, max_length=1024)


class ChatCorrection(BaseModel):
    id: str
    workspace_id: str
    tab_id: str
    turn_id: str
    message_id: str
    stream_sequence: int
    quote: str
    source_sha256: str
    created_at: datetime


def _has_human_turn_source(payload: dict[str, Any]) -> bool:
    """Accept legacy user turns or an explicit server-authored human origin."""

    if "metadata" not in payload:
        return True
    metadata = payload["metadata"]
    if not isinstance(metadata, dict):
        return False
    origin = metadata.get("origin")
    return isinstance(origin, str) and origin in _HUMAN_TURN_ORIGINS


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class FeedbackAutomationStore:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root

    def state(self, workspace_id: str, now: datetime) -> FeedbackAutomationState:
        path = self.root / workspace_id / "feedback" / "automation.json"
        if path.exists():
            # Fail closed on corrupt controls; never silently re-enable spending.
            return FeedbackAutomationState.model_validate_json(path.read_text(encoding="utf-8"))
        state = FeedbackAutomationState(initialized_at=now)
        self.save(workspace_id, state)
        return state

    def save(self, workspace_id: str, state: FeedbackAutomationState) -> None:
        _write_json(
            self.root / workspace_id / "feedback" / "automation.json",
            state.model_dump(mode="json"),
        )

    def sources(self, tab_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if not _ID.fullmatch(tab_id):
            raise ValueError("invalid tab_id")
        path = self.root / "terminal-tabs" / "agent_streams" / f"terminal-tab-{tab_id}.jsonl"
        if not path.exists():
            return []
        with path.open("rb") as source:
            source.seek(0, 2)
            start = max(0, source.tell() - _TAIL_BYTES)
            source.seek(start)
            lines = source.read(_TAIL_BYTES).splitlines()
            if start:
                lines = lines[1:]  # omit the potentially partial first row
        found: dict[str, dict[str, Any]] = {}
        for line in lines:
            try:
                item = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(item, dict) or item.get("type") != "turn_started":
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            turn_id = item.get("turn_id")
            message_id = item.get("message_id")
            summary = payload.get("summary")
            if (
                item.get("tab_id") != tab_id
                or item.get("redacted")
                or not isinstance(turn_id, str)
                or not turn_id
                or turn_id.startswith(("scheduled-", "goal-"))
                or not _has_human_turn_source(payload)
                or message_id != f"{turn_id}:user"
                or not isinstance(summary, str)
                or not summary.strip()
                or not isinstance(item.get("stream_sequence"), int)
            ):
                continue
            found[message_id] = {
                "tab_id": tab_id,
                "turn_id": turn_id,
                "message_id": message_id,
                "stream_sequence": item["stream_sequence"],
                "text": summary[:4096],
                "truncated": len(summary) > 4096,
                "source_sha256": hashlib.sha256(summary.encode()).hexdigest(),
            }
        return list(found.values())[-min(max(limit, 1), 20) :]

    def capture(
        self, workspace_id: str, request: ChatCorrectionCreate, now: datetime
    ) -> ChatCorrection:
        source = next(
            (
                item
                for item in self.sources(request.tab_id, limit=20)
                if item["turn_id"] == request.turn_id and item["message_id"] == request.message_id
            ),
            None,
        )
        if source is None or request.quote not in source["text"]:
            raise ValueError("exact correction quote must occur in a recent persisted user message")
        # One evidence object per source message, regardless of repeated calls or
        # alternate excerpts. Repeated quotes cannot masquerade as recurrence.
        record_id = (
            "chat-"
            + hashlib.sha256(
                f"{workspace_id}:{request.tab_id}:{request.message_id}".encode()
            ).hexdigest()[:24]
        )
        path = self.root / workspace_id / "feedback" / "chat-corrections" / f"{record_id}.json"
        if path.exists():
            return ChatCorrection.model_validate_json(path.read_text(encoding="utf-8"))
        self.state(workspace_id, now)  # initialize watermark before recording
        record = ChatCorrection(
            id=record_id,
            workspace_id=workspace_id,
            tab_id=request.tab_id,
            turn_id=request.turn_id,
            message_id=request.message_id,
            stream_sequence=source["stream_sequence"],
            quote=request.quote,
            source_sha256=source["source_sha256"],
            created_at=now,
        )
        _write_json(path, record.model_dump(mode="json"))
        return record

    def correction(self, workspace_id: str, record_id: str) -> ChatCorrection:
        if not re.fullmatch(r"chat-[0-9a-f]{24}", record_id):
            raise ValueError("invalid Chat correction record id")
        path = self.root / workspace_id / "feedback" / "chat-corrections" / f"{record_id}.json"
        record = ChatCorrection.model_validate_json(path.read_text(encoding="utf-8"))
        if record.workspace_id != workspace_id or record.id != record_id:
            raise ValueError("Chat correction belongs to a different workspace or identity")
        return record
