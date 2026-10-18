"""Scheduled-task schemas."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from claude_hub.models.schema_domains.runtime import AgentType


class ScheduledTaskKind(str, Enum):
    """How a scheduled task is executed when it fires."""

    CHAT_TURN = "chat_turn"
    """Queue a native turn in an existing top-level Chat conversation."""

    TAB_MESSAGE = "tab_message"
    """Send a message to a terminal tab (agent self-scheduling)."""

    NEW_SESSION = "new_session"
    """Create a new session and send it a message (manual one-off execution)."""

    HUB_TASK = "hub_task"
    """Publish a Hub-native task that runs and auto-cleans (no held resources)."""


class ScheduledTaskRunStatus(str, Enum):
    """Durable lifecycle of one scheduled Chat turn."""

    QUEUED = "queued"
    WAITING = "waiting"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class ScheduledTaskRun(BaseModel):
    """One durable occurrence of a ``chat_turn`` schedule."""

    id: str
    scheduled_task_id: str
    tab_id: str
    client_turn_id: str
    message: str
    status: ScheduledTaskRunStatus = ScheduledTaskRunStatus.QUEUED
    scheduled_for: datetime
    queued_at: datetime
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    waiting_reason: Optional[str] = None
    error: Optional[str] = None
    # Number of times the run was parked WAITING because the target Chat's
    # native runtime was cold/not yet ready. Bounds cold-start requeue so a
    # runtime that never comes up fails the run instead of retrying forever.
    dispatch_attempts: int = 0


class ScheduledTask(BaseModel):
    """A durable schedule that fires an action on a cron / interval / one-off basis.

    The schedule spec is exactly one of ``run_at`` (one-shot), ``cron``
    (5-field), or ``interval_seconds``. The action payload depends on ``kind``:

    * ``chat_turn``: ``tab_id`` + ``message`` (queued into that native Chat
      conversation and rendered in its structured transcript).
    * ``tab_message``: ``tab_id`` + ``message`` (typed into that terminal tab's
      pane and submitted).
    * ``new_session``: ``workspace_id`` + ``agent_type`` + ``message`` (a new
      session is created and the message sent to it).
    * ``hub_task``: ``workspace_id`` + ``agent_type`` + ``task_title`` +
      ``message`` (the task prompt); a system-internal task is published, runs
      on a caller-owned ephemeral orchestrator, and the session is auto-deleted
      once the task is DONE so no agent / reviewer resources are held.

    For the session-creating kinds (``new_session`` / ``hub_task``)
    ``env_preset`` optionally names a built-in or saved custom env preset
    (by id or name) used to launch the worker session. Without it the worker
    gets the agent type's built-in default launch environment.
    """

    model_config = {"extra": "forbid"}

    id: str
    name: str
    kind: ScheduledTaskKind
    enabled: bool = True

    # Schedule spec — exactly one.
    run_at: Optional[datetime] = None
    cron: Optional[str] = None
    interval_seconds: Optional[int] = Field(default=None, ge=1)

    # Action payload.
    tab_id: Optional[str] = None
    workspace_id: Optional[str] = None
    agent_type: AgentType = AgentType.CLAUDE
    message: Optional[str] = None
    task_title: Optional[str] = None
    # Env preset (id/name) for the spawned worker session; only valid for
    # new_session / hub_task kinds.
    env_preset: Optional[str] = None

    # Runtime state.
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_run_id: Optional[str] = None
    run_count: int = 0
    created_at: datetime
    updated_at: datetime


class ScheduledTaskCreate(BaseModel):
    """Payload for creating a scheduled task."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1)
    kind: ScheduledTaskKind
    enabled: bool = True

    run_at: Optional[datetime] = None
    cron: Optional[str] = None
    interval_seconds: Optional[int] = Field(default=None, ge=1)

    tab_id: Optional[str] = None
    workspace_id: Optional[str] = None
    agent_type: AgentType = AgentType.CLAUDE
    message: Optional[str] = None
    task_title: Optional[str] = None
    env_preset: Optional[str] = None


class ScheduledTaskUpdate(BaseModel):
    """Payload for updating a scheduled task (all fields optional).

    ``kind`` is immutable; delete and recreate to change it. When any schedule
    field is supplied the next-run time is recomputed.
    """

    model_config = {"extra": "forbid"}

    name: Optional[str] = Field(default=None, min_length=1)
    enabled: Optional[bool] = None

    run_at: Optional[datetime] = None
    cron: Optional[str] = None
    interval_seconds: Optional[int] = Field(default=None, ge=1)

    tab_id: Optional[str] = None
    workspace_id: Optional[str] = None
    agent_type: Optional[AgentType] = None
    message: Optional[str] = None
    task_title: Optional[str] = None
    env_preset: Optional[str] = None


class ScheduledTaskRunResult(BaseModel):
    """Result of a manual run-now (fire immediately) request."""

    id: str
    last_run_id: Optional[str] = None
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None


class ScheduledTaskView(ScheduledTask):
    """A scheduled task with live derived run/backlog counts (not persisted)."""

    # Occurrences that have not reached a terminal state (queued/waiting/
    # dispatching/running).
    active_run_count: int = 0
    # Occurrences piled up behind the one turn a target Chat can accept
    # (queued/waiting), i.e. the backlog that would replay when it frees up.
    queued_run_count: int = 0
    # The single occurrence currently dispatched to / running on the provider.
    in_flight_run_count: int = 0
    # Id of the in-flight (oldest dispatching/running) run, if any — lets the
    # UI cancel a wedged run directly.
    in_flight_run_id: Optional[str] = None
    # When that in-flight run was dispatched (or queued if dispatch stamp is
    # absent); used by the UI to flag wedged runs.
    in_flight_since: Optional[datetime] = None
