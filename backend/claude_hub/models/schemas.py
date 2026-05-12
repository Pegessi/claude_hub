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
