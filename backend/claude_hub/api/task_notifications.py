"""Deliver Task notices to the people who can act on them.

The workspace monitor decides *when* a Task needs a person (see
``services/workspace_manager/monitoring/lifecycle.py``); this module decides
*where*: the Feishu chat bound to the Task's source Chat, or to Dot when Dot
tracks the Task. It never depends on a model choosing to notify.
"""

import logging
from typing import Optional

from ..models import WorkspaceTask
from ..services.agent_stream.redaction import mask_secrets
from ..services.task_lifecycle import TaskNotice
from .feishu_bot import feishu_bound_to_tab, push_to_tab

logger = logging.getLogger(__name__)


def _dot_tab_tracking(task_id: str) -> Optional[str]:
    try:
        from ..services.dot import get_dot_service

        dot = get_dot_service()
        if not dot.is_set_up() or dot.store.get_work(task_id) is None:
            return None
        return dot.tab_id()
    except Exception:
        logger.debug("Dot lookup for task notice failed task_id=%s", task_id, exc_info=True)
        return None


def notice_recipient_tabs(task: WorkspaceTask) -> list[str]:
    """Candidate tabs in priority order: the source Chat, then Dot."""

    candidates = [task.source.tab_id if task.source else None, _dot_tab_tracking(task.id)]
    return list(dict.fromkeys(tab for tab in candidates if tab))


async def deliver_task_notice(task: WorkspaceTask, notice: TaskNotice) -> str:
    bound = [tab for tab in notice_recipient_tabs(task) if feishu_bound_to_tab(tab)]
    if not bound:
        return "unroutable"
    text = mask_secrets(notice.text)
    title = mask_secrets(notice.title)
    for tab_id in bound:
        if await push_to_tab(tab_id, text, title=title, tone=notice.tone):
            logger.info(
                "Task notice sent task_id=%s kind=%s tab_id=%s", task.id, notice.kind, tab_id
            )
            return "sent"
    return "failed"
