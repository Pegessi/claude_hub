"""Authentication module for Claude Hub."""

from .dependencies import get_current_user, optional_user
from .feishu import (
    get_feishu_auth_url,
    get_user_access_token,
    get_user_info,
    refresh_user_access_token,
)
from .session import (
    cleanup_expired_sessions,
    create_session,
    delete_session,
    get_session,
)

__all__ = [
    "get_feishu_auth_url",
    "get_user_access_token",
    "refresh_user_access_token",
    "get_user_info",
    "create_session",
    "get_session",
    "delete_session",
    "cleanup_expired_sessions",
    "get_current_user",
    "optional_user",
]
