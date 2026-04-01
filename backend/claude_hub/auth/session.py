"""Session management for Claude Hub."""

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict
import logging

from ..config import settings
from ..models.schemas import User, LoginSession

logger = logging.getLogger(__name__)

# Session storage file
SESSIONS_FILE = Path.home() / ".claude_hub" / "sessions.json"


def _ensure_sessions_dir() -> None:
    """Ensure the sessions directory exists."""
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_sessions() -> Dict[str, dict]:
    """Load sessions from file."""
    _ensure_sessions_dir()
    if not SESSIONS_FILE.exists():
        return {}
    try:
        with open(SESSIONS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load sessions: {e}")
        return {}


def _save_sessions(sessions: Dict[str, dict]) -> None:
    """Save sessions to file."""
    _ensure_sessions_dir()
    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f)
    except IOError as e:
        logger.error(f"Failed to save sessions: {e}")


def _session_to_dict(session: LoginSession) -> dict:
    """Convert LoginSession to dict for storage."""
    return {
        "session_id": session.session_id,
        "user": {
            "open_id": session.user.open_id,
            "name": session.user.name,
            "email": session.user.email,
            "avatar_url": session.user.avatar_url,
        },
        "created_at": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "feishu_access_token": session.feishu_access_token,
        "feishu_refresh_token": session.feishu_refresh_token,
    }


def _dict_to_session(data: dict) -> Optional[LoginSession]:
    """Convert dict to LoginSession."""
    try:
        return LoginSession(
            session_id=data["session_id"],
            user=User(**data["user"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            feishu_access_token=data["feishu_access_token"],
            feishu_refresh_token=data["feishu_refresh_token"],
        )
    except (KeyError, ValueError, TypeError) as e:
        logger.warning(f"Failed to parse session: {e}")
        return None


def create_session(
    user: User,
    feishu_access_token: str,
    feishu_refresh_token: str,
) -> LoginSession:
    """Create a new session."""
    session_id = secrets.token_urlsafe(32)
    now = datetime.now()
    expires_at = now + timedelta(days=settings.session_expire_days)

    session = LoginSession(
        session_id=session_id,
        user=user,
        created_at=now,
        expires_at=expires_at,
        feishu_access_token=feishu_access_token,
        feishu_refresh_token=feishu_refresh_token,
    )

    sessions = _load_sessions()
    sessions[session_id] = _session_to_dict(session)
    _save_sessions(sessions)

    logger.info(f"Created session for user: {user.email}")
    return session


def get_session(session_id: str) -> Optional[LoginSession]:
    """Get a session by ID. Returns None if session not found or expired."""
    sessions = _load_sessions()
    session_data = sessions.get(session_id)
    if not session_data:
        return None

    session = _dict_to_session(session_data)
    if not session:
        return None

    if datetime.now() > session.expires_at:
        logger.info(f"Session expired for user: {session.user.email}")
        delete_session(session_id)
        return None

    return session


def delete_session(session_id: str) -> None:
    """Delete a session."""
    sessions = _load_sessions()
    if session_id in sessions:
        session_data = sessions.pop(session_id)
        _save_sessions(sessions)
        user_email = session_data.get("user", {}).get("email", "unknown")
        logger.info(f"Deleted session for user: {user_email}")


def cleanup_expired_sessions() -> int:
    """Remove all expired sessions. Returns number of sessions removed."""
    sessions = _load_sessions()
    now = datetime.now()
    removed = 0

    for session_id, session_data in list(sessions.items()):
        try:
            expires_at = datetime.fromisoformat(session_data["expires_at"])
            if now > expires_at:
                del sessions[session_id]
                removed += 1
        except (KeyError, ValueError):
            del sessions[session_id]
            removed += 1

    if removed > 0:
        _save_sessions(sessions)
        logger.info(f"Cleaned up {removed} expired sessions")

    return removed
