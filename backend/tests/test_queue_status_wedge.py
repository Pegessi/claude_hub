"""Tests for the provider capacity-queue (``queue/status``) wedge.

Reproduces the production incident on tab 2feb646a with the scriptable fake
TraeX app-server:

    turn_started → real tool/text activity → provider ``queue/status``
    ("Too many current requests. Your queue position is N.") every second,
    N never reaching capacity, NO turn_completed for hours → a transient
    provider ``error`` notice "Reconnecting… 1/5" → eventually Stop.

The failure modes under test:

* queue/status heartbeats used to count as model-stream liveness, so a turn
  queued for hours was kept "active" forever and neither watchdog fired;
* the provider's recoverable "Reconnecting… n/m" ``error`` notice released the
  frontend active-turn lock, hiding Stop;
* the ~1/s snapshot flood accumulated against the durable stream / poll;
* Stop during a queue had to wait on provider teardown.

Fixes: queue heartbeats do not refresh liveness; a live-but-capped queue is
cancelled, a stalled (silent) queue is reaped and resumed; Stop persists the
terminal edge and releases the Hub guard before provider teardown; identical
snapshots are suppressed; reconnect notices are a coalesced STATUS.
"""

from __future__ import annotations

import asyncio
import itertools
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, cast

import pytest

from claude_hub.models import AgentStreamEventType, AgentType
from claude_hub.services.agent_stream import native as native_module
from claude_hub.services.agent_stream import tailer as tailer_module
from claude_hub.services.agent_stream.codex_jsonl import TraexJsonlAdapter
from claude_hub.services.agent_stream.native import ProviderSession, TraexNativeSession
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import SessionTailer
from tests.test_traex_agent import _ctx
from tests.test_traex_turn_wedge import (
    CALL_ID,
    SESSION_ID,
    THREAD_ID,
    WS_ID,
    _AppServerHarness,
    _event_types,
    _events,
    _patch_timings,
    _session,
    _tailer,
    _wait_until,
)


def _queue_message(position: int) -> str:
    return (
        f"Too many current requests. Your queue position is {position}. " "Please wait for a while."
    )


def _queue_notify(server: Any, *, position: int, state: str = "queued") -> None:
    params: Dict[str, Any] = {"state": state}
    if state == "queued":
        params["position"] = position
        params["message"] = _queue_message(position)
    server.notify("queue/status", params)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> AgentStreamStore:
    monkeypatch.setattr(native_module.shutil, "which", lambda _name: "/usr/bin/fake-traex")
    tmp = tempfile.mkdtemp()
    import importlib

    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_pkg, "STATE_ROOT", Path(tmp))
    return AgentStreamStore(WS_ID, SESSION_ID)


_OwnedAppServers = Tuple[Path, List[_AppServerHarness]]


@pytest.fixture
async def app_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[_OwnedAppServers, None]:
    # Dependency order keeps mocks installed until each owned runtime closes.
    harnesses: List[_AppServerHarness] = []
    yield tmp_path, harnesses
    errors: List[Exception] = []
    for harness in reversed(harnesses):
        try:
            await harness.aclose()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("fake app-server fixture cleanup failed", errors)


def _make_runtime(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
    app_servers: _OwnedAppServers,
    *,
    interrupt_modes: Optional[List[str]] = None,
    hold_initialize_at: Optional[int] = None,
) -> Tuple[_AppServerHarness, TraexNativeSession, SessionTailer]:
    root, harnesses = app_servers
    harness = _AppServerHarness(
        monkeypatch, interrupt_modes or ["confirm"], hold_initialize_at=hold_initialize_at
    )
    harnesses.append(harness)
    session = _session()
    transport = harness.create_transport(session, root)
    assert isinstance(transport, TraexNativeSession)
    return harness, transport, harness.create_tailer(session, transport, store)


async def _recovery_barrier(tailer: SessionTailer) -> None:
    # The watchdog holds this lock through its entire inline recovery.
    async with tailer._send_lock:
        pass


def _assert_resumed(harness: _AppServerHarness, transport: TraexNativeSession) -> None:
    assert len(harness.servers) == 2
    replacement = harness.servers[1]
    resumes = replacement.request_params("thread/resume")
    assert len(resumes) == 1 and resumes[0]["threadId"] == THREAD_ID
    assert "thread/start" not in replacement.request_methods()
    assert transport.turn_in_flight is False
    assert transport._started is True
    assert transport._reader_task is not None and not transport._reader_task.done()


async def _start_turn(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
    app_servers: _OwnedAppServers,
    *,
    interrupt_modes: Optional[List[str]] = None,
    agent_type: AgentType = AgentType.TRAEX,
    client_turn_id: str = "turn-q1",
    text: str = "do work",
    hold_initialize_at: Optional[int] = None,
) -> Tuple[_AppServerHarness, TraexNativeSession, SessionTailer]:
    if agent_type != AgentType.TRAEX:
        raise AssertionError("queue/status tests target TraeX")
    _patch_timings(monkeypatch)
    harness, transport, tailer = _make_runtime(
        monkeypatch,
        store,
        app_servers,
        interrupt_modes=interrupt_modes,
        hold_initialize_at=hold_initialize_at,
    )
    queue = await tailer.subscribe()
    await transport.start()
    await tailer.send_message(text, [], client_turn_id=client_turn_id)
    assert (
        await asyncio.wait_for(queue.get(), timeout=2.0)
    ).type == AgentStreamEventType.TURN_STARTED
    return harness, transport, tailer


async def _heartbeat(
    server: Any, stop_event: asyncio.Event, *, interval: float = 0.02, vary_position: bool = True
) -> None:
    positions = itertools.count(357, step=-1)
    constant = 200
    while not stop_event.is_set():
        position = next(positions) if vary_position else constant
        _queue_notify(server, position=position)
        await asyncio.sleep(interval)


async def _completion_events(store: AgentStreamStore) -> List[Any]:
    return [e for e in await _events(store) if e.type == AgentStreamEventType.TURN_COMPLETED]


# ── adapter: reconnect notice classification ────────────────────────────────


def test_reconnect_notice_maps_to_coalesced_status_not_terminal_error() -> None:
    """'Reconnecting… n/m' is recoverable: STATUS, not a turn-terminal ERROR."""
    adapter = TraexJsonlAdapter()
    events = adapter.normalize_line(
        {"method": "error", "params": {"error": {"message": "Reconnecting… 1/5"}}},
        _ctx(),
    )
    assert len(events) == 1
    event = events[0]
    assert event.type is AgentStreamEventType.STATUS
    assert event.payload["provider_status"] == "provider/reconnecting"
    assert event.payload["snapshot"] is True
    assert event.message_id == "provider-status:reconnect"


def test_retrying_notice_also_maps_to_status() -> None:
    adapter = TraexJsonlAdapter()
    events = adapter.normalize_line(
        {"method": "error", "params": {"error": {"message": "Retrying request (2/4)"}}},
        _ctx(),
    )
    assert events[0].type is AgentStreamEventType.STATUS


def test_genuine_error_still_maps_to_terminal_error() -> None:
    adapter = TraexJsonlAdapter()
    events = adapter.normalize_line(
        {"method": "error", "params": {"error": {"message": "Internal server error"}}},
        _ctx(),
    )
    assert events[0].type is AgentStreamEventType.ERROR


def test_queue_status_is_a_stable_snapshot_status() -> None:
    adapter = TraexJsonlAdapter()
    events = adapter.normalize_line(
        {"method": "queue/status", "params": {"state": "queued", "position": 12}},
        _ctx(),
    )
    event = events[0]
    assert event.type is AgentStreamEventType.STATUS
    assert event.payload["snapshot"] is True
    assert event.message_id == "traex-status:queue/status"


# ── Stop must always work during a queue ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_during_endless_queue_releases_guard_and_resends(
    monkeypatch: pytest.MonkeyPatch, store: AgentStreamStore, app_servers: _OwnedAppServers
) -> None:
    """turn_started → activity → endless queue/status → Stop frees everything."""
    harness, transport, tailer = await _start_turn(
        monkeypatch, store, app_servers, interrupt_modes=["timeout", "confirm"]
    )
    server = harness.current
    server.exec_item(CALL_ID, "started")
    server.exec_item(CALL_ID, "completed")
    await _wait_until(2.0, lambda: _has(store, AgentStreamEventType.TOOL_CALL_COMPLETED))

    stop_event = asyncio.Event()
    flood = harness.own_task(asyncio.create_task(_heartbeat(server, stop_event)))
    try:
        # The turn stays in flight while queued (queue is not completion).
        await asyncio.sleep(0.15)
        assert transport.turn_in_flight is True
        assert tailer._waiting_for_model_capacity is True

        # Stop even though the provider interrupt will be unconfirmed (timeout
        # mode): kill+relaunch+resume runs, but the Hub guard is released and
        # the cancelled edge persisted IMMEDIATELY — before the bounded
        # background provider teardown finishes — so the composer unlocks.
        assert await tailer.cancel_turn() is True
        assert tailer._active_turn_id is None
        completions = await _completion_events(store)
        assert completions and completions[-1].payload["status"] == "cancelled"
        assert completions[-1].turn_id == "turn-q1"

        # Provider-side teardown (kill + relaunch + resume) completes shortly
        # after, in the background: the cancel call did not wait for it.
        await tailer._await_turn_teardown()
        assert transport.turn_in_flight is False
        assert len(harness.servers) == 2
        assert "thread/resume" in harness.current.request_methods()

        # A new message sends without the "already in flight" rejection.
        await tailer.send_message("continue", [], client_turn_id="turn-q2")
        assert harness.current.request_params("turn/start")
    finally:
        stop_event.set()
        flood.cancel()


@pytest.mark.asyncio
async def test_terminal_edge_and_guard_release_precede_provider_teardown(
    store: AgentStreamStore,
) -> None:
    """The terminal edge must precede the provider's slow cancellation."""

    class _SlowCancelTransport:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def cancel_active_turn(self) -> None:
            self.entered.set()
            await self.release.wait()

    session = _session()
    transport = _SlowCancelTransport()
    tailer = _tailer(session, transport, store)
    tailer._active_turn_id = "turn-slow"
    tailer._run_epoch = 1
    teardown: Optional[asyncio.Task[None]] = None
    try:
        await tailer._cancel_active_turn_locked(cast(ProviderSession, transport))
        teardown = tailer._turn_teardown_task
        assert teardown is not None
        await asyncio.wait_for(transport.entered.wait(), timeout=2.0)
        assert tailer._active_turn_id is None
        completions = await _completion_events(store)
        assert completions and completions[-1].payload["status"] == "cancelled"
        assert tailer._turn_teardown_pending is True
        transport.release.set()
        await asyncio.wait_for(tailer._await_turn_teardown(), timeout=2.0)
        assert tailer._turn_teardown_pending is False
    finally:
        transport.release.set()
        task = teardown if teardown is not None else tailer._turn_teardown_task
        if task is not None:
            _, slow = await asyncio.wait({task}, timeout=2.0)
            if slow:
                task.cancel()
            _, pending = await asyncio.wait({task}, timeout=2.0)
            assert not pending, "slow-cancel teardown did not terminate"
            if not task.cancelled():
                task.result()
            assert not slow, "slow-cancel teardown exceeded its cleanup budget"


@pytest.mark.asyncio
async def test_double_cancel_during_queue_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, store: AgentStreamStore, app_servers: _OwnedAppServers
) -> None:
    harness, transport, tailer = await _start_turn(monkeypatch, store, app_servers)
    server = harness.current
    stop_event = asyncio.Event()
    flood = harness.own_task(asyncio.create_task(_heartbeat(server, stop_event)))
    try:
        await asyncio.sleep(0.1)
        results = await asyncio.gather(tailer.cancel_turn(), tailer.cancel_turn())
        # Both Stops report success and are idempotent: the second is folded
        # into the first one's already-running teardown.
        assert results[0] is True
        assert results[1] is True
        completions = await _completion_events(store)
        assert [c.payload["status"] for c in completions].count("cancelled") == 1
        await tailer._await_turn_teardown()
        # Still usable.
        await tailer.send_message("again", [], client_turn_id="turn-q3")
        assert harness.current.request_params("turn/start")
    finally:
        stop_event.set()
        flood.cancel()


# ── snapshot suppression / reconnect does not release the lock ──────────────


@pytest.mark.asyncio
async def test_identical_queue_snapshots_are_suppressed_but_position_changes_persist(
    monkeypatch: pytest.MonkeyPatch, store: AgentStreamStore, app_servers: _OwnedAppServers
) -> None:
    harness, transport, tailer = await _start_turn(monkeypatch, store, app_servers)
    server = harness.current
    # 5 identical heartbeats → one persisted snapshot.
    for _ in range(5):
        _queue_notify(server, position=100)
    await asyncio.sleep(0.15)
    queue_statuses = [
        e
        for e in await _events(store)
        if e.type is AgentStreamEventType.STATUS
        and (e.payload or {}).get("provider_status") == "queue/status"
    ]
    assert len(queue_statuses) == 1
    # A changed position persists as the in-place update.
    _queue_notify(server, position=99)
    _queue_notify(server, position=99)
    await asyncio.sleep(0.15)
    queue_statuses = [
        e
        for e in await _events(store)
        if e.type is AgentStreamEventType.STATUS
        and (e.payload or {}).get("provider_status") == "queue/status"
    ]
    assert len(queue_statuses) == 2
    assert queue_statuses[-1].payload["text"] == _queue_message(99)
    await tailer.cancel_turn()


@pytest.mark.asyncio
async def test_reconnect_notice_keeps_the_turn_locked(
    monkeypatch: pytest.MonkeyPatch, store: AgentStreamStore, app_servers: _OwnedAppServers
) -> None:
    """A provider 'Reconnecting…' while queued must not end the Hub turn."""
    harness, transport, tailer = await _start_turn(monkeypatch, store, app_servers)
    server = harness.current
    _queue_notify(server, position=50)
    server.notify("error", {"error": {"message": "Reconnecting… 1/5"}})
    await asyncio.sleep(0.15)
    assert transport.turn_in_flight is True
    types = await _event_types(store)
    assert AgentStreamEventType.ERROR not in types
    # Stop still finds and cancels the active turn.
    assert await tailer.cancel_turn() is True


# ── the queue watchdog ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_queue_past_cap_is_cancelled_without_restart(
    monkeypatch: pytest.MonkeyPatch, store: AgentStreamStore, app_servers: _OwnedAppServers
) -> None:
    """A healthy, heartbeating queue that never gets capacity is cancelled."""
    monkeypatch.setattr(tailer_module, "QUEUED_TURN_MAX_WAIT_S", 0.25)
    monkeypatch.setattr(tailer_module, "QUEUE_HEARTBEAT_STALL_S", 300.0)
    harness, transport, tailer = await _start_turn(monkeypatch, store, app_servers)
    server = harness.current
    stop_event = asyncio.Event()
    flood = harness.own_task(asyncio.create_task(_heartbeat(server, stop_event, interval=0.02)))
    try:

        async def _capped() -> bool:
            completions = await _completion_events(store)
            return bool(completions) and not transport.turn_in_flight

        await _wait_until(5.0, _capped)
        # Interrupt confirmed → same server, no relaunch (resending re-queues).
        assert len(harness.servers) == 1
        completion = (await _completion_events(store))[-1]
        assert completion.payload["status"] == "cancelled"
        await tailer.send_message("retry me", [], client_turn_id="turn-cap2")
        assert harness.current.request_params("turn/start")
    finally:
        stop_event.set()
        flood.cancel()


@pytest.mark.asyncio
async def test_silent_queue_heartbeat_is_reaped_and_resumed(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
    app_servers: _OwnedAppServers,
) -> None:
    """A silent queue's replacement must finish thread/resume before assertions."""
    monkeypatch.setattr(tailer_module, "QUEUE_HEARTBEAT_STALL_S", 0.2)
    monkeypatch.setattr(tailer_module, "QUEUED_TURN_MAX_WAIT_S", 300.0)
    harness, transport, tailer = await _start_turn(
        monkeypatch, store, app_servers, interrupt_modes=["timeout", "confirm"]
    )
    for _ in range(3):
        _queue_notify(harness.current, position=80)

    async def _replacement_created() -> bool:
        return bool(await _completion_events(store)) and len(harness.servers) == 2

    await _wait_until(5.0, _replacement_created)
    barrier = harness.own_task(asyncio.create_task(_recovery_barrier(tailer)))
    await asyncio.wait_for(barrier, timeout=5.0)
    _assert_resumed(harness, transport)
    assert (await _completion_events(store))[-1].payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_queue_then_generation_completes_normally(
    monkeypatch: pytest.MonkeyPatch, store: AgentStreamStore, app_servers: _OwnedAppServers
) -> None:
    """Queue → ready → real output → turn/completed(completed): not cancelled."""
    monkeypatch.setattr(tailer_module, "QUEUED_TURN_MAX_WAIT_S", 0.25)
    monkeypatch.setattr(tailer_module, "QUEUE_HEARTBEAT_STALL_S", 300.0)
    harness, transport, tailer = await _start_turn(monkeypatch, store, app_servers)
    server = harness.current
    _queue_notify(server, position=3)
    _queue_notify(server, position=2)
    _queue_notify(server, position=1, state="ready")
    server.notify("item/agentMessage/delta", {"turnId": "provider-turn-1", "delta": "done"})
    server.turn_completed("provider-turn-1", "completed")

    async def _done() -> bool:
        completions = await _completion_events(store)
        return bool(completions) and completions[-1].payload["status"] == "completed"

    await _wait_until(3.0, _done)
    assert transport.turn_in_flight is False
    assert tailer._waiting_for_model_capacity is False
    completions = await _completion_events(store)
    assert all(c.payload["status"] == "completed" for c in completions)


@pytest.mark.asyncio
async def test_single_queued_notice_mid_generation_does_not_cancel(
    monkeypatch: pytest.MonkeyPatch, store: AgentStreamStore, app_servers: _OwnedAppServers
) -> None:
    """An occasional queue/status inside an otherwise active turn is harmless."""
    monkeypatch.setattr(tailer_module, "QUEUED_TURN_MAX_WAIT_S", 0.2)
    harness, transport, tailer = await _start_turn(monkeypatch, store, app_servers)
    server = harness.current
    server.exec_item(CALL_ID, "started")
    _queue_notify(server, position=1)  # transient, immediately followed by work
    server.exec_item(CALL_ID, "completed")
    server.notify("item/agentMessage/delta", {"turnId": "provider-turn-1", "delta": "x"})
    server.turn_completed("provider-turn-1", "completed")

    async def _completed_ok() -> bool:
        return any(c.payload["status"] == "completed" for c in (await _completion_events(store)))

    await _wait_until(3.0, _completed_ok)
    assert transport.turn_in_flight is False
    assert not any(c.payload["status"] == "cancelled" for c in (await _completion_events(store)))


async def _has(store: AgentStreamStore, event_type: AgentStreamEventType) -> bool:
    return event_type in await _event_types(store)


@pytest.mark.asyncio
async def test_replacement_created_is_not_recovery_completed(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
    app_servers: _OwnedAppServers,
) -> None:
    monkeypatch.setattr(tailer_module, "QUEUE_HEARTBEAT_STALL_S", 0.2)
    monkeypatch.setattr(tailer_module, "QUEUED_TURN_MAX_WAIT_S", 300.0)
    harness, transport, tailer = await _start_turn(
        monkeypatch,
        store,
        app_servers,
        interrupt_modes=["timeout", "confirm"],
        hold_initialize_at=1,
    )
    for _ in range(3):
        _queue_notify(harness.current, position=80)
    await asyncio.wait_for(harness.initialize_blocked.wait(), timeout=5.0)
    assert await _completion_events(store)
    assert len(harness.servers) == 2
    assert transport.turn_in_flight is False
    assert harness.current.request_methods() == ["initialize"]
    entered = asyncio.Event()

    async def wait_for_real_recovery() -> None:
        entered.set()
        await _recovery_barrier(tailer)

    barrier = harness.own_task(asyncio.create_task(wait_for_real_recovery()))
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    assert not barrier.done()
    assert tailer._send_lock.locked()
    harness.release_initialize.set()
    await asyncio.wait_for(barrier, timeout=5.0)
    _assert_resumed(harness, transport)
    assert (await _completion_events(store))[-1].payload["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initialize", "assertion"])
async def test_owned_runtime_closes_before_patch_restore_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
    app_servers: _OwnedAppServers,
    stage: str,
) -> None:
    previous_exec = native_module.asyncio.create_subprocess_exec
    with monkeypatch.context() as scoped:
        _patch_timings(scoped)
        harness, transport, tailer = _make_runtime(
            scoped,
            store,
            app_servers,
            hold_initialize_at=0 if stage == "initialize" else None,
        )
        fake_exec = native_module.asyncio.create_subprocess_exec
        command = transport._build_command()
        with pytest.raises(AssertionError, match=f"^injected {stage} failure$"):
            try:
                await tailer.subscribe()
                if stage == "initialize":
                    await asyncio.wait_for(harness.initialize_blocked.wait(), timeout=2.0)
                    assert harness.current.request_methods() == ["initialize"]
                else:
                    await transport.start()
                    await tailer.send_message("work", [], client_turn_id="failure-turn")
                    assert transport.turn_in_flight is True
                raise AssertionError(f"injected {stage} failure")
            finally:
                await harness.aclose()
        assert native_module.asyncio.create_subprocess_exec is fake_exec
        assert harness.closed
        assert harness.tasks and all(task.done() for task in harness.tasks)
        assert harness.servers and all(
            server.proc._terminated.is_set() for server in harness.servers
        )
        assert tailer._task is None
        assert transport._process is None
        assert transport.turn_in_flight is False
    assert native_module.asyncio.create_subprocess_exec is previous_exec
    assert transport._build_command() == command
    assert Path(command[0]).is_absolute()
    assert not Path(command[0]).parent.exists()


@pytest.mark.asyncio
async def test_owned_task_failure_is_not_hidden_by_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    previous_exec = native_module.asyncio.create_subprocess_exec
    with monkeypatch.context() as scoped:
        harness = _AppServerHarness(scoped, ["confirm"])

        async def fail() -> None:
            raise RuntimeError("owned cleanup failure")

        failure = harness.own_task(asyncio.create_task(fail(), name="deliberate-owned-failure"))
        _, pending = await asyncio.wait({failure}, timeout=2.0)
        assert not pending
        with pytest.raises(AssertionError) as error:
            await harness.aclose()
        assert str(error.value) == (
            "fake app-server cleanup failed: deliberate-owned-failure: RuntimeError('owned cleanup failure')"
        )
        assert harness.closed and all(task.done() for task in harness.tasks)
    assert native_module.asyncio.create_subprocess_exec is previous_exec


@pytest.mark.asyncio
async def test_unknown_command_is_reported_even_when_caller_catches_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    previous_exec = native_module.asyncio.create_subprocess_exec
    with monkeypatch.context() as scoped:
        harness = _AppServerHarness(scoped, ["confirm"])
        unknown_command = str(tmp_path / "never-created-command" / "unknown")
        try:
            await native_module.asyncio.create_subprocess_exec(unknown_command)
        except AssertionError:
            pass
        else:
            pytest.fail("an unregistered command was accepted")
        with pytest.raises(AssertionError) as error:
            await harness.aclose()
        assert (
            str(error.value)
            == "fake app-server cleanup failed: unexpected command in fake app-server test"
        )
        assert harness.closed and not harness.servers
        assert all(task.done() for task in harness.tasks)
    assert native_module.asyncio.create_subprocess_exec is previous_exec
