"""Session management for Claude Hub."""

import json
import logging
import os
import secrets
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import settings
from ..models.schemas import LoginSession, User

logger = logging.getLogger(__name__)

# Session storage file
SESSIONS_FILE = Path.home() / ".claude_hub" / "sessions.json"

# The file holds bearer session ids and Feishu tokens, so it must be
# readable/writable by the owner only.
_SESSIONS_FILE_MODE = 0o600
# Its directory must be owner-only as well: it also holds any 0600 temp file
# left behind by an interrupted atomic rewrite, and a group/world-searchable
# directory would expose the file's existence and metadata.
_SESSIONS_DIR_MODE = 0o700


def _ensure_sessions_dir() -> None:
    """Ensure the sessions directory exists and is owner-only.

    The directory is created (if missing) and then explicitly chmod-ed: the
    ``mode`` argument to ``mkdir`` is still filtered by the process umask, and
    a directory created by an older release may be looser than 0700.
    """
    sessions_dir = SESSIONS_FILE.parent
    sessions_dir.mkdir(parents=True, exist_ok=True)
    try:
        current_mode = sessions_dir.stat().st_mode & 0o777
        if current_mode != _SESSIONS_DIR_MODE:
            os.chmod(sessions_dir, _SESSIONS_DIR_MODE)
    except OSError as e:
        logger.warning(f"Failed to tighten sessions directory permissions: {e}")


def _tighten_file_permissions() -> None:
    """Restrict an existing sessions file to owner-only access.

    Older releases wrote the file with the process umask (typically 0664);
    tighten it back to 0600 whenever the store is read.
    """
    try:
        current_mode = SESSIONS_FILE.stat().st_mode & 0o777
        if current_mode != _SESSIONS_FILE_MODE:
            os.chmod(SESSIONS_FILE, _SESSIONS_FILE_MODE)
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning(f"Failed to tighten sessions file permissions: {e}")


def _load_sessions() -> Dict[str, Any]:
    """Load sessions from file."""
    _ensure_sessions_dir()
    if not SESSIONS_FILE.exists():
        return {}
    _tighten_file_permissions()
    try:
        with open(SESSIONS_FILE, "r") as f:
            result: Dict[str, Any] = json.load(f)
            return result
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load sessions: {e}")
        return {}


def _save_sessions(sessions: Dict[str, Any]) -> None:
    """Save sessions to file atomically with owner-only permissions."""
    _ensure_sessions_dir()
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{SESSIONS_FILE.name}.",
            suffix=".tmp",
            dir=str(SESSIONS_FILE.parent),
        )
    except OSError as e:
        logger.error(f"Failed to save sessions: {e}")
        return

    replaced = False
    try:
        # mkstemp already creates the file 0600, but enforce it explicitly.
        os.fchmod(fd, _SESSIONS_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            json.dump(sessions, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, SESSIONS_FILE)
        replaced = True
    except OSError as e:
        logger.error(f"Failed to save sessions: {e}")
    finally:
        if not replaced:
            with suppress(OSError):
                os.unlink(tmp_name)


def _session_to_dict(session: LoginSession) -> Dict[str, Any]:
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


def _dict_to_session(data: Dict[str, Any]) -> Optional[LoginSession]:
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
