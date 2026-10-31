"""Tests for the Antigravity (agy) native transport and stream-json adapter."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ChatMode,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services import dot as dot_module
from claude_hub.services.agent_stream.antigravity_events import AntigravityEventAdapter
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.native import AntigravityNativeSession
from claude_hub.services.agent_stream.providers import antigravity as antigravity_module
from claude_hub.services.agent_stream.registry import get_adapter
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import SessionTailer
from tests.test_agent_stream_native import _FakeProcess, _FakeStream


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
                "num_turns": 1,
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
    assert completed == {
        "input": 120,
        "cached": 20,
        "output": 5,
        "reasoning": 7,
        "total": 105,
        "source": "antigravity",
    }


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


def _usage_result(
    usage: dict[str, Any], num_turns: int | None, *, conversation_id: str = "conversation-1"
) -> dict[str, Any]:
    return {
        "event": "result",
        "result": {
            "conversation_id": conversation_id,
            "status": "SUCCESS",
            "response": "answer",
            "num_turns": num_turns,
            "usage": usage,
        },
    }


def _step_usage(
    index: int, usage: dict[str, Any], *, state: str = "DONE", step_type: str = "agent_response"
) -> dict[str, Any]:
    return {
        "event": "step_update",
        "step_update": {
            "step_index": index,
            "state": state,
            "step_type": step_type,
            "usage": usage,
        },
    }


def test_adapter_usage_uses_per_step_totals_across_persistent_turns() -> None:
    adapter = AntigravityEventAdapter()
    first = _ctx("first")
    adapter.normalize_line(_step_usage(1, {"input_tokens": 8878, "output_tokens": 38}), first)
    adapter.normalize_line(_usage_result({"input_tokens": 8878, "output_tokens": 38}, 1), first)
    second = _ctx("second")
    current = {
        "input_tokens": 2184,
        "cache_read_tokens": 6824,
        "output_tokens": 163,
        "thinking_tokens": 108,
        "total_tokens": 2347,
    }
    adapter.normalize_line(_step_usage(3, current), second)
    out = adapter.normalize_line(
        _usage_result({"input_tokens": 11062, "cache_read_tokens": 6824, "output_tokens": 201}, 2),
        second,
    )
    assert out[-1].payload["usage"] == {
        "input": 9008,
        "cached": 6824,
        "output": 163,
        "reasoning": 108,
        "total": 2347,
        "source": "antigravity",
    }


def test_adapter_usage_deduplicates_step_snapshots_and_sums_checkpoints() -> None:
    adapter = AntigravityEventAdapter()
    ctx = _ctx("snapshots")
    adapter.normalize_line(
        _step_usage(4, {"input_tokens": 500, "output_tokens": 1}, state="ACTIVE"), ctx
    )
    final = _step_usage(4, {"input_tokens": 10, "output_tokens": 3, "cache_read_tokens": 20})
    adapter.normalize_line(final, ctx)
    adapter.normalize_line(final, ctx)
    adapter.normalize_line(
        _step_usage(5, {"input_tokens": 2, "output_tokens": 1}, step_type="checkpoint"), ctx
    )
    out = adapter.normalize_line(
        _usage_result({"input_tokens": 9999, "output_tokens": 999}, 9), ctx
    )
    assert out[-1].payload["usage"] == {
        "input": 32,
        "cached": 20,
        "output": 4,
        "reasoning": 0,
        "total": 16,
        "source": "antigravity",
    }


def test_adapter_usage_result_only_fallback_requires_contiguous_counter() -> None:
    adapter = AntigravityEventAdapter()
    first = adapter.normalize_line(
        _usage_result({"input_tokens": 100, "output_tokens": 5}, 1), _ctx("first")
    )
    assert first[-1].payload["usage"]["total"] == 105
    second = adapter.normalize_line(
        _usage_result({"input_tokens": 110, "output_tokens": 8, "cache_read_tokens": 90}, 2),
        _ctx("second"),
    )
    assert second[-1].payload["usage"] == {
        "input": 100,
        "cached": 90,
        "output": 3,
        "reasoning": 0,
        "total": 13,
        "source": "antigravity",
    }
    skipped = adapter.normalize_line(
        _usage_result({"input_tokens": 200, "output_tokens": 20, "cache_read_tokens": 100}, 4),
        _ctx("fourth"),
    )
    assert "usage" not in skipped[-1].payload


@pytest.mark.parametrize("num_turns", [None, 0, True, 5])
def test_adapter_usage_never_charges_unknown_result_baseline(num_turns: Any) -> None:
    adapter = AntigravityEventAdapter()
    out = adapter.normalize_line(
        _usage_result({"input_tokens": 1000, "output_tokens": 500}, num_turns), _ctx()
    )
    assert "usage" not in out[-1].payload


def test_adapter_usage_resume_init_discards_cumulative_baseline() -> None:
    adapter = AntigravityEventAdapter()
    adapter.normalize_line(_usage_result({"input_tokens": 10}, 1), _ctx("before"))
    adapter.normalize_line({"event": "init", "conversation_id": "conversation-1"}, _ctx("resumed"))
    resumed = adapter.normalize_line(_usage_result({"input_tokens": 20}, 2), _ctx("resumed"))
    assert "usage" not in resumed[-1].payload
    following = adapter.normalize_line(_usage_result({"input_tokens": 25}, 3), _ctx("following"))
    assert following[-1].payload["usage"]["input"] == 5


def test_adapter_usage_does_not_diff_across_conversations_or_counter_resets() -> None:
    adapter = AntigravityEventAdapter()
    adapter.normalize_line(_usage_result({"input_tokens": 100}, 1), _ctx("first"))
    other = adapter.normalize_line(
        _usage_result({"input_tokens": 200}, 2, conversation_id="other"), _ctx("other")
    )
    assert "usage" not in other[-1].payload
    reset = adapter.normalize_line(
        _usage_result({"input_tokens": 10}, 3, conversation_id="other"), _ctx("reset")
    )
    assert "usage" not in reset[-1].payload


def test_adapter_usage_cancelled_turn_steps_do_not_leak() -> None:
    adapter = AntigravityEventAdapter()
    adapter.normalize_line(_step_usage(0, {"input_tokens": 1000}), _ctx("cancelled"))
    adapter.normalize_line(_step_usage(0, {"input_tokens": 7}), _ctx("next"))
    out = adapter.normalize_line(_usage_result({}, 3), _ctx("next"))
    assert out[-1].payload["usage"]["input"] == 7


@pytest.mark.parametrize("invalid", [-1, True, "10", 1.5, None])
def test_adapter_usage_rejects_malformed_counter_without_poisoning_step(invalid: Any) -> None:
    adapter = AntigravityEventAdapter()
    ctx = _ctx()
    adapter.normalize_line(_step_usage(0, {"input_tokens": 7}), ctx)
    adapter.normalize_line(_step_usage(0, {"input_tokens": invalid}), ctx)
    out = adapter.normalize_line(_usage_result({"input_tokens": invalid}, 1), ctx)
    assert out[-1].payload["usage"]["input"] == 7


def test_adapter_usage_zero_step_does_not_fall_back_to_history() -> None:
    adapter = AntigravityEventAdapter()
    ctx = _ctx()
    adapter.normalize_line(_step_usage(0, {"input_tokens": 0}), ctx)
    out = adapter.normalize_line(_usage_result({"input_tokens": 1000}, 5), ctx)
    assert "usage" not in out[-1].payload


def test_adapter_usage_sessions_and_tool_call_ids_remain_independent() -> None:
    adapter = AntigravityEventAdapter()
    first = _ctx("first")
    second = _ctx("second")
    other = NormalizeContext(
        session_id="other-session",
        tab_id="other-tab",
        agent_type=AgentType.ANTIGRAVITY,
        run_epoch=1,
        turn_id="first",
    )
    adapter.normalize_line(_step_usage(0, {"input_tokens": 10}), first)
    adapter.normalize_line(_step_usage(0, {"input_tokens": 20}), other)
    assert adapter.normalize_line(_usage_result({}, 1), first)[-1].payload["usage"]["input"] == 10
    assert adapter.normalize_line(_usage_result({}, 1), other)[-1].payload["usage"]["input"] == 20
    call1 = adapter.normalize_line(_tool_step(0, "ACTIVE"), first)[0]
    call2 = adapter.normalize_line(_tool_step(0, "ACTIVE"), second)[0]
    assert call1.call_id != call2.call_id


class _PersistentInput(_FakeStream):
    def is_closing(self) -> bool:
        return self._closed


class _PersistentProcess(_FakeProcess):
    """Add process liveness to the existing shared stream test double."""

    def __init__(self) -> None:
        super().__init__([])
        self.stdin = _PersistentInput()
        self.pid = 0  # The test double must never address a real process group.
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        super().terminate()
        self.returncode = -15

    def kill(self) -> None:
        super().kill()
        self.returncode = -9


@pytest.fixture
async def persistent_native(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AntigravityNativeSession, AsyncMock, list[_PersistentProcess]]]:
    processes: list[_PersistentProcess] = []

    async def spawn(*_args: Any, **_kwargs: Any) -> _PersistentProcess:
        process = _PersistentProcess()
        processes.append(process)
        await asyncio.sleep(0)
        return process

    spawn_mock = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
    native_module = importlib.import_module("claude_hub.services.agent_stream.native")
    monkeypatch.setattr(native_module, "_help_confirms_plan_mode", lambda _binary, _flag: True)
    native = AntigravityNativeSession(_session())
    monkeypatch.setattr(native, "_prepare_turn_context", AsyncMock(return_value=""))
    try:
        yield native, spawn_mock, processes
    finally:
        await native.stop()


def _emit(process: _PersistentProcess, record: dict[str, Any]) -> None:
    process.stdout.push(json.dumps(record).encode("utf-8") + b"\n")


def _make_tailer(
    native: AntigravityNativeSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> SessionTailer:
    workspace_module = importlib.import_module("claude_hub.services.workspace_manager")
    native_module = importlib.import_module("claude_hub.services.agent_stream.native")
    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(native_module.shutil, "which", lambda _binary: "/fake/agyx")
    return SessionTailer(
        workspace_id=native.session.workspace_id,
        session_id=native.session.id,
        adapter=AntigravityEventAdapter(),
        session_getter=lambda: native.session,
        store=AgentStreamStore(native.session.workspace_id, native.session.id),
        native_transport=native,
    )


async def _through_completion(queue: asyncio.Queue[AgentStreamEvent]) -> list[AgentStreamEvent]:
    events: list[AgentStreamEvent] = []
    async with asyncio.timeout(2):
        while True:
            event = await queue.get()
            events.append(event)
            if event.type == AgentStreamEventType.TURN_COMPLETED:
                return events


async def _wait_until(predicate, *, attempts: int = 200, delay: float = 0.01) -> None:
    for _ in range(attempts):
        if predicate():
            return
        # The shared fake stdout reader sleeps briefly per record; yield real
        # time so a stdout-driven init can reach the consumer.
        await asyncio.sleep(delay)
    raise AssertionError("condition was not reached")


def _start_native_consumer(native: AntigravityNativeSession) -> asyncio.Task[None]:
    """Consume stdout records like NativeConsumerMixin does for a live tab."""

    async def consume() -> None:
        while True:
            record = await native.read_line()
            if record is not None:
                native.maybe_capture_conversation_id(record)

    return asyncio.create_task(consume())


async def _stop_native_consumer(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_dot_rotation_reuses_a_fresh_process_before_its_first_turn(
    persistent_native,
) -> None:
    native, spawn, processes = persistent_native
    consumer = _start_native_consumer(native)
    try:
        await native.start()
        _emit(processes[0], {"event": "init", "conversation_id": "fresh-1"})
        await _wait_until(lambda: native._conversation_id_verified)

        assert await native.rotate_conversation() == "fresh-1"
        assert spawn.await_count == 1
        assert native._process is processes[0]
        native.adopt_rotated_conversation("fresh-1")
        assert native._conversation_id == "fresh-1"
        assert native._conversation_id_verified is True
        assert native._resume_arg() == ["--conversation", "fresh-1"]
    finally:
        await _stop_native_consumer(consumer)


async def test_dot_rotation_waits_for_init_on_a_fresh_process(persistent_native) -> None:
    native, spawn, processes = persistent_native
    persisted: list[str] = []
    native._conversation_id_persist = persisted.append
    consumer = _start_native_consumer(native)
    try:
        await native.start()
        rotation = asyncio.create_task(native.rotate_conversation())
        await _wait_until(lambda: native._rotation_init is not None)
        _emit(processes[0], {"event": "init", "conversation_id": "fresh-2"})

        assert await asyncio.wait_for(rotation, 1) == "fresh-2"
        assert spawn.await_count == 1
        # The candidate is not resumable until Dot adopts the association.
        assert persisted == []
        native.adopt_rotated_conversation("fresh-2")
        assert persisted == ["fresh-2"]
    finally:
        await _stop_native_consumer(consumer)


async def test_dot_rotation_fails_fast_when_the_provider_exits_before_init(
    persistent_native,
) -> None:
    native, _spawn, processes = persistent_native
    consumer = _start_native_consumer(native)
    try:
        await native.start()
        rotation = asyncio.create_task(native.rotate_conversation())
        await _wait_until(lambda: native._rotation_init is not None)
        processes[0].returncode = 1

        with pytest.raises(RuntimeError, match="exited before reporting"):
            await asyncio.wait_for(rotation, 1)
        assert native._started is False
        assert processes[0]._terminated
        assert native._client_requested_stop is True
    finally:
        await _stop_native_consumer(consumer)


async def test_dot_rotation_replaces_a_used_process_and_keeps_the_new_id(
    persistent_native,
) -> None:
    native, spawn, processes = persistent_native
    consumer = _start_native_consumer(native)
    try:
        await native.start()
        _emit(processes[0], {"event": "init", "conversation_id": "old-conv"})
        await _wait_until(lambda: native._conversation_id_verified)
        native._turns_on_process = 1
        persisted: list[str] = []
        native._conversation_id_persist = persisted.append
        old = processes[0]
        rotation = asyncio.create_task(native.rotate_conversation())
        await _wait_until(lambda: len(processes) == 2 and native._rotation_init is not None)
        # The replacement must not resume the conversation it is replacing.
        assert "--conversation" not in spawn.call_args_list[1][0]

        _emit(processes[1], {"event": "init", "conversation_id": "new-conv"})
        assert await asyncio.wait_for(rotation, 1) == "new-conv"
        assert old._terminated or old.stdin.is_closing()
        assert spawn.await_count == 2
        assert native._conversation_id == "new-conv"
        assert persisted == []
        native.adopt_rotated_conversation("new-conv")
        assert native._conversation_id_verified is True
        assert native._resume_arg() == ["--conversation", "new-conv"]
        assert persisted == ["new-conv"]
    finally:
        await _stop_native_consumer(consumer)


async def test_dot_rotation_discard_retires_the_deferred_candidate(
    persistent_native,
) -> None:
    native, _spawn, processes = persistent_native
    consumer = _start_native_consumer(native)
    try:
        await native.start()
        rotation = asyncio.create_task(native.rotate_conversation())
        await _wait_until(lambda: native._rotation_init is not None)
        _emit(processes[0], {"event": "init", "conversation_id": "rejected"})
        assert await asyncio.wait_for(rotation, 1) == "rejected"

        await native.discard_rotated_conversation("rejected")
        assert native._conversation_id is None
        assert native._conversation_id_verified is False
        assert processes[0]._terminated
    finally:
        await _stop_native_consumer(consumer)


async def test_dot_rotation_discard_restores_the_previous_identity(
    persistent_native,
) -> None:
    native, _spawn, processes = persistent_native
    consumer = _start_native_consumer(native)
    try:
        await native.start()
        _emit(processes[0], {"event": "init", "conversation_id": "old-conv"})
        await _wait_until(lambda: native._conversation_id_verified)
        native._turns_on_process = 1
        rotation = asyncio.create_task(native.rotate_conversation())
        await _wait_until(lambda: len(processes) == 2 and native._rotation_init is not None)
        _emit(processes[1], {"event": "init", "conversation_id": "candidate"})
        assert await asyncio.wait_for(rotation, 1) == "candidate"

        await native.discard_rotated_conversation("candidate")
        assert native._conversation_id == "old-conv"
        assert native._conversation_id_verified is True
        assert native._resume_arg() == ["--conversation", "old-conv"]
        assert processes[1]._terminated
    finally:
        await _stop_native_consumer(consumer)


async def test_dot_rotation_timeout_releases_the_unused_process(
    persistent_native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, _spawn, processes = persistent_native
    monkeypatch.setattr(antigravity_module, "_ROTATION_INIT_TIMEOUT_SECONDS", 0.01)
    await native.start()

    with pytest.raises(RuntimeError, match="did not report a conversation id"):
        await native.rotate_conversation()

    assert native._conversation_id is None
    assert native._conversation_id_verified is False
    assert native._started is False
    assert processes[0]._terminated
    assert native._client_requested_stop is True


async def test_dot_preparation_rotates_antigravity_before_the_first_turn(
    persistent_native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, spawn, processes = persistent_native
    # Drop the fixture's no-op Dot preparation to exercise the real one.
    monkeypatch.delattr(native, "_prepare_turn_context")
    persisted: list[str] = []
    native._conversation_id_persist = persisted.append
    plan = SimpleNamespace(
        prepared_generation="gen-1",
        rotate=True,
        new_session=True,
        system_pack="PACK",
        turn_pack="TURN",
        delivered_event_ids=[],
    )
    associated: list[str] = []

    def associate(cid: str, **_kwargs: Any) -> bool:
        associated.append(cid)
        return True

    service = SimpleNamespace(
        is_dot_tab=lambda _tab_id: True,
        prepare_turn=lambda _text, _conversation_id: plan,
        associate_conversation=associate,
        clear_conversation_association=lambda _cid, **_kwargs: True,
        request_rotation=lambda **_kwargs: True,
    )
    monkeypatch.setattr(dot_module, "get_dot_service", lambda: service)
    consumer = _start_native_consumer(native)
    try:
        prepared = asyncio.create_task(native._prepare_turn_context("hello"))
        await _wait_until(lambda: len(processes) == 1 and native._rotation_init is not None)
        _emit(processes[0], {"event": "init", "conversation_id": "dot-conv"})

        assert await asyncio.wait_for(prepared, 1) == "PACK\n\nTURN"
        assert associated == ["dot-conv"]
        assert spawn.await_count == 1
        assert native._conversation_id == "dot-conv"
        assert persisted == ["dot-conv"]
    finally:
        await _stop_native_consumer(consumer)


async def test_persistent_concurrent_start_and_send_share_one_process(persistent_native) -> None:
    native, spawn, processes = persistent_native
    await asyncio.gather(native.start(), native.start(), native.send_message("hello", []))
    assert spawn.await_count == 1
    assert native._process is processes[0]
    assert native._reader_task is not None and not native._reader_task.done()
    assert len(processes[0].stdin.written) == 1


async def test_persistent_two_turns_preserve_reader_framing_and_guidance(persistent_native) -> None:
    native, spawn, processes = persistent_native
    await native.send_message("first\n行", [])
    process = processes[0]
    generation = native._stdout_generation
    reader = native._reader_task
    _emit(process, {"event": "init", "conversation_id": "resumable"})
    init = await asyncio.wait_for(native.read_line(), 2)
    native.maybe_capture_conversation_id(init)
    _emit(process, _usage_result({"input_tokens": 10}, 1))
    assert (await asyncio.wait_for(native.read_line(), 2))["event"] == "result"
    native.acknowledge_turn_complete()
    assert native._stdout_generation == generation
    await native.send_message("second", [])
    assert spawn.await_count == 1
    assert native._reader_task is reader
    assert not process.stdin.is_closing()
    assert len(process.stdin.written) == 2
    assert all(chunk.endswith(b"\n") and chunk.count(b"\n") == 1 for chunk in process.stdin.written)
    first, second = [json.loads(chunk) for chunk in process.stdin.written]
    assert "first\n行" in first["message"]["content"]
    assert "<claude_hub_instructions>" in first["message"]["content"]
    assert second["message"]["content"] == "second"
    with pytest.raises(RuntimeError, match="already in flight"):
        await native.send_message("overlap", [])
    assert len(process.stdin.written) == 2
    assert not process._terminated


async def test_persistent_ready_before_init_and_stop_releases_process(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, spawn, processes = persistent_native
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    assert not tailer.native_ready
    await native.start()
    assert tailer.native_ready
    assert native._conversation_id is None
    await native.stop()
    assert processes[0]._terminated or processes[0].stdin.is_closing()
    assert not tailer.native_ready
    await native.start()
    assert spawn.await_count == 2
    assert tailer.native_ready


async def test_persistent_failed_spawn_is_not_ready_and_can_retry(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, spawn, _processes = persistent_native
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    normal_spawn = spawn.side_effect
    spawn.side_effect = FileNotFoundError("agyx")
    with pytest.raises(FileNotFoundError):
        await native.start()
    assert not tailer.native_ready
    assert native._process is None
    spawn.side_effect = normal_spawn
    await native.start()
    assert tailer.native_ready


@pytest.mark.parametrize("changed", ["model", "environment", "solo", "mode"])
async def test_persistent_launch_changes_restart_only_before_next_turn(
    persistent_native, changed: str
) -> None:
    native, spawn, processes = persistent_native
    native.update_env({"ANTIGRAVITY_MODEL": "gemini-4-argon-high"})
    await native.send_message("first", [])
    native.maybe_capture_conversation_id({"event": "init", "conversation_id": "same-history"})
    native.acknowledge_turn_complete()
    if changed == "model":
        native.update_env({"ANTIGRAVITY_MODEL": "gemini-4-argon-low"})
    elif changed == "environment":
        native.update_env({**native.session.env, "SSL_CERT_FILE": "/new/test-ca.pem"})
    elif changed == "solo":
        native.session.solo_mode = True
    else:
        await native.set_mode("plan")
    assert spawn.await_count == 1
    await native.send_message("second", [])
    assert spawn.await_count == 2
    assert processes[0]._terminated or processes[0].stdin.is_closing()
    args = list(spawn.await_args.args)
    assert args[args.index("--conversation") + 1] == "same-history"
    if changed == "model":
        assert args[args.index("--model") + 1] == "gemini-4-argon-low"
    elif changed == "environment":
        assert spawn.await_args.kwargs["env"]["SSL_CERT_FILE"] == "/new/test-ca.pem"
    elif changed == "solo":
        assert "--dangerously-skip-permissions" in args
    else:
        assert args[args.index("--mode") + 1] == "plan"
        assert "--disable-slash-commands" not in args
    native.acknowledge_turn_complete()
    native.update_env(dict(native.session.env))
    await native.send_message("unchanged", [])
    assert spawn.await_count == 2
    if changed == "mode":
        native.acknowledge_turn_complete()
        await native.set_mode("default")
        await native.send_message("default again", [])
        assert spawn.await_count == 3
        assert "--mode" not in spawn.await_args.args
        assert "--disable-slash-commands" in spawn.await_args.args


async def test_persistent_inflight_env_and_mode_change_do_not_replace_process(
    persistent_native,
) -> None:
    native, spawn, processes = persistent_native
    await native.send_message("working", [])
    native.update_env({"ANTIGRAVITY_MODEL": "gemini-4-argon-low"})
    with pytest.raises(RuntimeError, match="already in flight"):
        await native.send_message("overlap", [])
    with pytest.raises(RuntimeError, match="in flight"):
        await native.set_mode("plan")
    assert spawn.await_count == 1
    assert not processes[0]._terminated


async def test_persistent_cancel_resumes_and_discards_old_generation(persistent_native) -> None:
    native, spawn, processes = persistent_native
    await native.send_message("working", [])
    native.maybe_capture_conversation_id({"event": "init", "conversation_id": "keep-history"})
    old_generation = native._stdout_generation
    await native._stdout_queue.put((old_generation, {"event": "result", "result": {}}))
    await native._stdout_queue.put((old_generation, None))
    await native.cancel_active_turn()
    assert not native.turn_in_flight
    assert spawn.await_count == 2
    assert processes[0]._terminated
    assert "keep-history" in spawn.await_args.args
    await native.send_message("next", [])
    fresh = {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE"}}
    _emit(processes[1], fresh)
    assert await asyncio.wait_for(native.read_line(), 2) == fresh
    assert spawn.await_count == 2
    assert native.turn_in_flight


async def test_persistent_large_fragmented_records_do_not_wait_for_eof(persistent_native) -> None:
    native, _spawn, processes = persistent_native
    await native.send_message("hello", [])
    record = {"event": "step_update", "step_update": {"text_delta": "x" * 70000}}
    wire = json.dumps(record).encode() + b"\n"
    processes[0].stdout.push(b"not json\n")
    processes[0].stdout.push(wire[:200])
    processes[0].stdout.push(wire[200:65500])
    processes[0].stdout.push(wire[65500:])
    assert await asyncio.wait_for(native.read_line(), 2) == record
    assert not processes[0]._terminated


async def test_persistent_stdin_failure_does_not_resubmit(persistent_native, monkeypatch) -> None:
    native, spawn, processes = persistent_native
    await native.start()
    monkeypatch.setattr(
        processes[0].stdin, "drain", AsyncMock(side_effect=BrokenPipeError("closed"))
    )
    with pytest.raises(RuntimeError, match="stdin closed") as exc_info:
        await native.send_message("once", [])
    assert isinstance(exc_info.value.__cause__, BrokenPipeError)
    assert spawn.await_count == 1
    assert len(processes[0].stdin.written) == 1
    assert not native.turn_in_flight


async def test_persistent_stop_waits_for_concurrent_start(persistent_native) -> None:
    native, spawn, processes = persistent_native
    entered = asyncio.Event()
    release = asyncio.Event()
    normal_spawn = spawn.side_effect

    async def delayed_spawn(*args: Any, **kwargs: Any) -> _PersistentProcess:
        process = await normal_spawn(*args, **kwargs)
        entered.set()
        await release.wait()
        return cast(_PersistentProcess, process)

    spawn.side_effect = delayed_spawn
    started = asyncio.create_task(native.start())
    stopped: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stopped = asyncio.create_task(native.stop())
        await asyncio.sleep(0)
        assert not stopped.done()
    finally:
        release.set()
        await asyncio.wait_for(started, 2)
        if stopped is not None:
            await asyncio.wait_for(stopped, 2)
    assert processes[0]._terminated
    assert not native._started
    assert not native._handshake_complete


async def test_persistent_cancel_before_init_reinjects_new_conversation_guidance(
    persistent_native,
) -> None:
    native, spawn, processes = persistent_native
    await native.send_message("first", [])
    await native.cancel_active_turn()
    await native.send_message("second", [])
    assert spawn.await_count == 2
    assert "--conversation" not in spawn.await_args.args
    for process in processes:
        message = json.loads(process.stdin.written[0])["message"]["content"]
        assert "<claude_hub_instructions>" in message


async def test_persistent_tailer_stop_fences_teardown_and_late_result(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, spawn, processes = persistent_native
    native._persist_conversation_id("keep-history")
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    queue = await tailer.subscribe()
    entered = asyncio.Event()
    release = asyncio.Event()
    terminate = native._terminate_subprocess

    async def delayed_terminate(process: asyncio.subprocess.Process, pgid: int) -> None:
        entered.set()
        await release.wait()
        await terminate(process, pgid)

    monkeypatch.setattr(native, "_terminate_subprocess", delayed_terminate)
    next_send: asyncio.Task[None] | None = None
    try:
        await tailer.send_message("working", [], client_turn_id="cancelled")
        generation = native._stdout_generation
        assert await tailer.cancel_turn()
        cancelled = await _through_completion(queue)
        assert cancelled[-1].payload["status"] == "cancelled"
        assert cancelled[-1].turn_id == "cancelled"
        await asyncio.wait_for(entered.wait(), 2)
        next_send = asyncio.create_task(tailer.send_message("next", [], client_turn_id="next"))
        await asyncio.sleep(0)
        assert not next_send.done()
        assert spawn.await_count == 1
        await native._stdout_queue.put((generation, _usage_result({"input_tokens": 9999}, 1)))
        release.set()
        await asyncio.wait_for(next_send, 2)
        assert spawn.await_count == 2
        assert "keep-history" in spawn.await_args.args
        _emit(processes[1], _step_usage(2, {"input_tokens": 7}))
        _emit(processes[1], _usage_result({"input_tokens": 10006}, 2))
        completed = await _through_completion(queue)
        assert completed[-1].payload["status"] == "completed"
        assert completed[-1].payload["usage"]["input"] == 7
        assert completed[-1].turn_id == "next"
        terminal = [
            event
            for event in (await tailer.store.read_since()).events
            if event.type == AgentStreamEventType.TURN_COMPLETED
        ]
        assert [event.turn_id for event in terminal] == ["cancelled", "next"]
        assert not tailer.hard_failed
    finally:
        release.set()
        if next_send is not None:
            await asyncio.wait_for(next_send, 2)
        await tailer.stop()


@pytest.mark.parametrize("active", [False, True])
async def test_persistent_unexpected_eof_fails_closed_without_duplicate_completion(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, active: bool
) -> None:
    native, _spawn, processes = persistent_native
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    await tailer.subscribe()
    try:
        await native.start()
        if active:
            await tailer.send_message("work", [], client_turn_id="eof-turn")
        processes[0].stdout.push(b"")
        assert tailer._task is not None
        await asyncio.wait_for(tailer._task, 2)
        assert tailer.hard_failed
        events = (await tailer.store.read_since()).events
        completions = [
            event for event in events if event.type == AgentStreamEventType.TURN_COMPLETED
        ]
        errors = [event for event in events if event.type == AgentStreamEventType.ERROR]
        assert len(completions) == len(errors) == int(active)
        if active:
            assert completions[0].turn_id == "eof-turn"
            assert completions[0].payload["status"] == "failed"
    finally:
        await tailer.stop()


async def test_persistent_nonzero_eof_preserves_redacted_stderr_detail(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, spawn, processes = persistent_native
    normal_spawn = spawn.side_effect

    async def with_open_stderr(*args: Any, **kwargs: Any) -> _PersistentProcess:
        process = cast(_PersistentProcess, await normal_spawn(*args, **kwargs))
        process.stderr = _FakeStream()
        return process

    spawn.side_effect = with_open_stderr
    proxy_module = importlib.import_module("claude_hub.services.provider_proxy")
    private_url = "http://test-user:private-password@private-proxy.example.test:8080"
    monkeypatch.setattr(proxy_module.settings, "cursor_proxy_url", SecretStr(private_url))
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    await tailer.subscribe()
    try:
        await tailer.send_message("work", [], client_turn_id="failed-turn")
        processes[0].stderr.push(f"certificate verify failed via {private_url}\n".encode())
        processes[0].stderr.push(b"")
        processes[0].returncode = 23
        processes[0].stdout.push(b"")
        assert tailer._task is not None
        await asyncio.wait_for(tailer._task, 2)
        assert tailer.hard_failed
        events = (await tailer.store.read_since()).events
        errors = [event for event in events if event.type == AgentStreamEventType.ERROR]
        failed = [event for event in events if event.type == AgentStreamEventType.TURN_COMPLETED]
        assert len(errors) == len(failed) == 1
        assert errors[0].turn_id == failed[0].turn_id == "failed-turn"
        assert failed[0].payload["status"] == "failed"
        detail = errors[0].payload["message"]
        assert detail == tailer.last_error == native.exit_error
        assert "provider exited with code 23" in detail
        assert "certificate verify failed" in detail
        assert "[REDACTED]" in detail
        assert "test-user" not in detail
        assert "private-password" not in detail
        assert "private-proxy.example.test" not in detail
        assert not native.turn_in_flight
    finally:
        await tailer.stop()


async def test_persistent_idle_stop_closes_stdin_before_wait_and_signal(
    persistent_native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, _spawn, processes = persistent_native
    await native.start()
    process = processes[0]
    generation = native._stdout_generation
    order: list[str] = []
    close = process.stdin.close
    terminate = process.terminate

    def close_stdin() -> None:
        assert native._stdout_generation > generation
        order.append("close")
        close()

    async def graceful_exit() -> int:
        assert process.stdin.is_closing()
        order.append("wait")
        process.returncode = 0
        return 0

    def signal_process() -> None:
        order.append("signal")
        terminate()

    monkeypatch.setattr(process.stdin, "close", close_stdin)
    monkeypatch.setattr(process, "wait", graceful_exit)
    monkeypatch.setattr(process, "terminate", signal_process)
    await asyncio.wait_for(native.stop(), 1)
    assert order[:2] == ["close", "wait"]
    assert order.index("signal") > 1
    assert native._process is None
    assert native._reader_task is None
    assert not native._started


async def test_persistent_idle_stop_grace_timeout_falls_back_to_signal(
    persistent_native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, _spawn, processes = persistent_native
    provider_module = importlib.import_module(
        "claude_hub.services.agent_stream.providers.antigravity"
    )
    monkeypatch.setattr(provider_module, "_IDLE_EXIT_GRACE_SECONDS", 0.01)
    await native.start()
    process = processes[0]
    terminated = asyncio.Event()
    grace_cancelled = asyncio.Event()
    terminate = process.terminate

    async def wait_for_signal() -> int:
        try:
            await terminated.wait()
        except asyncio.CancelledError:
            grace_cancelled.set()
            raise
        return -15

    def signal_process() -> None:
        assert process.stdin.is_closing()
        assert grace_cancelled.is_set()
        terminate()
        terminated.set()

    monkeypatch.setattr(process, "wait", wait_for_signal)
    monkeypatch.setattr(process, "terminate", signal_process)
    await asyncio.wait_for(native.stop(), 1)
    assert grace_cancelled.is_set()
    assert terminated.is_set()
    assert native._process is None
    assert native._reader_task is None


async def test_persistent_active_cancel_signals_without_closing_stdin_or_idle_wait(
    persistent_native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, spawn, processes = persistent_native
    await native.send_message("working", [])
    process = processes[0]
    terminated = asyncio.Event()
    terminate = process.terminate
    order: list[str] = []

    async def wait_after_signal() -> int:
        assert terminated.is_set(), "active cancellation must not wait for an EOF grace period"
        order.append("wait")
        return -15

    def signal_process() -> None:
        assert not process.stdin.is_closing()
        order.append("signal")
        terminate()
        terminated.set()

    monkeypatch.setattr(process, "wait", wait_after_signal)
    monkeypatch.setattr(process, "terminate", signal_process)
    await asyncio.wait_for(native.cancel_active_turn(), 1)
    assert order[0] == "signal"
    assert not process.stdin.is_closing()
    assert spawn.await_count == 2
    assert native._process is processes[1]
    assert not native.turn_in_flight


def _retain_stop_flag_at_current_eof(
    native: AntigravityNativeSession, monkeypatch: pytest.MonkeyPatch
) -> list[bool]:
    observed: list[bool] = []
    get = native._stdout_queue.get

    async def get_with_pending_restart() -> tuple[int, dict[str, Any] | None]:
        generation, record = await get()
        if generation == native._stdout_generation and record is None:
            # Pin the race at read_line's generation check, after the consumer
            # could clear the flag at its loop head. This EOF is the replacement's
            # first record, not the deliberately terminated process's farewell.
            native._client_requested_stop = True
            observed.append(native._client_requested_stop)
        return generation, record

    monkeypatch.setattr(native._stdout_queue, "get", get_with_pending_restart)
    return observed


async def test_persistent_stop_replacement_eof_before_init_fails_idle_tailer(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, spawn, processes = persistent_native
    native._persist_conversation_id("keep-history")
    observed = _retain_stop_flag_at_current_eof(native, monkeypatch)
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    queue = await tailer.subscribe()
    try:
        await tailer.send_message("working", [], client_turn_id="stopped")
        assert await tailer.cancel_turn()
        stopped = await _through_completion(queue)
        assert stopped[-1].payload["status"] == "cancelled"
        await asyncio.wait_for(tailer._await_turn_teardown(), 2)
        assert spawn.await_count == 2
        assert processes[1].stdin.written == []
        before = (await tailer.store.read_since()).events
        processes[1].stdout.push(b"")
        assert tailer._task is not None
        await asyncio.wait_for(tailer._task, 2)
        assert observed == [True]
        assert not native._client_requested_stop
        assert tailer.hard_failed
        assert not native.turn_in_flight
        assert (await tailer.store.read_since()).events == before
    finally:
        await tailer.stop()


async def test_persistent_config_replacement_eof_before_init_fails_accepted_turn_once(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, spawn, processes = persistent_native
    native._persist_conversation_id("keep-history")
    observed = _retain_stop_flag_at_current_eof(native, monkeypatch)
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    queue = await tailer.subscribe()
    try:
        await tailer.send_message("first", [], client_turn_id="completed")
        _emit(processes[0], _usage_result({"input_tokens": 10}, 1))
        assert (await _through_completion(queue))[-1].payload["status"] == "completed"
        native.update_env({"ANTIGRAVITY_MODEL": "gemini-4-argon-low"})
        await tailer.send_message("next", [], client_turn_id="replacement-turn")
        assert spawn.await_count == 2
        assert len(processes[1].stdin.written) == 1
        assert native.turn_in_flight
        processes[1].stdout.push(b"")
        assert tailer._task is not None
        await asyncio.wait_for(tailer._task, 2)
        assert observed == [True]
        assert not native._client_requested_stop
        assert tailer.hard_failed
        assert not native.turn_in_flight
        events = (await tailer.store.read_since()).events
        failed = [
            event
            for event in events
            if event.turn_id == "replacement-turn"
            and event.type == AgentStreamEventType.TURN_COMPLETED
        ]
        errors = [
            event
            for event in events
            if event.turn_id == "replacement-turn" and event.type == AgentStreamEventType.ERROR
        ]
        assert len(failed) == len(errors) == 1
        assert failed[0].payload["status"] == "failed"
    finally:
        await tailer.stop()


async def test_persistent_tailer_two_turns_share_process_and_correct_usage(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, spawn, processes = persistent_native
    tailer = _make_tailer(native, monkeypatch, tmp_path)
    queue = await tailer.subscribe()
    try:
        await tailer.send_message("first", [], client_turn_id="first")
        _emit(processes[0], {"event": "init", "conversation_id": "conversation-1"})
        _emit(processes[0], _step_usage(1, {"input_tokens": 100, "output_tokens": 5}))
        _emit(processes[0], _usage_result({"input_tokens": 100, "output_tokens": 5}, 1))
        first = await _through_completion(queue)
        assert first[-1].turn_id == "first"
        await tailer.send_message("second", [], client_turn_id="second")
        _emit(
            processes[0],
            _step_usage(3, {"input_tokens": 10, "cache_read_tokens": 90, "output_tokens": 3}),
        )
        _emit(
            processes[0],
            _usage_result({"input_tokens": 110, "cache_read_tokens": 90, "output_tokens": 8}, 2),
        )
        second = await _through_completion(queue)
        assert second[-1].turn_id == "second"
        assert second[-1].payload["usage"]["total"] == 13
        assert [
            event.payload["text"]
            for event in second
            if event.type == AgentStreamEventType.TEXT_DELTA
        ] == ["answer"]
        assert spawn.await_count == 1
        assert tailer.native_ready
        assert not tailer.hard_failed
    finally:
        await tailer.stop()


_VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


async def test_persistent_image_attachments_staged_and_cleaned_on_completion(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, _spawn, processes = persistent_native
    native_module = importlib.import_module("claude_hub.services.agent_stream.native")
    image_dir = tmp_path / "cursor-images"
    image_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(native_module, "_cursor_image_temp_dir", lambda: image_dir)

    assert native.supports_images is True
    await native.send_message("describe this image", [_VALID_PNG])

    staged_files = list(image_dir.glob("agy-img-*.png"))
    assert len(staged_files) == 1
    assert staged_files[0].read_bytes() == _VALID_PNG

    payload = json.loads(processes[0].stdin.written[0])
    content = payload["message"]["content"]
    assert "<<<HUB_IMAGE_ATTACHMENT_V1>>>" in content
    assert str(staged_files[0]) in content
    assert "<<<END_HUB_IMAGE_ATTACHMENT_V1>>>" in content
    assert "describe this image" in content

    native.acknowledge_turn_complete()
    assert not staged_files[0].exists()
    assert list(image_dir.glob("agy-img-*")) == []


async def test_persistent_image_attachments_cleaned_on_cancel_and_reject_invalid_bytes(
    persistent_native, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native, _spawn, processes = persistent_native
    native_module = importlib.import_module("claude_hub.services.agent_stream.native")
    image_dir = tmp_path / "cursor-images"
    image_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(native_module, "_cursor_image_temp_dir", lambda: image_dir)

    with pytest.raises(ValueError, match="unsupported image format"):
        await native.send_message("bad image", [_VALID_PNG, b"not-an-image"])
    assert not native.turn_in_flight
    assert list(image_dir.glob("agy-img-*")) == []

    await native.send_message("cancel this", [_VALID_PNG])
    staged_files = list(image_dir.glob("agy-img-*.png"))
    assert len(staged_files) == 1
    await native.cancel_active_turn()
    assert not native.turn_in_flight
    assert not staged_files[0].exists()
    assert len(processes) == 2
