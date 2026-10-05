"""Automatic feedback must yield to a real Chat send/turn lifecycle."""

import asyncio
from types import SimpleNamespace

import pytest

from claude_hub.api import agent_stream


def test_cold_chat_probe_never_initializes_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_stream, "_tab_tailer_manager", None)
    assert not agent_stream._feedback_chat_busy()
    assert agent_stream._tab_tailer_manager is None


@pytest.mark.asyncio
async def test_feedback_waits_through_chat_startup_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_lock = asyncio.Lock()
    transport = SimpleNamespace(turn_in_flight=False)
    tailer = SimpleNamespace(native_transport=transport, _send_lock=send_lock)
    manager = SimpleNamespace(tailers=lambda: [tailer])
    monkeypatch.setattr(agent_stream, "_tab_tailer_manager", manager)

    assert not agent_stream._feedback_chat_busy()
    async with send_lock:
        assert agent_stream._feedback_chat_busy()
        transport.turn_in_flight = True
    assert agent_stream._feedback_chat_busy()
    transport.turn_in_flight = False
    assert not agent_stream._feedback_chat_busy()


def test_transcript_only_tailer_does_not_launch_or_block_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tailer = SimpleNamespace(native_transport=None, _send_lock=asyncio.Lock())
    monkeypatch.setattr(
        agent_stream, "_tab_tailer_manager", SimpleNamespace(tailers=lambda: [tailer])
    )
    assert not agent_stream._feedback_chat_busy()
