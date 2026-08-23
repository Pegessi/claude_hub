"""Race-free directed wait: per-consumer lock + Event, optional subtree wake.

Extracted from Agent Tree ``wait`` / ``_wake_for_run`` so TaskMailbox can
long-poll on ``task:<id>`` / resident keys without an AgentRun.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, TypeVar

T = TypeVar("T")


class DirectedWaitCoordinator:
    """One lock+Event per consumer key. Subtree waiters are counted separately."""

    def __init__(self) -> None:
        self.events: Dict[str, asyncio.Event] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self.subtree_waiters: Dict[str, int] = {}

    def wake(self, key: str) -> None:
        ev = self.events.get(key)
        if ev is not None:
            ev.set()

    def wake_if_subtree(self, key: str) -> None:
        if self.subtree_waiters.get(key, 0) > 0:
            self.wake(key)

    async def wait(
        self,
        key: str,
        *,
        subtree: bool,
        timeout_seconds: float,
        poll: Callable[[], List[T]],
    ) -> List[T]:
        """Block until ``poll`` returns events or ``timeout_seconds`` elapses.

        The lock covers check-then-clear so an append between the empty
        check and ``Event.wait`` cannot be lost. Timeout returns ``[]``.
        """

        lock = self.locks.setdefault(key, asyncio.Lock())
        ev = self.events.setdefault(key, asyncio.Event())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_seconds)
        if subtree:
            self.subtree_waiters[key] = self.subtree_waiters.get(key, 0) + 1
        try:
            while True:
                async with lock:
                    events = poll()
                    if events:
                        return events
                    ev.clear()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(ev.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return []
        finally:
            if subtree:
                remaining_waiters = self.subtree_waiters.get(key, 1) - 1
                if remaining_waiters > 0:
                    self.subtree_waiters[key] = remaining_waiters
                else:
                    self.subtree_waiters.pop(key, None)
