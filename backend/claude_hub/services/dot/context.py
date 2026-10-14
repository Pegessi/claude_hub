"""Build Dot's context packs.

Two packs keep Dot's mind small and fresh:

* the **session pack** (role, tool guide, user profile, concepts) is passed as
  the system prompt and stays fixed for one provider session;
* the **turn pack** (time, focus, open work, new events, recalled cards and,
  on a session's first turn, the recent talk) is rebuilt for every turn.

Both have hard size caps so memory growth never floods the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from .index import DotIndex
from .prompts import DOT_CONTEXT_HEADER, TOOL_GUIDE
from .store import Card, DotStore

SESSION_PACK_CHARS = 9000
TURN_PACK_CHARS = 5000
_PROFILE_CHARS = 2500
_CONCEPT_CHARS = 1500
_LINE_CHARS = 240


def _clip(text: str, limit: int = _LINE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _card_line(card: Card) -> str:
    summary = f" — {_clip(card.summary)}" if card.summary else ""
    return f"- [{card.id}] {_clip(card.title, 120)}{summary}"


def _take_lines(lines: Iterable[str], budget: int) -> list[str]:
    taken: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        taken.append(line)
        used += len(line) + 1
    return taken


def _ranked(cards: list[Card]) -> list[Card]:
    newest_first = sorted(cards, key=lambda card: card.updated, reverse=True)
    return sorted(newest_first, key=lambda card: -card.importance)


def build_session_pack(store: DotStore) -> str:
    """Role + tools + user profile + concepts, capped at ``SESSION_PACK_CHARS``."""
    parts = [store.read_role().strip(), TOOL_GUIDE.strip()]
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


def _event_lines(events: list[dict[str, Any]]) -> list[str]:
    lines = []
    if len(events) > 12:
        lines.append(
            f"- ({len(events) - 12} older events omitted; see `claude-hub dot inbox --all`)"
        )
    for event in events[-12:]:
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
) -> TurnPack:
    """Assemble the per-turn context; sections are dropped from the end when over budget."""
    moment = (now or datetime.now()).astimezone()
    header = (
        f"{DOT_CONTEXT_HEADER}\n"
        f"Now: {moment.strftime('%Y-%m-%d %a %H:%M %Z')} · session turn {session_turn}"
    )
    events = store.list_inbox()
    sections: list[tuple[str, list[str]]] = [
        ("## Focus (what matters now)", _focus_lines(store) or ["- (empty — ask what matters)"]),
        ("## Delegated work", _work_lines(store) or ["- (none)"]),
    ]
    if events:
        sections.append(("## New events since your last turn", _event_lines(events)))
    if recall and text.strip():
        hits = index.search(text, limit=6) if index is not None else []
        if hits:
            sections.append(
                (
                    "## Memory that may help (use `claude-hub dot show <id>` for detail)",
                    [
                        f"- [{hit.id}] ({hit.kind}) {_clip(hit.title, 120)}"
                        + (f" — {_clip(hit.summary)}" if hit.summary else "")
                        for hit in hits
                    ],
                )
            )
    if first_turn:
        recent = store.recent_journal()
        if recent:
            sections.append(
                ("## Recent talk (before this fresh session)", [f"- {line}" for line in recent])
            )

    budget = TURN_PACK_CHARS - len(header)
    blocks = [header]
    for title, lines in sections:
        kept = _take_lines(lines, max(budget - len(title) - 2, 0))
        if not kept:
            break
        block = title + "\n" + "\n".join(kept)
        blocks.append(block)
        budget -= len(block) + 2
    return TurnPack(
        text="\n\n".join(blocks),
        delivered_event_ids=[str(event.get("id")) for event in events if event.get("id")],
    )
