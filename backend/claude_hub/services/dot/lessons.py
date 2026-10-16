"""Brief lessons and the brief check. Plain code, no model call.

A lesson is one rule for writing briefs, learned when a delegated task came
back wrong because of its brief. ``votes`` counts how often the same miss
happened. The score halves every ``LESSON_HALF_LIFE_DAYS`` without a new vote,
so a rule that stops mattering drops out of the session pack. Only the
strongest few are shown, all the time: a lesson shown after a brief is sent
could not change it. Hidden lessons stay in the file as history.

A vote or merge first decays the old strength to today and stores it as
``weight`` (with ``seen`` = now), so an old, faded rule cannot jump ahead of
fresh ones just because it once had many votes.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

LESSON_HALF_LIFE_DAYS = 60.0
RULES_IN_SESSION = 8
RULE_MIN_SCORE = 0.5
SAME_RULE_MIN_JACCARD = 0.6
MAX_DONE_WHEN = 7

_CJK = r"\u3400-\u9fff\uf900-\ufaff"
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
STOP_WORDS = frozenset(
    "the and for with that this are not when must should from into each all any "
    "you your what where how its has have was were will can".split()
)
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|cannot|without|avoid|disable|forbid|forbidden)\b|n['’]t"
    r"|[不別别勿沒没禁]|[无無][需須须]"
)
# Words that hold a negation character or word but do not negate the rule.
_NOT_NEGATION_RE = re.compile(
    r"\bno (?:more|less|fewer|later|earlier) than\b"
    r"|不同(?!意)|不[仅僅但断斷久]|[特区區分差类類级級识識告个個性][别別]"
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


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def lesson_score(lesson: dict[str, Any], now: datetime) -> float:
    """Strength (``weight``, else ``votes``), halved per half-life since last seen.

    The file is hand-editable, so a bad number counts as the votes, else 0.
    """
    weight = _number(lesson.get("weight"))
    if weight is None:
        weight = _number(lesson.get("votes")) or 0.0
    weight = max(weight, 0.0)
    seen = _parse_time(lesson.get("seen")) or _parse_time(lesson.get("created"))
    if seen is None:
        return weight
    days = max((now - seen).total_seconds() / 86400, 0.0)
    return float(weight * 0.5 ** (days / LESSON_HALF_LIFE_DAYS))


def ranked(lessons: Iterable[dict[str, Any]], now: datetime) -> list[tuple[float, dict[str, Any]]]:
    scored = [(lesson_score(lesson, now), lesson) for lesson in lessons]
    return sorted(scored, key=lambda pair: -pair[0])


def session_rules(lessons: Iterable[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """The few strongest lessons, shown in every session."""
    return [lesson for score, lesson in ranked(lessons, now) if score >= RULE_MIN_SCORE][
        :RULES_IN_SESSION
    ]


def tokens(text: str) -> set[str]:
    """Words of 3+ letters and CJK character pairs, to compare two rules."""
    lowered = text.lower()
    found = {word for word in _WORD_RE.findall(lowered) if len(word) >= 3} - STOP_WORDS
    for run in _CJK_RUN_RE.findall(lowered):
        found.update(run[index : index + 2] for index in range(len(run) - 1))
    return found


def _negated(text: str) -> bool:
    """Odd number of negations: "do not disable X" says the opposite of "disable X"."""
    return len(_NEGATION_RE.findall(_NOT_NEGATION_RE.sub(" ", text.lower()))) % 2 == 1


def same_rule(lessons: Iterable[dict[str, Any]], rule: str) -> Optional[dict[str, Any]]:
    """A live lesson that says nearly the same as ``rule`` (most shared words), if any.

    "Do X" and "do not X" share every word but say the opposite: never the same.
    """
    wanted = tokens(rule)
    if len(wanted) < 2:
        return None
    negated = _negated(rule)
    best: tuple[float, Optional[dict[str, Any]]] = (0.0, None)
    for lesson in lessons:
        if _negated(str(lesson.get("rule") or "")) != negated:
            continue
        theirs = tokens(str(lesson.get("rule") or ""))
        similarity = len(wanted & theirs) / len(wanted | theirs) if theirs else 0.0
        if similarity >= SAME_RULE_MIN_JACCARD and similarity > best[0]:
            best = (similarity, lesson)
    return best[1]


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
