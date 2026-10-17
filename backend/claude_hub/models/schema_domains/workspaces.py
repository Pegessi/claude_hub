"""Workspace configuration schemas."""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from claude_hub.models.schema_domains.runtime import AgentType, ExecutionTarget
from claude_hub.models.schema_domains.terminal import ResidentPeriodicTask


class WorkspaceCreate(BaseModel):
    """Payload for creating an agent workspace."""

    name: str
    path: str
    default_branch: str = "main"
    session_prefix: Optional[str] = None
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    resident_agent_enabled: bool = False
    resident_agent_paused: bool = False
    resident_agent_interval_minutes: int = 60
    resident_agent_directive: Optional[str] = None
    resident_agent_periodic_tasks: List[ResidentPeriodicTask] = Field(default_factory=list)
    resident_agent_type: AgentType = AgentType.CLAUDE
    resident_agent_env: Dict[str, str] = Field(default_factory=dict)
    resident_agent_solo_mode: bool = True
    resident_agent_master_mode: bool = False
    resident_agent_title: Optional[str] = None
    resident_agent_target: ExecutionTarget = ExecutionTarget.LOCAL
    resident_agent_remote_profile_id: Optional[str] = None
    resident_agent_cwd: Optional[str] = None
    resident_agent_remote_reconnect: bool = True
    allow_duplicate: bool = False


class WorkspaceEnsure(BaseModel):
    """Payload for ensuring a workspace exists for a repo identity."""

    name: Optional[str] = None
    path: str
    default_branch: str = "main"
    session_prefix: Optional[str] = None
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    create_if_missing: bool = True
    allow_duplicate: bool = False


class Workspace(BaseModel):
    """Agent workspace configuration."""

    id: str
    name: str
    path: str
    default_branch: str
    session_prefix: str
    dispatcher_session_id: Optional[str] = None
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    resident_agent_enabled: bool = False
    resident_agent_paused: bool = False
    resident_agent_interval_minutes: int = 60
    resident_agent_session_id: Optional[str] = None
    resident_agent_directive: Optional[str] = None
    resident_agent_periodic_tasks: List[ResidentPeriodicTask] = Field(default_factory=list)
    resident_agent_last_run_at: Optional[datetime] = None
    # Server-managed scheduling hints. ``run_requested_at`` is set by the
    # run-now endpoint to force a single manual cycle on the next monitor tick
    # (cleared once the resident actually runs). ``next_run_at`` is the computed
    # overdue-backstop time (last_run + interval + jitter) surfaced to the UI so
    # users can see WHEN the resident will next wake on its own.
    resident_agent_run_requested_at: Optional[datetime] = None
    resident_agent_next_run_at: Optional[datetime] = None
    resident_agent_type: AgentType = AgentType.CLAUDE
    resident_agent_env: Dict[str, str] = Field(default_factory=dict)
    resident_agent_solo_mode: bool = True
    resident_agent_master_mode: bool = False
    resident_agent_title: Optional[str] = None
    resident_agent_target: ExecutionTarget = ExecutionTarget.LOCAL
    resident_agent_remote_profile_id: Optional[str] = None
    resident_agent_cwd: Optional[str] = None
    resident_agent_remote_reconnect: bool = True
    created_at: datetime
    updated_at: datetime


class WorkspaceUpdate(BaseModel):
    """Editable fields for an existing workspace."""

    name: Optional[str] = None
    path: Optional[str] = None
    default_branch: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None
    resident_agent_enabled: Optional[bool] = None
    resident_agent_paused: Optional[bool] = None
    resident_agent_interval_minutes: Optional[int] = None
    resident_agent_directive: Optional[str] = None
    resident_agent_periodic_tasks: Optional[List[ResidentPeriodicTask]] = None
    resident_agent_type: Optional[AgentType] = None
    resident_agent_env: Optional[Dict[str, str]] = None
    resident_agent_solo_mode: Optional[bool] = None
    resident_agent_master_mode: Optional[bool] = None
    resident_agent_title: Optional[str] = None
    resident_agent_target: Optional[ExecutionTarget] = None
    resident_agent_remote_profile_id: Optional[str] = None
    resident_agent_cwd: Optional[str] = None
    resident_agent_remote_reconnect: Optional[bool] = None
