"""Behavioral contracts for main's Goal/Dot changes after responsibility splitting."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.api import agent_stream as stream_api
from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.tailer import SessionTailer, TailerManager


def _session() -> ManagedSession:
    return ManagedSession(
        id="merge-session",
        workspace_id="merge-workspace",
        tab_id="merge-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="merge contract",
        workspace_path="/tmp",
        tmux_session="unused-merge-tmux",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class _MultiStageTransport(ProviderSession):
    goal_completion_requires_provider_event = True

    def _build_command(self) -> list[str]:
        return []

    async def _send_text(self, text: str) -> None:
        raise AssertionError("this test never submits a provider turn")


@pytest.mark.asyncio
async def test_multistage_goal_cannot_synthesize_even_with_a_preexisting_terminal_latch(
    monkeypatch,
) -> None:
    session = _session()
    transport = _MultiStageTransport(session)
    tailer = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    tailer._goal_protocol_turn = True
    tailer._active_turn_id = "goal-turn"
    tailer._assistant_text = '<goal-status state="continue">next</goal-status>'
    tailer._note_assistant_text(transport)
    assert tailer._goal_terminal_at is None

    # A stale latch must not bypass the second guard at the terminal operation.
    tailer._goal_terminal_at = time.monotonic() - 100
    publish = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(tailer, "_publish", publish)
    monkeypatch.setattr(transport, "stop", stop)
    await tailer._synthesize_goal_completion_locked(transport)
    publish.assert_not_awaited()
    stop.assert_not_awaited()
    assert tailer._active_turn_id == "goal-turn"


@pytest.mark.asyncio
async def test_dot_yield_keeps_facade_patch_lookup_inside_the_admission_guard(monkeypatch) -> None:
    import claude_hub.services.goal_run as goal_run

    session = _session()
    manager = MagicMock(spec=TailerManager)
    goal_manager = MagicMock()
    goal_manager.current.return_value = None
    goal_manager.refresh_current = AsyncMock(return_value=None)
    goal_manager.has_goal_history.return_value = False
    monkeypatch.setattr(goal_run, "get_goal_manager", lambda: goal_manager)
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    order: list[str] = []

    @asynccontextmanager
    async def admission_guard():
        order.append("admit")
        yield
        order.append("release")

    async def yield_turn(tab_id, admitted_session, admitted_manager):
        assert tab_id == session.tab_id
        assert admitted_session is session and admitted_manager is manager
        order.append("yield")

    async def send(*args, **kwargs):
        order.append("send")

    monkeypatch.setattr(stream_api, "_yield_dot_turn", yield_turn)
    monkeypatch.setattr(stream_api, "_send_to_native", send)
    payload = stream_api.AgentStreamSendRequest(text="hello", client_turn_id="manual-turn")
    delivered = await stream_api._dispatch_tab_stream_input(
        session.tab_id, payload, admission_guard=admission_guard
    )
    assert delivered == "manual-turn"
    assert order == ["admit", "yield", "send", "release"]
