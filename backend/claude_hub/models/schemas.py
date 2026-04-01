from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class TerminalTabBase(BaseModel):
    """Base schema for TerminalTab."""
    name: str = Field(..., description="Name of the terminal tab")
    shell: Optional[str] = Field(None, description="Shell to use (default: $SHELL)")
    cwd: Optional[str] = Field(None, description="Working directory to start the terminal in")
    solo_mode: bool = Field(False, description="Whether to start in Claude solo mode")


class TerminalTabCreate(TerminalTabBase):
    """Schema for creating a TerminalTab."""
    pass


class TerminalTabUpdate(BaseModel):
    """Schema for updating a TerminalTab."""
    name: Optional[str] = None
    shell: Optional[str] = None
    cwd: Optional[str] = None
    solo_mode: Optional[bool] = None


class TerminalTab(TerminalTabBase):
    """Schema for returning a TerminalTab."""
    id: str
    port: int
    created_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


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
