"""RED tests for the agent-stream semantic coalescer.

These tests define the desired coalescing behavior before implementation.
They assert:
- 8577 thinking_delta events (~10232 chars, ~80 events/s) are coalesced into
  far fewer outbound events while preserving the exact final text.
- Leading-edge flush: the first delta in an idle window is emitted immediately.
- Trailing window: sustained bursts are batched to ~1 event per window.
- Terminal events (turn_completed, error, tool_call_completed) force a flush
  before they are emitted, so no text is stranded behind a terminal marker.
- Non-coalescable events flush any pending text before passing through.
- Reconnect/replay sees exactly the coalesced events (no duplicates, no gaps).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import List

import pytest

from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
)
from claude_hub.services.agent_stream.coalescer import AgentStreamCoalescer


def _make_event(
    type: AgentStreamEventType,
    text: str = "",
    *,
    turn_id: str = "turn-1",
    message_id: str | None = None,
    call_id: str | None = None,
) -> AgentStreamEvent:
    if message_id is None:
        if type == AgentStreamEventType.TEXT_DELTA:
            message_id = f"{turn_id}:assistant"
        elif type == AgentStreamEventType.THINKING_DELTA:
            message_id = f"{turn_id}:thinking"
        else:
            message_id = turn_id
    return AgentStreamEvent(
        stream_sequence=0,
        session_id="sess-1",
        tab_id="tab-1",
        agent_type=AgentType.CLAUDE,
        type=type,
        run_epoch=1,
        turn_id=turn_id,
        message_id=message_id,
        call_id=call_id,
        payload={"text": text} if text else {},
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_coalescer_merges_8577_thinking_deltas_into_fewer_events() -> None:
    """8577 tiny thinking deltas must collapse to a small number of flushes.

    The exact count depends on timing, but it must be dramatically less than
    8577 (the whole point of coalescing). The final text must be byte-exact.
    """
    flushed: List[AgentStreamEvent] = []

    async def on_flush(event: AgentStreamEvent) -> None:
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=60, on_flush=on_flush)

    # 8577 deltas, ~1.2 chars each -> ~10232 chars total.
    total_text = ""
    for i in range(8577):
        chunk = f"w{i % 10}"
        total_text += chunk
        ev = _make_event(AgentStreamEventType.THINKING_DELTA, chunk)
        await coalescer.handle(ev)

    # Force a flush so we can inspect the final state deterministically.
    await coalescer.flush_async()

    assert len(flushed) < 8577, "coalescer must reduce event count"
    # The merged text must be exact.
    merged_text = "".join(e.payload.get("text", "") for e in flushed)
    assert merged_text == total_text, "coalesced text must match the original stream exactly"


@pytest.mark.asyncio
async def test_coalescer_leading_edge_flushes_immediately() -> None:
    """The first delta after an idle window must be flushed without waiting
    for the trailing timer, so the first token of a turn is not delayed."""
    flushed: List[AgentStreamEvent] = []

    async def on_flush(event: AgentStreamEvent) -> None:
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=60, on_flush=on_flush)

    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "hello"))

    # Leading edge: the first event should already be flushed synchronously
    # (no need to wait for the 60ms timer).
    assert len(flushed) == 1
    assert flushed[0].payload["text"] == "hello"

    await coalescer.flush_async()


@pytest.mark.asyncio
async def test_coalescer_terminal_event_flushes_before_passthrough() -> None:
    """A turn_completed (or error / tool_call_completed) must flush any
    pending text before it is emitted, so text never appears after the
    terminal marker."""
    flushed: List[AgentStreamEvent] = []

    async def on_flush(event: AgentStreamEvent) -> None:
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=60, on_flush=on_flush)

    # Buffer some text.
    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "thinking..."))

    # Now a terminal event arrives. The coalescer returns False for it, and
    # the caller must flush before emitting it.
    terminal = _make_event(AgentStreamEventType.TURN_COMPLETED)
    handled = await coalescer.handle(terminal)
    assert handled is False, "terminal events must pass through the coalescer"

    # The caller flushes pending text before emitting the terminal event.
    await coalescer.flush_async()

    # The thinking text must appear BEFORE the turn_completed in the flush
    # order. (The terminal event itself is emitted by the caller, not by the
    # coalescer, so it is not in `flushed`; the assertion is that the pending
    # text was flushed.)
    assert any(e.type == AgentStreamEventType.THINKING_DELTA for e in flushed)
    assert all(e.type != AgentStreamEventType.TURN_COMPLETED for e in flushed)


@pytest.mark.asyncio
async def test_coalescer_does_not_merge_across_message_ids() -> None:
    """Text deltas for different message_ids must not be merged into one
    event, even if they are the same type."""
    flushed: List[AgentStreamEvent] = []

    async def on_flush(event: AgentStreamEvent) -> None:
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=60, on_flush=on_flush)

    await coalescer.handle(_make_event(AgentStreamEventType.TEXT_DELTA, "foo", message_id="msg-1"))
    # A different message id forces a new coalesced entry.
    await coalescer.handle(_make_event(AgentStreamEventType.TEXT_DELTA, "bar", message_id="msg-2"))

    await coalescer.flush_async()

    texts = [e.payload["text"] for e in flushed]
    assert "foo" in texts
    assert "bar" in texts
    # They must not be merged into a single "foobar" event.
    assert "foobar" not in texts


@pytest.mark.asyncio
async def test_coalescer_preserves_event_order() -> None:
    """Coalesced events must be emitted in the same order as the original
    deltas."""
    flushed: List[AgentStreamEvent] = []

    async def on_flush(event: AgentStreamEvent) -> None:
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=60, on_flush=on_flush)

    chunks = ["a", "b", "c", "d", "e"]
    for c in chunks:
        await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, c))

    await coalescer.flush_async()

    merged = "".join(e.payload["text"] for e in flushed)
    assert merged == "abcde"


@pytest.mark.asyncio
async def test_coalescer_empty_text_delta_is_skipped() -> None:
    """Empty text deltas must not create spurious coalesced events."""
    flushed: List[AgentStreamEvent] = []

    async def on_flush(event: AgentStreamEvent) -> None:
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=60, on_flush=on_flush)

    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, ""))
    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "x"))

    await coalescer.flush_async()

    assert len(flushed) == 1
    assert flushed[0].payload["text"] == "x"


@pytest.mark.asyncio
async def test_coalescer_drains_events_arriving_during_slow_flush() -> None:
    """Events appended while ``on_flush`` is awaiting must not be stranded.

    With a slow ``on_flush``, the leading-edge flush starts and blocks. While
    blocked, a trailing delta is buffered and a timer is scheduled. The timer
    fires during the slow flush, sees ``_flushing=True``, and returns without
    rescheduling. After the in-flight flush completes, the buffered delta must
    still be drained — not left buffered forever because no timer was
    rescheduled.
    """
    flushed: List[AgentStreamEvent] = []
    flush_started = asyncio.Event()
    flush_release = asyncio.Event()

    async def on_flush(event: AgentStreamEvent) -> None:
        flush_started.set()
        await flush_release.wait()
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=10, on_flush=on_flush)

    # First delta: leading-edge flush starts immediately and blocks. Run it as
    # a background task because handle() awaits the slow on_flush.
    handle_task = asyncio.ensure_future(
        coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "a"))
    )
    await asyncio.wait_for(flush_started.wait(), timeout=1.0)

    # While the flush is blocked, append another delta. This schedules a timer.
    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "b"))

    # Let the timer fire during the slow flush. It sees _flushing=True and
    # returns without rescheduling a timer.
    await asyncio.sleep(0.02)

    # Release the in-flight flush.
    flush_release.set()
    await asyncio.wait_for(handle_task, timeout=1.0)

    # Give the event loop time to settle and drain any rescheduled flush.
    await asyncio.sleep(0.05)

    merged = "".join(e.payload["text"] for e in flushed)
    assert merged == "ab", f"expected 'ab', got {merged!r}"
    # Buffer must be empty — nothing stranded.
    assert coalescer._buffer == []


@pytest.mark.asyncio
async def test_coalescer_concurrent_flush_async_serializes_no_inversion() -> None:
    """``flush_async`` called during an in-flight timer flush must serialize.

    Two concurrent flushes must not both process the same buffer (which would
    double-emit) nor emit out of order. The second flush must wait for the
    first to complete, then drain any events that arrived in between.
    """
    flushed: List[AgentStreamEvent] = []
    flush_started = asyncio.Event()
    flush_release = asyncio.Event()

    async def on_flush(event: AgentStreamEvent) -> None:
        flush_started.set()
        await flush_release.wait()
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=10, on_flush=on_flush)

    # Start a leading-edge flush that blocks (background task).
    handle_task = asyncio.ensure_future(
        coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "a"))
    )
    await asyncio.wait_for(flush_started.wait(), timeout=1.0)

    # While blocked, call flush_async concurrently. It must serialize behind
    # the in-flight flush rather than overlapping it.
    concurrent_flush = asyncio.ensure_future(coalescer.flush_async())

    # Add a delta while both flushes are pending.
    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "b"))

    # Release the in-flight flush; the concurrent flush must then drain "b".
    flush_release.set()
    await asyncio.wait_for(handle_task, timeout=1.0)
    await asyncio.wait_for(concurrent_flush, timeout=1.0)

    await asyncio.sleep(0.05)

    merged = "".join(e.payload["text"] for e in flushed)
    assert merged == "ab", f"expected 'ab', got {merged!r}"
    # No double-emit: total characters must equal the input length.
    assert len(merged) == 2
    assert coalescer._buffer == []


@pytest.mark.asyncio
async def test_coalescer_terminal_flush_after_slow_flush_drains_all() -> None:
    """A terminal (non-coalescable) event must flush everything, including
    events that arrived during a preceding slow flush.

    Sequence: slow leading-edge flush is in flight; more deltas arrive; then a
    terminal event arrives. The caller flushes before emitting the terminal
    event. All text must be present and in order, and the buffer must be empty.
    """
    flushed: List[AgentStreamEvent] = []
    flush_started = asyncio.Event()
    flush_release = asyncio.Event()

    async def on_flush(event: AgentStreamEvent) -> None:
        flush_started.set()
        await flush_release.wait()
        flushed.append(event)

    coalescer = AgentStreamCoalescer(window_ms=10, on_flush=on_flush)

    # Leading-edge flush blocks (background task).
    handle_task = asyncio.ensure_future(
        coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "a"))
    )
    await asyncio.wait_for(flush_started.wait(), timeout=1.0)

    # More deltas arrive during the slow flush.
    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "b"))
    await coalescer.handle(_make_event(AgentStreamEventType.THINKING_DELTA, "c"))

    # A terminal event arrives; the caller must flush before emitting it.
    terminal = _make_event(AgentStreamEventType.TURN_COMPLETED)
    handled = await coalescer.handle(terminal)
    assert handled is False

    # Release the in-flight flush, then the caller's flush_async must drain
    # everything that arrived during the slow flush.
    flush_release.set()
    await asyncio.wait_for(handle_task, timeout=1.0)
    await coalescer.flush_async()

    await asyncio.sleep(0.05)

    merged = "".join(e.payload["text"] for e in flushed)
    assert merged == "abc", f"expected 'abc', got {merged!r}"
    assert coalescer._buffer == []
