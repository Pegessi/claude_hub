"""Synchronous notifications after a tab record is archived or retired.

The terminal manager owns tab lifecycle and must not import feature services
that depend on it. Features that bind their own lifetime to a tab (for example
Hub subagents bound to their parent Chat) register a callback here. Callbacks
run inline after the retirement is persisted; they must only record or schedule
work, never await or re-enter the manager synchronously.
"""

from __future__ import annotations

import logging
from typing import Callable, List

logger = logging.getLogger(__name__)

TabRetiredListener = Callable[[str], None]
_listeners: List[TabRetiredListener] = []


def add_tab_retired_listener(listener: TabRetiredListener) -> Callable[[], None]:
    """Register ``listener(tab_id)``; return a callable that removes it."""
    _listeners.append(listener)

    def remove() -> None:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass

    return remove


def notify_tab_retired(tab_id: str) -> None:
    for listener in list(_listeners):
        try:
            listener(tab_id)
        except Exception:
            logger.exception("Tab retirement listener failed for tab %s", tab_id)
