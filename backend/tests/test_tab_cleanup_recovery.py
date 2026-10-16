"""Durable direct-tab retirement obligations survive storage and process failure."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterator, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.api import tabs as tabs_api
from claude_hub.models import AgentType, SessionKind, WorkspaceSessionRole
from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore
from claude_hub.services.agent_stream.fork_seed import seed_sidecar_path
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.ttyd_manager import (
    TabCleanupPendingError,
    TabStateError,
    TTYDManager,
    TTYDProcess,
)
from tests.test_agent_stream_retirement import turn_event
from tests.test_agent_stream_store_lifecycle import BlockedIO
from tests.test_ttyd_stream_lifecycle import DirectChat, direct_chat  # noqa: F401

TM = importlib.import_module("claude_hub.services.ttyd_manager")
STREAM = importlib.import_module("claude_hub.services.agent_stream.tailer")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1kAAAAASUVORK5CYII="
)


@pytest.fixture
async def subject(direct_chat: DirectChat) -> DirectChat:
    manager, _process, _tailers, _tailer, transport = direct_chat
    transport.turn_in_flight = False
    manager._persist_state()
    return direct_chat


async def seed_artifacts(subject: DirectChat) -> list[Path]:
    _manager, process, _tailers, tailer, _transport = subject
    store = tailer.store
    await store.append(turn_event(tailer.session_id, process.tab_id))
    paths = [
        store.path,
        store.cursor_path,
        store.path.with_name(store.path.name + ".edit-bak"),
        seed_sidecar_path(store.workspace_id, store.session_id),
    ]
    for path in paths[1:]:
        path.write_text("private retired bytes", encoding="utf-8")
    cache = AgentStreamAttachmentStore(store.workspace_id, store.session_id)
    preview = await cache.save("image/png", PNG)
    paths.append(cache._session_dir / preview["id"])
    return paths


async def retire_pending(subject: DirectChat, monkeypatch: pytest.MonkeyPatch) -> Any:
    manager, process, *_ = subject
    with monkeypatch.context() as patch:
        patch.setattr(
            STREAM,
            "discard_retired_session_artifacts",
            AsyncMock(side_effect=OSError("cleanup offline")),
        )
        with pytest.raises(TabCleanupPendingError, match="cleanup pending"):
            await manager.delete_tab(process.tab_id)
    return manager._pending_stream_cleanup[process.tab_id]


def disk_state() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(TM.STATE_FILE.read_text(encoding="utf-8")))


@pytest.mark.asyncio
async def test_legacy_load_is_read_only_and_new_writes_preserve_v2(subject: DirectChat) -> None:
    manager, process, *_ = subject
    TM.STATE_FILE.write_text(json.dumps([process.to_dict()]))
    original = TM.STATE_FILE.read_bytes()
    loaded = TTYDManager()
    assert loaded.get_tab(process.tab_id) is not None
    assert TM.STATE_FILE.read_bytes() == original
    loaded.rename_tab(process.tab_id, "renamed")
    assert disk_state()["version"] == 2
    assert disk_state()["pending_stream_cleanup"] == []
    assert disk_state()["tabs"][0]["name"] == "renamed"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["retire", "ack"])
@pytest.mark.parametrize("after_replace", [False, True])
async def test_commit_failure_and_ordinary_save_preserve_cleanup_obligation(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch, phase: str, after_replace: bool
) -> None:
    manager, process, *_ = subject
    paths = await seed_artifacts(subject)
    other = TTYDProcess("other-tab", 23002, "Other", agent_type=AgentType.TERMINAL)
    manager.processes[other.tab_id] = other
    manager._persist_state()
    call_count = 0
    target = 1 if phase == "retire" else 2
    real_replace, real_fsync = os.replace, os.fsync

    def fail_replace(src: Any, dst: Any) -> None:
        nonlocal call_count
        if Path(dst) == TM.STATE_FILE:
            call_count += 1
            if call_count == target:
                raise OSError("replace failed")
        real_replace(src, dst)

    def fail_directory_sync(fd: int) -> None:
        nonlocal call_count
        directory_stat = os.stat(TM.STATE_FILE.parent)
        actual = os.fstat(fd)
        if stat.S_ISDIR(actual.st_mode) and (actual.st_dev, actual.st_ino) == (
            directory_stat.st_dev,
            directory_stat.st_ino,
        ):
            call_count += 1
            if call_count == target:
                raise OSError("directory sync failed")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(
            os,
            "fsync" if after_replace else "replace",
            fail_directory_sync if after_replace else fail_replace,
        )
        with pytest.raises(TabCleanupPendingError):
            await manager.delete_tab(process.tab_id)
    if phase == "retire" and not after_replace:
        assert manager.processes[process.tab_id] is process
        assert not manager._pending_stream_cleanup
        assert {row["id"] for row in disk_state()["tabs"]} == {process.tab_id, other.tab_id}
        assert all(path.exists() for path in paths)
        assert await manager.delete_tab(process.tab_id)
        return
    record = manager._pending_stream_cleanup[process.tab_id]
    assert process.tab_id not in manager.processes
    assert not manager._state_confirmed
    if phase == "retire":
        assert all(path.exists() for path in paths)
        assert disk_state()["pending_stream_cleanup"][0]["token"] == record.token
    else:
        assert all(not path.exists() for path in paths)
        assert bool(disk_state()["pending_stream_cleanup"]) is not after_replace
    # A normal writer after the uncertain commit must not drop the obligation
    # or resurrect the removed owner, even when the last disk image was an ACK.
    manager.rename_tab(other.tab_id, "renamed after uncertainty")
    assert manager._state_confirmed
    snapshot = disk_state()
    assert [row["id"] for row in snapshot["tabs"]] == [other.tab_id]
    assert snapshot["pending_stream_cleanup"][0]["token"] == record.token
    loaded = TTYDManager()
    assert loaded._pending_stream_cleanup[process.tab_id] == record
    assert await loaded.delete_tab(process.tab_id)
    assert disk_state()["pending_stream_cleanup"] == []
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_file_sync_failure_never_commits_retirement(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    paths = await seed_artifacts(subject)
    before = TM.STATE_FILE.read_bytes()
    real_sync = os.fsync

    def fail_file_sync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("file sync failed")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_file_sync)
        with pytest.raises(TabCleanupPendingError, match="not committed"):
            await manager.delete_tab(process.tab_id)
    assert manager.processes[process.tab_id] is process
    assert not manager._pending_stream_cleanup
    assert TM.STATE_FILE.read_bytes() == before
    assert all(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_pending_record_collision_never_touches_live_owner(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    record = await retire_pending(subject, monkeypatch)
    replacement = TTYDProcess(
        process.tab_id, 23008, "Live replacement", agent_type=AgentType.TERMINAL
    )
    manager.processes[process.tab_id] = replacement
    cleanup = AsyncMock(side_effect=AssertionError("cleanup touched live collision"))
    fence = AsyncMock(side_effect=AssertionError("cleanup fenced live collision"))
    monkeypatch.setattr(STREAM, "discard_retired_session_artifacts", cleanup)
    monkeypatch.setattr(STREAM, "fence_session_stream", fence)
    with pytest.raises(TabCleanupPendingError, match="conflicts"):
        await manager.delete_tab(process.tab_id)
    assert await manager.retry_pending_stream_cleanup() == 0
    cleanup.assert_not_awaited()
    fence.assert_not_awaited()
    before = TM.STATE_FILE.read_bytes()
    manager.rename_tab(process.tab_id, "still live")
    assert TM.STATE_FILE.read_bytes() == before
    assert manager._pending_stream_cleanup[process.tab_id] == record


@pytest.mark.asyncio
async def test_same_delete_retries_after_restart_without_fresh_fence(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    paths = await seed_artifacts(subject)
    record = await retire_pending(subject, monkeypatch)
    assert disk_state()["pending_stream_cleanup"] == [asdict(record)]
    loaded = TTYDManager()
    monkeypatch.setattr(
        STREAM,
        "fence_session_stream",
        AsyncMock(side_effect=AssertionError("replay cannot invent runtime proof")),
    )
    monkeypatch.setattr(tabs_api, "ttyd_manager", loaded)
    with monkeypatch.context() as patch:
        patch.setattr(
            STREAM,
            "discard_retired_session_artifacts",
            AsyncMock(side_effect=OSError("still offline")),
        )
        with pytest.raises(tabs_api.HTTPException) as failure:
            await tabs_api.delete_tab(process.tab_id, current_user=MagicMock())
        assert failure.value.status_code == 503
        assert "retry DELETE" in failure.value.detail
    await tabs_api.delete_tab(process.tab_id, current_user=MagicMock())
    assert all(not path.exists() for path in paths)
    assert not disk_state()["pending_stream_cleanup"]
    with pytest.raises(tabs_api.HTTPException) as missing:
        await tabs_api.delete_tab("never-existed", current_user=MagicMock())
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_cancelled_cleanup_joins_disk_work_and_remains_replayable(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    paths = await seed_artifacts(subject)
    blocked = BlockedIO(AgentStreamStore._unlink_session_files)
    monkeypatch.setattr(AgentStreamStore, "_unlink_session_files", lambda store: blocked(store))
    deleting = asyncio.create_task(manager.delete_tab(process.tab_id))
    try:
        assert await asyncio.to_thread(blocked.entered.wait, 2)
        assert disk_state()["pending_stream_cleanup"]
        for _ in range(3):
            deleting.cancel()
            await asyncio.sleep(0)
        assert not deleting.done()
        assert manager._cleanup_locks[process.tab_id].locked()
        blocked.release.set()
        with pytest.raises(asyncio.CancelledError):
            await deleting
        assert blocked.finished.is_set()
        assert TTYDManager()._pending_stream_cleanup
        assert await manager.delete_tab(process.tab_id)
        assert all(not path.exists() for path in paths)
    finally:
        blocked.release.set()
        await asyncio.gather(deleting, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_worker_cannot_ack_new_cleanup_token(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    record = await retire_pending(subject, monkeypatch)
    newer = replace(record, token=uuid.uuid4().hex)

    async def replace_obligation(_workspace: str, _session: str) -> None:
        manager._pending_stream_cleanup[process.tab_id] = newer
        manager._persist_state()

    monkeypatch.setattr(STREAM, "discard_retired_session_artifacts", replace_obligation)
    with pytest.raises(TabCleanupPendingError, match="newer owner"):
        await manager.delete_tab(process.tab_id)
    assert manager._pending_stream_cleanup[process.tab_id] == newer
    assert disk_state()["pending_stream_cleanup"][0]["token"] == newer.token


@pytest.mark.asyncio
async def test_stale_delete_waiter_never_fences_replacement(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, _tailers, tailer, _transport = subject
    lock = manager._cleanup_locks.setdefault(process.tab_id, asyncio.Lock())
    await lock.acquire()
    deleting = asyncio.create_task(manager.delete_tab(process.tab_id))
    await asyncio.sleep(0)
    replacement = TTYDProcess(process.tab_id, 23003, "Replacement", agent_type=AgentType.TERMINAL)
    manager.processes[process.tab_id] = replacement
    manager._persist_state()
    fence = AsyncMock(side_effect=AssertionError("stale request fenced replacement"))
    monkeypatch.setattr(STREAM, "fence_session_stream", fence)
    lock.release()
    assert not await deleting
    fence.assert_not_awaited()
    assert manager.processes[process.tab_id] is replacement
    assert tailer.session_id not in STREAM._DELETION_FENCED_SESSION_IDS
    assert not manager._pending_stream_cleanup


@pytest.mark.asyncio
async def test_pending_identity_is_reserved_for_allocator(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    await retire_pending(subject, monkeypatch)
    real_uuid = uuid.uuid4
    choices = 0

    def candidate() -> Any:
        nonlocal choices
        choices += 1
        return process.tab_id if choices == 1 else real_uuid()

    monkeypatch.setattr(uuid, "uuid4", candidate)
    created = await manager.create_tab(
        name="new",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        workspace_role=WorkspaceSessionRole.WORKER,
    )
    assert created.id != process.tab_id
    assert process.tab_id in manager._pending_stream_cleanup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["version", "late-entry", "live-collision", "path", "authorization", "duplicate-token"],
)
async def test_invalid_ledger_never_publishes_or_overwrites_state(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    manager, process, *_ = subject
    record = await retire_pending(subject, monkeypatch)
    payload = disk_state()
    if corruption == "version":
        payload["version"] = 999
    elif corruption == "late-entry":
        payload["pending_stream_cleanup"].append({"tab_id": "bad-last"})
    elif corruption == "live-collision":
        payload["tabs"] = [process.to_dict()]
    elif corruption == "path":
        payload["pending_stream_cleanup"][0]["tab_id"] = "../../other"
    elif corruption == "authorization":
        payload["pending_stream_cleanup"][0]["authorization"] = "unverified"
    else:
        payload["pending_stream_cleanup"].append({**asdict(record), "tab_id": "another-tab"})
    payload["tabs"].append(
        TTYDProcess("valid-first", 23004, "Valid", agent_type=AgentType.TERMINAL).to_dict()
    )
    TM.STATE_FILE.write_text(json.dumps(payload))
    expected = TM.STATE_FILE.read_bytes()
    loaded = TTYDManager()
    assert loaded.processes == {}
    loaded._save_state()
    assert TM.STATE_FILE.read_bytes() == expected
    with pytest.raises(TabStateError):
        await loaded.retry_pending_stream_cleanup()
    with pytest.raises(TabStateError):
        await loaded.start_all_tabs()


@pytest.mark.asyncio
async def test_retry_pass_is_fair_and_missing_artifacts_are_success(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    record = await retire_pending(subject, monkeypatch)
    other = replace(record, tab_id="good-pending", token=uuid.uuid4().hex)
    manager._pending_stream_cleanup[other.tab_id] = other
    manager._persist_state()
    real_cleanup = STREAM.discard_retired_session_artifacts

    async def one_failure(workspace: str, session: str) -> None:
        if session == f"terminal-tab-{process.tab_id}":
            raise OSError("permanent failure")
        await real_cleanup(workspace, session)

    monkeypatch.setattr(STREAM, "discard_retired_session_artifacts", one_failure)
    assert await manager.retry_pending_stream_cleanup(limit=1) == 0
    assert await manager.retry_pending_stream_cleanup(limit=1) == 1
    assert list(manager._pending_stream_cleanup) == [process.tab_id]
    assert [row["tab_id"] for row in disk_state()["pending_stream_cleanup"]] == [process.tab_id]


@pytest.mark.asyncio
async def test_restart_reconfirms_loaded_obligation_before_destroying_artifacts(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, *_ = subject
    paths = await seed_artifacts(subject)
    tab_directory = TM.STATE_FILE.parent.stat()
    real_sync = os.fsync

    def fail_tab_directory_sync(fd: int) -> None:
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) == (tab_directory.st_dev, tab_directory.st_ino):
            raise OSError("unconfirmed tab directory")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_tab_directory_sync)
        with pytest.raises(TabCleanupPendingError, match="unconfirmed"):
            await manager.delete_tab(process.tab_id)
        loaded = TTYDManager()
        with pytest.raises(TabCleanupPendingError):
            await loaded.delete_tab(process.tab_id)
        assert all(path.exists() for path in paths)
        assert disk_state()["pending_stream_cleanup"]
    assert await loaded.delete_tab(process.tab_id)
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_stream_directory_sync_failure_retries_even_after_files_are_gone(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, process, _tailers, tailer, _transport = subject
    paths = await seed_artifacts(subject)
    parent_stat = tailer.store.path.parent.stat()
    real_sync = os.fsync
    calls = 0

    def fail_stream_sync(fd: int) -> None:
        nonlocal calls
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) == (parent_stat.st_dev, parent_stat.st_ino):
            calls += 1
            raise OSError("stream directory sync failed")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_stream_sync)
        for _ in range(2):
            with pytest.raises(TabCleanupPendingError):
                await manager.delete_tab(process.tab_id)
            assert disk_state()["pending_stream_cleanup"]
            assert all(not path.exists() for path in paths)
    assert calls == 2
    assert await TTYDManager().delete_tab(process.tab_id)
    assert not disk_state()["pending_stream_cleanup"]


@pytest.mark.asyncio
async def test_stream_cleanup_refuses_symlinked_namespace(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager, process, _tailers, tailer, _transport = subject
    await seed_artifacts(subject)
    await retire_pending(subject, monkeypatch)
    directory = tailer.store.path.parent
    original = tmp_path / "original-streams"
    directory.rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / tailer.store.path.name
    protected.write_text("unrelated data")
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(TabCleanupPendingError):
        await manager.delete_tab(process.tab_id)
    assert protected.read_text() == "unrelated data"
    assert disk_state()["pending_stream_cleanup"]
    directory.unlink()
    original.rename(directory)
    assert await manager.delete_tab(process.tab_id)


@pytest.mark.asyncio
async def test_cleanup_authorization_precedes_retirement(subject: DirectChat) -> None:
    manager, process, _tailers, tailer, _transport = subject
    await seed_artifacts(subject)
    evidence = tailer.store.path.with_suffix(".maso-delivery.json")
    evidence.write_text(json.dumps({"uncertain": True}))
    before = TM.STATE_FILE.read_bytes()
    with pytest.raises(RuntimeError, match="uncertain"):
        await manager.delete_tab(process.tab_id)
    assert manager.processes[process.tab_id] is process
    assert not manager._pending_stream_cleanup
    assert TM.STATE_FILE.read_bytes() == before
    assert evidence.exists()


@pytest.mark.asyncio
async def test_navigation_tab_cleanup_never_deletes_managed_session_namespace(
    subject: DirectChat,
) -> None:
    manager, process, *_ = subject
    process.workspace_role = WorkspaceSessionRole.WORKER
    process.managed_session_id = "managed-owner"
    managed = AgentStreamStore("real-workspace", "managed-owner")
    await managed.append(turn_event("managed-owner", process.tab_id))
    assert await manager.delete_tab(process.tab_id)
    assert managed.path.exists()


@pytest.mark.asyncio
async def test_configured_runtime_home_alias_commits_and_acknowledges_cleanup(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from claude_hub.services.runtime_isolation import resolve_runtime_home

    manager, process, *_ = subject
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    alias = tmp_path / "home-alias"
    alias.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(alias))
    monkeypatch.setattr(TM, "STATE_FILE", resolve_runtime_home() / "tabs.json")
    monkeypatch.setattr(TM, "ORDER_FILE", resolve_runtime_home() / "tab_order.json")
    manager._persist_state()
    paths = await seed_artifacts(subject)
    assert await manager.delete_tab(process.tab_id)
    assert manager._state_confirmed
    assert not manager._pending_stream_cleanup
    assert json.loads((real_home / "tabs.json").read_text())["pending_stream_cleanup"] == []
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile_child", [False, True])
async def test_configured_stream_root_alias_keeps_owned_children_no_follow(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hostile_child: bool
) -> None:
    manager, process, _tailers, tailer, _transport = subject
    real_root = tmp_path / "real-stream-root"
    real_root.mkdir()
    alias = tmp_path / "stream-root-alias"
    alias.symlink_to(real_root, target_is_directory=True)
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm, "STATE_ROOT", alias)
    tailer._store = AgentStreamStore(tailer.workspace_id, tailer.session_id)
    paths = await seed_artifacts(subject)
    if hostile_child:
        await retire_pending(subject, monkeypatch)
        directory = tailer.store.path.parent
        saved = tmp_path / "saved-streams"
        directory.rename(saved)
        outside = tmp_path / "unrelated-streams"
        outside.mkdir()
        protected = outside / tailer.store.path.name
        protected.write_text("unrelated private content")
        directory.symlink_to(outside, target_is_directory=True)
        with pytest.raises(TabCleanupPendingError):
            await manager.delete_tab(process.tab_id)
        assert protected.read_text() == "unrelated private content"
        directory.unlink()
        saved.rename(directory)
    assert await manager.delete_tab(process.tab_id)
    assert all(not path.exists() for path in paths)
    assert not manager._pending_stream_cleanup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix",
    [
        ".fork-seed.json",
        ".fork-seed.json.tmp",
        ".managed-delivery.json",
        ".maso-delivery.json",
        ".maso-delivery.tmp",
    ],
)
async def test_exact_sidecar_unlink_failure_never_acknowledges_cleanup(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    manager, process, _tailers, tailer, _transport = subject
    await seed_artifacts(subject)
    sidecar = tailer.store.path.with_suffix(suffix)
    if suffix == ".maso-delivery.json":
        sidecar.write_text(json.dumps({"uncertain": False}))
    elif suffix == ".managed-delivery.json":
        sidecar.write_text(json.dumps({"version": 1, "attempts": {}}))
    else:
        sidecar.write_text("private sidecar bytes")
    original_unlink = os.unlink

    def fail_sidecar_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == sidecar:
            raise PermissionError("sidecar unlink denied")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_sidecar_unlink)
        with pytest.raises(TabCleanupPendingError):
            await manager.delete_tab(process.tab_id)
        assert sidecar.exists()
        assert disk_state()["pending_stream_cleanup"]
    loaded = TTYDManager()
    assert await loaded.delete_tab(process.tab_id)
    assert not sidecar.exists()
    assert disk_state()["pending_stream_cleanup"] == []


@pytest.mark.asyncio
async def test_startup_schedules_only_after_runtime_lock_and_before_providers(
    subject: DirectChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub import main
    from claude_hub.api import agent_stream

    manager, process, *_ = subject
    await retire_pending(subject, monkeypatch)
    loaded = TTYDManager()
    ownership = False

    @contextmanager
    def owned(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        nonlocal ownership
        ownership = True
        try:
            yield
        finally:
            ownership = False

    class StopStartup(Exception):
        pass

    def schedule() -> list[asyncio.Task[bool]]:
        assert ownership
        assert loaded._pending_stream_cleanup
        raise StopStartup()

    monkeypatch.setattr(main, "BackendInstanceLock", owned)
    monkeypatch.setattr(main, "backend_file_logging", owned)
    monkeypatch.setattr(main, "ttyd_manager", loaded)
    monkeypatch.setattr(agent_stream, "_reset_tailer_manager", lambda: None)
    monkeypatch.setattr(loaded, "schedule_pending_stream_cleanup", schedule)
    providers = AsyncMock()
    monkeypatch.setattr(loaded, "start_all_tabs", providers)
    with pytest.raises(StopStartup):
        async with main.lifespan(main.app):
            pytest.fail("startup should stop in probe")
    providers.assert_not_awaited()
    assert disk_state()["pending_stream_cleanup"]
