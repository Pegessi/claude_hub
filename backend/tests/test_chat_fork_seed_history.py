"""Fork-seed-history tests.

Regression coverage for the bug where forking a Chat tab copied the structured
UI history but started a **zero-history** native provider session, so the model
could not resolve references to the visible copied conversation.

The end-to-end test below drives the real chain with no stubbed injection:

    fork_tab(ordinal=k)
      -> seed sidecar written from the real copied Hub events
      -> tailer-style create_native_session(seed_history=read_seed_sidecar(...))
      -> first send_message captured at the provider boundary
      -> assert the ACTUAL provider input contains every <=k user/assistant text
         and NONE of the >k turns.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.fork_seed import (
    build_seed_body,
    build_seed_prompt,
    discard_seed_sidecar,
    read_seed_sidecar,
    strip_fork_seed_history,
    wrap_fork_seed_history,
    write_seed_sidecar,
)
from claude_hub.services.agent_stream.native import (
    ClaudeNativeSession,
    CodexNativeSession,
    CursorNativeSession,
    wrap_hub_runtime_guidance,
)
from claude_hub.services.agent_stream.store import AgentStreamStore

# ── fixtures / builders ──────────────────────────────────────────────────────


def _set_state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    wm_module = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_module, "STATE_ROOT", tmp_path / "state")


def _session(agent_type: AgentType = AgentType.CLAUDE) -> ManagedSession:
    return ManagedSession(
        id="sess-1",
        workspace_id="terminal-tabs",
        tab_id="tab-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=agent_type,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        title="test",
        workspace_path="/tmp",
        tmux_session="tmux-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _turn_started(turn_id: str, text: str) -> AgentStreamEvent:
    return AgentStreamEvent(
        stream_sequence=0,
        session_id="s",
        tab_id="t",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TURN_STARTED,
        turn_id=turn_id,
        payload={"summary": text},
        created_at=datetime.now(timezone.utc),
    )


def _assistant_delta(turn_id: str, text: str) -> AgentStreamEvent:
    return AgentStreamEvent(
        stream_sequence=0,
        session_id="s",
        tab_id="t",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TEXT_DELTA,
        turn_id=turn_id,
        message_id=f"{turn_id}:assistant",
        payload={"text": text},
        created_at=datetime.now(timezone.utc),
    )


# ── seed transcript rendering ────────────────────────────────────────────────


def test_build_seed_body_renders_qa_text_in_turn_order() -> None:
    events = [
        _turn_started("t0", "what is 2+2?"),
        _assistant_delta("t0", "It is "),
        _assistant_delta("t0", "four."),
        _turn_started("t1", "and 3+3?"),
        _assistant_delta("t1", "Six."),
    ]
    body = build_seed_body(events)
    assert body is not None
    assert body == (
        "User: what is 2+2?\n\n" "Assistant: It is four.\n\n" "User: and 3+3?\n\n" "Assistant: Six."
    )


def test_build_seed_body_orders_by_first_appearance_for_interleaved_turns() -> None:
    # A, B, A — fork ordinal grouping keeps A's pieces together in first order.
    events = [
        _turn_started("a", "q-a"),
        _turn_started("b", "q-b"),
        _assistant_delta("a", "a-answer"),
        _assistant_delta("b", "b-answer"),
    ]
    body = build_seed_body(events)
    assert body is not None
    # Rendering groups by turn (first-appearance order): all of A's Q/A block
    # precedes B's block even though B's user event arrived between them.
    assert body == "User: q-a\n\nAssistant: a-answer\n\nUser: q-b\n\nAssistant: b-answer"


def test_build_seed_body_none_when_no_text() -> None:
    # A tool-only / image-only prefix yields no seedable Q/A text.
    events = [
        AgentStreamEvent(
            stream_sequence=0,
            session_id="s",
            tab_id="t",
            agent_type=AgentType.CLAUDE,
            type=AgentStreamEventType.TOOL_CALL_STARTED,
            turn_id="t0",
            payload={"tool_call_id": "x", "name": "Read", "args": {}},
            created_at=datetime.now(timezone.utc),
        )
    ]
    assert build_seed_body(events) is None
    assert build_seed_prompt(events) is None


# ── sentinel wrap / strip ────────────────────────────────────────────────────


def test_fork_seed_wrap_strip_round_trip() -> None:
    wrapped = wrap_fork_seed_history("User: hi\n\nAssistant: hello")
    assert wrapped.startswith("<<<FORK_SEED_HISTORY_V1>>>")
    assert wrapped.endswith("\n\n")
    real_prompt = wrapped + "now do X"
    assert strip_fork_seed_history(real_prompt) == "now do X"


def test_strip_fork_seed_noop_without_block() -> None:
    assert strip_fork_seed_history("ordinary message") == "ordinary message"
    assert strip_fork_seed_history("") == ""


def test_strip_fork_seed_leaves_malformed_block_untouched() -> None:
    malformed = "<<<FORK_SEED_HISTORY_V1>>> User: hi"
    assert strip_fork_seed_history(malformed) == malformed


def test_claude_normalizer_strips_seed_and_runtime_blocks_from_provider_user_line() -> None:
    """The seeded first turn's provider transcript line contains the seed block
    AND the hub-runtime block; normalizing it must surface only the clean real
    user message in the persisted timeline/UI (blocks never leak)."""
    prompt = wrap_fork_seed_history("User: prior\n\nAssistant: prior a")
    prompt = prompt + wrap_hub_runtime_guidance("real new question")
    raw = {"type": "user", "message": {"role": "user", "content": prompt}}
    ctx = NormalizeContext(session_id="s", tab_id="t", agent_type=AgentType.CLAUDE, run_epoch=1)
    events = ClaudeJsonlAdapter().normalize_line(raw, ctx)
    turn_started = next(e for e in events if e.type == AgentStreamEventType.TURN_STARTED)
    assert turn_started.payload["summary"] == "real new question"


# ── sidecar durability ───────────────────────────────────────────────────────


def test_seed_sidecar_write_read_discard(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _set_state_root(monkeypatch, tmp_path)
    assert read_seed_sidecar("terminal-tabs", "terminal-tab-abc") is None
    write_seed_sidecar("terminal-tabs", "terminal-tab-abc", "User: hi")
    assert read_seed_sidecar("terminal-tabs", "terminal-tab-abc") == "User: hi"
    discard_seed_sidecar("terminal-tabs", "terminal-tab-abc")
    assert read_seed_sidecar("terminal-tabs", "terminal-tab-abc") is None


def test_read_seed_sidecar_ignores_corrupt_file(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _set_state_root(monkeypatch, tmp_path)
    path = tmp_path / "state" / "terminal-tabs" / "agent_streams" / "terminal-tab-x.fork-seed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert read_seed_sidecar("terminal-tabs", "terminal-tab-x") is None


# ── native transport: first-turn injection, once ─────────────────────────────


def _claude_envelope_text(stdin_json: str) -> str:
    content = json.loads(stdin_json)["message"]["content"]
    return next(b["text"] for b in content if b.get("type") == "text")


@pytest.mark.asyncio
async def test_claude_seed_prepended_to_first_turn_only() -> None:
    sess = _session(AgentType.CLAUDE)
    transport = ClaudeNativeSession(sess, seed_history="User: prior q\n\nAssistant: prior a")
    consumed: List[bool] = []

    async def on_consumed() -> None:
        consumed.append(True)

    transport._on_seed_consumed = on_consumed
    mock_spawn = AsyncMock()
    with patch.object(transport, "_spawn_oneshot", mock_spawn):
        await transport.send_message("continue please", [])
        transport.acknowledge_turn_complete()
        await transport.send_message("second turn", [])

    first = _claude_envelope_text(mock_spawn.await_args_list[0].args[1])
    second = _claude_envelope_text(mock_spawn.await_args_list[1].args[1])
    assert "<<<FORK_SEED_HISTORY_V1>>>" in first
    assert "prior q" in first and "prior a" in first
    assert first.endswith("continue please")
    # The hub-runtime guidance still coexists on the first turn.
    assert "<<<HUB_RUNTIME_V1>>>" in first
    assert "FORK_SEED" not in second
    assert second == "second turn"
    assert consumed == [True]


@pytest.mark.asyncio
async def test_cursor_seed_prepended_to_first_turn_only() -> None:
    transport = CursorNativeSession(_session(AgentType.CURSOR), seed_history="User: q")
    mock_spawn = AsyncMock()
    with patch.object(transport, "_spawn_oneshot", mock_spawn):
        await transport.send_message("continue", [])
        transport.acknowledge_turn_complete()
        await transport.send_message("again", [])
    first = mock_spawn.await_args_list[0].args[1]
    second = mock_spawn.await_args_list[1].args[1]
    assert "FORK_SEED_HISTORY_V1" in first and first.endswith("continue")
    assert "FORK_SEED" not in second and second.endswith("again")


@pytest.mark.asyncio
async def test_codex_seed_prepended_to_first_turn_only() -> None:
    transport = CodexNativeSession(_session(AgentType.CODEX), seed_history="User: q")
    transport._started = True
    transport._process = MagicMock()
    mock_request = AsyncMock(return_value={})
    with patch.object(transport, "_send_request", mock_request):
        await transport.send_message("continue", [])
        transport.acknowledge_turn_complete()
        await transport.send_message("again", [])
    first = mock_request.await_args_list[0].args[1]["input"][0]["text"]
    second = mock_request.await_args_list[1].args[1]["input"][0]["text"]
    assert "FORK_SEED_HISTORY_V1" in first and first.endswith("continue")
    assert "FORK_SEED" not in second and second == "again"


@pytest.mark.asyncio
async def test_seed_retried_when_first_send_fails() -> None:
    """A rejected/failed first turn must not lose the seed: it is only
    committed after the provider accepts the turn, so a retry still carries it
    and the consumed callback fires exactly once."""
    transport = ClaudeNativeSession(_session(AgentType.CLAUDE), seed_history="User: q")
    consumed: List[int] = []
    transport._on_seed_consumed = lambda: _append(consumed)
    mock_spawn = AsyncMock()
    mock_spawn.side_effect = [RuntimeError("provider down"), None]
    with patch.object(transport, "_spawn_oneshot", mock_spawn):
        with pytest.raises(RuntimeError):
            await transport.send_message("first", [])
        # send_message released the turn guard on failure; retry directly.
        await transport.send_message("first retry", [])
    assert len(consumed) == 1
    retry = _claude_envelope_text(mock_spawn.await_args_list[1].args[1])
    assert "FORK_SEED_HISTORY" in retry and retry.endswith("first retry")


async def _append(consumed: List[int]) -> None:
    consumed.append(1)


# Minimal PNG magic+header bytes (only the magic prefix is validated when
# staging; the file is never actually decoded in these transport tests).
_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16


@pytest.mark.asyncio
async def test_image_only_first_turn_defers_seed_to_next_text_turn() -> None:
    """Review MUST-FIX regression: a forked tab whose FIRST turn carries only
    an image (empty text, allowed by the send API) must NOT commit/discard the
    seed. The seed is absent from that image-only turn (no prompt text) but
    must still be prepended to the following text turn."""
    transport = ClaudeNativeSession(
        _session(AgentType.CLAUDE), seed_history="User: prior q\n\nAssistant: prior a"
    )
    consumed: List[int] = []
    transport._on_seed_consumed = lambda: _append(consumed)
    mock_spawn = AsyncMock()
    with patch.object(transport, "_spawn_oneshot", mock_spawn):
        # First turn: image only, no text.
        await transport.send_message("", [_PNG_BYTES])
        first_envelope = json.loads(mock_spawn.await_args_list[0].args[1])["message"]["content"]
        first_text = next(
            (b.get("text", "") for b in first_envelope if b.get("type") == "text"), ""
        )
        # Image went out, but no seed was sent or committed; it stays pending.
        assert any(b.get("type") == "image" for b in first_envelope)
        assert "FORK_SEED" not in first_text
        assert transport._seed_history is not None
        assert transport._seed_history_injected is False
        assert consumed == []

        transport.acknowledge_turn_complete()
        # Second turn: real text — must still carry the deferred seed.
        await transport.send_message("what did I say before?", [])
        second_text = _claude_envelope_text(mock_spawn.await_args_list[1].args[1])

    # Following text turn carries the full seed (context not lost).
    assert "FORK_SEED_HISTORY_V1" in second_text
    assert "prior q" in second_text
    assert second_text.endswith("what did I say before?")
    assert transport._seed_history_injected is True
    assert consumed == [1]


# ── END-TO-END: fork_tab -> sidecar -> real provider first-turn input ────────


@pytest.mark.asyncio
async def test_fork_tab_seeds_truncated_history_into_new_provider_session(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_root(monkeypatch, tmp_path)
    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 14020
    manager.processes = {}
    manager._tab_order = []

    async def fake_start(self: TTYDProcess) -> None:
        return None

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return False

    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)

    source = await manager.create_tab(
        name="Source",
        shell="/bin/zsh",
        cwd=str(tmp_path),
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )
    source_id = source.id
    store = AgentStreamStore("terminal-tabs", f"terminal-tab-{source_id}")

    # Three real turns with user text AND assistant answers (deltas split to
    # prove concatenation), with a tool call interleaved to prove it is ignored.
    await store.append(_turn_started("turn-0", "remember the code is BLUE-42"))
    await store.append(_assistant_delta("turn-0", "Got it, "))
    await store.append(_assistant_delta("turn-0", "code BLUE-42 noted."))
    await store.append(_turn_started("turn-1", "what city are we in?"))
    await store.append(_assistant_delta("turn-1", "We are in Shanghai."))
    await store.append(
        AgentStreamEvent(
            stream_sequence=0,
            session_id=f"terminal-tab-{source_id}",
            tab_id=source_id,
            agent_type=AgentType.CLAUDE,
            type=AgentStreamEventType.TOOL_CALL_STARTED,
            turn_id="turn-1",
            payload={"tool_call_id": "tc1", "name": "Bash", "args": {}},
            created_at=datetime.now(timezone.utc),
        )
    )
    await store.append(_turn_started("turn-2", "AND THE FUTURE TURN SECRET IS ZULU-99"))
    await store.append(_assistant_delta("turn-2", "future answer must be excluded"))

    # Fork at ordinal 1 (inclusive): turns 0 and 1 seed; turn 2 must not.
    forked = await manager.fork_tab(source_id, 1)
    assert forked is not None
    forked_session_id = f"terminal-tab-{forked.id}"

    # The sidecar is the tailer's handoff. It exists and holds exactly the
    # <=1 turns.
    seed_body = read_seed_sidecar("terminal-tabs", forked_session_id)
    assert seed_body is not None
    assert "BLUE-42" in seed_body
    assert "Shanghai" in seed_body
    assert "ZULU-99" not in seed_body
    assert "future answer must be excluded" not in seed_body

    # Simulate exactly what TailerManager._get_or_create does for the forked
    # tab: build the transport from the pending sidecar and send the FIRST real
    # user message. Capture at the provider boundary (real _send_text /
    # _build_stdin path; only the subprocess is faked).
    forked_descriptor = ManagedSession(
        id=forked_session_id,
        workspace_id="terminal-tabs",
        tab_id=forked.id,
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        title=forked.name,
        workspace_path=str(tmp_path),
        tmux_session=f"claude-hub-{forked.id[:8]}",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    async def drop_sidecar() -> None:
        discard_seed_sidecar("terminal-tabs", forked_session_id)

    from claude_hub.services.agent_stream.native import create_native_session

    transport = create_native_session(
        forked_descriptor,
        seed_history=seed_body,
        on_seed_consumed=drop_sidecar,
    )
    captured: List[str] = []
    mock_spawn = AsyncMock(side_effect=lambda cmd, stdin_text: captured.append(stdin_text))
    with patch.object(transport, "_spawn_oneshot", mock_spawn):
        await transport.send_message("what was the code again?", [])

    # THE CORE ACCEPTANCE: the actual provider stdin contains the <=k Q/A text
    # (user AND assistant) and nothing from turn 2.
    provider_input = _claude_envelope_text(captured[0])
    assert "remember the code is BLUE-42" in provider_input
    assert "code BLUE-42 noted." in provider_input
    assert "what city are we in?" in provider_input
    assert "We are in Shanghai." in provider_input
    assert "what was the code again?" in provider_input
    assert "ZULU-99" not in provider_input
    assert "future answer must be excluded" not in provider_input

    # The sidecar is consumed (at-most-once) and the UI event log is untouched:
    # it still contains exactly the copied <=1 turns and no injected block.
    assert read_seed_sidecar("terminal-tabs", forked_session_id) is None
    forked_store = AgentStreamStore("terminal-tabs", forked_session_id)
    page = await forked_store.read_since(-1, limit=100)
    persisted_summaries = [
        ev.payload.get("summary", "")
        for ev in page.events
        if ev.type == AgentStreamEventType.TURN_STARTED
    ]
    assert persisted_summaries == ["remember the code is BLUE-42", "what city are we in?"]
    assert all("FORK_SEED" not in s for s in persisted_summaries)


@pytest.mark.asyncio
async def test_fork_with_textless_prefix_writes_no_seed_sidecar(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_root(monkeypatch, tmp_path)
    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 14120
    manager.processes = {}
    manager._tab_order = []

    async def fake_start(self: TTYDProcess) -> None:
        return None

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return False

    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)

    source = await manager.create_tab(
        name="Source",
        shell="/bin/zsh",
        cwd=str(tmp_path),
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )
    store = AgentStreamStore("terminal-tabs", f"terminal-tab-{source.id}")
    await store.append(
        AgentStreamEvent(
            stream_sequence=0,
            session_id=f"terminal-tab-{source.id}",
            tab_id=source.id,
            agent_type=AgentType.CLAUDE,
            type=AgentStreamEventType.TOOL_CALL_STARTED,
            turn_id="turn-0",
            payload={"tool_call_id": "x", "name": "Read", "args": {}},
            created_at=datetime.now(timezone.utc),
        )
    )
    forked = await manager.fork_tab(source.id, 0)
    assert forked is not None
    assert read_seed_sidecar("terminal-tabs", f"terminal-tab-{forked.id}") is None
