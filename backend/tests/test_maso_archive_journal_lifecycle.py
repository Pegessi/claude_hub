"""Journal-backed archive/restore/delete edges after incomplete adoption."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from claude_hub.services import managed_chat
from claude_hub.services.agent_stream import tailer
from claude_hub.services.agent_stream.maso_http import MasoHttpSession
from claude_hub.services.agent_stream.tailer import TailerManager
from tests.test_maso_session_archive import (
    BASE,
    MasoServer,
    add_managed,
    add_tab,
    owners,
    payloads,
    remote,
    session,
    ttyd_module,
)


async def _owner_with_provider(owner, owners, remote, *, verified=True, sid="old"):
    tabs, wm = owners
    if owner == "tab":
        local = await add_tab(owners, remote, sid=sid)
        local.agent_session_id_verified = verified
        tabs._save_state()
        record = session(
            id="terminal-tab-tab",
            workspace_id="terminal-tabs",
            tab_id="tab",
            env={"MASO_BASE_URL": BASE},
            agent_session_id=sid,
            agent_session_id_verified=verified,
        )
        persist = lambda cid: tabs.set_tab_agent_session_id("tab", cid)
        fault_owner, fault_name = ttyd_module, "_write_tabs_snapshot"
    else:
        local = await add_managed(owners, remote, sid=sid)
        local.agent_session_id_verified = verified
        wm._save_state()
        record = local
        persist = lambda cid: wm.set_session_agent_session_id(local.id, cid)
        fault_owner, fault_name = wm, "_atomic_save_state_text"
    await remote.coordinator.drain()
    remote.calls.clear()
    server = MasoServer()
    provider = MasoHttpSession(record, conversation_id_persist=persist)
    provider._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(server.handle)
    )
    return SimpleNamespace(
        local=local,
        record=record,
        server=server,
        provider=provider,
        fault_owner=fault_owner,
        fault_name=fault_name,
    )


async def _register_transport(prepared, monkeypatch):
    manager = TailerManager(lambda _id: prepared.record)
    monkeypatch.setattr(tailer, "create_native_session", lambda *args, **kwargs: prepared.provider)
    await manager._get_or_create(prepared.record)
    return manager


async def _archive(owner, owners, prepared):
    tabs, wm = owners
    if owner == "tab":
        await tabs.archive_tab(prepared.local.tab_id)
    else:
        await wm.archive_session("w", prepared.local.id)


async def _restore(owner, owners, prepared):
    tabs, wm = owners
    if owner == "tab":
        await tabs.unarchive_tab(prepared.local.tab_id)
    else:
        await wm.unarchive_session("w", prepared.local.id)


async def _delete(owner, owners, prepared):
    tabs, wm = owners
    if owner == "tab":
        await tabs.delete_tab(prepared.local.tab_id)
    else:
        await wm.delete_session(prepared.local.id)


def _fail(*_args, **_kwargs):
    raise OSError("injected persistence failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["tab", "managed"])
@pytest.mark.parametrize("sid", ["local-placeholder", None], ids=["constructive", "empty"])
async def test_journal_only_allocation_restores_after_archive_is_fully_drained(
    owners, remote, monkeypatch, owner, sid
):
    prepared = await _owner_with_provider(owner, owners, remote, verified=False, sid=sid)
    provider = prepared.provider
    with monkeypatch.context() as faults:
        faults.setattr(prepared.fault_owner, prepared.fault_name, _fail)
        with pytest.raises(OSError, match="injected persistence failure"):
            await provider._create_session()
    assert provider._delivery_path.exists()
    assert not prepared.local.agent_session_id_verified
    manager = await _register_transport(prepared, monkeypatch)
    try:
        await _archive(owner, owners, prepared)
        await remote.coordinator.drain()
        target = BASE + "/api/sessions/opaque-sid/archive"
        assert payloads(remote) == [(target, {"archived": True})]
        assert provider._delivery_path.exists()
        remote.calls.clear()
        # Restoration must send false itself, not rely on a subsequent native
        # reopen/send or on correction of an archive that is still in flight.
        monkeypatch.setattr(managed_chat, "start", AsyncMock())
        await _restore(owner, owners, prepared)
        await remote.coordinator.drain()
        assert payloads(remote) == [(target, {"archived": False})]
        assert provider._delivery_path.exists()
        assert prepared.server.session_creations == 1
        assert not any(path.endswith("/messages") for _, path, _ in prepared.server.calls)
    finally:
        await manager.stop_all()


async def _pending_rotation(prepared, monkeypatch):
    provider = prepared.provider
    candidate = await provider.rotate_conversation()
    original_unlink = Path.unlink

    def fail_journal_unlink(path, *args, **kwargs):
        if path == provider._delivery_path:
            _fail()
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as faults:
        faults.setattr(Path, "unlink", fail_journal_unlink)
        with pytest.raises(OSError, match="injected persistence failure"):
            provider.adopt_rotated_conversation(candidate)
    assert prepared.local.agent_session_id == candidate
    journal = json.loads(provider._delivery_path.read_text())
    assert journal["session_id"] == "old"
    assert journal["session_id_verified"] is True
    assert journal["adopted_session_id"] == candidate
    return candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["tab", "managed"])
@pytest.mark.parametrize(
    "shared", [None, "old", "candidate"], ids=["sole", "shared-old", "shared-new"]
)
async def test_permanent_removal_captures_verified_journal_ids_and_respects_other_owners(
    owners, remote, monkeypatch, owner, shared
):
    prepared = await _owner_with_provider(owner, owners, remote)
    candidate = await _pending_rotation(prepared, monkeypatch)
    if shared:
        await add_tab(owners, remote, tab_id="other", sid="old" if shared == "old" else candidate)
    await remote.coordinator.drain()
    remote.calls.clear()
    manager = await _register_transport(prepared, monkeypatch)
    try:
        await _delete(owner, owners, prepared)
        await remote.coordinator.drain()
        assert not prepared.provider._delivery_path.exists()
        actual = {url for url, body in payloads(remote) if body == {"archived": True}}
        expected_ids = {"old", candidate}
        if shared:
            expected_ids.discard("old" if shared == "old" else candidate)
        assert actual == {BASE + "/api/sessions/" + sid + "/archive" for sid in expected_ids}
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["tab", "managed"])
async def test_resumable_archive_keeps_old_adoption_guard(owners, remote, monkeypatch, owner):
    prepared = await _owner_with_provider(owner, owners, remote)
    candidate = await _pending_rotation(prepared, monkeypatch)
    await remote.coordinator.drain()
    remote.calls.clear()
    manager = await _register_transport(prepared, monkeypatch)
    try:
        await _archive(owner, owners, prepared)
        await remote.coordinator.drain()
        assert payloads(remote) == [
            (BASE + "/api/sessions/" + candidate + "/archive", {"archived": True})
        ]
        assert prepared.provider._delivery_path.exists()
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["tab", "managed"])
@pytest.mark.parametrize(
    "verified", [False, None, "true"], ids=["unverified", "legacy", "not-bool"]
)
async def test_permanent_removal_never_promotes_unverified_journal_old_id(
    owners, remote, monkeypatch, owner, verified
):
    prepared = await _owner_with_provider(owner, owners, remote)
    candidate = await _pending_rotation(prepared, monkeypatch)
    path = prepared.provider._delivery_path
    journal = json.loads(path.read_text())
    if verified is None:
        journal.pop("session_id_verified")
    else:
        journal["session_id_verified"] = verified
    path.write_text(json.dumps(journal))
    await remote.coordinator.drain()
    remote.calls.clear()
    manager = await _register_transport(prepared, monkeypatch)
    try:
        await _delete(owner, owners, prepared)
        await remote.coordinator.drain()
        assert payloads(remote) == [
            (BASE + "/api/sessions/" + candidate + "/archive", {"archived": True})
        ]
        assert not path.exists()
    finally:
        await manager.stop_all()
