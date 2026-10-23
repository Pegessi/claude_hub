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
    owner: TaskOwner
    attention: bool
    # Queued work waiting behind a session that another Task still holds.
    blocked_by_task_id: Optional[str] = None
    # Whether this Task currently keeps its assigned session from new work.
    holds_session: bool = False
    since: datetime
    legacy_status: Literal["todo", "queued", "working", "review", "done", "failed"]
