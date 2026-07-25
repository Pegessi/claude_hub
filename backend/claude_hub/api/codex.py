"""Codex session listing API.

Exposes the local Codex sessions stored under ``~/.codex/sessions/`` and
``~/.codex/archived_sessions/`` so the frontend can offer a "resume session"
picker when creating a new Codex tab.
"""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from ..auth.dependencies import get_current_user
from ..models import User
from ..services.ttyd_manager import list_codex_sessions as _list_codex_sessions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/codex", tags=["codex"])


@router.get("/sessions", response_model=List[Dict[str, Any]])
async def list_codex_sessions(
    current_user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """List available local Codex sessions grouped by working directory.

    Each entry has a ``cwd`` and a ``sessions`` list. Sessions within a group
    are sorted most-recent-first and include ``session_id``, ``cwd``,
    ``start_time`` (ISO-8601), and ``title`` (the first real user message).
    Walks both active (``sessions/``) and archived (``archived_sessions/``)
    locations since ``codex resume <id>`` works against both.
    """
    logger.info(f"Listing codex sessions, user={current_user.email}")
    return _list_codex_sessions()
