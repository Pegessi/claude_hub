"""Authentication, filesystem, and environment-preset schemas."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


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


# ---------------------------------------------------------------------------
# Environment-variable presets (cross-origin persisted)
# ---------------------------------------------------------------------------


class EnvPreset(BaseModel):
    """A saved named environment-variable preset."""

    id: str
    name: str
    text: str  # KEY=VALUE newline-delimited text, same format as the textarea


class EnvPresetCreate(BaseModel):
    """Payload for creating a new custom env preset."""

    name: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class EnvPresetUpdate(BaseModel):
    """Payload for updating an existing custom env preset (all fields optional)."""

    name: Optional[str] = Field(None, min_length=1)
    text: Optional[str] = Field(None, min_length=1)


class EnvPresetHiddenRequest(BaseModel):
    """Payload to hide or unhide a built-in preset."""

    hidden: bool


class EnvPresetBulkImport(BaseModel):
    """Bulk-import payload used for one-time localStorage → backend migration."""

    custom_presets: List[EnvPreset] = Field(default_factory=list)
    hidden_builtin_ids: List[str] = Field(default_factory=list)


class EnvPresetsResponse(BaseModel):
    """Full state response for env presets."""

    custom_presets: List[EnvPreset]
    hidden_builtin_ids: List[str]
