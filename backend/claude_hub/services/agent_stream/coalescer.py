"""Semantic coalescer for structured agent-stream text deltas.

Long Thinking / text streams from Claude and other providers emit many tiny
deltas (often 1-2 characters each) at rates that can overwhelm the browser's
rendering pipeline and fill the subscriber queue. This coalescer merges
consecutive same-stream text deltas within a short time window so the
downstream fanout rate stays bounded while the final text remains byte-exact.

Design (independently follows the same principles as Paseo's
``AgentStreamCoalescer``; no AGPL code is copied):

* **Coalescable events**: ``text_delta`` and ``thinking_delta``.
* **Same-stream merge**: consecutive deltas are merged only when they share
  ``type``, ``turn_id``, ``message_id``, and ``run_epoch``. A change in any
  of those starts a new coalesced entry.
* **Leading-edge immediate flush**: the first delta after an idle period
  longer than the window is flushed synchronously so the first token of a
  turn is not delayed a full window.
* **Trailing window flush**: sustained bursts accumulate and are flushed by
  a timer firing once per window, capping the outbound event rate.
* **Terminal / non-coalescable flush**: ``turn_completed``, ``error``,
  ``tool_call_completed``, and any other non-text event are not buffered;
  the caller must flush pending text before emitting them so text never
  appears after a terminal marker.
* **Serialized drain**: all flush paths (leading-edge, trailing timer, and
  explicit ``flush_async``) acquire a single lock and drain the buffer in a
  loop. Events that arrive while ``on_flush`` is awaiting are caught by the
  loop, so nothing is stranded. The lock guarantees at most one flush runs
  at a time, preventing sequence inversion and double-emit.

The coalescer itself does not persist or fan out: it invokes ``on_flush``
with each merged event. The caller (the tailer) is responsible for
``store.append`` + ``_fanout`` so sequence numbers and persistence remain
single-sourced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional

from ...models import AgentStreamEvent, AgentStreamEventType

logger = logging.getLogger(__name__)

__all__ = ["AgentStreamCoalescer", "DEFAULT_COALESCE_WINDOW_MS"]

DEFAULT_COALESCE_WINDOW_MS = 60

# Event types that carry incremental text and can be merged.
_COALESCABLE_TYPES = frozenset(
    {AgentStreamEventType.TEXT_DELTA, AgentStreamEventType.THINKING_DELTA}
)


class AgentStreamCoalescer:
    """Merge consecutive same-stream text deltas within a time window."""

    def __init__(
        self,
        *,
        window_ms: int = DEFAULT_COALESCE_WINDOW_MS,
        on_flush: Callable[[AgentStreamEvent], Awaitable[None]],
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._window = window_ms / 1000.0
        self._on_flush = on_flush
        self._loop = loop or asyncio.get_event_loop()

        self._buffer: List[AgentStreamEvent] = []
        self._timer_handle: Optional[asyncio.TimerHandle] = None
        self._last_flush_at: Optional[float] = None
        # Serializes all flush paths so at most one drain runs at a time.
        self._flush_lock = asyncio.Lock()
        # True while a drain is in flight (held under _flush_lock).
        self._flushing = False

    # ── public API ────────────────────────────────────────────────────────

    async def handle(self, event: AgentStreamEvent) -> bool:
        """Offer ``event`` to the coalescer.

        Returns ``True`` if the event was buffered (and will be emitted on a
        future flush), or ``False`` if the caller should emit it directly.
        Non-coalescable events always return ``False``; the caller is
        responsible for flushing pending text before emitting them.

        The leading-edge flush is awaited so the first token of a turn is
        persisted and fanned out before ``handle`` returns.
        """
        if event.type not in _COALESCABLE_TYPES:
            return False

        text = event.payload.get("text", "")
        if not text:
            # Empty text deltas carry no information; absorb them without
            # scheduling a flush.
            return True

        self._buffer.append(event)

        # Only schedule a flush if no timer is pending AND no drain is in
        # flight. An in-flight drain loops on the buffer, so events added
        # during it will be caught without needing another timer.
        if self._timer_handle is None and not self._flushing:
            elapsed = (
                float("inf")
                if self._last_flush_at is None
                else self._loop.time() - self._last_flush_at
            )
            if elapsed >= self._window:
                # Leading edge: the stream was idle, so flush synchronously
                # (awaited) so the first token is not delayed.
                await self.flush_async()
            else:
                self._schedule_trailing_flush()

        return True

    def flush(self) -> None:
        """Synchronously trigger a flush (fire-and-forget async task).

        Use this from sync contexts (e.g. a timer callback). The actual
        persistence happens in a background task.
        """
        self._schedule_async_flush()

    async def flush_async(self) -> None:
        """Flush pending text and await completion.

        Acquires the flush lock and drains the buffer in a loop: events that
        arrive while ``on_flush`` is awaiting are flushed in the same
        iteration, so nothing is stranded. Use this from the async processing
        loop when ordering matters (e.g. before emitting a terminal event).
        """
        async with self._flush_lock:
            await self._drain_locked()

    def dispose(self) -> None:
        """Cancel any pending timer. Does not flush buffered events."""
        self._clear_timer()

    # ── internals ─────────────────────────────────────────────────────────

    async def _drain_locked(self) -> None:
        """Drain the buffer under ``_flush_lock``.

        Loops while the buffer is non-empty so events appended during
        ``on_flush`` are flushed in the same drain. Sets ``_flushing`` for
        the duration so ``handle`` does not schedule a redundant timer.

        Exceptions from ``on_flush`` are caught and logged so a single
        failed persist does not abort the whole drain or leave the lock
        held. The failed event is dropped (same as the pre-coalescer
        behavior where a failed ``store.append`` dropped the event).
        """
        self._clear_timer()
        self._flushing = True
        try:
            while self._buffer:
                events = self._collapse(self._buffer)
                self._buffer = []
                self._last_flush_at = self._loop.time()
                for event in events:
                    try:
                        await self._on_flush(event)
                    except Exception:
                        logger.exception("agent_stream coalescer on_flush failed; dropping event")
        finally:
            self._flushing = False

    def _schedule_trailing_flush(self) -> None:
        self._timer_handle = self._loop.call_later(self._window, self._on_timer)

    def _on_timer(self) -> None:
        self._timer_handle = None
        self._schedule_async_flush()

    def _schedule_async_flush(self) -> None:
        """Schedule a drain as a background task.

        If a drain is already in flight, skip: the in-flight drain's loop
        will catch any events added since it started.
        """
        if self._flushing:
            return
        asyncio.ensure_future(self.flush_async())

    def _clear_timer(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

    @staticmethod
    def _collapse(events: List[AgentStreamEvent]) -> List[AgentStreamEvent]:
        """Merge consecutive same-stream text deltas into single events.

        Two adjacent deltas merge when they share ``type``, ``turn_id``,
        ``message_id``, and ``run_epoch``. The merged event carries the
        concatenated ``payload.text`` and the identity of the first delta.
        """
        collapsed: List[AgentStreamEvent] = []
        for event in events:
            prev = collapsed[-1] if collapsed else None
            if (
                prev is not None
                and prev.type == event.type
                and prev.turn_id == event.turn_id
                and prev.message_id == event.message_id
                and prev.run_epoch == event.run_epoch
            ):
                merged_text = prev.payload.get("text", "") + event.payload.get("text", "")
                merged_payload = dict(prev.payload)
                merged_payload["text"] = merged_text
                collapsed[-1] = prev.model_copy(update={"payload": merged_payload})
            else:
                collapsed.append(event.model_copy(deep=True))
        return collapsed
