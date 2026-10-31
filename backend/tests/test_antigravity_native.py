"""Tests for the Antigravity (agy) native transport and stream-json adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from claude_hub.models import (
    AgentType,
    ChatMode,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.antigravity_events import AntigravityEventAdapter
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.native import AntigravityNativeSession
from claude_hub.services.agent_stream.registry import get_adapter


def _session(**overrides: Any) -> ManagedSession:
    base: dict[str, Any] = dict(
        id="sess-1",
        workspace_id="ws-1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.ANTIGRAVITY,
        status=ManagedSessionStatus.IDLE,
        title="test",
        workspace_path="/tmp",
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return ManagedSession(**base)


def _ctx(turn_id: str = "turn1") -> NormalizeContext:
    return NormalizeContext(
        session_id="sess-1",
        tab_id="tab-1",
        agent_type=AgentType.ANTIGRAVITY,
        run_epoch=1,
        turn_id=turn_id,
    )


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_adapter() -> None:
    assert get_adapter(AgentType.ANTIGRAVITY) is AntigravityEventAdapter


# ── command construction ─────────────────────────────────────────────────────


def test_command_fresh_minimal() -> None:
    s = AntigravityNativeSession(_session())
    assert s._build_command({}) == [
        "agyx",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
    ]
    assert s._resume_arg() == []


def test_command_model_and_verified_resume() -> None:
    s = AntigravityNativeSession(_session())
    s._persist_conversation_id("conv-42")
    cmd = s._build_command({"ANTIGRAVITY_MODEL": "gemini-3.8-flash-low"})
    assert cmd == [
        "agyx",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
        "--model",
        "gemini-3.8-flash-low",
        "--conversation",
        "conv-42",
    ]
    # Blank model env must not add an empty --model value.
    assert "--model" not in s._build_command({"ANTIGRAVITY_MODEL": "  "})


def test_command_solo_and_plan() -> None:
    solo = AntigravityNativeSession(_session(solo_mode=True))
    assert "--dangerously-skip-permissions" in solo._build_command({})

    plan = AntigravityNativeSession(_session(chat_mode=ChatMode.PLAN))
    plan._current_mode = ChatMode.PLAN.value
    plan_cmd = plan._build_command({})
    assert plan_cmd[-2:] == ["--mode", "plan"]
    # Plan mode does not combine with the permission bypass.
    assert "--dangerously-skip-permissions" not in plan_cmd
    # agy makes --mode plan a no-op while slash expansion is disabled; the two
    # flags must never be combined or the UI shows an empty "successful" turn.
    assert "--disable-slash-commands" not in plan_cmd
    # Default (incl. solo) keeps slash expansion disabled headless.
    assert "--disable-slash-commands" in AntigravityNativeSession(_session())._build_command({})


def test_available_modes_probes_agyx_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    import claude_hub.services.agent_stream.native as native_module

    seen: list[tuple[str, str]] = []

    def _probe(binary: str, flag: str) -> bool:
        seen.append((binary, flag))
        return True

    monkeypatch.setattr(native_module, "_help_confirms_plan_mode", _probe)
    s = AntigravityNativeSession(_session())
    modes = s.available_modes()
    assert seen == [("agyx", "--mode")]
    assert [m.id for m in modes] == ["default", "plan"]

    seen.clear()
    monkeypatch.setattr(native_module, "_help_confirms_plan_mode", lambda binary, flag: False)
    assert [m.id for m in s.available_modes()] == ["default"]


def test_stdin_envelope() -> None:
    import json

    s = AntigravityNativeSession(_session())
    assert json.loads(s._build_stdin("hello")) == {
        "event": "user",
        "message": {"content": "hello"},
    }


def test_init_captures_conversation_id() -> None:
    persisted: list[str] = []
    s = AntigravityNativeSession(_session(), conversation_id_persist=persisted.append)
    s.maybe_capture_conversation_id({"event": "init", "conversation_id": "cvt-9"})
    assert s._conversation_id == "cvt-9"
    assert s._conversation_id_verified is True
    assert s._handshake_complete is True
    assert persisted == ["cvt-9"]
    assert s._resume_arg() == ["--conversation", "cvt-9"]
    # Unrelated records are ignored.
    s.maybe_capture_conversation_id({"event": "step_update", "step_update": {}})
    assert s._conversation_id == "cvt-9"


# ── adapter ──────────────────────────────────────────────────────────────────


def _tool_step(index: int, state: str, output: str | None = None) -> dict:
    info = {"name": "run_command", "parameters": {"CommandLine": "ls -la"}}
    if output is not None:
        info["output"] = output
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": "c1",
            "step_index": index,
            "state": state,
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": info,
        },
    }


def test_adapter_tool_timeline_and_text() -> None:
    ad = AntigravityEventAdapter()
    ctx = _ctx()
    records = [
        {"event": "init", "conversation_id": "c1"},
        _tool_step(0, "ACTIVE"),
        _tool_step(0, "DONE", output="Command exited with code 0.\n\nx"),
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": "HELLO\n",
            },
        },
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "HELLO\n",
                "num_turns": 1,
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
        },
    ]
    events = [e for r in records for e in ad.normalize_line(r, ctx)]
    types = [e.type.value for e in events]
    assert types == [
        "tool_call_started",
        "tool_call_completed",
        "text_delta",
        "turn_completed",
    ]
    completed = events[-1].payload
    assert completed["status"] == "completed"
    assert completed["usage"]["input"] == 10
    assert completed["usage"]["output"] == 2
    # Same call id correlates the ACTIVE/DONE tool pair.
    assert events[0].call_id == events[1].call_id
    assert events[1].payload["status"] == "completed"


def test_adapter_failed_tool_status() -> None:
    ad = AntigravityEventAdapter()
    events = ad.normalize_line(
        _tool_step(3, "DONE", output="Command failed to run: sandbox reset\n"), _ctx()
    )
    assert len(events) == 1
    assert events[0].payload["status"] == "failed"


def test_adapter_result_error() -> None:
    ad = AntigravityEventAdapter()
    out = ad.normalize_line(
        {"event": "result", "result": {"status": "ERROR", "error": "boom"}},
        _ctx("turn2"),
    )
    assert [e.type.value for e in out] == ["error", "turn_completed"]
    assert out[0].payload["message"] == "boom"
    assert out[1].payload["status"] == "failed"


def test_adapter_headless_denied_action_is_actionable_failure() -> None:
    # agy headless auto-denies a tool: SUCCESS + denied_actions + empty response.
    ad = AntigravityEventAdapter()
    out = ad.normalize_line(
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "",
                "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            },
        },
        _ctx("deny"),
    )
    assert [e.type.value for e in out] == ["error", "turn_completed"]
    assert "RunCommand" in out[0].payload["message"]
    assert "Solo Mode" in out[0].payload["message"]
    assert out[1].payload["status"] == "failed"
    # A denied action alongside a real answer does not fail the turn.
    ok = ad.normalize_line(
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "partial answer",
                "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            },
        },
        _ctx("deny2"),
    )
    assert ok[-1].payload["status"] == "completed"


def test_adapter_unknown_status_is_failure() -> None:
    ad = AntigravityEventAdapter()
    out = ad.normalize_line({"event": "result", "result": {}}, _ctx("weird"))
    assert [e.type.value for e in out] == ["error", "turn_completed"]
    assert out[1].payload["status"] == "failed"


def test_adapter_usage_maps_agy_fields() -> None:
    ad = AntigravityEventAdapter()
    out = ad.normalize_line(
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "x",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "thinking_tokens": 7,
                    "cache_read_tokens": 20,
                    "total_tokens": 85,
                },
            },
        },
        _ctx("usage"),
    )
    completed = next(e for e in out if e.type.value == "turn_completed").payload["usage"]
    assert completed["reasoning"] == 7
    assert completed["cached"] == 20


def test_adapter_result_response_without_step_delta_emits_once() -> None:
    ad = AntigravityEventAdapter()
    ctx = _ctx("turn3")
    out = ad.normalize_line(
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "ONLY HERE",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        ctx,
    )
    assert [e.type.value for e in out] == ["text_delta", "turn_completed"]
    assert out[0].payload["text"] == "ONLY HERE"


def test_adapter_streaming_active_done_fragments() -> None:
    # Some models stream the answer as an ACTIVE prefix fragment plus a DONE
    # suffix fragment; the terminal result.response must not duplicate it.
    ad = AntigravityEventAdapter()
    ctx = _ctx("stream")
    records = [
        {
            "event": "step_update",
            "step_update": {
                "step_index": 0,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": "RAW",
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "step_index": 0,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": "PROBE\n",
            },
        },
        {"event": "result", "result": {"status": "SUCCESS", "response": "RAWPROBE\n"}},
    ]
    events = [e for r in records for e in ad.normalize_line(r, ctx)]
    text = [e.payload["text"] for e in events if e.type.value == "text_delta"]
    assert text == ["RAW", "PROBE\n"]
    assert [e.type.value for e in events].count("text_delta") == 2
    assert events[-1].payload["status"] == "completed"


def test_adapter_ignores_unknown_records() -> None:
    ad = AntigravityEventAdapter()
    assert ad.normalize_line({"event": "something_else"}, _ctx()) == []
    assert ad.normalize_line({}, _ctx()) == []
    assert ad.discover_source(_session()) is None


def test_remote_tab_rejected() -> None:
    import pytest
    from claude_hub.models import SessionKind, TerminalTabCreate

    with pytest.raises(ValueError, match="Hub host"):
        TerminalTabCreate(
            name="r",
            agent_type=AgentType.ANTIGRAVITY,
            session_kind=SessionKind.TERMINAL,
            target=ExecutionTarget.REMOTE,
        )
