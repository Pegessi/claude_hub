"""Unified Agent Tree + Durable Mailbox/Event Stream models.

This module defines the data structures for the agent-to-agent coordination
layer that unifies the Resident agent, managed workspace tasks, native
subagents, and external jobs under a single parent/child tree with an
append-only event stream.

Design goals:
- Runtime/Hub owns lifecycle state; agents never forge status via free text.
- Every action and result carries a ``call_id`` / ``correlation_id`` for
  idempotency, retry, and replay.
- Events are append-only and addressable by a monotonic ``sequence`` cursor
  so supervisors can ``wait`` for directed events from their subtree.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ExecutorKind(str, Enum):
    """How a child agent run is executed.

    ``managed_task`` wraps the existing workspace task/session/report flow.
    ``native_subagent`` is a future in-process subagent (stub for now).
    ``external_job`` is a future remote/third-party job (stub for now).
    """

    MANAGED_TASK = "managed_task"
    NATIVE_SUBAGENT = "native_subagent"
    EXTERNAL_JOB = "external_job"


class AgentRunStatus(str, Enum):
    """Lifecycle status of an agent run node.

    The Hub owns this state; agents report progress via events, not by
    mutating status directly.
    """

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AgentEventType(str, Enum):
    """Append-only event types emitted by/for agent runs."""

    DISPATCHED = "dispatched"
    STARTED = "started"
    PROGRESS = "progress"
    HEARTBEAT = "heartbeat"
    MESSAGE = "message"
    TOOL_WAIT = "tool_wait"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


# Terminal event types that move a run out of RUNNING.
TERMINAL_EVENT_TYPES = frozenset(
    {
        AgentEventType.COMPLETED,
        AgentEventType.FAILED,
        AgentEventType.INTERRUPTED,
    }
)


class AgentRun(BaseModel):
    """A node in the agent tree (DelegationNode).

    Each run has a ``path`` (slash-separated ancestor ids, e.g.
    ``root/child1/grandchild``) so subtree scoping is a simple prefix match
    without recursive traversal.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workspace_id: str
    parent_id: Optional[str] = None
    # Slash-separated path of ancestor run ids, including self. Root runs
    # have path == id.
    path: str
    # The supervisor run that owns this child (the parent's id). For root
    # runs this is None.
    supervisor_id: Optional[str] = None
    executor_kind: ExecutorKind
    status: AgentRunStatus = AgentRunStatus.PENDING
    # Opaque reference to the executor's context (e.g. workspace task id,
    # native subagent handle, external job id).
    context_ref: Optional[str] = None
    # Human-readable last task message surfaced to the tree UI.
    last_task_message: Optional[str] = None
    title: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AgentEvent(BaseModel):
    """An append-only entry in the agent event stream.

    Events are addressed by a monotonic ``sequence`` number (per workspace)
    and carry ``call_id`` / ``correlation_id`` for idempotency and tracing.
    ``author`` and ``recipient`` are run ids; a supervisor's mailbox only
    receives events whose recipient is itself or whose author is in its
    subtree.
    """

    sequence: int
    call_id: str
    # Optional correlation id tying a request to its response events.
    correlation_id: Optional[str] = None
    agent_run_id: str
    type: AgentEventType
    author: str
    recipient: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Action request / response models
# ---------------------------------------------------------------------------


class SpawnRequest(BaseModel):
    """Create a child run and dispatch its initial task.

    The child is created under ``parent_id`` with the given executor kind.
    ``initial_message`` is the first task instruction delivered to the
    child's mailbox as a ``dispatched`` event.
    """

    workspace_id: str
    parent_id: str
    executor_kind: ExecutorKind
    title: Optional[str] = None
    initial_message: str
    call_id: str
    context_ref: Optional[str] = None


class SendRequest(BaseModel):
    """Append a message to the recipient's mailbox without starting a turn.

    Use this for one-way notifications that do not require the recipient to
    act immediately.
    """

    workspace_id: str
    recipient_id: str
    author_id: str
    message: str
    call_id: str
    correlation_id: Optional[str] = None


class FollowupRequest(BaseModel):
    """Append a message to the recipient's mailbox AND start/resume its turn.

    Unlike ``send``, this signals the executor to wake up and process the
    message. For managed_task this maps to continue_task / start_task.
    """

    workspace_id: str
    recipient_id: str
    author_id: str
    message: str
    call_id: str
    correlation_id: Optional[str] = None


class WaitRequest(BaseModel):
    """Block until an event matching the filter arrives, or timeout.

    ``since_sequence`` is the cursor (exclusive). The call returns all
    events for ``recipient_id`` (or its subtree when ``subtree=True``) with
    sequence > since_sequence. If none arrive before ``timeout_seconds``,
    returns an empty list.
    """

    workspace_id: str
    recipient_id: str
    since_sequence: int = 0
    subtree: bool = True
    timeout_seconds: float = 30.0


class InterruptRequest(BaseModel):
    """Interrupt a run, preserving its context for later resume."""

    workspace_id: str
    run_id: str
    call_id: str
    reason: Optional[str] = None


class ListRunsRequest(BaseModel):
    """List runs in a workspace, optionally scoped to a subtree."""

    workspace_id: str
    root_id: Optional[str] = None
    status: Optional[AgentRunStatus] = None
