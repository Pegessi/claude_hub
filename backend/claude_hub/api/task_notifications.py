"""Hand Task notices to Dot, which decides whether the person hears them.

The workspace monitor decides *when* a Task needs a person (see
``services/workspace_manager/monitoring/liveness.py``). Dot judges whether
and how to tell the user; the Hub never messages the user on its own.
"""

import hashlib
import logging
from datetime import datetime

from ..models import WorkspaceTask
from ..services.agent_stream.redaction import mask_secrets
from ..services.task_lifecycle import TaskNotice

logger = logging.getLogger(__name__)

# Reasons Dot's own work watcher already raises for Tasks it tracks (worker or
# reviewer questions, results awaiting acceptance, failures). Everything else,
# e.g. an agent stuck on an approval prompt, is forwarded.
_WATCHER_COVERED_REASONS = frozenset(
    {
        "reviewer_needs_input",
        "awaiting_acceptance",
        "review_skipped",
        "initiator_ready",
        "failed",
    }
)


async def deliver_task_notice(task: WorkspaceTask, notice: TaskNotice) -> str:
    try:
        from ..services.dot import get_dot_service

        dot = get_dot_service()
        if not dot.is_set_up():
            return "unroutable"
        if task.system_internal:
            # Hub housekeeping is not something the user asked for.
            return "unroutable"
        item = dot.store.get_work(task.id)
        tracked = item is not None and not item.get("stopped")
        if tracked and notice.kind == "attention" and notice.reason in _WATCHER_COVERED_REASONS:
            # Dot already hears about this from its own work watcher.
            return "unroutable"
        kind = "task_plan" if notice.kind == "plan" else "task_attention"
        if notice.reason == "liveness_attention":
            kind = "task_stalled"
        text = mask_secrets(f"{notice.title} — {notice.text}")
        digest = hashlib.sha256(notice.text.encode("utf-8")).hexdigest()[:12]
        key = f"{kind}:{task.id}:{notice.phase}:{notice.reason}:{digest}"
        if notice.reason == "liveness_attention" and task.liveness:
            key += f":{task.liveness.generation}:{task.liveness.incident}"
        received = await dot.receive_hub_notice(task.id, kind, text, key)
    except Exception:
        logger.exception("Handing task notice to Dot failed task_id=%s", task.id)
        return "failed"
    return "sent" if received else "unroutable"


def task_liveness_watch(task: WorkspaceTask) -> tuple[datetime, float] | None:
    """Supply persisted watch configuration; all timeout decisions live in Hub."""
    from ..services.dot import get_dot_service
    from ..services.dot.runtime.common import DEFAULT_STALL_HOURS, _parse_time

    item = get_dot_service().store.get_work(task.id)
    if item is None or item.get("stopped") or item.get("closed"):
        return None
    started = _parse_time(item.get("watched_at") or item.get("created"))
    if started is None:
        return None
    # Dot stores UTC-aware ISO timestamps; Task clocks are local naive UTC.
    return started.replace(tzinfo=None), float(item.get("stall_hours") or DEFAULT_STALL_HOURS)
