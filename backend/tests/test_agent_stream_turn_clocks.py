"""Watchdog clocks belong to each turn, not idle provider notifications."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType, SessionKind
from claude_hub.services.agent_stream import tailer as tailer_module
from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter
from claude_hub.services.agent_stream.tailer import SessionTailer
from tests.test_agent_stream import (
    _as_provider_session,
    _FakeNativeTransport,
    _isolate_state_root,
    _native_session,
)


class _ClockTransport(_FakeNativeTransport):
    def __init__(self) -> None:
        super().__init__(eof_is_fatal=True)
        self.active_thread_id = "clock-thread"
        self.polled = asyncio.Event()

    async def read_line(self) -> Any:
        self.polled.set()
        return await super().read_line()

    async def wait_for_poll(self) -> None:
        # A new read means the consumer has run its watchdog checks. Waiting
        # for that boundary avoids assuming a sleep let the consumer run.
        self.polled.clear()
        await asyncio.wait_for(self.polled.wait(), timeout=1)

    def acknowledge_provider_turn_started(self) -> None:
        self._turn_in_flight = True


@pytest.fixture
async def stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[tuple[SessionTailer, _ClockTransport, asyncio.Queue[AgentStreamEvent]]]:
    _isolate_state_root(monkeypatch, tmp_path)
    monkeypatch.setattr(tailer_module, "POLL_INTERVAL_S", 0.02)
    session = _native_session(AgentType.CODEX).model_copy(
        update={"session_kind": SessionKind.CHAT, "workspace_path": str(tmp_path)}
    )
    transport = _ClockTransport()
    transport.session = session
    owner = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=CodexJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=_as_provider_session(transport),
    )
    try:
        queue = await owner.subscribe()
        yield owner, transport, queue
    finally:
        await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_completion", [False, True], ids=["initial-idle", "after-turn"])
@pytest.mark.parametrize("idle_age", [60.0, 782.231], ids=["recent-control", "stale-control"])
async def test_new_send_owns_its_activity_clock(
    stream: tuple[SessionTailer, _ClockTransport, asyncio.Queue[AgentStreamEvent]],
    after_completion: bool,
    idle_age: float,
) -> None:
    owner, transport, queue = stream
    if after_completion:
        await owner.send_message("first", [], client_turn_id="previous-turn")
        assert (await asyncio.wait_for(queue.get(), 1)).type == AgentStreamEventType.TURN_STARTED
        transport._records.put_nowait(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "clock-thread",
                    "turn": {"id": "previous-provider-turn", "status": "completed"},
                },
            }
        )
        assert (await asyncio.wait_for(queue.get(), 1)).type == AgentStreamEventType.TURN_COMPLETED
        await transport.wait_for_poll()
        assert not transport.turn_in_flight

    transport._records.put_nowait(
        {"method": "thread/goal/cleared", "params": {"threadId": "clock-thread"}}
    )
    idle = await asyncio.wait_for(queue.get(), 1)
    assert idle.type == AgentStreamEventType.STATUS
    assert idle.turn_id is None
    assert owner._last_event_at is not None
    # Advance only this idle record's age; real asyncio deadlines stay intact.
    owner._last_event_at = time.monotonic() - idle_age
    await transport.wait_for_poll()

    started_at = time.monotonic()
    await owner.send_message("next", [], client_turn_id="new-turn")
    started = await asyncio.wait_for(queue.get(), 1)
    assert started.type == AgentStreamEventType.TURN_STARTED
    assert started.turn_id == "new-turn"
    assert owner._last_event_at is not None
    assert owner._last_event_at >= started_at
    await transport.wait_for_poll()
    assert owner._turn_in_flight_since is not None
    assert owner._turn_in_flight_since >= started_at
    assert owner._active_turn_id == "new-turn"
    assert transport.turn_in_flight
    assert not transport.stop_called
    assert queue.empty()
    page = await owner.store.read_since(-1, limit=20)
    assert not any(event.type == AgentStreamEventType.ERROR for event in page.events)


@pytest.mark.asyncio
async def test_provider_started_turn_owns_both_watchdog_clocks(
    stream: tuple[SessionTailer, _ClockTransport, asyncio.Queue[AgentStreamEvent]],
) -> None:
    owner, transport, queue = stream
    await transport.wait_for_poll()
    previous_clock = time.monotonic() - 782.231
    owner._last_event_at = previous_clock
    owner._turn_in_flight_since = previous_clock
    started_at = time.monotonic()
    transport._records.put_nowait(
        {
            "method": "turn/started",
            "params": {"threadId": "clock-thread", "turn": {"id": "provider-next-turn"}},
        }
    )
    started = await asyncio.wait_for(queue.get(), 1)
    assert started.type == AgentStreamEventType.TURN_STARTED
    assert started.turn_id is not None
    assert started.run_epoch == 1
    await transport.wait_for_poll()
    assert owner._last_event_at is not None
    assert owner._last_event_at >= started_at
    assert owner._turn_in_flight_since is not None
    assert owner._turn_in_flight_since >= started_at
    assert owner._active_turn_id == started.turn_id
    assert transport.turn_in_flight
    assert queue.empty()


@pytest.mark.asyncio
async def test_current_turn_silence_still_cancels_after_clock_reset(
    stream: tuple[SessionTailer, _ClockTransport, asyncio.Queue[AgentStreamEvent]],
) -> None:
    owner, transport, queue = stream
    await owner.send_message("silent", [], client_turn_id="silent-turn")
    assert (await asyncio.wait_for(queue.get(), 1)).type == AgentStreamEventType.TURN_STARTED
    await transport.wait_for_poll()
    owner._last_event_at = time.monotonic() - tailer_module.STREAM_INACTIVITY_TIMEOUT_S - 1

    error = await asyncio.wait_for(queue.get(), 1)
    completed = await asyncio.wait_for(queue.get(), 1)
    assert error.type == AgentStreamEventType.ERROR
    assert error.turn_id == "silent-turn"
    assert error.payload["message"] == "Turn stopped after the stream went silent."
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    assert completed.turn_id == "silent-turn"
    assert completed.payload["status"] == "cancelled"
    page = await owner.store.read_since(-1, limit=10)
    assert [event.type for event in page.events] == [
        AgentStreamEventType.TURN_STARTED,
        AgentStreamEventType.ERROR,
        AgentStreamEventType.TURN_COMPLETED,
    ]
