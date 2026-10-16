"""Build Dot's context packs.

Two packs keep Dot's mind small and fresh:

* the **session pack** (role, tool guide, delegation rules, user profile,
  concepts) is passed as the system prompt and stays fixed for one provider
  session;
* the **turn pack** (time, focus, open work, tasks waiting for review, new
  events, recalled cards and, on a session's first turn, the recent talk) is
  rebuilt for every turn.

Both have hard size caps so memory growth never floods the conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from .index import DotIndex, recallable
from .lessons import session_rules
from .prompts import DOT_CONTEXT_HEADER, TOOL_GUIDE
from .store import DEFAULT_NAME, Card, DotStore, utc_now

logger = logging.getLogger(__name__)

SESSION_PACK_CHARS = 9000
TURN_PACK_CHARS = 5000
_PROFILE_CHARS = 2500
_CONCEPT_CHARS = 1500
# Focus and work come first; caps keep room for reviews and events behind them.
_FOCUS_CHARS = 1600
_WORK_CHARS = 1400
_LINE_CHARS = 240


def _clip(text: str, limit: int = _LINE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _card_line(card: Card) -> str:
    summary = f" — {_clip(card.summary)}" if card.summary else ""
    return f"- [{_clip(card.id, 120)}] {_clip(card.title, 120)}{summary}"


def _take_lines(lines: Iterable[str], budget: int, *, newest_last: bool = False) -> list[str]:
    """Lines from the front that fit ``budget``; from the back when the newest are last."""
    ordered = list(lines)
    taken: list[str] = []
    used = 0
    for line in reversed(ordered) if newest_last else ordered:
        if used + len(line) + 1 > budget:
            break
        taken.append(line)
        used += len(line) + 1
    return taken[::-1] if newest_last else taken


def _ranked(cards: list[Card]) -> list[Card]:
    newest_first = sorted(cards, key=lambda card: card.updated, reverse=True)
    return sorted(newest_first, key=lambda card: -card.importance)


def build_session_pack(store: DotStore) -> str:
    """Role + name + tools + rules + user profile + concepts, capped at ``SESSION_PACK_CHARS``."""
    parts = [store.read_role().strip()]
    name = store.read_identity()["name"]
    if name != DEFAULT_NAME:
        parts.append(f'## Your name\nThe user named you "{name}". Use this name instead of Dot.')
    parts.append(TOOL_GUIDE.strip())
    rules = session_rules(store.list_lessons(), utc_now())
    if rules:
        parts.append(
            "## Delegation rules (learned from your past briefs)\n"
            + "\n".join(f"- {_clip(rule.get('rule', ''))} [{rule.get('id')}]" for rule in rules)
        )
    profile = _take_lines(
        (_card_line(card) for card in _ranked(store.list_cards("user"))), _PROFILE_CHARS
    )
    if profile:
        parts.append("## About the user\n" + "\n".join(profile))
    concepts = _take_lines(
        (_card_line(card) for card in _ranked(store.list_cards("concept"))), _CONCEPT_CHARS
    )
    if concepts:
        parts.append("## Key concepts\n" + "\n".join(concepts))
    pack = "\n\n".join(part for part in parts if part)
    return pack[:SESSION_PACK_CHARS]


def session_pack_cuts(store: DotStore) -> list[str]:
    """Session pack sections with cards that did not fit (for ``health``)."""
    cuts = []
    for name, kind, cap in (
        ("profile", "user", _PROFILE_CHARS),
        ("concepts", "concept", _CONCEPT_CHARS),
    ):
        lines = [_card_line(card) for card in store.list_cards(kind)]
        if len(_take_lines(lines, cap)) < len(lines):
            cuts.append(name)
    return cuts


@dataclass
class TurnPack:
    text: str
    delivered_event_ids: list[str] = field(default_factory=list)


def _focus_lines(store: DotStore) -> list[str]:
    lines = []
    for item in store.list_focus():
        why = f" — {_clip(item.get('why', ''), 160)}" if item.get("why") else ""
        lines.append(
            f"- [{item.get('id')}] P{item.get('priority', 3)} {_clip(item.get('title', ''), 120)}{why}"
        )
    return lines


def _work_lines(store: DotStore) -> list[str]:
    lines = []
    for item in store.list_work():
        state = item.get("status") or "unknown"
        where = item.get("workspace_id") or "?"
        lines.append(
            f"- [{item.get('task_id')}] {_clip(item.get('title', ''), 120)} — {state} "
            f"(workspace {where})"
        )
    return lines[-15:]


_EVENTS_PER_TURN = 12
_EVENTS_TITLE = "## New events since your last turn"
_REVIEWS_PER_TURN = 8
_RECENT_TITLE = "## Recent talk (before this fresh session)"


def _review_lines(store: DotStore) -> list[str]:
    """Returned tasks stay listed every turn until Dot records a verdict."""
    pending = store.pending_reviews()
    if not pending:
        return []
    lines = []
    for item in pending[:_REVIEWS_PER_TURN]:
        notes = [str(item.get("status") or "?")]
        if item.get("result_card"):
            notes.append(f"card {item['result_card']}")
        if item.get("asked"):
            notes.append("the worker had to ask a question")
        lines.append(
            f"- [{item.get('task_id')}] {_clip(item.get('title', ''), 120)} — {', '.join(notes)}"
        )
    if len(pending) > _REVIEWS_PER_TURN:
        lines.append(f"- ({len(pending) - _REVIEWS_PER_TURN} more)")
    lines.append(
        "- Judge each result against its done-when list, then "
        "`claude-hub dot review <task-id> --verdict ok|redo|skip [--cause brief|env|worker|change] "
        '[--note "..."]`; a brief miss needs `--lesson` or `--lesson-id`, any other redo or a '
        "skip needs `--note`."
    )
    return lines


def _event_lines(events: list[dict[str, Any]]) -> list[str]:
    lines = []
    if len(events) > _EVENTS_PER_TURN:
        lines.append(
            f"- ({len(events) - _EVENTS_PER_TURN} older events stay unread for a later turn; "
            "see `claude-hub dot inbox`)"
        )
    for event in events[-_EVENTS_PER_TURN:]:
        card = f" (card {event['card_id']})" if event.get("card_id") else ""
        lines.append(f"- {event.get('kind', 'event')}: {_clip(event.get('text', ''), 400)}{card}")
    return lines


def build_turn_pack(
    store: DotStore,
    index: Optional[DotIndex],
    text: str,
    *,
    first_turn: bool,
    session_turn: int,
    now: Optional[datetime] = None,
    recall: bool = True,
    inbox: bool = True,
) -> TurnPack:
    """Assemble the per-turn context; a section that does not fit is cut or skipped."""
    moment = (now or datetime.now()).astimezone()
    header = (
        f"{DOT_CONTEXT_HEADER}\n"
        f"Now: {moment.strftime('%Y-%m-%d %a %H:%M %Z')} · session turn {session_turn}"
    )
    events = store.list_inbox() if inbox else []
    focus = _take_lines(_focus_lines(store), _FOCUS_CHARS)
    # The newest work is last; a cut drops the oldest.
    work = _take_lines(_work_lines(store), _WORK_CHARS, newest_last=True)
    sections: list[tuple[str, list[str]]] = [
        ("## Focus (what matters now)", focus or ["- (empty — ask what matters)"]),
        ("## Delegated work", work or ["- (none)"]),
    ]
    # Reviews go before events: a busy inbox must not push them out of the pack.
    reviews = _review_lines(store)
    if reviews:
        sections.append(("## Waiting for your review", reviews))
    if events:
        sections.append((_EVENTS_TITLE, _event_lines(events)))
    if recall and recallable(text) and index is not None:
        try:
            hits = index.search(text, limit=6)
        except Exception:
            # A locked or broken index costs the recall, not the turn's whole pack.
            logger.warning("dot: recall failed", exc_info=True)
            hits = []
        if hits:
            sections.append(
                (
                    "## Memory that may help (use `claude-hub dot show <id>` for detail)",
                    [
                        f"- [{_clip(hit.id, 120)}] ({hit.kind}) {_clip(hit.title, 120)}"
                        + (f" — {_clip(hit.summary)}" if hit.summary else "")
                        for hit in hits
                    ],
                )
            )
    if first_turn:
        recent = store.recent_journal()
        if recent:
            sections.append((_RECENT_TITLE, [f"- {line}" for line in recent]))

    budget = TURN_PACK_CHARS - len(header)
    blocks = [header]
    shown_events = 0
    for title, lines in sections:
        # The recent talk is oldest first: a cut keeps the latest exchanges.
        kept = _take_lines(
            lines, max(budget - len(title) - 2, 0), newest_last=title == _RECENT_TITLE
        )
        if not kept:
            continue
        if title == _EVENTS_TITLE:
            shown_events = len(kept) - (len(events) > _EVENTS_PER_TURN)
        block = title + "\n" + "\n".join(kept)
        blocks.append(block)
        budget -= len(block) + 2
    # Only events Dot actually saw are delivered; cut ones stay unread.
    shown = events[-_EVENTS_PER_TURN:][:shown_events] if shown_events > 0 else []
    return TurnPack(
        text="\n\n".join(blocks),
        delivered_event_ids=[str(event.get("id")) for event in shown if event.get("id")],
    )
