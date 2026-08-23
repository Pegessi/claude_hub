"""Task-owned durable mailbox event schema.

Slice 2 of the Task-centric mailbox. WorkspaceTask is the work node;
events carry actor fields that must be persisted, not inferred.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class TaskActorRole(str, Enum):
    """Who produced a Task mailbox event. Session id is assignment metadata only."""

    WORKER = "worker"
    REVIEWER = "reviewer"
    SUPERVISOR = "supervisor"
    HUMAN = "human"


class TaskEventType(str, Enum):
    """Task-level event types. Coarser AgentEventType is a compat projection only."""

    DISPATCHED = "dispatched"
    STARTED = "started"
    PROGRESS = "progress"
    REPORT = "report"
    FOLLOWUP = "followup"
    MESSAGE = "message"
    REVIEW_STARTED = "review_started"
    REVIEW_PASSED = "review_passed"
    REVIEW_FAILED = "review_failed"
    REVIEW_NEEDS_INPUT = "review_needs_input"
    NEEDS_INPUT = "needs_input"
    HUMAN_ACCEPTANCE_REQUESTED = "human_acceptance_requested"
    HUMAN_ACCEPTED = "human_accepted"
    ABORT = "abort"
    INTERRUPT = "interrupt"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskEvent(BaseModel):
    """Append-only Task mailbox entry with workspace-monotonic sequence."""

    sequence: int
    call_id: str
    fingerprint: str
    task_id: str
    actor_session_id: Optional[str] = None
    actor_role: TaskActorRole
    review_cycle: Optional[int] = None
    type: TaskEventType
    action: str
    target: str
    consumer_key: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    compat_run_id: Optional[str] = None
    report_id: Optional[str] = None
