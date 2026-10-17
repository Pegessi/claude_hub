"""Runtime result values and exceptions, defined once."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

from ....models import AgentRuntimeStatus, AgentStreamEvent

PostPersistObserver = Callable[[AgentStreamEvent], Awaitable[None]]


class EditResendTurnInFlightError(RuntimeError):
    """Edit-resend rejected because a turn is currently running.

    Raised by :meth:`TailerManager._edit_resend_locked` when the session's
    native transport reports ``turn_in_flight=True``.  The API layer maps it
    to HTTP **409 Conflict**.  Subclasses ``RuntimeError`` so non-API callers
    that catch ``RuntimeError`` still see it; the API mapping checks this more
    specific type before the generic ``RuntimeError`` branch.
    """


@dataclass(frozen=True)
class NativeRuntimeSnapshot:
    """Read-only runtime state derived from the sole native transport owner."""

    status: AgentRuntimeStatus
    detail: str


class ExternalTurnRetirement(str, Enum):
    """Outcome of atomically retiring an external-channel turn."""

    CANCELLED_MATCHING = "cancelled_matching"
    NOT_STARTED = "not_started"
    ALREADY_TERMINAL = "already_terminal"
    DIFFERENT_TURN = "different_turn"
    UNKNOWN = "unknown"

    @property
    def safe_to_retry(self) -> bool:
        return self in {self.CANCELLED_MATCHING, self.NOT_STARTED}


@dataclass(frozen=True)
class _PendingApproval:
    """An unanswered approval card's identity, captured when its
    ``approval_required`` event flows through ``_run_native``.

    The tailer stamps the matching ``approval_resolved`` with the card's own
    ``turn_id``/``run_epoch`` so a reload marks the card resolved inside the
    correct turn even if the answer arrives after the turn's active id was
    released (the Claude/Cursor steer path cancels the blocked turn).
    """

    call_id: str
    turn_id: Optional[str]
    run_epoch: Optional[int]
    question_ids: frozenset[str] = frozenset()


class StructuredSourceUnavailable(RuntimeError):
    """The adapter exists, but its transcript source is terminally unavailable."""
