"""Bounded process-local originals of cards successfully sent by each Bot app."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Mapping

_LIMIT = 500
_lock = threading.Lock()
_cards: OrderedDict[tuple[str, str], str] = OrderedDict()


def remember_card(app_id: str, message_id: str, content: Mapping[str, Any]) -> None:
    """Store the actual Schema 2.0 title and markdown once Feishu acknowledges it."""
    if content.get("schema") != "2.0":
        forget_card(app_id, message_id)
        return
    header = content.get("header")
    title = header.get("title") if isinstance(header, Mapping) else None
    heading = title.get("content") if isinstance(title, Mapping) else None
    body = content.get("body")
    elements = body.get("elements") if isinstance(body, Mapping) else None
    if (
        not isinstance(heading, str)
        or not isinstance(elements, list)
        or any(
            not isinstance(element, Mapping)
            or element.get("tag") != "markdown"
            or not isinstance(element.get("content"), str)
            for element in elements
        )
    ):
        forget_card(app_id, message_id)
        return
    parts = [heading]
    for element in elements:
        if isinstance(element, Mapping) and element.get("tag") == "markdown":
            text = element.get("content")
            if isinstance(text, str):
                parts.append(text)
    remember_text(app_id, message_id, "\n\n".join(parts))


def remember_text(app_id: str, message_id: str, text: str) -> None:
    key = (app_id, message_id)
    with _lock:
        _cards[key] = text
        _cards.move_to_end(key)
        while len(_cards) > _LIMIT:
            _cards.popitem(last=False)


def card_text(app_id: str, message_id: str) -> str | None:
    with _lock:
        return _cards.get((app_id, message_id))


def forget_card(app_id: str, message_id: str) -> None:
    with _lock:
        _cards.pop((app_id, message_id), None)
