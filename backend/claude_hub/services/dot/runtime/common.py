"""Dot service: tab ownership, per-turn context, journal sync, delegation and wake-ups."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..context import TurnPack, build_session_pack, build_turn_pack, reach_note
from ..index import DotIndex
from ..lessons import lesson_score, lint_brief, same_rule, session_rules
from ..prompts import (
    BRIEF_TEMPLATE,
    QUIET_REPLY,
    RETIRED_WAKE_MESSAGES,
    TIDY_MESSAGE,
    WAKE_MESSAGE,
)
from ..storage.cards import FOCUS_LIMIT, Card, now_iso, utc_now
from ..storage.store import DotStore

logger = logging.getLogger(__name__)

TAB_STREAM_WORKSPACE_ID = "terminal-tabs"
TAB_STREAM_SESSION_PREFIX = "terminal-tab-"
# Rotate the provider session before raw history piles up.
MAX_SESSION_TURNS = 24
MAX_SESSION_AGE = timedelta(hours=18)
# Dot is woken only by watcher events (run now). Schedules need a timing, so the
# wake-up gets one that never comes due: ten years.
WAKE_INTERVAL_SECONDS = 10 * 365 * 24 * 3600
TIDY_CRON = "17 3 * * *"
# Task state changes are rare; one cheap in-memory pass a minute is enough.
WATCH_INTERVAL_SECONDS = 60.0
# A wake-up that gave way to the user (or failed) waits this long before it runs again.
YIELD_PAUSE_SECONDS = 180.0
# A wake-up that ran again (stopped, failed) may send the same notice twice: skip it.
NOTIFY_REPEAT_SECONDS = 1800.0
DEFAULT_STALL_HOURS = 2.0
_TERMINAL_TASK_STATES = {"done", "failed"}
# Only running tasks can look stuck; review waits on a person, todo was never started.
_STALL_TASK_STATES = {"queued", "working"}
# A worker question: it counts against first pass.
_QUESTION_REPORT_STATES = {"needs_input", "blocked"}
_ATTENTION_REPORT_STATES = _QUESTION_REPORT_STATES | {"review_needs_input"}
# Report states that hand the work back (a new round after a verdict).
_RETURN_REPORT_STATES = {"ready_for_review", "completed", "failed"}
# review_reason of the report the Hub writes when a task is moved to review by hand,
_MANUAL_REVIEW_REASON = "Manual status transition to REVIEW."
# ... and the end of the review_reason of the Hub's stuck-review reaper.
_REAPER_REASON_SUFFIX = "recovered by background dispatcher."
# Per report field on a result card; a huge report must not make the card unsavable.
RESULT_TEXT_LIMIT = 20_000
# Prepared turns waiting for their end; more than this and the oldest never ended.
_SENT_TURNS_KEPT = 10
_JOURNAL_TEXT_LIMIT = 20_000
# skip: Dot cannot judge this one (say why in the note). Not counted anywhere.
REVIEW_VERDICTS = ("ok", "redo", "skip")
# change: the user changed what they want. No lesson, and not counted in the first-pass rate.
REVIEW_CAUSES = ("brief", "env", "worker", "change")
# First-pass rate covers the latest reviews only, so it shows the current trend.
STATS_WINDOW = 20


class LessonNotFound(LookupError):
    """A review named a lesson id that does not exist."""


class DelegateNotStarted(Exception):
    """The delegated task was created (and is tracked) but did not start."""


@dataclass
class TurnPlan:
    """What the provider transport must apply before sending one Dot turn."""

    rotate: bool
    conversation_id: Optional[str]
    system_pack: str
    turn_pack: str
    # Inbox events shown in ``turn_pack``; ack them only once the turn is sent.
    delivered_event_ids: list[str]


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _checklist(value: Any) -> list[str]:
    """Done-when items; work items from before checklists hold one string."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _ended_at(task: Any) -> Optional[str]:
    """When the task's latest run failed or finished; each run sets its own stamp."""
    status = getattr(getattr(task, "status", None), "value", None)
    field = {"failed": "failed_at", "done": "completed_at"}.get(str(status))
    moment = getattr(task, field, None) if field else None
    return moment.isoformat() if isinstance(moment, datetime) else None


def _report_state(report: Any) -> str:
    return str(getattr(getattr(report, "state", None), "value", "") or "")


def _sent_turns(state: dict[str, Any]) -> list[dict[str, Any]]:
    turns = state.get("sent_turns")
    return [turn for turn in turns if isinstance(turn, dict)] if isinstance(turns, list) else []


def _worker_report(report: Any) -> bool:
    """A report from the worker itself: not a Hub review request, not the Hub reviewer,
    not a Hub audit line (lessons put into a prompt)."""
    return (
        not _hub_report(report)
        and not _report_state(report).startswith("review_")
        and getattr(report, "risk_level", None) != "system_audit"
    )


def _worker_return(report: Any) -> bool:
    """A worker report that hands work back: a result (or failure) for a person."""
    return _worker_report(report) and _report_state(report) in _RETURN_REPORT_STATES


def _continue_report(report: Any, task: Any) -> bool:
    """The report the Hub writes when a person continues the task (their message, or
    "Task continued ..."). It is written as the run starts, with the same time.
    """
    started = getattr(task, "started_at", None)
    return (
        _report_state(report) == "working"
        and getattr(report, "risk_level", None) != "system_audit"
        and started is not None
        and getattr(report, "created_at", None) == started
    )


def _hub_report(report: Any) -> bool:
    """A report the Hub wrote to ask for a review: a manual move to review, a review
    asked again, or the stuck-review reaper. It carries no work and no question, so
    it is neither a new round, a result to show, nor news for Dot.
    """
    decision = getattr(getattr(report, "review_decision", None), "value", None)
    reason = str(getattr(report, "review_reason", None) or "")
    return (
        decision == "request"
        and not getattr(report, "changed_files", None)
        and not getattr(report, "validation", None)
        and not getattr(report, "risks", None)
        and (
            reason == getattr(report, "message", None)
            or reason == _MANUAL_REVIEW_REASON
            or reason.endswith(_REAPER_REASON_SUFFIX)
        )
    )


def _clip_text(text: str, limit: int = RESULT_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""
