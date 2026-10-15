"""Retirement also owns edit rollback and retry work detached from a tailer."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType, SessionKind
from claude_hub.services.agent_stream.store import AgentStreamStore
from tests.test_agent_stream_store_lifecycle import BlockedIO
from tests.test_ttyd_stream_lifecycle import DirectChat, direct_chat, stream_api  # noqa: F401


def turn_event(session_id: str, tab_id: str) -> AgentStreamEvent:
    from datetime import datetime, timezone

    return AgentStreamEvent(
        session_id=session_id,
        tab_id=tab_id,
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TURN_STARTED,
        stream_sequence=-1,
        created_at=datetime.now(timezone.utc),
        turn_id="old-turn",
        payload={"text": "private old turn"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_times_out", [False, True])
async def test_direct_delete_waits_for_detached_edit_rollback(
    direct_chat: DirectChat, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, delete_times_out: bool
) -> None:
    tabs, process, manager, tailer, transport = direct_chat
    tailer_module = importlib.import_module("claude_hub.services.agent_stream.tailer")
    store_module = importlib.import_module("claude_hub.services.agent_stream.store")
    session = stream_api._terminal_tab_stream_session(process.tab_id)
    assert session is not None
    transport.turn_in_flight = False
    original = turn_event(session.id, process.tab_id)
    await tailer.store.append(original)
    monkeypatch.setattr(
        tailer_module, "discover_source_cached", lambda *_: tmp_path / "transcript.jsonl"
    )
    monkeypatch.setattr(tailer_module, "snapshot_transcript", lambda *_: None)
    monkeypatch.setattr(tailer_module, "restore_transcript", lambda *_: None)
    monkeypatch.setattr(tailer_module, "fork_transcript", lambda *_: None)
    monkeypatch.setattr(tailer_module, "get_adapter_for_session", lambda *_: MagicMock())
    monkeypatch.setattr(
        manager, "_get_or_create", AsyncMock(side_effect=RuntimeError("provider restart failed"))
    )
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    goal = importlib.import_module("claude_hub.services.goal_run")
    monkeypatch.setattr(goal, "get_goal_manager", lambda: MagicMock(current=lambda *_: None))
    copy = store_module.shutil.copy2
    blocked = BlockedIO(copy)

    def block_restore(src: Any, dst: Any) -> Any:
        return blocked(src, dst) if str(src).endswith(".edit-bak") else copy(src, dst)

    monkeypatch.setattr(store_module.shutil, "copy2", block_restore)
    payload = stream_api.AgentStreamEditResendRequest(
        text="edited", client_turn_id="client", turn_id="old-turn"
    )
    editing = asyncio.create_task(
        stream_api.edit_resend_tab_stream(process.tab_id, payload, current_user=MagicMock())
    )
    deleting: asyncio.Task[bool] | None = None
    try:
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        assert manager.get_tailer(session.id) is None
        if delete_times_out:
            monkeypatch.setattr(tailer_module, "_STOP_JOIN_TIMEOUT_S", 0.02)
            with pytest.raises(RuntimeError, match="mutation is still pending"):
                await asyncio.wait_for(tabs.delete_tab(process.tab_id), 1)
        else:
            deleting = asyncio.create_task(tabs.delete_tab(process.tab_id))
            await asyncio.sleep(0.02)
            assert not deleting.done()
        assert tabs.get_tab(process.tab_id) is not None
        assert not blocked.finished.is_set()
        blocked.release.set()
        with pytest.raises(stream_api.HTTPException, match="provider restart failed"):
            await editing
        assert blocked.finished.is_set()
        if deleting is None:
            assert await tabs.delete_tab(process.tab_id)
        else:
            assert await deleting
        assert tabs.get_tab(process.tab_id) is None
        assert not tailer.store.path.exists()
        fresh = AgentStreamStore(session.workspace_id, session.id)
        await fresh.append(original.model_copy(update={"tab_id": "replacement-tab"}))
        page = await fresh.read_since(-1, 100)
        assert [(item.tab_id, item.stream_sequence) for item in page.events] == [
            ("replacement-tab", 0)
        ]
    finally:
        blocked.release.set()
        await asyncio.gather(editing, *([deleting] if deleting else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_retry", [False, True])
async def test_real_retry_keeps_pending_terminal_writer_owned(
    direct_chat: DirectChat, monkeypatch: pytest.MonkeyPatch, cancel_retry: bool
) -> None:
    tabs, process, manager, tailer, _transport = direct_chat
    process.session_kind = SessionKind.TERMINAL
    tailer._native_transport = None
    tailer_module = importlib.import_module("claude_hub.services.agent_stream.tailer")
    monkeypatch.setattr(tailer_module, "_STOP_JOIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    blocked = BlockedIO(tailer.store._write_line)
    monkeypatch.setattr(tailer.store, "_write_line", blocked)
    writer = tailer._task = asyncio.create_task(
        tailer._persist_and_fanout(turn_event(tailer.session_id, process.tab_id))
    )
    retrying: asyncio.Task[Any] | None = None
    try:
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        if cancel_retry:
            retrying = asyncio.create_task(
                stream_api.retry_tab_stream(process.tab_id, current_user=MagicMock())
            )

            async def stop_entered() -> None:
                while not tailer._retirement_pending:
                    await asyncio.sleep(0)

            await asyncio.wait_for(stop_entered(), 1)
            retrying.cancel()
            with pytest.raises(asyncio.CancelledError):
                await retrying
        else:
            with pytest.raises(stream_api.HTTPException, match="teardown still pending"):
                await stream_api.retry_tab_stream(process.tab_id, current_user=MagicMock())
        assert manager.get_tailer(tailer.session_id) is tailer
        assert not writer.done()
        with pytest.raises(RuntimeError, match="teardown is in progress"):
            await tailer.start()
        with pytest.raises(RuntimeError, match="teardown still pending"):
            await tabs.delete_tab(process.tab_id)
        assert tabs.get_tab(process.tab_id) is not None
        blocked.release.set()
        await asyncio.gather(writer, return_exceptions=True)
        assert await tabs.delete_tab(process.tab_id)
        assert not tailer.store.path.exists()
    finally:
        blocked.release.set()
        await asyncio.gather(writer, *([retrying] if retrying else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_delete_failure", [False, True])
async def test_archive_api_cannot_forget_a_failed_provider_stop(
    direct_chat: DirectChat,
    monkeypatch: pytest.MonkeyPatch,
    prior_delete_failure: bool,
) -> None:
    from claude_hub.api import tabs as tabs_api

    tabs, process, manager, tailer, transport = direct_chat
    monkeypatch.setattr(tabs_api, "ttyd_manager", tabs)
    await tailer.store.append(turn_event(tailer.session_id, process.tab_id))
    transport.stop.side_effect = OSError("native provider remains alive")
    if prior_delete_failure:
        with pytest.raises(RuntimeError, match="could not safely stop"):
            await tabs.delete_tab(process.tab_id)
    archived = await tabs_api.archive_tab(
        process.tab_id, current_user=MagicMock(email="retirement@example.invalid")
    )
    assert archived.archived
    assert manager.get_tailer(tailer.session_id) is tailer
    assert tailer._retirement_pending
    assert tailer.store.path.exists()
    with pytest.raises(RuntimeError, match="could not safely stop"):
        await tabs.delete_tab(process.tab_id)
    assert tabs.get_tab(process.tab_id) is not None
    assert manager.get_tailer(tailer.session_id) is tailer
    transport.stop.side_effect = None
    assert await tabs.delete_tab(process.tab_id)
    assert manager.get_tailer(tailer.session_id) is None
    assert not tailer.store.path.exists()


@pytest.mark.asyncio
async def test_shutdown_retains_failed_provider_for_strict_deletion_retry(
    direct_chat: DirectChat,
) -> None:
    tabs, process, manager, tailer, transport = direct_chat
    entered, release = asyncio.Event(), asyncio.Event()

    async def failing_stop() -> None:
        assert not manager._lock.locked()
        entered.set()
        await release.wait()
        raise OSError("provider remains live")

    transport.stop.side_effect = failing_stop
    shutdown = asyncio.create_task(manager.stop_all())
    deleting: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        deleting = asyncio.create_task(tabs.delete_tab(process.tab_id))
        await asyncio.sleep(0.01)
        release.set()
        for operation in (shutdown, deleting):
            with pytest.raises(RuntimeError, match="could not safely stop"):
                await operation
        assert manager.get_tailer(tailer.session_id) is tailer
        assert tabs.get_tab(process.tab_id) is not None
        cast(AsyncMock, process.stop).assert_not_awaited()
        with pytest.raises(RuntimeError, match="teardown is in progress"):
            await tailer.start()
        transport.stop.side_effect = None
        assert await tabs.delete_tab(process.tab_id)
    finally:
        release.set()
        await asyncio.gather(shutdown, *([deleting] if deleting else []), return_exceptions=True)
