"""Manual Stop resets ANY wedged/reconnecting turn (handoff verification).

Companion to ``test_queue_status_wedge.py`` / ``test_traex_turn_wedge.py`` for
WIP commit 302cd62. These tests pin the four guarantees the fix adds, using the
same scriptable fake TraeX app-server and event-replay harness:

1.  A turn that keeps emitting real activity but never closes is left alone by
    the conservative auto-watchdogs (a long task must not be false-killed), yet
    a user Stop terminalizes the Hub turn and releases the guard IMMEDIATELY,
    before the provider kill/relaunch teardown finishes.
2.  With NO live turn, Stop is idempotent: a durable orphan is terminalized
    once and notified, a repeat Stop is a clean no-op (never a second edge), and
    with nothing open it is a no-op that does not fabricate an edge.
4.  A provider ``turn_completed`` arriving LATE (after Stop already persisted
    ``cancelled``) is dropped under the send lock — one terminal edge per turn;
    a late completion for an OLD turn can never clear a NEWER turn's guard; two
    near-simultaneous Stops are folded into one.
5.  While a manual Stop's provider teardown is in flight, EVERY auto-watchdog is
    disarmed, so a stale/queued/long turn cannot be double-reaped or raced.

(The frontend half — Reconnecting/FAILED/working Stop resetting on a 200 even
when the body says ``cancelled:false``, the 5/5 Retry path, and the one-RTT
``nudge`` — lives in ``frontend/tests/agentStreamStopReset.test.mjs``.)
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from claude_hub.models import AgentStreamEventType, AgentType
from claude_hub.services.agent_stream import native as native_module
from claude_hub.services.agent_stream import tailer as tailer_module
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.codex_jsonl import TraexJsonlAdapter
from claude_hub.services.agent_stream.native import TraexNativeSession
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import SessionTailer
from tests.test_agent_stream import _FakeNativeTransport, _native_session
from tests.test_queue_status_wedge import _completion_events
from tests.test_traex_turn_wedge import (
    CALL_ID,
    SESSION_ID,
    WS_ID,
    _AppServerHarness,
    _events,
    _patch_timings,
    _session,
    _tailer,
)


def _make_store(monkeypatch: pytest.MonkeyPatch, *, binary: str = "fake-traex") -> AgentStreamStore:
    """Build an isolated durable event store (plain helper, not a fixture)."""
    monkeypatch.setattr(native_module.shutil, "which", lambda _name: f"/usr/bin/{binary}")
    import importlib

    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_pkg, "STATE_ROOT", Path(tempfile.mkdtemp()))
    return AgentStreamStore(WS_ID, SESSION_ID)


@pytest.fixture
def claude_store(monkeypatch: pytest.MonkeyPatch) -> AgentStreamStore:
    monkeypatch.setattr(native_module.shutil, "which", lambda _name: "/usr/bin/fake-claude")
    import importlib

    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_pkg, "STATE_ROOT", Path(tempfile.mkdtemp()))
    return AgentStreamStore("ws-1", "sess-native")


# ── (1) live, never-closing turn: not watchdog-killed, but Stop is immediate ─


@pytest.mark.asyncio
async def test_live_never_closing_turn_survives_watchdogs_but_stop_releases_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous activity with no completion is a long task, not a wedge.

    The auto-watchdogs must NOT reap it across many timeout windows. A user Stop
    must nevertheless terminalize the Hub turn and drop the guard at once — even
    though the provider (unconfirmed interrupt) takes ~0.3s to restart.
    """
    _patch_timings(monkeypatch)
    # Every wall-clock watchdog is hair-trigger…
    monkeypatch.setattr(tailer_module, "ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S", 0.05)
    monkeypatch.setattr(tailer_module, "STREAM_INACTIVITY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(tailer_module, "QUEUED_TURN_MAX_WAIT_S", 0.05)
    # …and the TraeX interrupt will never confirm, forcing the restart path.
    harness = _AppServerHarness(monkeypatch, ["timeout", "confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    store = _make_store(monkeypatch)
    tailer = _tailer(session, transport, store)

    queue = await tailer.subscribe()
    await asyncio.sleep(0.1)
    await tailer.send_message("run a very long task", [], client_turn_id="turn-long")
    assert (await asyncio.wait_for(queue.get(), timeout=2.0)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    server = harness.current

    # Keep emitting genuine model activity well past every watchdog window; the
    # turn NEVER closes.
    for _ in range(30):
        server.notify("item/reasoning/textDelta", {"itemId": "r1", "delta": "working"})
        await asyncio.sleep(0.01)

    assert transport.turn_in_flight is True
    assert tailer._active_turn_id == "turn-long"
    assert len(harness.servers) == 1
    assert await _completion_events(store) == []  # no false terminal edge

    # User Stop: the Hub turn is terminal IMMEDIATELY, before the ~0.3s provider
    # interrupt timeout has elapsed (so the restart has not happened yet).
    assert await tailer.cancel_turn() is True
    assert tailer._active_turn_id is None
    completions = await _completion_events(store)
    assert completions and completions[-1].payload["status"] == "cancelled"
    assert completions[-1].turn_id == "turn-long"
    assert len(harness.servers) == 1  # provider teardown still in flight

    await asyncio.wait_for(tailer.stop(), timeout=5.0)


# ── (2) no active turn: orphan terminalized once, repeat Stop is a no-op ─────


def _orphan_tailer(store, *, observers=None) -> SessionTailer:
    # native_transport=None models a backend that restarted and owns no live
    # provider guard; only the durable stream can still hold an open turn.
    return SessionTailer(
        workspace_id="ws-wedge",
        session_id="sess-wedge",
        adapter=TraexJsonlAdapter(),
        session_getter=_session,
        store=store,
        native_transport=None,
        post_persist_observers=observers or [],
    )


@pytest.mark.asyncio
async def test_stop_with_no_live_turn_terminalizes_orphan_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(monkeypatch)
    ctx = NormalizeContext(
        session_id="sess-wedge",
        tab_id="tab-wedge",
        agent_type=AgentType.TRAEX,
        run_epoch=4,
        turn_id="turn-orphan",
    )
    await store.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": "stranded"}))

    observed = []
    done = asyncio.Event()

    async def _observe(event):
        observed.append(event)
        if event.type is AgentStreamEventType.TURN_COMPLETED:
            done.set()

    tailer = _orphan_tailer(store, observers=[_observe])

    # First Stop closes the durable orphan and fires its completion observer
    # (the signal that drains scheduled-run / Goal FIFOs).
    assert await tailer.cancel_turn() is True
    await asyncio.wait_for(done.wait(), timeout=1.0)
    edges = await _events(store)
    assert [e.type for e in edges][-2:] == [
        AgentStreamEventType.ERROR,
        AgentStreamEventType.TURN_COMPLETED,
    ]
    assert edges[-1].payload["status"] == "cancelled"
    assert edges[-1].turn_id == "turn-orphan"
    assert edges[-1].run_epoch == 4
    assert any(
        e.type is AgentStreamEventType.TURN_COMPLETED and e.turn_id == "turn-orphan"
        for e in observed
    )

    # Idempotent: a repeat Stop finds no open turn, returns False, persists
    # NOTHING new, and leaves no provider teardown pending.
    before = len(await _events(store))
    assert await tailer.cancel_turn() is False
    assert len(await _events(store)) == before
    assert tailer._turn_teardown_pending is False


@pytest.mark.asyncio
async def test_stop_with_no_turn_and_no_orphan_is_a_clean_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(monkeypatch)
    tailer = _orphan_tailer(store)
    assert await tailer.cancel_turn() is False
    assert (await store.read_since(-1, limit=10)).events == []
    assert tailer._turn_teardown_pending is False


# ── (4) late turn_completed vs Stop, stale-vs-newer guard, double Stop ──────


def _claude_tailer(session, transport, store) -> SessionTailer:
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    return SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        store=store,
        native_transport=transport,
    )


@pytest.mark.asyncio
async def test_late_turn_completed_after_stop_is_dropped_not_double_terminal(
    claude_store,
) -> None:
    """Stop persisted ``cancelled``; the provider's own completion lands late.

    It must be dropped (identity re-check under the send lock): exactly one
    terminal edge survives for the turn.
    """
    session = _native_session()
    transport = _FakeNativeTransport()
    tailer = _claude_tailer(session, transport, claude_store)
    await tailer.send_message("wedged", [], client_turn_id="turn-x")
    assert tailer._active_turn_id == "turn-x"

    assert await tailer.cancel_turn() is True
    await tailer._await_turn_teardown()
    assert tailer._active_turn_id is None

    # The consumer now receives the provider's terminal record for the SAME Hub
    # turn — the Stop-vs-natural-completion race, completion arriving second.
    ctx = NormalizeContext(
        session_id=session.id,
        tab_id=session.tab_id,
        agent_type=session.agent_type,
        run_epoch=tailer._run_epoch,
        turn_id="turn-x",
    )
    late = ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "completed"})
    consumed = await tailer._consume_turn_completion_record(late, late, transport)
    assert consumed is False

    completions = [
        e
        for e in (await claude_store.read_since(-1, limit=50)).events
        if e.type is AgentStreamEventType.TURN_COMPLETED
    ]
    assert len(completions) == 1
    assert completions[0].payload["status"] == "cancelled"  # Stop's edge wins


@pytest.mark.asyncio
async def test_late_completion_for_old_turn_clears_newer_guard_never(
    claude_store,
) -> None:
    """A stale completion must not terminalize the turn currently active."""
    session = _native_session()
    transport = _FakeNativeTransport()
    tailer = _claude_tailer(session, transport, claude_store)
    await tailer.send_message("new turn", [], client_turn_id="turn-new")

    ctx = NormalizeContext(
        session_id=session.id,
        tab_id=session.tab_id,
        agent_type=session.agent_type,
        run_epoch=tailer._run_epoch,
        turn_id="turn-old",
    )
    stale = ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "completed"})
    consumed = await tailer._consume_turn_completion_record(stale, stale, transport)
    assert consumed is False
    # The newer turn is untouched: still active, guard held, no edge persisted.
    assert tailer._active_turn_id == "turn-new"
    assert transport.turn_in_flight is True
    assert [e.type for e in (await claude_store.read_since(-1, limit=10)).events] == [
        AgentStreamEventType.TURN_STARTED
    ]


@pytest.mark.asyncio
async def test_rapid_double_stop_on_active_turn_is_one_edge(claude_store) -> None:
    session = _native_session()
    transport = _FakeNativeTransport()
    tailer = _claude_tailer(session, transport, claude_store)
    await tailer.send_message("double tap", [], client_turn_id="turn-z")
    results = await asyncio.gather(tailer.cancel_turn(), tailer.cancel_turn())
    assert results == [True, True]
    await tailer._await_turn_teardown()
    completions = [
        e
        for e in (await claude_store.read_since(-1, limit=20)).events
        if e.type is AgentStreamEventType.TURN_COMPLETED
    ]
    assert [c.payload["status"] for c in completions].count("cancelled") == 1
    # Tailer still usable afterwards.
    await tailer.send_message("after", [], client_turn_id="turn-z2")
    assert transport.sent_messages[-1][0] == "after"


# ── (5) watchdogs fully disarmed while a manual Stop teardown is pending ─────


@pytest.mark.asyncio
async def test_no_watchdog_reaps_while_stop_teardown_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During Stop's provider kill/relaunch, no watchdog may double-terminalize.

    The interrupt never confirms and its grace is held open for 1s, so for a
    ~0.4s observation window the provider guard is still held by the Stop
    teardown. Every watchdog predicate is forced expired; with the fix the outer
    ``watchdogs_armed`` gate (plus the in-lock recheck) suppresses every reap —
    one cancelled edge, no extra server restart.
    """
    _patch_timings(monkeypatch)
    monkeypatch.setattr(tailer_module, "POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(native_module, "_STARTUP_GRACE_S", 1.0)
    monkeypatch.setattr(tailer_module, "STREAM_INACTIVITY_TIMEOUT_S", 0.0)
    monkeypatch.setattr(tailer_module, "ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S", 0.0)
    monkeypatch.setattr(tailer_module, "QUEUED_TURN_MAX_WAIT_S", 0.0)
    monkeypatch.setattr(tailer_module, "QUEUE_HEARTBEAT_STALL_S", 0.0)

    harness = _AppServerHarness(monkeypatch, ["timeout", "confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    store = _make_store(monkeypatch)
    tailer = _tailer(session, transport, store)
    queue = await tailer.subscribe()
    await asyncio.sleep(0.1)
    await tailer.send_message("stalled", [], client_turn_id="turn-stall")
    assert (await asyncio.wait_for(queue.get(), timeout=2.0)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    harness.current.exec_item(CALL_ID, "started")

    # Force the inactivity / hard-liveness watchdogs to be READY TO FIRE on the
    # next tick (total silence, no approval exemption).
    tailer._last_event_at = time.monotonic() - 100.0

    assert await tailer.cancel_turn() is True
    assert tailer._turn_teardown_pending is True

    # Let many poll ticks run with every watchdog primed.
    await asyncio.sleep(0.4)

    completions = await _completion_events(store)
    assert len(completions) == 1
    assert completions[0].payload["status"] == "cancelled"
    assert completions[0].turn_id == "turn-stall"
    # Stop's own restart has not even fired yet (1s grace), and certainly no
    # watchdog added a second server.
    assert len(harness.servers) == 1

    await asyncio.wait_for(tailer.stop(), timeout=5.0)
