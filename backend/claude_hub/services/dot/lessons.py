"""Brief lessons and the brief check. Plain code, no model call.

A lesson is one rule for writing briefs, learned when a delegated task came
back wrong because of its brief. Votes count how often the same miss
happened. The score halves every ``LESSON_HALF_LIFE_DAYS`` without a new vote,
so a rule that stops mattering fades: first out of the always-on rules in the
session pack, later out of the delegate-time matches. Faded lessons stay in
the file as history.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

LESSON_HALF_LIFE_DAYS = 60.0
RULES_IN_SESSION = 5
RULE_MIN_SCORE = 0.5
MATCH_MIN_SCORE = 0.1
MATCH_MIN_OVERLAP = 2
MAX_DONE_WHEN = 7

_CJK = r"\u3400-\u9fff\uf900-\ufaff"
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset(
    "the and for with that this are not when must should from into each all any "
    "you your what where how its has have was were will can".split()
)
_VAGUE = frozenset(
    {
        "done",
        "finished",
        "complete",
        "completed",
        "works",
        "it works",
        "working",
        "fixed",
        "good",
        "looks good",
        "ok",
        "okay",
        "完成",
        "完成了",
        "做完",
        "做好",
        "做好了",
        "搞定",
        "好了",
        "修好",
        "可以",
        "可以用",
        "沒問題",
        "没问题",
    }
)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lesson_score(lesson: dict[str, Any], now: datetime) -> float:
    """Votes, halved for every half-life since the lesson was last seen."""
    votes = max(int(lesson.get("votes") or 0), 0)
    seen = _parse_time(lesson.get("seen")) or _parse_time(lesson.get("created"))
    if seen is None:
        return float(votes)
    days = max((now - seen).total_seconds() / 86400, 0.0)
    return float(votes * 0.5 ** (days / LESSON_HALF_LIFE_DAYS))


def ranked(lessons: Iterable[dict[str, Any]], now: datetime) -> list[tuple[float, dict[str, Any]]]:
    scored = [(lesson_score(lesson, now), lesson) for lesson in lessons]
    return sorted(scored, key=lambda pair: -pair[0])


def session_rules(lessons: Iterable[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """The few strongest lessons, shown in every session."""
    return [lesson for score, lesson in ranked(lessons, now) if score >= RULE_MIN_SCORE][
        :RULES_IN_SESSION
    ]


def tokens(text: str) -> set[str]:
    """Words of 3+ letters and CJK character pairs, for a rough topic match."""
    lowered = text.lower()
    found = {word for word in _WORD_RE.findall(lowered) if len(word) >= 3} - _STOP_WORDS
    for run in _CJK_RUN_RE.findall(lowered):
        found.update(run[index : index + 2] for index in range(len(run) - 1))
    return found


def match_lessons(
    lessons: Iterable[dict[str, Any]], text: str, now: datetime, limit: int = 3
) -> list[dict[str, Any]]:
    """Lessons that share enough words with a brief, strongest first."""
    wanted = tokens(text)
    hits: list[tuple[float, dict[str, Any]]] = []
    for score, lesson in ranked(lessons, now):
        overlap = len(wanted & tokens(str(lesson.get("rule") or "")))
        if score >= MATCH_MIN_SCORE and overlap >= MATCH_MIN_OVERLAP:
            hits.append((score * overlap, lesson))
    hits.sort(key=lambda pair: -pair[0])
    return [lesson for _, lesson in hits[:limit]]


def _checkable(item: str) -> bool:
    plain = " ".join(re.sub(r"[^\w]+", " ", item.lower()).split())
    if plain in _VAGUE:
        return False
    cjk = sum(len(run) for run in _CJK_RUN_RE.findall(plain))
    return len(_WORD_RE.findall(plain)) + cjk / 2 >= 2


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def lint_brief(*, done_when: list[str], context: str, output: str) -> list[str]:
    """Cheap signs a brief will come back wrong. Warnings, not rules: Dot may override."""
    warnings = [
        f'Done-when {number} ("{_clip(item)}") is not something a stranger can check. '
        "Name the file, command, number or behaviour."
        for number, item in enumerate(done_when, start=1)
        if not _checkable(item)
    ]
    if len(done_when) > MAX_DONE_WHEN:
        warnings.append(
            f"{len(done_when)} done-when checks: is this one goal? If not, split it into tasks."
        )
    if not output.strip():
        warnings.append(
            "No output: say what the result is and where it goes (file path, PR, card, reply)."
        )
    if not context.strip():
        warnings.append("No context: could the worker finish without asking you anything?")
    return warnings
