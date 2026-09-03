"""Tests for native provider transports (Claude / Codex / Cursor).

Phase 1 covers the Claude stream-json delta normalization. Phase 3 adds Codex
app-server and Cursor stream-json parsing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    ChatMode,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.native import (
    ClaudeNativeSession,
    CodexNativeSession,
    CursorNativeSession,
    create_native_session,
)


def _session(agent_type: AgentType = AgentType.CLAUDE) -> ManagedSession:
    return ManagedSession(
        id="sess-1",
        workspace_id="ws-1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=agent_type,
        status=ManagedSessionStatus.IDLE,
        title="test",
        workspace_path="/tmp",
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _ctx() -> NormalizeContext:
    return NormalizeContext(
        session_id="sess-1",
        tab_id="tab-1",
        agent_type=AgentType.CLAUDE,
        run_epoch=1,
    )


# ── factory ─────────────────────────────────────────────────────────────────


def test_create_native_session_returns_correct_type() -> None:
    assert isinstance(create_native_session(_session(AgentType.CLAUDE)), ClaudeNativeSession)
    assert isinstance(create_native_session(_session(AgentType.CODEX)), CodexNativeSession)
    assert isinstance(create_native_session(_session(AgentType.CURSOR)), CursorNativeSession)


def test_create_native_session_rejects_terminal() -> None:
    with pytest.raises(ValueError):
        create_native_session(_session(AgentType.TERMINAL))


def test_claude_plan_mode_uses_permission_mode_without_permission_bypass() -> None:
    session = _session(AgentType.CLAUDE).model_copy(
        update={"chat_mode": ChatMode.PLAN, "solo_mode": True}
    )

    command = ClaudeNativeSession(session)._build_command()

    assert command[-2:] == ["--permission-mode", "plan"]
    assert "--dangerously-skip-permissions" not in command


def test_cursor_plan_mode_uses_mode_without_yolo() -> None:
    session = _session(AgentType.CURSOR).model_copy(
        update={"chat_mode": ChatMode.PLAN, "solo_mode": True}
    )

    command = CursorNativeSession(session)._build_command()

    assert command[-2:] == ["--mode", "plan"]
    assert "--yolo" not in command


# ── Claude command building ─────────────────────────────────────────────────


def test_claude_command_includes_streaming_flags() -> None:
    sess = _session()
    cmd = ClaudeNativeSession(sess)._build_command()
    assert "--print" in cmd
    assert "--input-format" in cmd
    assert "stream-json" in cmd
    assert "--output-format" in cmd
    assert "--include-partial-messages" in cmd
    assert "--verbose" in cmd


def test_claude_command_solo_mode_adds_dangerously_skip() -> None:
    sess = _session()
    sess.solo_mode = True
    cmd = ClaudeNativeSession(sess)._build_command()
    assert "--dangerously-skip-permissions" in cmd


def test_claude_command_uses_session_id_for_unverified_pinned_id() -> None:
    """A constructive (unverified) agent_session_id must use --session-id,
    which creates the conversation, not --resume (which requires it to exist)."""
    sess = _session()
    sess.agent_session_id = "abc-123"
    cmd = ClaudeNativeSession(sess)._build_command()
    assert "--session-id" in cmd
    assert "abc-123" in cmd
    assert "--resume" not in cmd


def test_claude_command_uses_resume_for_verified_id() -> None:
    """Once the provider has emitted the id (verified), use --resume so a
    missing conversation fails closed rather than silently creating a new one."""
    sess = _session()
    sess.agent_session_id = "abc-123"
    native = ClaudeNativeSession(sess)
    native._conversation_id_verified = True
    cmd = native._build_command()
    assert "--resume" in cmd
    assert "abc-123" in cmd
    assert "--session-id" not in cmd


def test_verified_flag_seeded_from_session_not_inferred_from_uuid() -> None:
    """``_conversation_id_verified`` must come from the persisted
    ``agent_session_id_verified`` flag, never inferred from the mere presence
    of a UUID. A constructive (unverified) id must use ``--session-id`` even
    though it looks like a real conversation id."""
    sess = _session()
    sess.agent_session_id = str(__import__("uuid").uuid4())
    sess.agent_session_id_verified = False
    native = ClaudeNativeSession(sess)
    assert native._conversation_id_verified is False
    cmd = native._build_command()
    assert "--session-id" in cmd
    assert "--resume" not in cmd


def test_verified_flag_true_seeds_conversation_id_verified() -> None:
    """A persisted ``agent_session_id_verified=True`` seeds
    ``_conversation_id_verified=True`` so the first turn after a cold restart
    uses ``--resume`` rather than ``--session-id``."""
    sess = _session()
    sess.agent_session_id = "captured-id"
    sess.agent_session_id_verified = True
    native = ClaudeNativeSession(sess)
    assert native._conversation_id_verified is True
    cmd = native._build_command()
    assert "--resume" in cmd
    assert "captured-id" in cmd
    assert "--session-id" not in cmd


# ── Claude stream_event normalization ───────────────────────────────────────


def test_claude_message_start_maps_to_turn_started() -> None:
    adapter = ClaudeJsonlAdapter()
    raw = {
        "type": "stream_event",
        "event": {"type": "message_start", "message": {"id": "msg-1", "role": "assistant"}},
    }
    events = adapter.normalize_line(raw, _ctx())
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_STARTED


def test_claude_text_delta_maps_to_text_delta() -> None:
    adapter = ClaudeJsonlAdapter()
    raw = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Hello"},
        },
    }
    events = adapter.normalize_line(raw, _ctx())
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TEXT_DELTA
    assert events[0].payload["text"] == "Hello"


def test_claude_thinking_delta_maps_to_thinking_delta() -> None:
    adapter = ClaudeJsonlAdapter()
    raw = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "The user wants"},
        },
    }
    events = adapter.normalize_line(raw, _ctx())
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.THINKING_DELTA
    assert events[0].payload["text"] == "The user wants"


def test_claude_message_stop_does_not_emit_turn_completed() -> None:
    """``message_stop`` must NOT emit TURN_COMPLETED.

    The provider may still emit a final top-level ``assistant`` snapshot and a
    ``result`` record after ``message_stop``. TURN_COMPLETED is emitted on the
    top-level ``result`` record instead, so the final assistant/tool records
    always precede the turn-completion event.
    """
    adapter = ClaudeJsonlAdapter()
    raw = {"type": "stream_event", "event": {"type": "message_stop"}}
    events = adapter.normalize_line(raw, _ctx())
    assert events == []


def test_claude_result_emits_turn_completed() -> None:
    """The top-level ``result`` record emits TURN_COMPLETED."""
    adapter = ClaudeJsonlAdapter()
    raw = {"type": "result"}
    events = adapter.normalize_line(raw, _ctx())
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_COMPLETED


def test_claude_unknown_stream_event_is_skipped() -> None:
    adapter = ClaudeJsonlAdapter()
    raw = {"type": "stream_event", "event": {"type": "content_block_start", "index": 0}}
    events = adapter.normalize_line(raw, _ctx())
    assert events == []


def test_claude_non_dict_stream_event_is_skipped() -> None:
    adapter = ClaudeJsonlAdapter()
    raw = {"type": "stream_event", "event": None}
    events = adapter.normalize_line(raw, _ctx())
    assert events == []


# ── Codex app-server protocol ───────────────────────────────────────────────


class _FakeStream:
    """A fake asyncio stream backed by an asyncio.Queue.

    ``readline`` blocks until a line is pushed, mirroring real subprocess I/O
    where responses arrive one at a time (after the request future exists).
    """

    def __init__(self, lines: Optional[List[bytes]] = None, eof: bool = False) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.written: List[bytes] = []
        self._closed = False
        self._eof = eof
        if lines:
            for line in lines:
                self._queue.put_nowait(line)

    def push(self, line: bytes) -> None:
        self._queue.put_nowait(line)

    async def readline(self) -> bytes:
        # Simulate subprocess I/O latency so request futures are created
        # before the matching response is consumed.
        await asyncio.sleep(0.01)
        return await self._queue.get()

    async def read(self, n: int) -> bytes:
        # Return the next queued chunk.  When the queue is empty:
        #   - if ``eof`` was set (e.g. stderr with no output), return b"" to
        #     signal end-of-stream;
        #   - otherwise block, mirroring a live subprocess whose stdout has
        #     not yet closed.
        # The real transport reads stdout in chunks via ``read(n)`` instead of
        # ``readline()`` to avoid asyncio's 64 KiB line-length limit.
        await asyncio.sleep(0.01)
        if self._queue.empty():
            if self._eof:
                return b""
            return await self._queue.get()
        return await self._queue.get()

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True


class _FakeProcess:
    def __init__(self, stdout_lines: List[bytes]) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stdin = _FakeStream()
        self.stderr = _FakeStream(eof=True)
        self._terminated = False

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._terminated = True


def _codex_session() -> CodexNativeSession:
    return CodexNativeSession(_session(AgentType.CODEX))


def _written_requests(proc: _FakeProcess) -> List[Dict[str, Any]]:
    """Parse all JSON-RPC messages written to stdin."""
    out: List[Dict[str, Any]] = []
    for chunk in proc.stdin.written:
        for line in chunk.decode("utf-8").strip().splitlines():
            if line:
                out.append(json.loads(line))
    return out


@pytest.mark.asyncio
async def test_codex_initialize_sends_client_info() -> None:
    """initialize must include clientInfo{name, version}."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
        ]
    )
    sess = _codex_session()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await sess.start()
    msgs = _written_requests(proc)
    init = next(m for m in msgs if m.get("method") == "initialize")
    params = init["params"]
    assert "clientInfo" in params
    assert params["clientInfo"]["name"]
    assert params["clientInfo"]["version"]


@pytest.mark.asyncio
async def test_codex_sends_initialized_notification_after_initialize() -> None:
    """After initialize response, the client must send an initialized notification."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
        ]
    )
    sess = _codex_session()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await sess.start()
    msgs = _written_requests(proc)
    methods = [m.get("method") for m in msgs]
    assert "initialize" in methods
    assert "initialized" in methods
    # initialized must come after initialize and must be a notification (no id).
    init_idx = methods.index("initialize")
    initd_idx = methods.index("initialized")
    assert initd_idx > init_idx
    initd = msgs[initd_idx]
    assert "id" not in initd


@pytest.mark.asyncio
async def test_codex_concurrent_start_is_single_flight() -> None:
    """Tailer startup and a first send may call ``start`` concurrently.

    They must share one app-server and one stdout reader.  Spawning twice
    overwrites ``self._process`` before either reader runs, which makes both
    tasks read the same stream and fails with asyncio's concurrent-reader
    RuntimeError.
    """
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
        ]
    )
    sess = _codex_session()
    spawn = AsyncMock(return_value=proc)

    with patch("asyncio.create_subprocess_exec", new=spawn):
        await asyncio.gather(sess.start(), sess.start())

    spawn.assert_awaited_once()
    assert sess._thread_id == "th-1"
    assert sess._reader_task is not None
    assert not sess._reader_task.done()


@pytest.mark.asyncio
async def test_codex_restart_discards_idle_stop_eof_sentinel() -> None:
    """A fresh app-server generation must not consume the prior stop's EOF."""

    def process(thread_id: str, first_request_id: int) -> _FakeProcess:
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"jsonrpc": "2.0", "id": first_request_id, "result": {}}).encode()
                + b"\n",
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": first_request_id + 1,
                        "result": {"thread": {"id": thread_id}},
                    }
                ).encode()
                + b"\n",
            ]
        )

    first = process("th-first", 1)
    second = process("th-first", 3)
    sess = _codex_session()
    spawn = AsyncMock(side_effect=[first, second])

    with patch("asyncio.create_subprocess_exec", new=spawn):
        await sess.start()
        await sess.stop()
        # The cancelled first reader published an EOF to its generation's
        # notification queue. Starting again must replace that queue.
        await sess.start()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sess.read_line(), timeout=0.05)
        await sess.stop()

    assert spawn.await_count == 2


@pytest.mark.asyncio
async def test_codex_thread_start_reads_thread_id_from_result() -> None:
    """thread/start result is {thread: {id, ...}}; read result.thread.id."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-abc", "cwd": "/x"}}}
            ).encode()
            + b"\n",
        ]
    )
    sess = _codex_session()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await sess.start()
    assert sess._thread_id == "th-abc"


@pytest.mark.asyncio
async def test_codex_turn_start_uses_input_array_not_prompt() -> None:
    """turn/start requires {threadId, input:[{type:text,text:...}]}; no prompt field."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
            json.dumps(
                {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tu-1", "status": "running"}}}
            ).encode()
            + b"\n",
        ]
    )
    sess = _codex_session()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await sess.start()
        await sess.send_message("hello", [])
    msgs = _written_requests(proc)
    turn = next(m for m in msgs if m.get("method") == "turn/start")
    params = turn["params"]
    assert "prompt" not in params
    assert params["threadId"] == "th-1"
    assert isinstance(params["input"], list)
    assert params["input"][0] == {"type": "text", "text": "hello"}


@pytest.mark.asyncio
async def test_codex_plan_mode_uses_schema_verified_collaboration_mode_payload() -> None:
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"thread": {"id": "th-1"}, "model": "gpt-5.6"},
                }
            ).encode()
            + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "name": "Default",
                                "mode": "default",
                                "model": None,
                                "reasoning_effort": None,
                            },
                            {
                                "name": "Plan",
                                "mode": "read",
                                "model": None,
                                "reasoning_effort": "medium",
                            },
                        ]
                    },
                }
            ).encode()
            + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"turn": {"id": "tu-1"}}}).encode()
            + b"\n",
        ]
    )
    native = _codex_session()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.set_mode("plan")
        await native.send_message("inspect only", [])

    messages = _written_requests(proc)
    initialize = next(item for item in messages if item.get("method") == "initialize")
    assert initialize["params"]["capabilities"]["experimentalApi"] is True
    turn = next(item for item in messages if item.get("method") == "turn/start")
    assert turn["params"]["collaborationMode"] == {
        "mode": "read",
        "settings": {
            "model": "gpt-5.6",
            "developer_instructions": None,
            "reasoning_effort": "medium",
        },
    }
    capabilities = native.capabilities()
    assert capabilities.current_mode == "plan"
    assert [item.id for item in capabilities.available_modes] == ["default", "plan"]
    assert capabilities.supports_dynamic_modes is True


@pytest.mark.asyncio
async def test_codex_selected_model_overrides_thread_model_in_default_mode() -> None:
    """CODEX_MODEL in the session env must reach turn/start via
    collaborationMode.settings.model, overriding the thread's own model."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"thread": {"id": "th-1"}, "model": "gpt-5.6-sol"},
                }
            ).encode()
            + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "name": "Default",
                                "mode": "default",
                                "model": None,
                                "reasoning_effort": None,
                            },
                            {
                                "name": "Plan",
                                "mode": "plan",
                                "model": None,
                                "reasoning_effort": "medium",
                            },
                        ]
                    },
                }
            ).encode()
            + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"turn": {"id": "tu-1"}}}).encode()
            + b"\n",
        ]
    )
    session = _session(AgentType.CODEX)
    session.env["CODEX_MODEL"] = "gpt-5.3-codex-spark"
    native = CodexNativeSession(session)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.prepare_capabilities()
        await native.send_message("hello", [])

    messages = _written_requests(proc)
    turn = next(item for item in messages if item.get("method") == "turn/start")
    assert turn["params"]["collaborationMode"] == {
        "mode": "default",
        "settings": {
            "model": "gpt-5.3-codex-spark",
            "developer_instructions": None,
        },
    }


@pytest.mark.asyncio
async def test_codex_selected_model_sent_even_without_advertised_preset() -> None:
    """When collaborationMode/list was never run (no presets loaded), a pinned
    CODEX_MODEL must still be sent in default mode so the selection is not
    silently dropped."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"thread": {"id": "th-1"}, "model": "gpt-5.6-sol"},
                }
            ).encode()
            + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tu-1"}}}).encode()
            + b"\n",
        ]
    )
    session = _session(AgentType.CODEX)
    session.env["CODEX_MODEL"] = "gpt-5.4-mini"
    native = CodexNativeSession(session)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.send_message("hello", [])

    messages = _written_requests(proc)
    turn = next(item for item in messages if item.get("method") == "turn/start")
    assert turn["params"]["collaborationMode"] == {
        "mode": "default",
        "settings": {
            "model": "gpt-5.4-mini",
            "developer_instructions": None,
        },
    }


@pytest.mark.asyncio
async def test_codex_selected_model_overrides_in_plan_mode() -> None:
    """CODEX_MODEL must override the thread model even when plan mode is
    active, so the picker and mode toggle compose."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"thread": {"id": "th-1"}, "model": "gpt-5.6-sol"},
                }
            ).encode()
            + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "name": "Default",
                                "mode": "default",
                                "model": None,
                                "reasoning_effort": None,
                            },
                            {
                                "name": "Plan",
                                "mode": "read",
                                "model": None,
                                "reasoning_effort": "medium",
                            },
                        ]
                    },
                }
            ).encode()
            + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"turn": {"id": "tu-1"}}}).encode()
            + b"\n",
        ]
    )
    session = _session(AgentType.CODEX)
    session.env["CODEX_MODEL"] = "gpt-5.4"
    native = CodexNativeSession(session)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.set_mode("plan")
        await native.send_message("inspect only", [])

    messages = _written_requests(proc)
    turn = next(item for item in messages if item.get("method") == "turn/start")
    assert turn["params"]["collaborationMode"] == {
        "mode": "read",
        "settings": {
            "model": "gpt-5.4",
            "developer_instructions": None,
            "reasoning_effort": "medium",
        },
    }


@pytest.mark.asyncio
async def test_codex_no_model_selection_falls_back_to_thread_model() -> None:
    """Without CODEX_MODEL the collaboration payload keeps using the thread's
    model (regression guard for the no-selection path)."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"thread": {"id": "th-1"}, "model": "gpt-5.6-sol"},
                }
            ).encode()
            + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "name": "Default",
                                "mode": "default",
                                "model": None,
                                "reasoning_effort": None,
                            },
                        ]
                    },
                }
            ).encode()
            + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"turn": {"id": "tu-1"}}}).encode()
            + b"\n",
        ]
    )
    native = _codex_session()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.prepare_capabilities()
        await native.send_message("hello", [])

    messages = _written_requests(proc)
    turn = next(item for item in messages if item.get("method") == "turn/start")
    assert turn["params"]["collaborationMode"]["settings"]["model"] == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_codex_model_switch_via_update_env_takes_effect_next_turn() -> None:
    """update_env() mid-session must change the model on the NEXT turn/start,
    and clearing CODEX_MODEL must revert to the thread model.

    Guards the live per-turn read of ``session.env`` in
    ``_selected_model_override`` (the composer model-picker path) against a
    value cached at thread start. The picker writes the whole env via
    switchEnv -> set_env -> update_env; the next turn must reflect it without
    restarting the persistent app-server.
    """
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"thread": {"id": "th-1"}, "model": "gpt-5.6-sol"},
                }
            ).encode()
            + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "data": [
                            {
                                "name": "Default",
                                "mode": "default",
                                "model": None,
                                "reasoning_effort": None,
                            },
                        ]
                    },
                }
            ).encode()
            + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"turn": {"id": "tu-1"}}}).encode()
            + b"\n",
        ]
    )
    native = _codex_session()

    async def _complete_turn(turn_id: str) -> None:
        """Deliver turn/completed and release the guard so the next send runs."""
        proc.stdout.push(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "threadId": "th-1",
                        "turn": {"id": turn_id, "status": "completed"},
                    },
                }
            ).encode()
            + b"\n"
        )
        notif = await asyncio.wait_for(native.read_line(), timeout=1.0)
        assert notif is not None and notif.get("method") == "turn/completed"
        native.acknowledge_turn_complete()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.prepare_capabilities()

        # Turn 1: no selection -> thread model.
        await native.send_message("first", [])
        await _complete_turn("tu-1")

        # Switch model mid-session (the model-picker path).
        native.update_env({"CODEX_MODEL": "gpt-5.4"})
        proc.stdout.push(
            json.dumps(
                {"jsonrpc": "2.0", "id": 5, "result": {"turn": {"id": "tu-2"}}}
            ).encode()
            + b"\n"
        )
        await native.send_message("second", [])
        await _complete_turn("tu-2")

        # Clear the selection -> revert to the thread model.
        native.update_env({})
        proc.stdout.push(
            json.dumps(
                {"jsonrpc": "2.0", "id": 6, "result": {"turn": {"id": "tu-3"}}}
            ).encode()
            + b"\n"
        )
        await native.send_message("third", [])

    messages = _written_requests(proc)
    turns = [m for m in messages if m.get("method") == "turn/start"]
    assert len(turns) == 3
    assert turns[0]["params"]["collaborationMode"]["settings"]["model"] == "gpt-5.6-sol"
    assert turns[1]["params"]["collaborationMode"]["settings"]["model"] == "gpt-5.4"
    assert turns[2]["params"]["collaborationMode"]["settings"]["model"] == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_codex_notifications_go_to_separate_queue_not_response_futures() -> None:
    """Notifications (no id) must be placed on the notification queue and never
    re-queued ahead of a pending response. The response must resolve its
    matching Future even when notifications arrive first."""
    # Simulate: notification arrives before the response for request id 1.
    proc = _FakeProcess(
        stdout_lines=[
            # initialize response (id=1)
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            # thread/start response (id=2)
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
            # turn/start response (id=3)
            json.dumps(
                {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tu-1", "status": "running"}}}
            ).encode()
            + b"\n",
            # A notification that arrives before any further response.
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "th-1",
                        "turnId": "tu-1",
                        "itemId": "it-1",
                        "delta": "Hi",
                    },
                }
            ).encode()
            + b"\n",
        ]
    )
    sess = _codex_session()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await sess.start()
        await sess.send_message("hello", [])
    # The notification must be available on the notification queue.
    notif = await asyncio.wait_for(sess.read_line(), timeout=1.0)
    assert notif is not None
    assert notif["method"] == "item/agentMessage/delta"
    assert notif["params"]["delta"] == "Hi"
    # No pending requests should remain (all responses resolved their futures).
    assert sess._pending_requests == {}


@pytest.mark.asyncio
async def test_codex_response_dispatch_does_not_spin_on_notifications() -> None:
    """If a notification is enqueued, it must not be re-put back in front of a
    pending response. We verify this by checking that a notification arriving
    before the response does not block the response from being resolved."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
            # Notification arrives BEFORE the turn/start response.
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/started",
                    "params": {"threadId": "th-1", "turn": {"id": "tu-1", "status": "running"}},
                }
            ).encode()
            + b"\n",
            # Now the response for turn/start (id=3).
            json.dumps(
                {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tu-1", "status": "running"}}}
            ).encode()
            + b"\n",
        ]
    )
    sess = _codex_session()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await sess.start()
        # send_message awaits the turn/start response; if the notification were
        # re-queued ahead of it, this would time out.
        await asyncio.wait_for(sess.send_message("hello", []), timeout=2.0)
    # The turn/started notification should be on the queue, not lost.
    notif = await asyncio.wait_for(sess.read_line(), timeout=1.0)
    assert notif is not None
    assert notif["method"] == "turn/started"


# ── Turn-in-flight guard ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_second_send_while_turn_in_flight_raises_and_does_not_cancel_first() -> None:
    """A second ``send_message`` while a one-shot turn is in flight must raise
    ``RuntimeError`` and must NOT cancel the first turn's process/reader."""
    sess = _session()
    native = ClaudeNativeSession(sess)

    first_proc = _FakeProcess(stdout_lines=[])
    second_proc = _FakeProcess(stdout_lines=[])
    procs = [first_proc, second_proc]
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        proc = procs[calls["n"]]
        calls["n"] += 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        # First turn: spawns first_proc; stdout blocks so the turn stays in
        # flight (EOF not yet delivered).
        await native.send_message("first", [])
        assert native._turn_in_flight is True
        assert first_proc._terminated is False

        # Second send while the first turn is still running must raise and
        # must NOT have terminated the first process.
        with pytest.raises(RuntimeError):
            await native.send_message("second", [])
        assert first_proc._terminated is False
        assert native._turn_in_flight is True

        # Deliver EOF to the first process. ``_drain_stdout`` puts the EOF
        # sentinel on the queue but does NOT release the turn guard — the
        # consumer (tailer) must acknowledge consumption first.
        first_proc.stdout.push(b"")
        eof = await asyncio.wait_for(native.read_line(), timeout=1.0)
        assert eof is None
        native.acknowledge_turn_complete()
        assert native._turn_in_flight is False

        # A subsequent send must succeed now that the turn is complete.
        await native.send_message("third", [])
        assert native._turn_in_flight is True
        assert calls["n"] == 2


@pytest.mark.asyncio
async def test_cancel_active_turn_terminates_in_flight_oneshot() -> None:
    sess = _session()
    native = ClaudeNativeSession(sess)
    proc = _FakeProcess(stdout_lines=[])
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.send_message("first", [])
        assert native.turn_in_flight is True
        await native.cancel_active_turn()
        assert native.turn_in_flight is False
        assert proc._terminated is True


@pytest.mark.asyncio
async def test_stop_does_not_hang_when_stdout_reader_is_blocked() -> None:
    """``stop`` must complete quickly even when the stdout reader is blocked
    on ``readline()``. ``_terminate_process`` cancels the reader task; the
    reader's ``finally`` block must NOT await ``proc.wait()`` on cancellation
    (the process is still alive), or ``stop`` would deadlock until the process
    is killed — which only happens *after* the reader returns."""
    sess = _session()
    native = ClaudeNativeSession(sess)

    # A process whose stdout never produces output (readline blocks forever)
    # and whose ``wait()`` blocks until ``terminate()`` is called.
    terminated = asyncio.Event()

    class _BlockingProc:
        def __init__(self) -> None:
            self.stdout = _FakeStream([])
            self.stdin = _FakeStream()
            self.stderr = _FakeStream()

        async def wait(self) -> int:
            await terminated.wait()
            return 0

        def terminate(self) -> None:
            terminated.set()

        def kill(self) -> None:
            terminated.set()

    proc = _BlockingProc()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.send_message("hello", [])

    # stop() must return well before the 1s timeout; if the reader's finally
    # block awaited proc.wait(), this would hang until terminate() — but
    # terminate() is only called after the reader returns, so it would
    # deadlock. The 1s guard catches that regression.
    await asyncio.wait_for(native.stop(), timeout=1.0)
    assert native._turn_in_flight is False


@pytest.mark.asyncio
async def test_codex_second_send_before_turn_completed_raises() -> None:
    """For Codex, a second ``send_message`` before ``turn/completed`` must raise
    ``RuntimeError``; after ``turn/completed`` the next send succeeds."""
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
            json.dumps(
                {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tu-1", "status": "running"}}}
            ).encode()
            + b"\n",
        ]
    )
    sess = _session(AgentType.CODEX)
    native = CodexNativeSession(sess)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.send_message("first", [])
        assert native._turn_in_flight is True

        # Second send before turn/completed must raise.
        with pytest.raises(RuntimeError):
            await native.send_message("second", [])
        assert native._turn_in_flight is True

        # Deliver turn/completed. ``_drain_stdout`` puts the notification on
        # the queue but does NOT release the turn guard — the consumer (tailer)
        # must acknowledge consumption first.
        proc.stdout.push(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"threadId": "th-1", "turn": {"id": "tu-1", "status": "completed"}},
                }
            ).encode()
            + b"\n"
        )
        notif = await asyncio.wait_for(native.read_line(), timeout=1.0)
        assert notif is not None and notif.get("method") == "turn/completed"
        native.acknowledge_turn_complete()
        assert native._turn_in_flight is False

        # Next send after turn/completed must succeed.
        proc.stdout.push(
            json.dumps(
                {"jsonrpc": "2.0", "id": 4, "result": {"turn": {"id": "tu-2", "status": "running"}}}
            ).encode()
            + b"\n"
        )
        await native.send_message("third", [])
        assert native._turn_in_flight is True


# ── Atomic send_message guarantees ──────────────────────────────────────────

# A minimal valid PNG (1x1 transparent). Only the magic bytes matter for
# ``_detect_image_mime``; the rest is padding so the file is well-formed.
_VALID_PNG = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452"
    "0000000100000001"
    "08060000001f15c4"
    "890000000d494441"
    "54789c6300010000"
    "0500010d0a2db4"
    "0000000049454e44"
    "ae426082"
)


@pytest.mark.asyncio
async def test_invalid_image_resets_turn_guard() -> None:
    """If ``_stage_images`` raises (invalid image bytes), the turn guard must be
    released so a subsequent text-only turn can be sent. The base class
    ``send_message`` except block calls ``_end_turn`` regardless of how the
    subclass failed."""
    native = ClaudeNativeSession(_session())
    invalid = b"not an image"

    # Sending an invalid image must raise ValueError and reset the guard.
    with pytest.raises(ValueError):
        await native.send_message("hello", [invalid])
    assert native._turn_in_flight is False
    assert native._staged_images == []

    # A subsequent text-only turn must succeed (guard was released).
    proc = _FakeProcess(stdout_lines=[])
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.send_message("hello", [])
    assert native._turn_in_flight is True


@pytest.mark.asyncio
async def test_unsupported_image_resets_turn_guard() -> None:
    """Cursor does not support images. ``send_message`` with images must raise
    ``NotImplementedError`` and reset the turn guard so a text turn works."""
    native = CursorNativeSession(_session(AgentType.CURSOR))

    with pytest.raises(NotImplementedError):
        await native.send_message("hello", [_VALID_PNG])
    assert native._turn_in_flight is False

    # A text-only turn must succeed after the failed image send.
    proc = _FakeProcess(stdout_lines=[])
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.send_message("hello", [])
    assert native._turn_in_flight is True


@pytest.mark.asyncio
async def test_busy_turn_with_images_does_not_leak_staged_images() -> None:
    """When a turn is already in flight, ``send_message`` with images must
    raise ``RuntimeError`` (turn-in-flight) and must NOT leave any images
    staged. The guard check happens before ``_stage_images``."""
    native = ClaudeNativeSession(_session())
    proc = _FakeProcess(stdout_lines=[])

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.send_message("first", [])
    assert native._turn_in_flight is True

    # Second send while the first turn is in flight must raise and must not
    # stage any images.
    with pytest.raises(RuntimeError):
        await native.send_message("second", [_VALID_PNG])
    assert native._staged_images == []
    # The first turn is still in flight; the guard was not cleared.
    assert native._turn_in_flight is True


@pytest.mark.asyncio
async def test_concurrent_sends_do_not_mix_images() -> None:
    """Two ``send_message`` calls (A with images, B text-only) must not leak
    A's images into B's turn. The send lock serializes them; A's images are
    consumed into its envelope and ``_staged_images`` is cleared before B
    runs, so B's subprocess stdin contains no image content blocks."""
    native = ClaudeNativeSession(_session())
    proc_a = _FakeProcess(stdout_lines=[])
    proc_b = _FakeProcess(stdout_lines=[])
    procs = [proc_a, proc_b]
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        proc = procs[calls["n"]]
        calls["n"] += 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        # A: text + image. Images are consumed into the envelope and
        # ``_staged_images`` is cleared before the subprocess spawns.
        await native.send_message("first", [_VALID_PNG])
        assert native._staged_images == []

        # Complete A's turn so the guard is released for B. The consumer
        # (tailer) must acknowledge the EOF before the guard is released.
        proc_a.stdout.push(b"")
        eof = await asyncio.wait_for(native.read_line(), timeout=1.0)
        assert eof is None
        native.acknowledge_turn_complete()
        assert native._turn_in_flight is False

        # B: text-only. Must not see A's images.
        await native.send_message("second", [])
        assert native._staged_images == []

    # B's stdin must contain only a text block, no image content blocks.
    b_stdin = b"".join(proc_b.stdin.written).decode("utf-8")
    envelope = json.loads(b_stdin)
    content = envelope["message"]["content"]
    assert all(block["type"] == "text" for block in content)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_failed_image_send_does_not_leak_into_next_turn() -> None:
    """If A's ``send_message`` fails during staging (invalid image), B's
    subsequent text-only turn must not carry A's (rejected) images."""
    native = ClaudeNativeSession(_session())

    # A fails: invalid image raises ValueError; staged images are cleared.
    with pytest.raises(ValueError):
        await native.send_message("first", [b"not an image"])
    assert native._staged_images == []
    assert native._turn_in_flight is False

    # B succeeds with text only; no images are staged.
    proc = _FakeProcess(stdout_lines=[])
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.send_message("second", [])
    assert native._staged_images == []


@pytest.mark.asyncio
async def test_image_only_claude_envelope_contains_image_block() -> None:
    """An image-only turn (empty text, one image) must produce an SDKUserMessage
    envelope with an image content block followed by an (empty) text block."""
    native = ClaudeNativeSession(_session())
    proc = _FakeProcess(stdout_lines=[])

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.send_message("", [_VALID_PNG])

    stdin = b"".join(proc.stdin.written).decode("utf-8")
    envelope = json.loads(stdin)
    content = envelope["message"]["content"]
    assert len(content) == 2
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["type"] == "text"
    assert content[1]["text"] == ""
    # Staged images must be cleared after being consumed into the envelope.
    assert native._staged_images == []


@pytest.mark.asyncio
async def test_codex_turn_completed_before_response_cleans_up_images() -> None:
    """The app-server may emit turn/completed before the turn/start response
    arrives. The image temp files must be cleaned up by the turn/completed
    handler (which runs first), not leaked because _inflight_images was only
    set after the response."""
    proc = _FakeProcess(
        stdout_lines=[
            # initialize response (id=1)
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            # thread/start response (id=2)
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
            # turn/completed notification arrives BEFORE the turn/start response.
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"threadId": "th-1", "turn": {"id": "tu-1", "status": "completed"}},
                }
            ).encode()
            + b"\n",
            # turn/start response (id=3) — resolves the pending request future.
            json.dumps(
                {"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "tu-1", "status": "running"}}}
            ).encode()
            + b"\n",
        ]
    )
    native = _codex_session()

    # Capture the temp file paths that _stage_images creates so we can assert
    # they are deleted after the turn completes.
    captured_paths: List[Path] = []
    original_stage = native._stage_images

    def tracking_stage(images: List[bytes]) -> None:
        original_stage(images)
        captured_paths.extend(native._staged_images)

    native._stage_images = tracking_stage  # type: ignore[assignment]

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await native.start()
        await native.send_message("hello", [_VALID_PNG])

    # The turn/completed notification was queued by _drain_stdout. Consume it
    # and acknowledge completion to release the turn guard (mirrors what the
    # tailer does after processing the turn-end record).
    notif = await asyncio.wait_for(native.read_line(), timeout=1.0)
    assert notif is not None and notif.get("method") == "turn/completed"
    native.acknowledge_turn_complete()

    # The turn/completed handler must have cleaned up the image temp files.
    assert native._inflight_images == []
    assert native._staged_images == []
    for p in captured_paths:
        assert not p.exists(), f"image temp file leaked: {p}"
    # The turn guard is released only after the consumer acknowledges the
    # turn/completed record.
    assert not native._turn_in_flight


@pytest.mark.asyncio
async def test_codex_eof_before_turn_completed_cleans_inflight_images(
    tmp_path: Path,
) -> None:
    """If the persistent app-server dies mid-turn, no ``turn/completed``
    notification will arrive, so the stdout-reader ``finally`` block owns
    cleanup of the localImage temp files.
    """
    proc = _FakeProcess(
        stdout_lines=[
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode() + b"\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}}).encode()
            + b"\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {"turn": {"id": "tu-1", "status": "running"}},
                }
            ).encode()
            + b"\n",
        ]
    )
    native = _codex_session()
    captured_paths: List[Path] = []
    original_stage = native._stage_images

    def tracking_stage(images: List[bytes]) -> None:
        original_stage(images)
        captured_paths.extend(native._staged_images)

    native._stage_images = tracking_stage  # type: ignore[assignment]

    with (
        patch(
            "claude_hub.services.agent_stream.native._runtime_home",
            return_value=tmp_path,
        ),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        await native.start()
        await native.send_message("hello", [_VALID_PNG])
        assert captured_paths and all(path.exists() for path in captured_paths)

        proc.stdout.push(b"")
        assert await asyncio.wait_for(native.read_line(), timeout=1.0) is None

    assert native._inflight_images == []
    assert native._staged_images == []
    assert all(not path.exists() for path in captured_paths)


def test_codex_image_staging_uses_private_runtime_owned_files(tmp_path: Path) -> None:
    native = _codex_session()
    with patch(
        "claude_hub.services.agent_stream.native._runtime_home",
        return_value=tmp_path,
    ):
        native._stage_images([_VALID_PNG])
        staged = list(native._staged_images)
        temp_dir = tmp_path / "tmp" / "codex-images"

        assert staged and staged[0].parent == temp_dir
        assert temp_dir.stat().st_mode & 0o777 == 0o700
        assert staged[0].stat().st_mode & 0o777 == 0o600

        native._clear_staged_images()

    assert all(not path.exists() for path in staged)


def test_codex_startup_cleanup_removes_prior_process_temp_files(tmp_path: Path) -> None:
    from claude_hub.services.agent_stream.native import cleanup_codex_temp_dir

    temp_dir = tmp_path / "tmp" / "codex-images"
    temp_dir.mkdir(parents=True)
    orphan = temp_dir / "codex-img-orphan.png"
    orphan.write_bytes(_VALID_PNG)

    with patch(
        "claude_hub.services.agent_stream.native._runtime_home",
        return_value=tmp_path,
    ):
        assert cleanup_codex_temp_dir() == 1

    assert not orphan.exists()
