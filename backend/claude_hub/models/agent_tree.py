"""AgentRun + Durable Mailbox/Event Stream models (deprecated compatibility).

``AgentRun`` is the deprecated compatibility projection of the canonical
Workspace Task Graph. New orchestration should use Task Graph / TaskMailbox
APIs; this module remains for legacy linked run ids and cold replay.

Design goals:
- Runtime/Hub owns lifecycle state; agents never forge status via free text.
- Every action and result carries a ``call_id`` / ``correlation_id`` for
  idempotency, retry, and replay.
- Events are append-only and addressable by a monotonic ``sequence`` cursor
  so callers can ``wait`` for directed events from a linked run subtree.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .schemas import AgentType, ExecutionTarget


class ExecutorKind(str, Enum):
    """How a linked AgentRun child is executed (compat projection).

    ``managed_task`` wraps the workspace task/session/report flow.
    ``native_subagent`` is a future in-process subagent (stub for now).
    ``external_job`` is a future remote/third-party job (stub for now).
    ``resident_root`` is a legacy load-only enum value from pre-Task-Graph
    persistence; it is not a supervisor role and has no runtime semantics.
    """

    MANAGED_TASK = "managed_task"
    NATIVE_SUBAGENT = "native_subagent"
    EXTERNAL_JOB = "external_job"
    RESIDENT_ROOT = "resident_root"


class ExecutorCapabilities(BaseModel):
    """Durable snapshot of an executor adapter's public capabilities.

    A run stores this snapshot when it is created.  Keeping it on the run,
    instead of deriving it only from an in-memory adapter instance, makes the
    execution contract visible after a Hub restart and prevents a placeholder
    adapter from being presented as a real executor.
    """

    available: bool
    supports_spawn: bool = False
    supports_send: bool = False
    supports_followup: bool = False
    supports_interrupt: bool = False
    durable_status: bool = False
    supported_agent_types: List[AgentType] = Field(default_factory=list)
    model_configurable_agent_types: List[AgentType] = Field(default_factory=list)
    unavailable_reason: Optional[str] = None


class ManagedExecutorConfig(BaseModel):
    """Launch configuration for a real ``managed_task`` executor.

    ``agent_type`` selects the existing Hub CLI integration (Claude Code,
    Codex, or Cursor).  ``model`` is translated to the launch mechanism that
    the selected CLI actually supports; arbitrary command strings are
    intentionally not accepted.  The remaining fields map directly to the
    existing managed-session creation API.

    Defaults preserve the historical Agent Tree behavior: a legacy managed
    spawn without ``executor_config`` runs Claude in solo mode and inherits
    the workspace execution target.
    """

    agent_type: AgentType = AgentType.CLAUDE
    model: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)
    solo_mode: bool = True
    target: Optional[ExecutionTarget] = None
    cwd: Optional[str] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None


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
    # The concrete CLI/model launch contract for managed tasks.  This is
    # persisted with the run so crash recovery reuses the same executor
    # rather than falling back to a hard-coded Claude worker.
    executor_config: Optional[ManagedExecutorConfig] = None
    # Capability snapshot supplied by the adapter at creation time.  Legacy
    # persisted runs may not have one; their manager can backfill it on load.
    executor_capabilities: Optional[ExecutorCapabilities] = None
    status: AgentRunStatus = AgentRunStatus.PENDING
    # Opaque reference to the executor's context (e.g. workspace task id,
    # native subagent handle, external job id).
    context_ref: Optional[str] = None
    # Last event sequence acknowledged by this run's supervisor. Persisted
    # so a restarted supervisor can resume from where it left off.
    ack_sequence: int = 0
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

    ``action`` and ``target`` record the user-facing action (e.g. ``spawn``,
    ``send``, ``followup``, ``interrupt``) and the run id it targets, so the
    call_id idempotency index can be rebuilt after a restart and reject
    mismatched call_id reuse.

    ``fingerprint`` is a hash of the full request payload that produced this
    event. It is persisted so that a restarted process can still detect a
    call_id reused with a different request body (not just a different
    action/target).
    """

    sequence: int
    call_id: str
    # Optional correlation id tying a request to its response events.
    correlation_id: Optional[str] = None
    agent_run_id: str
    type: AgentEventType
    author: str
    # Recipient is mandatory: every event is directed to exactly one run.
    # Root runs (supervisor_id is None) self-address their events so the
    # directed mailbox filter (e.recipient == run_id) still delivers them.
    recipient: str
    # The action that produced this event (e.g. "spawn", "send", "followup",
    # "interrupt", "emit"). Used for call_id idempotency namespacing.
    action: Optional[str] = None
    # The target run id of the action. Used for call_id idempotency
    # namespacing.
    target: Optional[str] = None
    # Full request fingerprint (SHA-256 of the canonicalized request body).
    # Persisted so call_id reuse with a different payload is detected even
    # after a process restart.
    fingerprint: Optional[str] = None
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

    ``session_id`` is an optional hint for the managed-task adapter: when
    provided, the task is dispatched to that specific orchestrator session
    (used by the resident master mode to route work to existing workers).
    """

    workspace_id: str
    parent_id: str
    executor_kind: ExecutorKind
    executor_config: Optional[ManagedExecutorConfig] = None
    title: Optional[str] = None
    initial_message: str
    call_id: str
    context_ref: Optional[str] = None
    session_id: Optional[str] = None


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
