"""Background retirement jobs must not block unrelated service work or escape ownership."""

from __future__ import annotations

import asyncio
import importlib
import uuid
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest

from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.ttyd_manager import TTYDManager
from tests.test_agent_stream_store_lifecycle import BlockedIO
from tests.test_tab_cleanup_recovery import (  # noqa: F401
    disk_state,
    retire_pending,
    seed_artifacts,
    subject,
)
from tests.test_ttyd_stream_lifecycle import DirectChat, direct_chat  # noqa: F401


async def wait_until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_monitor_and_other_cleanup_progress_while_submitted_unlink_is_blocked(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    await seed_artifacts(subject)
    first = await retire_pending(subject, monkeypatch)
    good = replace(first, tab_id="unrelated-pending", token=uuid.uuid4().hex)
    manager._pending_stream_cleanup[good.tab_id] = good
    manager._persist_state()
    original = AgentStreamStore._unlink_session_files
    blocked = BlockedIO(original)

    def block_one(store: AgentStreamStore) -> None:
        if store.session_id == f"terminal-tab-{first.tab_id}":
            blocked(store)
        else:
            original(store)

    monkeypatch.setattr(AgentStreamStore, "_unlink_session_files", block_one)
    workspace_module = importlib.import_module("claude_hub.services.workspace_manager._workspaces")
    monkeypatch.setattr(workspace_module, "ttyd_manager", manager)
    refreshed = asyncio.Event()

    class ReachedRefresh(BaseException):
        pass

    async def refresh(**_kwargs: Any) -> None:
        refreshed.set()
        raise ReachedRefresh()

    from types import SimpleNamespace

    fake_workspace = SimpleNamespace(_refresh_session_statuses=refresh)
    async with manager.cleanup_task_lifetime():
        monitoring = asyncio.create_task(
            workspace_module._WorkspacesMixin._background_monitor_loop(fake_workspace)
        )
        deleting: asyncio.Task[bool] | None = None
        try:
            assert await asyncio.to_thread(blocked.entered.wait, 2)
            with pytest.raises(ReachedRefresh):
                await monitoring
            assert refreshed.is_set()
            # Wait for the owned I/O job rather than busy-polling shared state;
            # the worker thread must make progress while coverage traces Python.
            other_cleanup = manager._cleanup_tasks.get((good.tab_id, good.token))
            if other_cleanup is not None:
                assert await asyncio.wait_for(asyncio.shield(other_cleanup), 2)
            assert good.tab_id not in manager._pending_stream_cleanup
            assert first.tab_id in manager._pending_stream_cleanup
            assert manager._cleanup_locks[first.tab_id].locked()
            assert (first.tab_id, first.token) in manager._cleanup_tasks
            # Explicit DELETE waits on the same owner, and cancelling that
            # waiter must not cancel the strongly-owned cleanup job.
            deleting = asyncio.create_task(manager.delete_tab(process.tab_id))
            await asyncio.sleep(0)
            assert not deleting.done()
            deleting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await deleting
            assert not blocked.finished.is_set()
            blocked.release.set()
            await manager._await_cleanup_tasks(list(manager._cleanup_tasks.values()))
            assert not manager._pending_stream_cleanup
        finally:
            blocked.release.set()
            await asyncio.gather(
                monitoring, *([deleting] if deleting else []), return_exceptions=True
            )


@pytest.mark.asyncio
async def test_scheduler_owns_at_most_eight_unique_record_jobs(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, *_ = subject
    first = await retire_pending(subject, monkeypatch)
    for index in range(1, 10):
        record = replace(first, tab_id=f"pending-{index}", token=uuid.uuid4().hex)
        manager._pending_stream_cleanup[record.tab_id] = record
    manager._persist_state()
    stream = importlib.import_module("claude_hub.services.agent_stream.tailer")
    original = stream.discard_retired_session_artifacts
    release = asyncio.Event()
    started: list[str] = []

    async def wait_to_remove(workspace: str, session: str) -> None:
        started.append(session)
        await release.wait()
        await original(workspace, session)

    monkeypatch.setattr(stream, "discard_retired_session_artifacts", wait_to_remove)
    async with manager.cleanup_task_lifetime():
        try:
            jobs = manager.schedule_pending_stream_cleanup(limit=100)
            assert len(jobs) == 8
            await asyncio.wait_for(wait_until(lambda: len(started) == 8), 2)
            for _ in range(3):
                assert manager.schedule_pending_stream_cleanup(limit=100) == []
            assert len(manager._cleanup_tasks) == len(set(started)) == 8
            release.set()
            assert all(await manager._await_cleanup_tasks(jobs))
            assert await manager.retry_pending_stream_cleanup(limit=100) == 2
            assert not manager._pending_stream_cleanup
            assert not manager._cleanup_tasks
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_failure", [False, True])
async def test_lifespan_joins_submitted_cleanup_through_repeated_cancellation(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch, startup_failure: bool
) -> None:
    from claude_hub import main
    from claude_hub.api import agent_stream, feishu_bot
    from claude_hub.services import restart_continuation

    manager, process, *_ = subject
    await seed_artifacts(subject)
    first = await retire_pending(subject, monkeypatch)
    blocked = BlockedIO(AgentStreamStore._unlink_session_files)
    monkeypatch.setattr(AgentStreamStore, "_unlink_session_files", lambda store: blocked(store))
    runtime_owned = False
    providers_reached, yielded, shutdown = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @contextmanager
    def owner_lock(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        nonlocal runtime_owned
        runtime_owned = True
        try:
            yield
        finally:
            runtime_owned = False

    @contextmanager
    def no_file_log(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        yield

    class StartupFailure(RuntimeError):
        pass

    async def start_unrelated_providers() -> None:
        assert runtime_owned
        providers_reached.set()
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        assert first.tab_id in manager._pending_stream_cleanup
        if startup_failure:
            raise StartupFailure("startup failed after submitting cleanup")

    monkeypatch.setattr(main, "BackendInstanceLock", owner_lock)
    monkeypatch.setattr(main, "backend_file_logging", no_file_log)
    monkeypatch.setattr(main, "ttyd_manager", manager)
    monkeypatch.setattr(manager, "start_all_tabs", start_unrelated_providers)
    monkeypatch.setattr(manager, "cleanup", AsyncMock())
    monkeypatch.setattr(agent_stream, "_reset_tailer_manager", lambda: None)
    monkeypatch.setattr(agent_stream, "_quiesce_tailer_managers", lambda: None)
    monkeypatch.setattr(agent_stream, "_stop_all_tailer_managers", AsyncMock())
    monkeypatch.setattr(feishu_bot, "start_feishu_websockets", AsyncMock())
    monkeypatch.setattr(feishu_bot, "stop_feishu_websockets", AsyncMock())
    monkeypatch.setattr(main.workspace_manager, "start_background_monitor", lambda: None)
    monkeypatch.setattr(main.workspace_manager, "stop_background_monitor", AsyncMock())
    monkeypatch.setattr(restart_continuation, "continue_interrupted_chats", AsyncMock())
    monkeypatch.setattr(restart_continuation, "capture_interrupted_chats", AsyncMock())
    monkeypatch.setattr("claude_hub.services.feishu_bot_websocket.prewarm_lark_sdk", lambda: None)
    monkeypatch.setattr("claude_hub.services.agent_stream.attachments.run_global_gc", AsyncMock())
    monkeypatch.setattr("claude_hub.services.agent_stream.native.cleanup_codex_temp_dir", lambda: 0)
    monkeypatch.setattr(
        "claude_hub.services.agent_stream.native.cleanup_cursor_temp_dir", lambda: 0
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        "claude_hub.services.goal_run.get_goal_manager", lambda: SimpleNamespace(recover=lambda: 0)
    )

    async def run_lifespan() -> None:
        async with main.lifespan(main.app):
            yielded.set()
            await shutdown.wait()

    serving = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(providers_reached.wait(), 2)
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        if not startup_failure:
            await asyncio.wait_for(yielded.wait(), 2)
            shutdown.set()
        await asyncio.wait_for(wait_until(lambda: manager._cleanup_scheduling_stopped), 2)
        for _ in range(3):
            serving.cancel()
            await asyncio.sleep(0)
        assert runtime_owned
        assert not serving.done()
        assert not blocked.finished.is_set()
        assert manager._cleanup_locks[process.tab_id].locked()
        assert manager.schedule_pending_stream_cleanup() == []
        assert disk_state()["pending_stream_cleanup"]
        blocked.release.set()
        with pytest.raises(asyncio.CancelledError):
            await serving
        assert not runtime_owned
        assert blocked.finished.is_set()
        assert not manager._cleanup_tasks
        assert TTYDManager()._pending_stream_cleanup[process.tab_id] == first
        assert await manager.delete_tab(process.tab_id)
    finally:
        blocked.release.set()
        shutdown.set()
        await asyncio.gather(serving, return_exceptions=True)
