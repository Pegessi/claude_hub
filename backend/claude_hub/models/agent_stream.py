"""Structured agent-stream models (Layer B observation plane).

These provider-neutral records are normalized from agent CLI transcripts,
persisted per session, and exposed by the structured timeline API.
"""

import enum
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .schemas import AgentType, StreamCapabilities

__all__ = [
    "AgentStreamEvent",
    "AgentStreamEventType",
    "AgentStreamEventPage",
    "StreamCapabilities",
]


class AgentStreamEventType(str, enum.Enum):
    """The normalized event types rendered by the structured pane."""

    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    ERROR = "error"
    STATUS = "status"


class AgentStreamEvent(BaseModel):
    """One normalized structured-stream event with a session-local cursor."""

    stream_sequence: int
    session_id: str
    tab_id: str
    agent_type: AgentType
    type: AgentStreamEventType
    run_epoch: Optional[int] = None
    call_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    redacted: bool = False


class AgentStreamEventPage(BaseModel):
    """A page of events; ``since_sequence`` is exclusive on reads."""

    events: List[AgentStreamEvent]
    next_sequence: int
    has_more: bool
