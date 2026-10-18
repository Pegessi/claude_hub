"""Behavioral compatibility for split stream runtime and media ownership."""

from __future__ import annotations

import asyncio
import importlib
import weakref
from pathlib import Path
from typing import Any, Optional, assert_type
from unittest.mock import Mock

import pytest

from claude_hub.models import SessionKind
from claude_hub.services.agent_stream import attachments, registry, tailer
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.runtime import values
from claude_hub.services.agent_stream.tailer import SessionTailer
from tests.test_agent_stream import _as_provider_session, _FakeNativeTransport, _native_session
from tests.test_agent_stream_attachments import _png


@pytest.fixture
def state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(tailer, "_TAILER_MANAGERS", weakref.WeakSet())
    monkeypatch.setattr(tailer, "_HARD_FAILED_SESSION_IDS", set())
    monkeypatch.setattr(tailer, "_DELETION_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailer, "_PROVIDER_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailer, "_RETIRING_TAILER_IDS", set())
    return tmp_path


def _owner(transport: Optional[ProviderSession] = None) -> tailer.SessionTailer:
    session = _native_session()
    return tailer.SessionTailer(
        session.workspace_id,
        session.id,
        ClaudeJsonlAdapter(),
        lambda: session,
        native_transport=transport,
    )


@pytest.mark.parametrize(
    "name",
    [
        "NativeRuntimeSnapshot",
        "ExternalTurnRetirement",
        "StructuredSourceUnavailable",
        "EditResendTurnInFlightError",
    ],
)
def test_result_and_exception_types_have_one_definition(name: str) -> None:
    assert getattr(tailer, name) is getattr(values, name)


@pytest.mark.asyncio
async def test_existing_session_uses_patched_watchdog_limits(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner()
    now = tailer.time.monotonic()
    owner._last_event_at = now - 20
    owner._turn_in_flight_since = now - 20
    monkeypatch.setattr(tailer, "STREAM_INACTIVITY_TIMEOUT_S", 100.0)
    assert not owner._stream_inactive()
    monkeypatch.setattr(tailer, "STREAM_INACTIVITY_TIMEOUT_S", 1.0)
    assert owner._stream_inactive()
    assert not owner.is_running()


@pytest.mark.asyncio
async def test_existing_session_uses_replaced_fence_set(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner()
    owner._require_startable_incarnation()
    replacement = {owner.session_id}
    monkeypatch.setattr(tailer, "_DELETION_FENCED_SESSION_IDS", replacement)
    with pytest.raises(tailer.StructuredSourceUnavailable, match="deleted or replaced"):
        owner._require_startable_incarnation()
    replacement.clear()
    owner._require_startable_incarnation()
    assert owner._deps._DELETION_FENCED_SESSION_IDS is replacement


@pytest.mark.asyncio
async def test_existing_manager_uses_patched_provider_factory_and_keeps_own_state(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_session = _native_session().model_copy(
        update={"id": "first", "session_kind": SessionKind.CHAT}
    )
    second_session = _native_session().model_copy(
        update={"id": "second", "session_kind": SessionKind.CHAT}
    )
    first = tailer.TailerManager(
        lambda session_id: first_session if session_id == "first" else None
    )
    second = tailer.TailerManager(
        lambda session_id: second_session if session_id == "second" else None
    )
    transports: list[_FakeNativeTransport] = []

    def factory(session: Any, **_kwargs: Any) -> ProviderSession:
        transport = _FakeNativeTransport()
        transport.session = session
        transport.active_thread_id = None
        transports.append(transport)
        return _as_provider_session(transport)

    replacement = Mock(side_effect=factory)
    owner_type = tailer.SessionTailer
    owner_factory = Mock(wraps=owner_type)
    monkeypatch.setattr(tailer, "create_native_session", replacement)
    monkeypatch.setattr(tailer, "SessionTailer", owner_factory)
    try:
        first_owner = assert_type(await first.ensure_started(first_session), SessionTailer)
        second_owner = assert_type(await second.ensure_started(second_session), SessionTailer)
        assert_type(first.get_tailer(first_session.id), Optional[SessionTailer])
        assert_type(first.tailers(), list[SessionTailer])
        assert isinstance(first_owner, owner_type)
        assert isinstance(second_owner, owner_type)
        assert owner_factory.call_count == 2
        assert first._tailers is not second._tailers
        assert first._lifecycle_locks is not second._lifecycle_locks
        assert first_owner._send_lock is not second_owner._send_lock
        assert first.get_tailer("second") is None
        assert second.get_tailer("first") is None
        assert replacement.call_count == 2
        assert set(tailer._TAILER_MANAGERS) == {first, second}
        assert vars(first._deps) == vars(second._deps) == {}
    finally:
        await first.stop_all()
        await second.stop_all()


@pytest.mark.asyncio
async def test_registry_patch_after_import_controls_local_manager_lookups(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _native_session().model_copy(update={"session_kind": SessionKind.CHAT})
    manager = tailer.TailerManager(lambda _session_id: session)
    source_lookup = Mock(return_value=None)
    facade_lookup = Mock(side_effect=AssertionError("local lookup must use registry"))
    provider_factory = Mock(side_effect=AssertionError("adapter rejection must precede provider"))
    monkeypatch.setattr(registry, "get_adapter_for_session", source_lookup)
    monkeypatch.setattr(tailer, "get_adapter_for_session", facade_lookup)
    monkeypatch.setattr(tailer, "create_native_session", provider_factory)

    assert not manager.is_structured_available(session)
    with pytest.raises(ValueError, match="no structured adapter"):
        await manager._get_or_create(session)
    assert source_lookup.call_count == 2
    facade_lookup.assert_not_called()
    provider_factory.assert_not_called()
    assert manager.get_tailer(session.id) is None


@pytest.mark.asyncio
async def test_local_manager_lookup_and_global_edit_lookup_remain_distinct(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _native_session().model_copy(update={"session_kind": SessionKind.CHAT})
    manager = tailer.TailerManager(lambda _session_id: session)
    adapter = ClaudeJsonlAdapter()
    source_lookup = Mock(return_value=adapter)
    facade_lookup = Mock(return_value=None)
    transport = _FakeNativeTransport()
    monkeypatch.setattr(registry, "get_adapter_for_session", source_lookup)
    monkeypatch.setattr(tailer, "get_adapter_for_session", facade_lookup)
    monkeypatch.setattr(
        tailer, "create_native_session", Mock(return_value=_as_provider_session(transport))
    )

    try:
        assert manager.is_structured_available(session)
        owner = await manager._get_or_create(session)
        assert owner.adapter is adapter
        assert source_lookup.call_count == 2
        facade_lookup.assert_not_called()

        with pytest.raises(RuntimeError, match="no structured adapter for session"):
            await manager.edit_resend(session, "edited", "new-turn", "old-turn")
        facade_lookup.assert_called_once_with(session)
        assert source_lookup.call_count == 2
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_existing_attachment_stores_share_current_facade_lock(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = attachments.AgentStreamAttachmentStore("workspace", "first")
    second = attachments.AgentStreamAttachmentStore("workspace", "second")
    shared_lock = asyncio.Lock()
    lock_lookup = Mock(return_value=shared_lock)
    monkeypatch.setattr(attachments, "_get_global_lock", lock_lookup)
    await shared_lock.acquire()
    saving = asyncio.create_task(first.save("image/png", _png()))
    try:
        await asyncio.sleep(0)
        assert not saving.done()
        assert not attachments.AgentStreamAttachmentStore._read_index()
    finally:
        shared_lock.release()
    first_meta = await saving
    second_meta = await second.save("image/png", _png())
    assert lock_lookup.call_count == 2
    assert {entry["id"] for entry in first._read_index()} == {first_meta["id"], second_meta["id"]}
    assert vars(first._deps) == vars(second._deps) == {}


@pytest.mark.asyncio
async def test_existing_attachment_store_uses_current_path_and_validation_helpers(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = attachments.AgentStreamAttachmentStore("workspace", "existing")
    shifted_root = state_root / "shifted"
    monkeypatch.setattr(attachments, "_attachments_root", lambda: shifted_root)
    meta = await store.save("image/png", _png())
    assert attachments._resolve_attachment_path("workspace", "existing", meta["id"]).is_file()
    assert store._session_dir.is_relative_to(shifted_root)
    validate = Mock(side_effect=ValueError("replacement validator"))
    monkeypatch.setattr(attachments, "_validate_preview_mime", validate)
    with pytest.raises(ValueError, match="replacement validator"):
        await store.save("image/png", _png())
    validate.assert_called_once()


@pytest.mark.asyncio
async def test_existing_attachment_store_observes_static_index_method_patch(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = attachments.AgentStreamAttachmentStore("workspace", "existing")
    write = Mock(wraps=attachments.AgentStreamAttachmentStore._write_index)
    monkeypatch.setattr(attachments.AgentStreamAttachmentStore, "_write_index", staticmethod(write))
    await store.save("image/png", _png())
    write.assert_called_once()
