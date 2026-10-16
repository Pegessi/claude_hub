"""Disk I/O must settle before cancellation can release a stream owner."""

from __future__ import annotations

import asyncio
import importlib
import threading
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Coroutine, cast
from unittest.mock import MagicMock

import pytest

from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.base import AgentStreamAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import (
    SessionTailer,
    TailerManager,
    discard_session_stream,
    fence_session_stream,
)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    tailers = importlib.import_module("claude_hub.services.agent_stream.tailer")
    monkeypatch.setattr(wm, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(tailers, "_TAILER_MANAGERS", weakref.WeakSet())
    monkeypatch.setattr(tailers, "_DELETION_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailers, "_PROVIDER_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailers, "_RETIRING_TAILER_IDS", set())


def event(tab_id: str = "old-tab") -> AgentStreamEvent:
    return AgentStreamEvent(
        session_id="worker-1",
        tab_id=tab_id,
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TOOL_CALL_STARTED,
        stream_sequence=-1,
        created_at=datetime.now(timezone.utc),
        payload={"name": "Read", "args": {"file_path": "/tmp/old-owner.png"}},
    )


class BlockedIO:
    def __init__(self, original: Callable[..., Any], *, fail: bool = False) -> None:
        self.original = original
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self, *args: Any) -> Any:
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test did not release disk work")
        try:
            result = self.original(*args)
            if self.fail:
                raise OSError("disk completion failed")
            return result
        finally:
            self.finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["append", "replace", "clear", "truncate", "restore", "snapshot"]
)
@pytest.mark.parametrize("fail", [False, True])
async def test_cancelled_store_io_joins_and_recovers_sequence(
    monkeypatch: pytest.MonkeyPatch, operation: str, fail: bool
) -> None:
    store = AgentStreamStore("workspace", "worker-1")
    await store.append(event())
    await store.append(event())
    backup = await store.snapshot()
    target: Any
    operation_task: Callable[[], Coroutine[Any, Any, Any]]
    if operation == "append":
        target, method = store, "_write_line"
        operation_task = lambda: store.append(event())
        next_sequence = 3
    elif operation == "replace":
        target, method = store, "_write_all"
        operation_task = lambda: store.replace_all([event()])
        next_sequence = 1
    elif operation == "clear":
        target, method = store, "_unlink_session_files"
        operation_task = store.clear
        next_sequence = 0
    elif operation == "truncate":
        target, method = store, "_write_all"
        operation_task = lambda: store.truncate_before(1)
        next_sequence = 1
    else:
        target = importlib.import_module("claude_hub.services.agent_stream.store").shutil
        method = "copy2"
        operation_task = (
            (lambda: store.restore(backup)) if operation == "restore" else store.snapshot
        )
        next_sequence = 2
    blocked = BlockedIO(getattr(target, method), fail=fail)
    monkeypatch.setattr(target, method, blocked)
    task = asyncio.create_task(operation_task())
    try:
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert store._lock.locked()
        blocked.release.set()
        with pytest.raises(OSError if fail else asyncio.CancelledError):
            await task
        assert blocked.finished.is_set()
    finally:
        blocked.release.set()
        await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(target, method, blocked.original)
    appended = await store.append(event("new-tab"))
    assert appended.stream_sequence == next_sequence
    page = await store.read_since(-1, 100, compact=False)
    assert [item.stream_sequence for item in page.events] == list(range(next_sequence + 1))


def native_owner() -> (
    tuple[ManagedSession, dict[str, ManagedSession], TailerManager, SessionTailer]
):
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="worker-1",
        workspace_id="workspace",
        tab_id="old-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        title="Chat",
        workspace_path="/tmp",
        tmux_session="",
        created_at=now,
        updated_at=now,
    )
    current = {session.id: session}
    transport = SimpleNamespace(turn_in_flight=True, active_thread_id=None)

    async def stop() -> None:
        transport.turn_in_flight = False

    transport.stop = stop
    transport.cancel_active_turn = stop
    manager = TailerManager(current.get)
    tailer = SessionTailer(
        session.workspace_id,
        session.id,
        cast(AgentStreamAdapter, MagicMock()),
        lambda: current.get(session.id),
        native_transport=cast(ProviderSession, transport),
    )
    tailer._active_turn_id = "native-turn"
    manager._tailers[session.id] = tailer
    return session, current, manager, tailer


@pytest.mark.asyncio
async def test_fence_joins_native_append_before_friendly_id_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, current, manager, tailer = native_owner()
    blocked = BlockedIO(tailer._store._write_line)
    monkeypatch.setattr(tailer._store, "_write_line", blocked)
    tailer._task = asyncio.create_task(tailer._persist_and_fanout(event()))
    fences: list[asyncio.Task[bool]] = []
    try:
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        fences = [asyncio.create_task(fence_session_stream(session.id)) for _ in range(2)]
        await asyncio.sleep(0.02)
        assert all(not task.done() for task in fences)
        assert manager.get_tailer(session.id) is tailer
        blocked.release.set()
        assert all(await asyncio.gather(*fences))
        assert blocked.finished.is_set()
        current.clear()
        await discard_session_stream(session.workspace_id, session.id, provider_fenced=True)
        current[session.id] = session.model_copy(
            update={"tab_id": "new-tab", "created_at": session.created_at + timedelta(seconds=1)}
        )
        new_store = AgentStreamStore(session.workspace_id, session.id)
        await new_store.append(event("new-tab"))
        page = await new_store.read_since(-1, 100, compact=False)
        assert [(item.tab_id, item.stream_sequence) for item in page.events] == [("new-tab", 0)]
    finally:
        blocked.release.set()
        await asyncio.gather(*fences, return_exceptions=True)
        await tailer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_stuck_disk_teardown_is_bounded_and_retains_retryable_owner(
    monkeypatch: pytest.MonkeyPatch, shutdown: bool
) -> None:
    tailer_module = importlib.import_module("claude_hub.services.agent_stream.tailer")
    monkeypatch.setattr(tailer_module, "_STOP_JOIN_TIMEOUT_S", 0.02)
    session, current, manager, tailer = native_owner()
    blocked = BlockedIO(tailer._store._write_line)
    monkeypatch.setattr(tailer._store, "_write_line", blocked)
    tailer._task = asyncio.create_task(tailer._persist_and_fanout(event()))
    try:
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        operation = manager.stop_all() if shutdown else fence_session_stream(session.id)
        with pytest.raises(RuntimeError, match="teardown still pending"):
            await asyncio.wait_for(operation, 0.5)
        assert manager.get_tailer(session.id) is tailer
        assert current[session.id] is session
        assert tailer._task is not None and not tailer._task.done()
        assert not blocked.finished.is_set()
        with pytest.raises(RuntimeError, match="teardown is in progress"):
            await tailer.start()
        blocked.release.set()
        await asyncio.wait_for(asyncio.gather(tailer._task, return_exceptions=True), 2)
        if shutdown:
            await manager.stop_all()
        else:
            assert await fence_session_stream(session.id)
        assert manager.get_tailer(session.id) is None
    finally:
        blocked.release.set()
        await tailer.stop()
