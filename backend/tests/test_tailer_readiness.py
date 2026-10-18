"""Read-only native startup and input activity contracts for API probes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional, cast
from unittest.mock import AsyncMock

import pytest

from claude_hub.api import agent_stream
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.tailer import SessionTailer, StructuredSourceUnavailable


def _tailer(
    transport: Optional[SimpleNamespace], *, native_error: Optional[str] = None
) -> SessionTailer:
    return SessionTailer(
        workspace_id="readiness",
        session_id="chat",
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: None,
        native_transport=cast(Optional[ProviderSession], transport),
        native_error=native_error,
    )


def _patch_scheduled_probe(
    monkeypatch: pytest.MonkeyPatch, tailer: object, *, hard_failed: bool = False
) -> AsyncMock:
    ensure_started = AsyncMock(return_value=tailer)
    manager = SimpleNamespace(
        ensure_started=ensure_started,
        hard_failed=lambda _session_id: hard_failed,
    )
    monkeypatch.setattr(
        agent_stream, "_terminal_tab_stream_session", lambda _tab_id: SimpleNamespace(id="chat")
    )
    monkeypatch.setattr(agent_stream, "_get_tab_tailer_manager", lambda: manager)
    return ensure_started


@pytest.mark.parametrize(
    ("transport", "started", "ready"),
    [
        (None, False, False),
        (SimpleNamespace(eof_is_fatal=False), False, False),
        (SimpleNamespace(_started=False, eof_is_fatal=False), False, False),
        (SimpleNamespace(_started=True, eof_is_fatal=False), True, True),
        (SimpleNamespace(_started=True, eof_is_fatal=True), True, False),
        (
            SimpleNamespace(_started=True, eof_is_fatal=True, _handshake_complete=False),
            True,
            False,
        ),
        (
            SimpleNamespace(_started=True, eof_is_fatal=True, _handshake_complete=True),
            True,
            True,
        ),
        (
            SimpleNamespace(_started=False, eof_is_fatal=True, _handshake_complete=True),
            False,
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_startup_accessors_preserve_provider_handshake_rules(
    transport: Optional[SimpleNamespace], started: bool, ready: bool
) -> None:
    tailer = _tailer(transport)
    owner_state = vars(tailer).copy()
    transport_state = vars(transport).copy() if transport is not None else None

    assert tailer.native_started is started
    assert tailer.native_ready is ready
    assert vars(tailer) == owner_state
    assert transport is None or vars(transport) == transport_state
    assert tailer.native_transport is transport
    assert not tailer.is_running()


@pytest.mark.parametrize("name", ["native_started", "native_ready", "input_busy"])
@pytest.mark.asyncio
async def test_runtime_probes_are_read_only(name: str) -> None:
    tailer = _tailer(None)
    with pytest.raises(AttributeError):
        setattr(tailer, name, True)


@pytest.mark.asyncio
async def test_startup_accessors_follow_live_transport_changes_without_caching() -> None:
    transport = SimpleNamespace(_started=False, eof_is_fatal=True, _handshake_complete=False)
    tailer = _tailer(transport)
    assert not tailer.native_ready
    transport._started = True
    assert tailer.native_started and not tailer.native_ready
    transport._handshake_complete = True
    assert tailer.native_ready
    transport._started = False
    assert not tailer.native_started and not tailer.native_ready


@pytest.mark.asyncio
async def test_input_activity_accessor_reads_owner_lock_without_a_transport() -> None:
    tailer = _tailer(None)
    assert not tailer.input_busy
    async with tailer._send_lock:
        assert tailer.input_busy
    assert not tailer.input_busy
    assert tailer.native_transport is None
    assert not tailer.is_running()


@pytest.mark.asyncio
async def test_scheduled_probe_yields_one_tick_for_new_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SimpleNamespace(_started=False, eof_is_fatal=False)
    tailer = _tailer(transport)
    ensure_started = _patch_scheduled_probe(monkeypatch, tailer)
    asyncio.get_running_loop().call_soon(setattr, transport, "_started", True)

    assert await agent_stream._ensure_scheduled_chat_runtime("tab")
    ensure_started.assert_awaited_once()
    assert not tailer.is_running()


@pytest.mark.asyncio
async def test_scheduled_probe_does_not_wait_for_persistent_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SimpleNamespace(_started=True, eof_is_fatal=True, _handshake_complete=False)
    tailer = _tailer(transport)
    _patch_scheduled_probe(monkeypatch, tailer)
    asyncio.get_running_loop().call_soon(setattr, transport, "_handshake_complete", True)

    assert not await agent_stream._ensure_scheduled_chat_runtime("tab")
    await asyncio.sleep(0)
    assert await agent_stream._ensure_scheduled_chat_runtime("tab")


@pytest.mark.asyncio
async def test_scheduled_probe_uses_public_read_only_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deliberately exposes no transport internals: only the tailer's public probes.
    tailer = SimpleNamespace(
        native_transport=object(), native_error=None, native_started=True, native_ready=True
    )
    _patch_scheduled_probe(monkeypatch, tailer)
    assert await agent_stream._ensure_scheduled_chat_runtime("tab")


@pytest.mark.parametrize("failure", ["absent", "native_error", "hard_failed", "still_starting"])
@pytest.mark.asyncio
async def test_scheduled_probe_fails_closed(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    transport = (
        None
        if failure == "absent"
        else SimpleNamespace(_started=failure != "still_starting", eof_is_fatal=False)
    )
    tailer = _tailer(transport, native_error="unavailable" if failure == "native_error" else None)
    _patch_scheduled_probe(monkeypatch, tailer, hard_failed=failure == "hard_failed")
    assert not await agent_stream._ensure_scheduled_chat_runtime("tab")


@pytest.mark.parametrize("error_type", [ValueError, StructuredSourceUnavailable])
@pytest.mark.asyncio
async def test_scheduled_probe_retries_unavailable_runtime(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    ensure_started = _patch_scheduled_probe(monkeypatch, _tailer(None))
    ensure_started.side_effect = error_type("unavailable")
    assert not await agent_stream._ensure_scheduled_chat_runtime("tab")


@pytest.mark.asyncio
async def test_scheduled_probe_missing_tab_does_not_create_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_stream, "_terminal_tab_stream_session", lambda _tab_id: None)
    create_manager = AsyncMock()
    monkeypatch.setattr(agent_stream, "_get_tab_tailer_manager", create_manager)
    assert not await agent_stream._ensure_scheduled_chat_runtime("gone")
    create_manager.assert_not_called()


def test_feedback_probe_uses_public_read_only_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    tailer = SimpleNamespace(input_busy=True)
    monkeypatch.setattr(
        agent_stream, "_tab_tailer_manager", SimpleNamespace(tailers=lambda: [tailer])
    )
    assert agent_stream._feedback_chat_busy()
    tailer.input_busy = False
    assert not agent_stream._feedback_chat_busy()
