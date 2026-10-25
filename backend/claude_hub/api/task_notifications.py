"""Hand Task notices to Dot, which decides whether the person hears them.

The workspace monitor decides *when* a Task needs a person (see
``services/workspace_manager/monitoring/lifecycle.py``). Dot judges whether
and how to tell the user; the Hub never messages the user on its own.
"""

import hashlib
import logging

from ..models import WorkspaceTask
from ..services.agent_stream.redaction import mask_secrets
from ..services.task_lifecycle import TaskNotice

logger = logging.getLogger(__name__)

# Dot's own watcher already raises these for Tasks it tracks.
_WATCHER_COVERED_PHASES = frozenset({"needs_user", "awaiting_acceptance", "failed"})


async def deliver_task_notice(task: WorkspaceTask, notice: TaskNotice) -> str:
    try:
        from ..services.dot import get_dot_service

        dot = get_dot_service()
        if not dot.is_set_up():
            return "unroutable"
        tracked = dot.store.get_work(task.id) is not None
        if tracked and notice.kind == "attention" and notice.phase in _WATCHER_COVERED_PHASES:
            # Dot already hears about this from its own work watcher.
            return "unroutable"
        kind = "task_plan" if notice.kind == "plan" else "task_attention"
        text = mask_secrets(f"{notice.title} — {notice.text}")
        digest = hashlib.sha256(notice.text.encode("utf-8")).hexdigest()[:12]
        key = f"{kind}:{task.id}:{notice.phase}:{notice.reason}:{digest}"
        received = await dot.receive_hub_notice(task.id, kind, text, key)
    except Exception:
        logger.exception("Handing task notice to Dot failed task_id=%s", task.id)
        return "failed"
    return "sent" if received else "unroutable"
