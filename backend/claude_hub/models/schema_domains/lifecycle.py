"""Canonical Task lifecycle vocabulary.

``WorkspaceTaskStatus`` is a coarse board column; ``review`` alone hides five
different situations and ``working`` cannot say whether anyone is still
executing. ``TaskLifecycle`` names exactly one phase per Task, who owns it,
whether that owner is still present, and whether a human must act.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel


class TaskPhase(str, Enum):
    DRAFT = "draft"  # recorded, nobody asked to execute it yet
    QUEUED = "queued"  # wants an executor, none is executing yet
    RUNNING = "running"  # an owner is executing
    REVIEWING = "reviewing"  # an AI reviewer is judging the latest round
    NEEDS_USER = "needs_user"  # blocked on a human answer or decision
    AWAITING_ACCEPTANCE = "awaiting_acceptance"  # finished, waiting for human accept
    ORPHANED = "orphaned"  # non-terminal, but its owner is gone or released it
    DONE = "done"
    FAILED = "failed"


ATTENTION_PHASES = frozenset(
    {TaskPhase.NEEDS_USER, TaskPhase.AWAITING_ACCEPTANCE, TaskPhase.ORPHANED}
)
TERMINAL_PHASES = frozenset({TaskPhase.DONE, TaskPhase.FAILED})


class TaskOwnerKind(str, Enum):
    SESSION = "session"  # Workspace-managed agent session (worker or reviewer)
    CHAT = "chat"  # initiator-controlled: the Chat that registered the Task
    NONE = "none"


class TaskOwnerState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    GONE = "gone"  # deleted, archived, or stopped
    UNKNOWN = "unknown"


class TaskOwner(BaseModel):
    kind: TaskOwnerKind
    id: Optional[str] = None
    state: TaskOwnerState = TaskOwnerState.UNKNOWN


class TaskLifecycle(BaseModel):
    """Derived, read-only projection of one Task's lifecycle."""

    task_id: str
    phase: TaskPhase
    # Machine-readable cause for the phase, e.g. ``reviewer_needs_input``.
    reason: str
    # Human-readable form of ``reason`` for UI and notices.
    reason_text: str = ""
    owner: TaskOwner
    attention: bool
    # Queued work waiting behind a session that another Task still holds.
    blocked_by_task_id: Optional[str] = None
    # Whether this Task currently keeps its assigned session from new work.
    holds_session: bool = False
    since: datetime
    legacy_status: Literal["todo", "queued", "working", "review", "done", "failed"]


class TaskLifecycleMark(BaseModel):
    """Last lifecycle the Hub observed for a Task, plus its notification state.

    Persisted so a phase change is announced exactly once across restarts.
    """

    model_config = {"extra": "forbid"}

    phase: TaskPhase
    reason: str
    changed_at: datetime
    # Set once the change was delivered, had no recipient, or needs no notice.
    notified_at: Optional[datetime] = None
    notify_attempts: int = 0
    next_notify_at: Optional[datetime] = None
    # Digest of the last Goal Packet announced as a plan checkpoint.
    plan_digest: Optional[str] = None
    # The last attention notice actually considered for delivery, carried across
    # marks so a phase that flaps back within the cooldown is not re-announced.
    last_notice_key: Optional[str] = None
    last_notice_at: Optional[datetime] = None
