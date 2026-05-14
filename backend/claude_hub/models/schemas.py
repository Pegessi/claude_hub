from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class AgentType(str, Enum):
    """Type of agent to run in the terminal."""

    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"


class ExecutionTarget(str, Enum):
    """Where the terminal command should run."""

    LOCAL = "local"
    REMOTE = "remote"


class AgentRuntimeStatus(str, Enum):
    """Best-effort runtime status for a terminal agent."""

    IDLE = "idle"
    WORKING = "working"
    ATTENTION = "attention"
    OFFLINE = "offline"


class WorkspaceSessionRole(str, Enum):
    """Role of a managed workspace session."""

    WORKER = "worker"
    ORCHESTRATOR = "orchestrator"
    DISPATCHER = "dispatcher"


class WorkspaceTaskStatus(str, Enum):
    """Status of a human-orchestrated workspace task."""

    TODO = "todo"
    QUEUED = "queued"
    WORKING = "working"
    REVIEW = "review"
    DONE = "done"


class ManagedSessionStatus(str, Enum):
    """Lifecycle status for a managed workspace session."""

    SPAWNING = "spawning"
    WORKING = "working"
    IDLE = "idle"
    NEEDS_INPUT = "needs_input"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"


class AgentReportState(str, Enum):
    """Self-reported workflow state from a managed worker session."""

    STARTED = "started"
    WORKING = "working"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    READY_FOR_REVIEW = "ready_for_review"
    COMPLETED = "completed"


class TerminalTabBase(BaseModel):
    """Base schema for TerminalTab."""

    name: str = Field(..., description="Name of the terminal tab")
    shell: Optional[str] = Field(None, description="Shell to use (default: $SHELL)")
    cwd: Optional[str] = Field(None, description="Working directory to start the terminal in")
    solo_mode: bool = Field(False, description="Whether to start in agent solo mode")
    agent_type: AgentType = Field(AgentType.CLAUDE, description="Type of agent to run")
    target: ExecutionTarget = Field(ExecutionTarget.LOCAL, description="Where to run the tab")
    remote_profile_id: Optional[str] = Field(None, description="Remote profile ID for remote tabs")
    remote_cwd: Optional[str] = Field(None, description="Remote working directory")
    remote_reconnect: bool = Field(True, description="Reconnect SSH automatically for remote tabs")


class TerminalTabCreate(TerminalTabBase):
    """Schema for creating a TerminalTab."""

    pass


class TerminalTabUpdate(BaseModel):
    """Schema for updating a TerminalTab."""

    name: Optional[str] = None
    shell: Optional[str] = None
    cwd: Optional[str] = None
    solo_mode: Optional[bool] = None
    agent_type: Optional[AgentType] = None
    target: Optional[ExecutionTarget] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None


class TerminalTab(TerminalTabBase):
    """Schema for returning a TerminalTab."""

    id: str
    port: int
    created_at: datetime
    is_active: bool
    workspace_id: Optional[str] = Field(None, description="Workspace that created this tab")
    workspace_name: Optional[str] = Field(None, description="Display name of the owning workspace")
    workspace_role: Optional[WorkspaceSessionRole] = Field(
        None,
        description="Managed workspace role for this tab",
    )

    class Config:
        from_attributes = True


class TerminalAgentStatus(BaseModel):
    """Status summary for the floating terminal agent panel."""

    tab_id: str
    tab_name: str
    agent_type: AgentType
    status: AgentRuntimeStatus
    status_text: str
    detail: Optional[str] = None
    tmux_session: str
    last_changed_at: Optional[datetime] = None
    sampled_at: datetime


class RemoteProfile(BaseModel):
    """Configured SSH target for remote tabs."""

    id: str
    name: str
    ssh_host: str
    user: Optional[str] = None
    port: int = 22
    default_cwd: Optional[str] = None


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
    created_at: datetime
    updated_at: datetime


class WorkspaceTaskCreate(BaseModel):
    """Payload for creating a workspace task."""

    title: str
    prompt: str
    agent_type: AgentType = AgentType.CODEX
    related_task_id: Optional[str] = None


class WorkspaceTaskUpdate(BaseModel):
    """Payload for updating a workspace task."""

    status: Optional[WorkspaceTaskStatus] = None


class WorkspaceTask(BaseModel):
    """Task tracked by Agent Workspace mode."""

    id: str
    workspace_id: str
    title: str
    prompt: str
    agent_type: AgentType
    status: WorkspaceTaskStatus
    session_id: Optional[str] = None
    related_task_id: Optional[str] = None
    clear_context: Optional[bool] = None
    dispatch_reason: Optional[str] = None
    dispatch_pending: bool = False
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ManagedSession(BaseModel):
    """Managed agent session backed by a terminal tab."""

    id: str
    workspace_id: str
    task_id: Optional[str] = None
    tab_id: str
    role: WorkspaceSessionRole
    agent_type: AgentType
    status: ManagedSessionStatus
    runtime_status: AgentRuntimeStatus = AgentRuntimeStatus.IDLE
    current_task_id: Optional[str] = None
    queued_count: int = 0
    title: str
    branch: Optional[str] = None
    workspace_path: str
    tmux_session: str
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    solo_mode: bool = True
    remote_forward_port: Optional[int] = None
    auto_continue_task_id: Optional[str] = None
    auto_continue_attempts: int = 0
    last_auto_continue_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    last_activity_at: Optional[datetime] = None


class AgentReportCreate(BaseModel):
    """Payload for appending a worker progress report."""

    state: AgentReportState
    message: str
    task_id: Optional[str] = None
    changed_files: List[str] = Field(default_factory=list)
    validation: Optional[str] = None
    risks: Optional[str] = None


class AgentReport(BaseModel):
    """Progress report recorded against a managed session."""

    id: str
    workspace_id: str
    task_id: Optional[str] = None
    session_id: str
    state: AgentReportState
    message: str
    changed_files: List[str] = Field(default_factory=list)
    validation: Optional[str] = None
    risks: Optional[str] = None
    created_at: datetime


class WorkspaceBoard(BaseModel):
    """Workspace board response for Agent Workspace mode."""

    workspace: Workspace
    tasks: List[WorkspaceTask]
    sessions: List[ManagedSession]
    reports: List[AgentReport]
    snapshot_path: Optional[str] = None


class SpawnWorkerRequest(BaseModel):
    """Payload for spawning a worker session for a task."""

    agent_type: Optional[AgentType] = None


class EnsureWorkspaceAgentRequest(BaseModel):
    """Payload for ensuring a resident workspace agent session."""

    agent_type: AgentType = AgentType.CODEX
    title: Optional[str] = None
    role: WorkspaceSessionRole = WorkspaceSessionRole.ORCHESTRATOR
    reuse_existing: bool = False
    cwd: Optional[str] = None
    solo_mode: bool = True
    target: Optional[ExecutionTarget] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None


class StartTaskRequest(BaseModel):
    """Payload for queueing a task and optionally overriding dispatch."""

    agent_type: Optional[AgentType] = None
    target_session_id: Optional[str] = None
    clear_context: Optional[bool] = None
    related_task_id: Optional[str] = None


class ContinueTaskRequest(BaseModel):
    """Payload for continuing a task from review with its original agent."""

    message: Optional[str] = None


class DispatchDecisionRequest(BaseModel):
    """Structured dispatch decision produced by a dispatcher agent."""

    target_session_id: str
    clear_context: bool = False
    reason: Optional[str] = None


class SendSessionMessageRequest(BaseModel):
    """Payload for sending a message to a managed session."""

    message: str


class User(BaseModel):
    """Schema for a user."""

    open_id: str
    name: str
    email: str
    avatar_url: Optional[str] = None


class LoginSession(BaseModel):
    """Schema for a login session."""

    session_id: str
    user: User
    created_at: datetime
    expires_at: datetime
    feishu_access_token: str
    feishu_refresh_token: str


class DirectoryListing(BaseModel):
    """Schema for directory listing."""

    path: str
    files: List["FileInfo"]


class FileInfo(BaseModel):
    """Schema for file information."""

    name: str
    path: str
    type: str  # "file" or "directory"
    is_dir: bool
