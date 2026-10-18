"""Automatic feedback must yield to a real Chat send/turn lifecycle."""

from types import SimpleNamespace
from typing import Optional, cast

import pytest

from claude_hub.api import agent_stream
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.tailer import SessionTailer


def _tailer(transport: Optional[SimpleNamespace]) -> SessionTailer:
    return SessionTailer(
        workspace_id="feedback-readiness",
        session_id="chat",
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: None,
        native_transport=cast(Optional[ProviderSession], transport),
    )


def test_cold_chat_probe_never_initializes_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_stream, "_tab_tailer_manager", None)
    assert not agent_stream._feedback_chat_busy()
    assert agent_stream._tab_tailer_manager is None


@pytest.mark.asyncio
async def test_feedback_waits_through_chat_startup_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SimpleNamespace(turn_in_flight=False)
    tailer = _tailer(transport)
    manager = SimpleNamespace(tailers=lambda: [tailer])
    monkeypatch.setattr(agent_stream, "_tab_tailer_manager", manager)

    assert not agent_stream._feedback_chat_busy()
    async with tailer._send_lock:
        assert agent_stream._feedback_chat_busy()
        transport.turn_in_flight = True
    assert agent_stream._feedback_chat_busy()
    transport.turn_in_flight = False
    assert not agent_stream._feedback_chat_busy()


@pytest.mark.asyncio
async def test_transcript_only_tailer_does_not_launch_or_block_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tailer = _tailer(None)
    monkeypatch.setattr(
        agent_stream, "_tab_tailer_manager", SimpleNamespace(tailers=lambda: [tailer])
    )
    assert not agent_stream._feedback_chat_busy()
