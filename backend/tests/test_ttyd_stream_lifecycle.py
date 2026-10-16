"""Direct-tab deletion must retain its stream owner until teardown succeeds."""

from __future__ import annotations

import asyncio
import base64
import importlib
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.api import agent_stream as stream_api
from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType, SessionKind
from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore
from claude_hub.services.agent_stream.base import AgentStreamAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.tailer import SessionTailer, TailerManager
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

DirectChat = tuple[TTYDManager, TTYDProcess, TailerManager, SessionTailer, SimpleNamespace]


@pytest.fixture
async def direct_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DirectChat:
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    tailer_module = importlib.import_module("claude_hub.services.agent_stream.tailer")
    monkeypatch.setattr(wm, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(tm, "STATE_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(tm, "ORDER_FILE", tmp_path / "order.json")
    monkeypatch.setattr(tailer_module, "_TAILER_MANAGERS", weakref.WeakSet())
    monkeypatch.setattr(tailer_module, "_DELETION_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailer_module, "_PROVIDER_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailer_module, "_RETIRING_TAILER_IDS", set())
    manager = TTYDManager()
    process = TTYDProcess(
        "direct-tab",
        23001,
        "Direct Chat",
        cwd=str(tmp_path),
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )
    monkeypatch.setattr(process, "stop", AsyncMock())
    manager.processes[process.tab_id] = process
    manager._tab_order = [process.tab_id]
    monkeypatch.setattr(stream_api, "ttyd_manager", manager)
    session = stream_api._terminal_tab_stream_session(process.tab_id)
    assert session is not None
    transport = SimpleNamespace(turn_in_flight=True, active_thread_id=None)

    async def cancel() -> None:
        transport.turn_in_flight = False

    transport.cancel_active_turn = AsyncMock(side_effect=cancel)
    transport.stop = AsyncMock()
    tailer = SessionTailer(
        session.workspace_id,
        session.id,
        cast(AgentStreamAdapter, MagicMock()),
        lambda: stream_api._terminal_tab_stream_session(process.tab_id),
        native_transport=cast(ProviderSession, transport),
    )
    tailer._active_turn_id = "active-turn"
    tailers = TailerManager(lambda _sid: stream_api._terminal_tab_stream_session(process.tab_id))
    tailers._tailers[session.id] = tailer
    return manager, process, tailers, tailer, transport


@pytest.mark.asyncio
async def test_delete_active_direct_chat_clears_stream_and_cached_image(
    direct_chat: DirectChat,
) -> None:
    manager, process, tailers, tailer, transport = direct_chat
    await tailer._store.append(
        AgentStreamEvent(
            session_id=tailer.session_id,
            tab_id=process.tab_id,
            agent_type=AgentType.CLAUDE,
            type=AgentStreamEventType.TURN_STARTED,
            stream_sequence=-1,
            created_at=process.created_at,
            turn_id="active-turn",
            payload={"text": "private"},
        )
    )
    store = AgentStreamAttachmentStore(tailer.workspace_id, tailer.session_id)
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1kAAAAASUVORK5CYII="
    )
    preview = await store.save("image/png", image)

    assert await manager.delete_tab(process.tab_id)
    assert manager.get_tab(process.tab_id) is None
    assert not tailer._store.path.exists()
    with pytest.raises(KeyError):
        await store.read(preview["id"])
    assert tailers.get_tailer(tailer.session_id) is None
    transport.stop.assert_awaited_once()
    cast(AsyncMock, process.stop).assert_awaited_once_with(kill_tmux=True)


@pytest.mark.asyncio
async def test_failed_direct_fence_preserves_tab_and_can_retry(direct_chat: DirectChat) -> None:
    manager, process, tailers, tailer, transport = direct_chat
    transport.stop.side_effect = OSError("provider stop failed")
    with pytest.raises(RuntimeError, match="could not safely stop"):
        await manager.delete_tab(process.tab_id)
    assert manager.processes[process.tab_id] is process
    assert tailers.get_tailer(tailer.session_id) is tailer
    cast(AsyncMock, process.stop).assert_not_awaited()
    transport.stop.side_effect = None
    assert await manager.delete_tab(process.tab_id)
    assert tailers.get_tailer(tailer.session_id) is None


@pytest.mark.asyncio
async def test_concurrent_direct_deletes_join_same_owner(direct_chat: DirectChat) -> None:
    manager, process, tailers, tailer, transport = direct_chat
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop() -> None:
        entered.set()
        await release.wait()

    transport.stop.side_effect = stop
    first = asyncio.create_task(manager.delete_tab(process.tab_id))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(manager.delete_tab(process.tab_id))
        await asyncio.sleep(0.02)
        assert not first.done() and not second.done()
        assert manager.processes[process.tab_id] is process
        assert tailers.get_tailer(tailer.session_id) is tailer
        release.set()
        assert sorted(await asyncio.gather(first, second)) == [False, True]
        transport.stop.assert_awaited_once()
        cast(AsyncMock, process.stop).assert_awaited_once_with(kill_tmux=True)
        tailer_module = importlib.import_module("claude_hub.services.agent_stream.tailer")
        assert tailer.session_id not in tailer_module._DELETION_FENCED_SESSION_IDS
        assert tailer.session_id not in tailer_module._PROVIDER_FENCED_SESSION_IDS
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_direct_fence_keeps_retryable_owner(direct_chat: DirectChat) -> None:
    manager, process, tailers, tailer, transport = direct_chat
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop() -> None:
        entered.set()
        await release.wait()

    transport.stop.side_effect = stop
    deleting = asyncio.create_task(manager.delete_tab(process.tab_id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        deleting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deleting
        assert manager.processes[process.tab_id] is process
        assert tailers.get_tailer(tailer.session_id) is tailer
        cast(AsyncMock, process.stop).assert_not_awaited()
        with pytest.raises(RuntimeError, match="teardown is in progress"):
            await tailer.start()
        release.set()
        assert await manager.delete_tab(process.tab_id)
        assert tailers.get_tailer(tailer.session_id) is None
    finally:
        release.set()
        await asyncio.gather(deleting, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_delete_rechecks_incarnation_after_unlocked_fence(
    direct_chat: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, _tailers, _tailer, _transport = direct_chat
    replacement = TTYDProcess(
        process.tab_id,
        23002,
        "Replacement",
        cwd=process.cwd,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )
    replacement_stop = AsyncMock()
    monkeypatch.setattr(replacement, "stop", replacement_stop)

    async def replace_during_fence(_session_id: str) -> bool:
        # Acquiring the tab lock here proves deletion did not hold it while
        # entering provider/tailer teardown (the opposite lock order exists).
        async with manager._locked_current_tab(process.tab_id):
            manager.processes[process.tab_id] = replacement
        return True

    monkeypatch.setattr(
        "claude_hub.services.agent_stream.tailer.fence_session_stream", replace_during_fence
    )
    assert not await asyncio.wait_for(manager.delete_tab(process.tab_id), 1)
    assert manager.processes[process.tab_id] is replacement
    replacement_stop.assert_not_awaited()
    cast(AsyncMock, process.stop).assert_not_awaited()
