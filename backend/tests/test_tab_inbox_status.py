"""Human activity labels and the bounded reply excerpt behind the inbox."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from claude_hub.models import AgentRuntimeStatus, AgentType, SessionKind, TerminalAgentStatus
from claude_hub.services.agent_stream import store as store_module
from claude_hub.services.agent_stream.activity_labels import (
    IDLE_DETAIL,
    WORKING_ACTIVITY,
    tool_activity_label,
)
from claude_hub.services.agent_stream.store import AgentStreamStore, read_reply_excerpt
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")


def _write_stream(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _event(
    sequence: int,
    kind: str,
    payload: dict[str, Any],
    *,
    turn_id: Optional[str] = "turn-1",
    created_at: str = "2026-11-09T10:00:00",
) -> dict[str, Any]:
    return {
        "stream_sequence": sequence,
        "session_id": "terminal-tab-tab-1",
        "tab_id": "tab-1",
        "agent_type": "claude",
        "type": kind,
        "turn_id": turn_id,
        "payload": payload,
        "created_at": created_at,
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"name": "Bash", "args": {"command": "pytest -q tests/test_tabs.py"}},
            'Running "pytest -q tests/test_tabs.py"',
        ),
        ({"name": "Read", "args": {"file_path": "src/app.ts"}}, 'Reading "src/app.ts"'),
        ({"name": "Grep", "args": {"pattern": "needs_you"}}, 'Searching "needs_you"'),
        (
            {"name": "WebSearch", "args": {"query": "claude agent view"}},
            'Searching the web for "claude agent view"',
        ),
        ({"name": "Task", "args": {"description": "review the diff"}}, "Delegating"),
        ({"name": "mcp__linear__search", "args": {}}, 'Running "mcp__linear__search"'),
        ({"name": "", "args": {}}, None),
        ({"args": {}}, None),
        (None, None),
        ("not-a-payload", None),
    ],
)
def test_tool_activity_label_is_human_and_never_provider_jargon(
    payload: Any, expected: Optional[str]
) -> None:
    assert tool_activity_label(payload) == expected


def test_tool_activity_label_truncates_long_commands() -> None:
    label = tool_activity_label({"name": "Bash", "args": {"command": "x" * 200}})
    assert label is not None
    assert label.startswith('Running "')
    assert label.endswith('…"')
    assert len(label) < 80


def test_reply_excerpt_reads_only_the_final_answer(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    _write_stream(
        stream,
        [
            _event(0, "turn_started", {"text": "please fix it"}),
            _event(1, "text_delta", {"text": "Let me look at the tests."}),
            _event(2, "tool_call_started", {"tool_call_id": "call-1", "name": "Bash"}),
            _event(3, "tool_call_completed", {"tool_call_id": "call-1"}),
            _event(4, "text_delta", {"text": "All 12 tests"}),
            _event(5, "text_delta", {"text": " pass now."}),
            _event(6, "turn_completed", {"status": "completed"}),
        ],
    )
    excerpt = read_reply_excerpt(stream, max_chars=160, max_scan_bytes=4096)
    assert excerpt is not None
    # Narration before the tool call is dropped: the answer is what the row
    # shows, not the agent's thinking out loud.
    assert excerpt.text == "All 12 tests pass now."
    assert excerpt.written_at is not None
    # The rest of the API speaks aware UTC; a naive stamp here would be read
    # as local time by the browser.
    assert excerpt.written_at.tzinfo is not None
    assert excerpt.written_at.utcoffset() == timedelta(0)
    assert excerpt.size == stream.stat().st_size


def test_reply_excerpt_keeps_the_previous_turn_before_new_output(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    _write_stream(
        stream,
        [
            _event(0, "turn_started", {"text": "one"}),
            _event(1, "text_delta", {"text": "First answer."}),
            _event(2, "turn_completed", {"status": "completed"}),
            # A new turn has been accepted but has written no text yet.
            _event(3, "turn_started", {"text": "two"}, turn_id="turn-2"),
            _event(
                4, "tool_call_started", {"tool_call_id": "call-2", "name": "Bash"}, turn_id="turn-2"
            ),
        ],
    )
    excerpt = read_reply_excerpt(stream, max_chars=160, max_scan_bytes=4096)
    assert excerpt is not None
    assert excerpt.text == "First answer."
    # The text is the previous turn's answer, so it must not claim the newest
    # write's time (the row would read "New reply · just now" for old text).
    assert excerpt.written_at is None


def test_reply_excerpt_scans_a_bounded_tail_of_a_large_stream(tmp_path: Path) -> None:
    filler = [
        _event(index, "text_delta", {"text": "padding " * 10}, turn_id="turn-old")
        for index in range(4000)
    ]
    newest = [
        _event(4001, "turn_started", {}, turn_id="turn-new"),
        _event(4002, "text_delta", {"text": "short final answer"}, turn_id="turn-new"),
    ]
    stream = tmp_path / "stream.jsonl"
    _write_stream(stream, filler + newest)
    assert stream.stat().st_size > 32 * 1024
    excerpt = read_reply_excerpt(stream, max_chars=160, max_scan_bytes=32 * 1024)
    assert excerpt is not None
    assert excerpt.text == "short final answer"


def test_reply_excerpt_is_absent_for_empty_or_missing_streams(tmp_path: Path) -> None:
    assert read_reply_excerpt(tmp_path / "missing.jsonl", 160, 4096) is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert read_reply_excerpt(empty, 160, 4096) is None


@pytest.mark.asyncio
async def test_store_reply_excerpt_returns_none_without_a_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    store = AgentStreamStore("terminal-tabs", "terminal-tab-tab-1")
    assert await store.reply_excerpt() is None


def _chat_manager(
    *,
    reply_events: list[dict[str, Any]] | None = None,
    state_root: Path,
    workspace_id: str | None = None,
    managed_session_id: str | None = None,
) -> TTYDManager:
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    manager._status_cache = {}
    process = TTYDProcess(
        tab_id="tab-1",
        port=12345,
        name="Native Chat",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        workspace_id=workspace_id,
        managed_session_id=managed_session_id,
    )
    manager.processes = {process.tab_id: process}
    if reply_events is not None:
        # A workspace Chat writes its stream under its managed session, not
        # under the terminal-tab namespace.
        if workspace_id and managed_session_id:
            store = AgentStreamStore(workspace_id, managed_session_id)
        else:
            store = AgentStreamStore("terminal-tabs", f"terminal-tab-{process.tab_id}")
        _write_stream(store.path, reply_events)
    return manager


@pytest.mark.asyncio
async def test_status_payload_carries_what_the_inbox_needs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    started = datetime(2026, 11, 9, 10, 0, 0)
    # A question card blocks an idle turn: the row still needs the user, and
    # the previous answer stays readable (the working case is covered below,
    # where the excerpt read is skipped on purpose).
    monkeypatch.setattr(
        ttyd_manager_module,
        "get_tab_native_runtime_snapshot",
        lambda _tab_id: SimpleNamespace(
            status=AgentRuntimeStatus.IDLE,
            detail=IDLE_DETAIL,
            pending_question=True,
            activity="Asking you",
            activity_since=started,
        ),
    )
    manager = _chat_manager(
        state_root=tmp_path,
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "Previously answered."}),
            _event(2, "turn_completed", {"status": "completed"}),
            _event(3, "turn_started", {"text": "again"}, turn_id="turn-2"),
        ],
    )

    status = await manager.get_tab_agent_status("tab-1", use_cache=False)

    assert isinstance(status, TerminalAgentStatus)
    assert status.pending_question is True
    assert status.activity == "Asking you"
    assert status.activity_since == started
    assert status.last_reply_excerpt == "Previously answered."
    # turn-2 has announced itself but written nothing: the excerpt is the
    # previous answer and therefore carries no reply time.
    assert status.last_reply_at is None


@pytest.mark.asyncio
async def test_status_payload_tolerates_producers_without_inbox_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An older snapshot producer (or a bare test double) must keep working."""
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        ttyd_manager_module,
        "get_tab_native_runtime_snapshot",
        lambda _tab_id: SimpleNamespace(status=AgentRuntimeStatus.IDLE, detail=IDLE_DETAIL),
    )
    manager = _chat_manager(state_root=tmp_path)

    status = await manager.get_tab_agent_status("tab-1", use_cache=False)

    assert status is not None
    assert status.pending_question is False
    assert status.activity is None
    assert status.last_reply_excerpt is None


@pytest.mark.asyncio
async def test_status_payload_reuses_one_excerpt_scan_per_stream_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 5-second poll must not rescan history for an unchanged stream."""
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        ttyd_manager_module,
        "get_tab_native_runtime_snapshot",
        lambda _tab_id: SimpleNamespace(status=AgentRuntimeStatus.IDLE, detail=IDLE_DETAIL),
    )
    manager = _chat_manager(
        state_root=tmp_path,
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "Working on it."}),
        ],
    )
    scans = 0
    original = store_module.read_reply_excerpt

    def counting_read(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal scans
        scans += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store_module, "read_reply_excerpt", counting_read)

    first = await manager.get_tab_agent_status("tab-1", use_cache=False)
    second = await manager.get_tab_agent_status("tab-1", use_cache=False)
    third = await manager.get_tab_agent_status("tab-1", use_cache=False)

    # One scan for the first poll; the next two only stat an unchanged file.
    assert scans == 1
    assert first is not None and second is not None and third is not None
    assert first.last_reply_excerpt == "Working on it."
    assert second.last_reply_excerpt == first.last_reply_excerpt
    assert third.last_reply_at == first.last_reply_at


@pytest.mark.asyncio
async def test_working_turns_without_a_question_skip_the_excerpt_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A merely streaming turn would only show the previous answer and would
    rescan a growing file on every poll."""
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        ttyd_manager_module,
        "get_tab_native_runtime_snapshot",
        lambda _tab_id: SimpleNamespace(
            status=AgentRuntimeStatus.WORKING,
            detail=WORKING_ACTIVITY,
            activity='Running "pytest -q"',
            pending_question=False,
        ),
    )
    manager = _chat_manager(
        state_root=tmp_path,
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "Previous answer."}),
        ],
    )
    reads = 0
    original = store_module.read_reply_excerpt

    def counting_read(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store_module, "read_reply_excerpt", counting_read)

    status = await manager.get_tab_agent_status("tab-1", use_cache=False)

    assert reads == 0
    assert status is not None
    assert status.status == AgentRuntimeStatus.WORKING
    assert status.activity == 'Running "pytest -q"'
    assert status.last_reply_excerpt is None
    assert status.last_reply_at is None


@pytest.mark.asyncio
async def test_working_turn_blocked_on_a_question_still_reads_the_excerpt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A working tab that is also waiting for an answer is the top
    "Waiting for your answer" row: it keeps its preview, so it must read."""
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        ttyd_manager_module,
        "get_tab_native_runtime_snapshot",
        lambda _tab_id: SimpleNamespace(
            status=AgentRuntimeStatus.WORKING,
            detail=WORKING_ACTIVITY,
            activity="Asking you",
            pending_question=True,
        ),
    )
    manager = _chat_manager(
        state_root=tmp_path,
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "Which branch should I rebase?"}),
        ],
    )
    reads = 0
    original = store_module.read_reply_excerpt

    def counting_read(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store_module, "read_reply_excerpt", counting_read)

    status = await manager.get_tab_agent_status("tab-1", use_cache=False)

    assert reads == 1
    assert status is not None
    assert status.status == AgentRuntimeStatus.WORKING
    assert status.pending_question is True
    assert status.activity == "Asking you"
    assert status.last_reply_excerpt == "Which branch should I rebase?"
    # The text is the working turn's own output, so it keeps its write time.
    assert status.last_reply_at is not None


@pytest.mark.asyncio
async def test_managed_chat_tab_reads_its_excerpt_from_the_managed_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Workspace Chat persists its stream under its managed session, so
    reading the terminal-tab namespace would silently report "no reply" for
    every agent row in the inbox."""
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        ttyd_manager_module,
        "get_tab_native_runtime_snapshot",
        lambda _tab_id: SimpleNamespace(status=AgentRuntimeStatus.IDLE, detail=IDLE_DETAIL),
    )
    manager = _chat_manager(
        state_root=tmp_path,
        workspace_id="ws-1",
        managed_session_id="managed-session-1",
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "Rebased onto main."}),
        ],
    )

    status = await manager.get_tab_agent_status("tab-1", use_cache=False)

    assert status is not None
    assert status.last_reply_excerpt == "Rebased onto main."
    assert status.last_reply_at is not None
    # ...and it came from the managed session file, not the tab namespace.
    managed = AgentStreamStore("ws-1", "managed-session-1")
    assert managed.path.exists()
    assert not AgentStreamStore("terminal-tabs", "terminal-tab-tab-1").path.exists()


@pytest.mark.asyncio
async def test_managed_chat_tab_reports_unread_from_the_managed_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    manager = _chat_manager(
        state_root=tmp_path,
        workspace_id="ws-1",
        managed_session_id="managed-session-1",
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "done"}),
            _event(
                2,
                "turn_completed",
                {"status": "completed"},
                created_at="2026-11-09T10:05:00+00:00",
            ),
        ],
    )
    process = manager.processes["tab-1"]

    assert await manager.compute_tab_unread(process) is True


@pytest.mark.asyncio
async def test_direct_chat_tab_still_reports_unread_from_the_tab_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The direct-Chat namespace must keep working exactly as before."""
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    manager = _chat_manager(
        state_root=tmp_path,
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "done"}),
            _event(
                2,
                "turn_completed",
                {"status": "completed"},
                created_at="2026-11-09T10:05:00+00:00",
            ),
        ],
    )
    process = manager.processes["tab-1"]

    assert await manager.compute_tab_unread(process) is True


@pytest.mark.asyncio
async def test_status_payload_reads_the_excerpt_again_after_the_stream_grows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        ttyd_manager_module,
        "get_tab_native_runtime_snapshot",
        lambda _tab_id: SimpleNamespace(status=AgentRuntimeStatus.IDLE, detail=IDLE_DETAIL),
    )
    manager = _chat_manager(
        state_root=tmp_path,
        reply_events=[
            _event(0, "turn_started", {"text": "go"}),
            _event(1, "text_delta", {"text": "First pass."}),
        ],
    )
    first = await manager.get_tab_agent_status("tab-1", use_cache=False)
    assert first is not None
    assert first.last_reply_excerpt == "First pass."

    store = AgentStreamStore("terminal-tabs", "terminal-tab-tab-1")
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_event(2, "text_delta", {"text": " Then the rest."})) + "\n")

    second = await manager.get_tab_agent_status("tab-1", use_cache=False)
    assert second is not None
    assert second.last_reply_excerpt == "First pass. Then the rest."
