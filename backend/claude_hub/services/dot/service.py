"""Compatibility entrypoint and singleton owner for the Dot service."""

from datetime import datetime
from typing import Optional

from claude_hub.services.dot.runtime.common import (
    _ATTENTION_REPORT_STATES,
    _JOURNAL_TEXT_LIMIT,
    _MANUAL_REVIEW_REASON,
    _QUESTION_REPORT_STATES,
    _REAPER_REASON_SUFFIX,
    _RETURN_REPORT_STATES,
    _SENT_TURNS_KEPT,
    _STALL_TASK_STATES,
    _TERMINAL_TASK_STATES,
    BRIEF_TEMPLATE,
    DEFAULT_STALL_HOURS,
    FOCUS_LIMIT,
    MAX_SESSION_AGE,
    MAX_SESSION_TURNS,
    QUIET_REPLY,
    RESULT_TEXT_LIMIT,
    REVIEW_CAUSES,
    REVIEW_VERDICTS,
    STATS_WINDOW,
    TAB_STREAM_SESSION_PREFIX,
    TAB_STREAM_WORKSPACE_ID,
    TIDY_CRON,
    TIDY_MESSAGE,
    WAKE_INTERVAL_SECONDS,
    WAKE_MESSAGE,
    WATCH_INTERVAL_SECONDS,
    Any,
    Card,
    DelegateNotStarted,
    DotIndex,
)
from claude_hub.services.dot.runtime.common import DotStore as _RuntimeDotStore
from claude_hub.services.dot.runtime.common import (
    LessonNotFound,
    Optional,
    TurnPack,
    TurnPlan,
    _checklist,
    _clip_text,
    _continue_report,
    _ended_at,
    _first_line,
    _hub_report,
    _parse_time,
    _report_state,
    _sent_turns,
    _worker_report,
    _worker_return,
    utc_now,
)
from claude_hub.services.dot.runtime.service import DotService as _RuntimeDotService
from claude_hub.services.dot.store import DotStore


class DotService(_RuntimeDotService):
    """Dot runtime with legacy module dependency hooks."""

    def __init__(self, store: Optional[_RuntimeDotStore] = None) -> None:
        super().__init__(store or DotStore())

    def _utc_now(self) -> datetime:
        return utc_now()

    def _watch_interval_seconds(self) -> float:
        return WATCH_INTERVAL_SECONDS

    def _wake_interval_seconds(self) -> int:
        return WAKE_INTERVAL_SECONDS


_service: Optional[DotService] = None


def get_dot_service() -> DotService:
    global _service
    if _service is None:
        _service = DotService()
    return _service


def reset_dot_service(service: Optional[DotService] = None) -> None:
    """Test hook: replace the process-wide service."""
    global _service
    _service = service
