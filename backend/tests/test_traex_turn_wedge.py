"""Tests for the TraeX native-chat turn wedge recovery.

Reproduces the production sequence with a scriptable fake app-server:

    item/started (exec) → item/completed → NEVER turn/completed
    → (hours later) duplicate item/completed with no turn id
    → thread/goal/updated (turnId=null)

Assertions:

* Stop always releases the Hub active-turn guard; when the provider-native
  interrupt cannot be confirmed, the app-server is killed and restarted,
  resuming the same thread, and the next message sends normally.
* A silent active turn (including one with an outstanding tool) is bounded by
  the hard liveness watchdog; a turn that keeps emitting events is not.
* Unattributed replay records and ``thread/goal/updated`` (turnId=null)
  neither mint a phantom turn nor wedge the composer.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream import native as native_module
from claude_hub.services.agent_stream import tailer as tailer_module
from claude_hub.services.agent_stream.codex_jsonl import TraexJsonlAdapter
from claude_hub.services.agent_stream.native import (
    CodexNativeSession,
    TraexNativeSession,
)
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import SessionTailer

WS_ID = "ws-wedge"
SESSION_ID = "sess-wedge"
THREAD_ID = "th-wedge-1"
CALL_ID = "code-mode-nested:29:call_abc:exec-1"


# ── Scriptable fake app-server ──────────────────────────────────────────────


class _FakeStderr:
    async def read(self, _n: int) -> bytes:
        return b""


class _FakeStdout:
    def __init__(self, server: "_FakeAppServer") -> None:
        self._server = server
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()

    def push(self, line: bytes) -> None:
        self._queue.put_nowait(line)

    async def read(self, _n: int) -> bytes:
        return await self._queue.get()


class _FakeStdin:
    def __init__(self, server: "_FakeAppServer") -> None:
        self._server = server
        self.written: List[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)
        loop = asyncio.get_event_loop()
        for line in data.decode("utf-8").strip().splitlines():
            if line:
                loop.call_soon(self._server.handle_message, json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeAppProc:
    def __init__(self, server: "_FakeAppServer") -> None:
        self._server = server
        self.stdout = _FakeStdout(server)
        self.stdin = _FakeStdin(server)
        self.stderr = _FakeStderr()
        self._terminated = asyncio.Event()

    async def wait(self) -> int:
        await self._terminated.wait()
        return 0

    def terminate(self) -> None:
        self._signal_dead()

    def kill(self) -> None:
        self._signal_dead()

    def _signal_dead(self) -> None:
        if not self._terminated.is_set():
            self._terminated.set()
            # EOF on stdout, mirroring a real killed app-server.
            self.stdout.push(b"")


class _FakeAppServer:
    """JSON-RPC app-server whose interrupt behaviour is scriptable.

    ``interrupt_mode``:

    * ``"confirm"`` — answer turn/interrupt and emit turn/completed(interrupted)
    * ``"timeout"`` — never answer turn/interrupt (frozen turn)
    * ``"error"``   — answer turn/interrupt with a JSON-RPC error
    """

    def __init__(self, thread_id: str, *, interrupt_mode: str = "confirm") -> None:
        self.thread_id = thread_id
        self.interrupt_mode = interrupt_mode
        self.proc = _FakeAppProc(self)
        self.requests: List[Dict[str, Any]] = []
        self.notifications: List[Dict[str, Any]] = []
        self.turn_seq = 0

    # ── outbound helpers ─────────────────────────────────────────────────────

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self.proc.stdout.push(
            (json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode()
        )

    def exec_item(self, call_id: str, phase: str, *, status: str = "completed") -> None:
        item: Dict[str, Any] = {
            "id": call_id,
            "type": "commandExecution",
            "command": "sleep 9999",
            "cwd": "/tmp",
        }
        if phase == "completed":
            item["status"] = status
        self.notify(f"item/{phase}", {"item": item})

    def turn_completed(self, turn_id: str, status: str = "completed") -> None:
        self.notify("turn/completed", {"turn": {"id": turn_id, "status": status}})

    def goal_updated(self, *, turn_id: Optional[str], status: str = "complete") -> None:
        self.notify(
            "thread/goal/updated",
            {
                "threadId": self.thread_id,
                "turnId": turn_id,
                "goal": {"objective": "keep going", "status": status},
            },
        )

    # ── JSON-RPC handling ────────────────────────────────────────────────────

    def handle_message(self, msg: Dict[str, Any]) -> None:
        if "id" not in msg:
            self.notifications.append(msg)
            return
        self.requests.append(msg)
        method = msg["method"]
        params = msg.get("params") or {}
        result: Optional[Dict[str, Any]]
        error: Optional[Dict[str, Any]] = None
        if method == "initialize":
            result = {}
        elif method in {"thread/resume", "thread/start"}:
            result = {"thread": {"id": params.get("threadId") or self.thread_id}, "model": "m-1"}
        elif method == "turn/start":
            self.turn_seq += 1
            result = {"turn": {"id": f"provider-turn-{self.turn_seq}"}}
        elif method in {"turn/interrupt", "turn/cancel"}:
            if self.interrupt_mode == "timeout":
                # Frozen provider: never answer. The client must time out.
                return
            if self.interrupt_mode == "error":
                self.respond(msg["id"], error={"code": -32603, "message": "turn not active"})
                return
            result = {}
            self.turn_completed(
                f"provider-turn-{self.turn_seq}",
                status="interrupted" if method == "turn/interrupt" else "cancelled",
            )
        else:
            result = {}
        self.respond(msg["id"], result=result)

    def respond(
        self,
        req_id: int,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        self.proc.stdout.push((json.dumps(payload) + "\n").encode())

    def request_methods(self) -> List[str]:
        return [msg["method"] for msg in self.requests]

    def request_params(self, method: str) -> List[Dict[str, Any]]:
        return [msg.get("params") or {} for msg in self.requests if msg["method"] == method]


class _AppServerHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, interrupt_modes: List[str]) -> None:
        self.servers: List[_FakeAppServer] = []
        self._interrupt_modes = list(interrupt_modes)

        async def fake_exec(*_args: Any, **_kwargs: Any) -> _FakeAppProc:
            mode = self._interrupt_modes[min(len(self.servers), len(self._interrupt_modes) - 1)]
            server = _FakeAppServer(THREAD_ID, interrupt_mode=mode)
            self.servers.append(server)
            return server.proc

        monkeypatch.setattr(
            native_module.asyncio, "create_subprocess_exec", fake_exec, raising=True
        )

    @property
    def current(self) -> _FakeAppServer:
        return self.servers[-1]


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> AgentStreamStore:
    monkeypatch.setattr(
        native_module.shutil,
        "which",
        lambda _name: "/usr/bin/fake-traex",
    )
    tmp = tempfile.mkdtemp()
    import importlib

    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_pkg, "STATE_ROOT", Path(tmp))
    return AgentStreamStore(WS_ID, SESSION_ID)


def _session(agent_type: AgentType = AgentType.TRAEX) -> ManagedSession:
    return ManagedSession(
        id=SESSION_ID,
        workspace_id=WS_ID,
        tab_id="tab-wedge",
        role=WorkspaceSessionRole.WORKER,
        agent_type=agent_type,
        status=ManagedSessionStatus.IDLE,
        title="wedge",
        session_kind=SessionKind.CHAT,
        workspace_path="/tmp",
        tmux_session="tmux-wedge",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _tailer(session: ManagedSession, transport: Any, store: AgentStreamStore) -> SessionTailer:
    return SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=TraexJsonlAdapter(),
        session_getter=lambda: session,
        store=store,
        native_transport=transport,
    )


async def _event_types(store: AgentStreamStore) -> List[AgentStreamEventType]:
    page = await store.read_since(-1, limit=500)
    return [event.type for event in page.events]


async def _events(store: AgentStreamStore) -> List[Any]:
    return list((await store.read_since(-1, limit=500)).events)


async def _wait_until(deadline_s: float, predicate: Any) -> None:
    loop_deadline = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < loop_deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met before deadline")


def _patch_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tailer_module, "POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(tailer_module, "IDLE_TTL_S", 300.0)
    monkeypatch.setattr(native_module, "_STARTUP_GRACE_S", 0.3)


# ── transport-level recovery ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_traex_unconfirmed_interrupt_kills_restarts_and_resumes_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frozen TraeX turn: Stop must restart the app-server and thread/resume."""
    _patch_timings(monkeypatch)
    harness = _AppServerHarness(monkeypatch, ["timeout", "confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    transport._conversation_id = THREAD_ID

    with patch.object(native_module.shutil, "which", lambda _name: "/usr/bin/fake-traex"):
        await transport.start()
        await transport.send_message("do work", [])
        assert transport.turn_in_flight is True
        await transport.cancel_active_turn()

    assert transport.turn_in_flight is False
    assert len(harness.servers) == 2
    new_server = harness.servers[1]
    assert "initialize" in new_server.request_methods()
    resumes = new_server.request_params("thread/resume")
    assert resumes and resumes[0]["threadId"] == THREAD_ID
    assert "thread/start" not in new_server.request_methods()
    # The interrupted turn's discard filter must not leak into the new process.
    assert transport._discard_turn_id is None
    # Expected-EOF flag stays raised until the push consumer acknowledges it.
    assert transport._client_requested_stop is True
    await transport.stop()


@pytest.mark.asyncio
async def test_codex_cancel_rpc_failure_restarts_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Codex-family fallback is identical: failed turn/cancel → resume."""
    _patch_timings(monkeypatch)
    harness = _AppServerHarness(monkeypatch, ["error", "confirm"])
    session = _session(AgentType.CODEX)
    transport = CodexNativeSession(session)
    transport._conversation_id = THREAD_ID

    with patch.object(native_module.shutil, "which", lambda _name: "/usr/bin/fake-codex"):
        await transport.start()
        await transport.send_message("do work", [])
        await transport.cancel_active_turn()

    assert transport.turn_in_flight is False
    assert len(harness.servers) == 2
    assert "thread/resume" in harness.current.request_methods()
    await transport.stop()


# ── tailer-level wedge recovery ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_silent_tool_completion_then_no_turn_end_stop_restarts_and_resends(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
) -> None:
    """The exact production sequence: tool completes, turn never ends.

    The streaming-inactivity watchdog reaps the wedged turn; because TraeX's
    interrupt is unconfirmed the app-server restarts in place and resumes the
    thread, after which a new message sends without an in-flight conflict.
    """
    _patch_timings(monkeypatch)
    monkeypatch.setattr(tailer_module, "STREAM_INACTIVITY_TIMEOUT_S", 0.0)
    monkeypatch.setattr(tailer_module, "ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S", 3600.0)
    harness = _AppServerHarness(monkeypatch, ["timeout", "confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    tailer = _tailer(session, transport, store)

    queue = await tailer.subscribe()
    # Wait for the app-server handshake to settle before sending.
    await asyncio.sleep(0.1)

    await tailer.send_message("run a long exec", [], client_turn_id="turn-1")
    first_event = await asyncio.wait_for(queue.get(), timeout=2.0)
    assert first_event.type == AgentStreamEventType.TURN_STARTED

    server = harness.current
    server.exec_item(CALL_ID, "started")
    server.exec_item(CALL_ID, "completed")
    # Provider goes silent: no turn/completed, ever.

    await _wait_until(2.0, lambda: _store_has(store, AgentStreamEventType.TOOL_CALL_COMPLETED))

    # Force silence past the (zeroed) streaming timeout.
    tailer._last_event_at = asyncio.get_event_loop().time() - 1.0

    async def _reaped() -> bool:
        types = await _event_types(store)
        return (
            AgentStreamEventType.TURN_COMPLETED in types
            and len(harness.servers) == 2
            and not transport.turn_in_flight
        )

    await _wait_until(5.0, _reaped)

    # The reaper restarted in place and the push consumer is still alive.
    assert tailer.is_running() is True
    assert "thread/resume" in harness.current.request_methods()
    completion = [e for e in await _events(store) if e.type == AgentStreamEventType.TURN_COMPLETED]
    assert completion[-1].payload["status"] == "cancelled"
    assert completion[-1].turn_id == "turn-1"

    # A new message now sends cleanly (guard released, thread resumed).
    await tailer.send_message("continue please", [], client_turn_id="turn-2")
    assert harness.current.request_params("turn/start"), "new turn/start after resume"
    started = [e for e in await _events(store) if e.type == AgentStreamEventType.TURN_STARTED]
    assert any(event.turn_id == "turn-2" for event in started)

    await tailer.stop()


async def _store_has(store: AgentStreamStore, event_type: AgentStreamEventType) -> bool:
    return event_type in await _event_types(store)


@pytest.mark.asyncio
async def test_explicit_stop_with_confirmed_interrupt_keeps_server_and_resends(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
) -> None:
    """When turn/interrupt is confirmed, Stop cancels in place without restart."""
    _patch_timings(monkeypatch)
    harness = _AppServerHarness(monkeypatch, ["confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    tailer = _tailer(session, transport, store)

    queue = await tailer.subscribe()
    await asyncio.sleep(0.1)
    await tailer.send_message("work", [], client_turn_id="turn-a")
    assert (await asyncio.wait_for(queue.get(), timeout=2.0)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    harness.current.exec_item(CALL_ID, "started")

    assert await tailer.cancel_turn() is True
    assert transport.turn_in_flight is False
    assert len(harness.servers) == 1
    interrupts = harness.current.request_params("turn/interrupt")
    assert interrupts and interrupts[0]["turnId"]

    # Same app-server accepts the next turn — no restart, no in-flight block.
    await tailer.send_message("more work", [], client_turn_id="turn-b")
    assert len(harness.servers) == 1
    assert any(
        event.turn_id == "turn-b"
        for event in await _events(store)
        if event.type == (AgentStreamEventType.TURN_STARTED)
    )
    await tailer.stop()


@pytest.mark.asyncio
async def test_explicit_stop_frozen_turn_restarts_and_resends(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
) -> None:
    """Stop on a turn whose interrupt never responds restarts and resumes."""
    _patch_timings(monkeypatch)
    harness = _AppServerHarness(monkeypatch, ["timeout", "confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    tailer = _tailer(session, transport, store)

    queue = await tailer.subscribe()
    await asyncio.sleep(0.1)
    await tailer.send_message("frozen", [], client_turn_id="turn-frozen")
    assert (await asyncio.wait_for(queue.get(), timeout=2.0)).type == (
        AgentStreamEventType.TURN_STARTED
    )

    assert await tailer.cancel_turn() is True
    # Let the interrupt timeout + restart handshake settle.
    await _wait_until(
        5.0,
        lambda: len(harness.servers) == 2 and "thread/resume" in harness.current.request_methods(),
    )
    assert transport.turn_in_flight is False
    assert tailer.is_running() is True

    await tailer.send_message("after stop", [], client_turn_id="turn-after")
    assert harness.current.request_params("turn/start")
    await tailer.stop()


@pytest.mark.asyncio
async def test_hard_liveness_reaps_silent_turn_with_outstanding_tool(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
) -> None:
    """Total provider silence past the outer bound reaps even with an open tool."""
    _patch_timings(monkeypatch)
    monkeypatch.setattr(tailer_module, "STREAM_INACTIVITY_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(tailer_module, "ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S", 0.0)
    harness = _AppServerHarness(monkeypatch, ["timeout", "confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    tailer = _tailer(session, transport, store)

    queue = await tailer.subscribe()
    await asyncio.sleep(0.1)
    await tailer.send_message("run forever", [], client_turn_id="turn-hard")
    assert (await asyncio.wait_for(queue.get(), timeout=2.0)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    harness.current.exec_item(CALL_ID, "started")  # tool stays outstanding
    await _wait_until(2.0, lambda: _store_has(store, AgentStreamEventType.TOOL_CALL_STARTED))
    tailer._last_event_at = asyncio.get_event_loop().time() - 1.0

    async def _reaped() -> bool:
        types = await _event_types(store)
        return AgentStreamEventType.TURN_COMPLETED in types and len(harness.servers) == 2

    await _wait_until(5.0, _reaped)
    assert tailer.is_running() is True
    completion = [e for e in await _events(store) if e.type == AgentStreamEventType.TURN_COMPLETED]
    assert completion[-1].payload["status"] == "cancelled"
    await tailer.stop()


@pytest.mark.asyncio
async def test_actively_emitting_turn_is_not_hard_reaped(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
) -> None:
    """Recent provider activity keeps an old turn alive regardless of age."""
    _patch_timings(monkeypatch)
    monkeypatch.setattr(tailer_module, "STREAM_INACTIVITY_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(tailer_module, "ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S", 0.05)
    harness = _AppServerHarness(monkeypatch, ["confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    tailer = _tailer(session, transport, store)

    queue = await tailer.subscribe()
    await asyncio.sleep(0.1)
    await tailer.send_message("busy", [], client_turn_id="turn-busy")
    assert (await asyncio.wait_for(queue.get(), timeout=2.0)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    # Keep emitting across many hard-timeout windows: total silence must be the
    # trigger, not elapsed wall-clock.
    for _ in range(20):
        harness.current.notify("item/reasoning/textDelta", {"itemId": "r1", "delta": "thinking"})
        await asyncio.sleep(0.01)
    assert transport.turn_in_flight is True
    assert tailer.is_running() is True
    assert len(harness.servers) == 1
    assert tailer._turn_hard_liveness_expired() is False

    # A blocking approval card also exempts an otherwise silent turn.
    tailer._blocking_approval_call_ids.add("card-1")
    tailer._last_event_at = asyncio.get_event_loop().time() - 100.0
    assert tailer._turn_hard_liveness_expired() is False
    await tailer.stop()


@pytest.mark.asyncio
async def test_unattributed_replay_and_null_goal_update_do_not_wedge(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
) -> None:
    """The post-completion replay sequence must be dropped, not persisted."""
    _patch_timings(monkeypatch)
    harness = _AppServerHarness(monkeypatch, ["confirm"])
    session = _session()
    transport = TraexNativeSession(session)
    tailer = _tailer(session, transport, store)

    queue = await tailer.subscribe()
    await asyncio.sleep(0.1)
    await tailer.send_message("finish then replay", [], client_turn_id="turn-real")
    assert (await asyncio.wait_for(queue.get(), timeout=2.0)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    server = harness.current
    server.exec_item(CALL_ID, "started")
    server.exec_item(CALL_ID, "completed")
    provider_turn_id = transport._provider_turn_id
    server.turn_completed(provider_turn_id or "provider-turn-1")

    async def _completed() -> bool:
        types = await _event_types(store)
        return types[-1:] == [AgentStreamEventType.TURN_COMPLETED] and not (
            transport.turn_in_flight
        )

    await _wait_until(2.0, _completed)
    before = len(await _events(store))

    # Hours later: duplicate item completion with no active turn, then a
    # goal update carrying turnId=null.
    server.exec_item(CALL_ID, "completed")
    server.goal_updated(turn_id=None)
    await asyncio.sleep(0.2)

    events = await _events(store)
    assert len(events) == before + 1  # only the goal STATUS event survives
    assert events[-1].type == AgentStreamEventType.STATUS
    assert events[-1].payload["provider_notification"] == "thread/goal/updated"
    assert transport.turn_in_flight is False
    assert tailer._active_turn_id is None

    # The tab is usable: the next send is not rejected as in-flight.
    await tailer.send_message("next", [], client_turn_id="turn-next")
    assert server.request_params("turn/start")
    await tailer.stop()
