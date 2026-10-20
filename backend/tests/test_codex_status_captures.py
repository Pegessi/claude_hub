"""Real tmux replay; spinner/elapsed/chrome transformations are derived."""

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import AgentRuntimeStatus, AgentType
from claude_hub.services.agent_status_markers import codex_output_is_working
from claude_hub.services.terminal_runtime.defaults import (
    _ANSI_ESCAPE_RE,
    _WORKING_FRAME_STALE_SECONDS,
)
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

FIXTURES = Path(__file__).parent / "fixtures" / "codex-status-20261020"


def frame(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("utf-8")


def classify(manager: TTYDManager, raw: str, provider: AgentType = AgentType.CODEX):
    output = _ANSI_ESCAPE_RE.sub("", raw)
    process = TTYDProcess(tab_id="capture-replay", port=12345, name="Replay", agent_type=provider)
    return manager._classify_agent_status(
        process, output, hashlib.sha256(output.encode()).hexdigest(), provider.value
    )[0]


@pytest.fixture
def manager() -> TTYDManager:
    result = TTYDManager.__new__(TTYDManager)
    result._status_snapshots = {}
    result._status_cache = {}
    return result


@pytest.mark.parametrize(
    "name", ["busy.ansi", "busy-subagent.ansi"] + [f"busy-frame-{i}.ansi" for i in range(6)]
)
def test_real_busy_frames_override_idle_footer(manager, name):
    raw = frame(name)
    assert "\x1b[" in raw
    normalized = _ANSI_ESCAPE_RE.sub("", raw)
    assert "? for shortcuts" in normalized
    assert codex_output_is_working(normalized)  # Existing regex already works.
    assert classify(manager, raw) == AgentRuntimeStatus.WORKING


def test_real_idle_with_tool_history(manager):
    raw = frame("idle.ansi")
    assert "Ran" in raw and "Worked for" in raw
    assert not codex_output_is_working(_ANSI_ESCAPE_RE.sub("", raw))
    assert classify(manager, raw) == AgentRuntimeStatus.IDLE


@pytest.mark.parametrize("spinner", ["•", "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
@pytest.mark.parametrize("elapsed", ["1s", "19m", "1h 2m 3s"])
def test_derived_spinner_elapsed(manager, spinner, elapsed):
    raw = frame("busy.ansi").replace("12m 13s", elapsed)
    raw = raw.replace("•\x1b[0m \x1b[2mWorking", spinner + "\x1b[0m \x1b[2mWorking")
    assert f"{spinner} Working ({elapsed}" in _ANSI_ESCAPE_RE.sub("", raw)
    assert classify(manager, raw) == AgentRuntimeStatus.WORKING


def test_derived_tall_chrome(manager):
    raw = frame("busy.ansi").replace(
        "Ask Codex to do anything", "Ask Codex to do anything\n" + "queued follow-up\n" * 20
    )
    assert classify(manager, raw) == AgentRuntimeStatus.WORKING


def test_real_rapid_frames_then_idle_then_busy(manager):
    for _ in range(2):
        for i in range(6):
            assert classify(manager, frame(f"busy-frame-{i}.ansi")) == AgentRuntimeStatus.WORKING
        assert classify(manager, frame("idle.ansi")) == AgentRuntimeStatus.IDLE
        assert classify(manager, frame("busy.ansi")) == AgentRuntimeStatus.WORKING


def test_frozen_real_busy_keeps_stall_guard(manager):
    raw = frame("busy.ansi")
    assert classify(manager, raw) == AgentRuntimeStatus.WORKING
    manager._status_snapshots["capture-replay"]["frame_first_seen_at"] = datetime.now() - timedelta(
        seconds=_WORKING_FRAME_STALE_SECONDS + 1
    )
    assert classify(manager, raw) == AgentRuntimeStatus.ATTENTION


@pytest.mark.parametrize(
    "suffix, expected",
    [
        ("\n$ ", AgentRuntimeStatus.IDLE),
        ("\nEnter to select · Esc to cancel", AgentRuntimeStatus.ATTENTION),
    ],
)
def test_shell_and_selection_precedence(manager, suffix, expected):
    assert classify(manager, frame("busy.ansi") + suffix) == expected


@pytest.mark.parametrize(
    "provider", [AgentType.CLAUDE, AgentType.CURSOR, AgentType.MASO, AgentType.TRAEX]
)
def test_other_providers_keep_idle_hint_precedence(manager, provider):
    assert classify(manager, frame("busy.ansi"), provider) == AgentRuntimeStatus.IDLE


@pytest.mark.asyncio
async def test_raw_capture_status_path_normalizes_ansi_and_transitions(manager, monkeypatch):
    process = TTYDProcess(tab_id="raw-replay", port=12345, name="Raw", agent_type=AgentType.CODEX)
    monkeypatch.setattr(
        process, "capture_history", AsyncMock(side_effect=[frame("busy.ansi"), frame("idle.ansi")])
    )
    monkeypatch.setattr(process, "capture_foreground_command", AsyncMock(return_value="codex"))
    manager.processes = {process.tab_id: process}
    for expected in [AgentRuntimeStatus.WORKING, AgentRuntimeStatus.IDLE]:
        status = await manager.get_tab_agent_status(
            process.tab_id, use_cache=False, live_sessions={process.tmux_session}
        )
        assert status is not None and status.status == expected


@pytest.mark.asyncio
async def test_captured_native_provider_statuses_bypass_terminal_classifier(manager, monkeypatch):
    import importlib
    import json
    from types import SimpleNamespace

    from claude_hub.models import SessionKind

    module = importlib.import_module("claude_hub.services.ttyd_manager")
    rows = json.loads((FIXTURES / "native-statuses.json").read_text())
    for row in rows:
        process = TTYDProcess(
            tab_id="native-replay",
            port=12345,
            name="Native",
            agent_type=AgentType(row["agent_type"]),
            session_kind=SessionKind.CHAT,
        )
        manager.processes = {process.tab_id: process}
        monkeypatch.setattr(
            module,
            "get_tab_native_runtime_snapshot",
            lambda _tab_id: SimpleNamespace(
                status=AgentRuntimeStatus(row["status"]), detail=row["detail"]
            ),
        )
        monkeypatch.setattr(
            process,
            "capture_history",
            AsyncMock(side_effect=AssertionError("native path captured terminal")),
        )
        status = await manager.get_tab_agent_status(process.tab_id, use_cache=False)
        assert status is not None and status.status.value == row["status"]
        assert status.detail == row["detail"]


@pytest.mark.parametrize("verb", ["Ran", "Started", "Interacted", "Completed"])
def test_derived_tool_and_subagent_history_does_not_mean_busy(manager, verb):
    raw = frame("idle.ansi").replace("Ran", verb)
    assert classify(manager, raw) == AgentRuntimeStatus.IDLE
    busy = frame("busy.ansi").replace("Ran", verb)
    assert classify(manager, busy) == AgentRuntimeStatus.WORKING
