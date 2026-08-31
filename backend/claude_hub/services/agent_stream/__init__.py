"""Structured observation plane (Layer B) service package.

Provider adapters normalize each agent's CLI transcript into a stream of
:class:`~claude_hub.models.AgentStreamEvent` records; a per-session tailer
tails the transcript, redacts + persists events, and fans them out to live
SSE/wait subscribers. The raw terminal (Layer C) is always one click away and
is the fail-closed fallback whenever structured observation is unavailable.
"""

from __future__ import annotations

from .base import AgentStreamAdapter, NormalizeContext
from .redaction import redact_event
from .registry import get_adapter, get_adapter_for_session, supports_structured
from .store import AgentStreamStore
from .tailer import (
    SessionTailer,
    StructuredSourceUnavailable,
    TailerManager,
    discard_session_stream,
    structured_source_hard_failed,
)

__all__ = [
    "AgentStreamAdapter",
    "NormalizeContext",
    "AgentStreamStore",
    "SessionTailer",
    "StructuredSourceUnavailable",
    "TailerManager",
    "discard_session_stream",
    "get_adapter",
    "get_adapter_for_session",
    "redact_event",
    "structured_source_hard_failed",
    "supports_structured",
]
