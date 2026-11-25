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
from pathlib import Path
from typing import Any, Iterable, Optional

from .events_gateway import EXTERNAL_EVENT, render_external_event, triggers_wake
from .index import DotIndex, recallable
from .lessons import session_rules
from .prompts import DOT_CONTEXT_HEADER, TOOL_GUIDE, TURN_RULES
from .storage.store import DotStore
from .store import DEFAULT_NAME, Card, utc_now

logger = logging.getLogger(__name__)

SESSION_PACK_CHARS = 12000
TURN_PACK_CHARS = 5000
_LESSONS_CHARS = 1800
_PROFILE_CHARS = 3600
_CONCEPT_CHARS = 1800
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


def _coerce_store(store: DotStore | Path | str) -> DotStore:
    return store if isinstance(store, DotStore) else DotStore(Path(store))


def _assemble_session_pack(store_or_home: DotStore | Path | str) -> tuple[str, list[str]]:
    """Assemble the session pack by whole lines and record any sections that were cut."""
    store = _coerce_store(store_or_home)
    budget = SESSION_PACK_CHARS
    parts: list[str] = []
    cuts: list[str] = []

    preamble_blocks: list[tuple[str, str]] = [("role", store.read_role().strip())]
    name = store.read_identity()["name"]
    if name != DEFAULT_NAME:
        preamble_blocks.append(
            (
                "identity",
                f'## Your name\nThe user named you "{name}". Use this name instead of Dot.',
            )
        )
    preamble_blocks.append(("turn_rules", TURN_RULES.strip()))
    preamble_blocks.append(("tool_guide", TOOL_GUIDE.strip()))

    for section_name, raw_block in preamble_blocks:
        if not raw_block:
            continue
        lines = raw_block.splitlines()
        sep = 2 if parts else 0
        kept = _take_lines(lines, max(budget - sep, 0))
        if len(kept) < len(lines) and section_name not in cuts:
            cuts.append(section_name)
        if not kept:
            continue
        rendered = "\n".join(kept)
        parts.append(rendered)
        budget -= sep + len(rendered)

    rules = session_rules(store.list_lessons(), utc_now())
    lesson_lines = [f"- {_clip(rule.get('rule', ''))} [{rule.get('id')}]" for rule in rules]
    profile_lines = [_card_line(card) for card in _ranked(store.list_cards("user"))]
    concept_lines = [_card_line(card) for card in _ranked(store.list_cards("concept"))]

    dynamic_sections = (
        ("lessons", "## Brief guidance (learned from past reviews)", lesson_lines, _LESSONS_CHARS),
        ("profile", "## About the user", profile_lines, _PROFILE_CHARS),
        ("concepts", "## Key concepts", concept_lines, _CONCEPT_CHARS),
    )
    for section_name, title, lines, section_cap in dynamic_sections:
        if not lines:
            continue
        sep = 2 if parts else 0
        line_budget = min(section_cap, max(budget - sep - len(title), 0))
        kept = _take_lines(lines, line_budget)
        if len(kept) < len(lines):
            cuts.append(section_name)
        if not kept:
            continue
        block = title + "\n" + "\n".join(kept)
        parts.append(block)
        budget -= sep + len(block)

    return "\n\n".join(parts), cuts


def build_session_pack(store: DotStore | Path | str) -> str:
    """Role + name + turn rules + tools + lessons + user profile + concepts, capped at ``SESSION_PACK_CHARS``."""
    pack, _ = _assemble_session_pack(store)
    return pack


def session_pack_cuts(store: DotStore | Path | str) -> list[str]:
    """Session pack sections with cards that did not fit (for ``health``)."""
    _, cuts = _assemble_session_pack(store)
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
        tracking = "watched" if item.get("watched") else "delegated"
        lines.append(
            f"- [{item.get('task_id')}] {_clip(item.get('title', ''), 120)} — {state} "
            f"({tracking}; workspace {where})"
        )
    return lines[-15:]


_EVENTS_PER_TURN = 12
_EVENTS_TITLE = "## New events since your last turn"
# The user's turn shows waiting events too, but a wake-up handles them.
_WAITING_EVENTS_TITLE = "## Events waiting for your next wake-up"
_REVIEWS_PER_TURN = 8
_RECENT_TITLE = "## Recent talk (before this fresh session)"


def _review_lines(store: DotStore, wake: bool) -> list[str]:
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
    if not wake:
        lines.append("- Prefer to answer the user first; unrelated reviews can wait for a wake-up.")
        return lines
    lines.append(
        "- Consider the evidence against each done-when item. "
        "`claude-hub dot review` records ok/redo/skip for delegated tasks; "
        "see `claude-hub dot review --help` for causes and notes."
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
        if event.get("kind") == EXTERNAL_EVENT:
            lines.append(render_external_event(event))
            continue
        card = f" (card {event['card_id']})" if event.get("card_id") else ""
        action = (
            " Consider a brief update if useful; [quiet] is fine if there is nothing new "
            "worth reporting. Task acceptance still requires user authorization."
            if event.get("kind") == "task_review_passed"
            else ""
        )
        lines.append(
            f"- {event.get('kind', 'event')}: {_clip(event.get('text', ''), 400)}{card}{action}"
        )
    return lines


def reach_note(source: str, feishu_bound: bool) -> str:
    """Where this turn came from, where Dot's answer goes, and whether
    ``claude-hub dot notify`` can reach the user on Feishu.

    ``source`` is ``feishu``, ``web``, ``wake`` or ``tidy``.
    """
    if source == "feishu":
        return (
            "Reach: the user wrote from Feishu; the last message of your turn goes back "
            "to their Feishu chat. Keep it short: conclusion first, a few bullets."
        )
    if source == "web":
        line = "Reach: the user wrote in the web Chat; your answer shows there."
        if feishu_bound:
            return line + " A Feishu bot is bound to this Chat, but they are here now."
        return line
    line = (
        f"Reach: a Hub {'tidy' if source == 'tidy' else 'wake-up'}, not the user; your answer "
        "shows in the web Chat only."
    )
    if feishu_bound:
        return line + (
            " A Feishu bot is bound and the user reads there: a task they asked for that "
            "is done, failed or needs them goes there with `claude-hub dot notify` "
            "(a card with --title and --tone)."
        )
    return line + " No Feishu bot is bound, so `claude-hub dot notify` cannot reach them."


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
    wake: bool = True,
    reach: str = "",
) -> TurnPack:
    """Assemble the per-turn context; a section that does not fit is cut or skipped.

    On the user's turn, only passive external events are eligible for
    acknowledgement. Wake-triggering events stay unread for the next wake-up.
    ``reach`` is the ``reach_note`` line.
    """
    moment = (now or datetime.now()).astimezone()
    header = (
        f"{DOT_CONTEXT_HEADER}\n"
        f"Now: {moment.strftime('%Y-%m-%d %a %H:%M %Z')} · session turn {session_turn}"
    )
    if reach:
        header += "\n" + reach
    events = store.list_inbox() if inbox else []
    if wake:
        events = [event for event in events if triggers_wake(event)]
    focus = _take_lines(_focus_lines(store), _FOCUS_CHARS)
    # The newest work is last; a cut drops the oldest.
    work = _take_lines(_work_lines(store), _WORK_CHARS, newest_last=True)
    sections: list[tuple[str, list[str]]] = [
        ("## Focus (what matters now)", focus or ["- (empty — ask what matters)"]),
        ("## Tracked work", work or ["- (none)"]),
    ]
    # Reviews go before events: a busy inbox must not push them out of the pack.
    reviews = _review_lines(store, wake)
    if reviews:
        sections.append(("## Waiting for your review", reviews))
    events_title = _EVENTS_TITLE if wake else _WAITING_EVENTS_TITLE
    if events:
        sections.append((events_title, _event_lines(events)))
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
        if title == events_title:
            shown_events = len(kept) - (len(events) > _EVENTS_PER_TURN)
        block = title + "\n" + "\n".join(kept)
        blocks.append(block)
        budget -= len(block) + 2
    # Only events Dot actually saw are delivered; cut ones stay unread.
    shown = events[-_EVENTS_PER_TURN:][:shown_events] if shown_events > 0 else []
    return TurnPack(
        text="\n\n".join(blocks),
        delivered_event_ids=[
            str(event["id"])
            for event in shown
            if event.get("id") and (wake or not triggers_wake(event))
        ],
    )
