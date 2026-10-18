"""Terminal value records and errors, shared by implementation and compatibility imports."""

from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TypedDict

ScanEntry = namedtuple(
    "ScanEntry",
    ["path", "mtime_ns", "size", "cwd", "ts", "is_archived", "title", "thread_source"],
    defaults=["", ""],
)


class TabLimitExceededError(Exception):
    """Raised when creating/forking a tab would exceed a growth limit.

    Maps to HTTP 429 in the API layer. The bound exists so a loop of fork (or
    create) requests cannot exhaust ttyd ports, processes, and tmux sessions.
    """


class TabStartupTimeoutError(RuntimeError):
    """Raised after an unregistered tab startup times out and is rolled back."""


class CursorPosition(TypedDict):
    cursor_x: int
    cursor_y: int


class _AgentStatusSnapshot(TypedDict):
    hash: str
    last_changed_at: Optional[datetime]
    frame_first_seen_at: Optional[datetime]


class TabStateError(RuntimeError):
    """The tab snapshot cannot be trusted or safely overwritten."""


class TabCleanupPendingError(RuntimeError):
    """Deletion or its durable cleanup acknowledgment needs a retry."""


class _TabStateCommitError(OSError):
    def __init__(self, error: OSError, *, replaced: bool) -> None:
        super().__init__(str(error))
        self.replaced = replaced


@dataclass(frozen=True)
class _PendingTabCleanup:
    tab_id: str
    created_at: str
    token: str
    authorization: str = "stream-discard-validated-v1"
