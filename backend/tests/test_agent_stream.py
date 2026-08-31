"""Focused tests for the structured observation plane (Layer B).

Covers redaction, the append-only store, the adapter registry (including the
Cursor fail-closed contract), and the Claude/Codex line normalizers.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    StreamCapabilities,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream import (
    get_adapter,
    get_adapter_for_session,
    redact_event,
    supports_structured,
)
from claude_hub.services.agent_stream.base import (
    AgentStreamAdapter,
    NormalizeContext,
    SnapshotRecord,
    TranscriptSnapshot,
)
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import SessionTailer

# ── redaction ────────────────────────────────────────────────────────────────


def test_redact_event_strips_env_values():
    ev = AgentStreamEvent(
        stream_sequence=0,
        session_id="s1",
        tab_id="t1",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TOOL_CALL_STARTED,
        payload={"env": {"FOO": "bar", "SECRET": "shh"}},
        created_at=datetime.now(timezone.utc),
    )
    out = redact_event(ev)
    assert out.payload["env"] == {"FOO": "[REDACTED]", "SECRET": "[REDACTED]"}
    assert out.redacted is True


def test_redact_event_masks_sensitive_keys():
    ev = AgentStreamEvent(
        stream_sequence=0,
        session_id="s1",
        tab_id="t1",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TOOL_CALL_STARTED,
        payload={"api_key": "sk-abcdefghijklmnop", "name": "read_file"},
        created_at=datetime.now(timezone.utc),
    )
    out = redact_event(ev)
    assert out.payload["api_key"] == "[REDACTED]"
    assert out.payload["name"] == "read_file"


def test_redact_event_masks_token_literals():
    ev = AgentStreamEvent(
        stream_sequence=0,
        session_id="s1",
        tab_id="t1",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TEXT_DELTA,
        payload={"text": "my token is sk-1234567890abcdef and ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        created_at=datetime.now(timezone.utc),
    )
    out = redact_event(ev)
    text = out.payload["text"]
    assert "sk-1234567890abcdef" not in text
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in text
    assert "[REDACTED]" in text


def test_redact_event_truncates_long_fields():
    long_text = "x" * 5000
    ev = AgentStreamEvent(
        stream_sequence=0,
        session_id="s1",
        tab_id="t1",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TEXT_DELTA,
        payload={"text": long_text},
        created_at=datetime.now(timezone.utc),
    )
    out = redact_event(ev)
    assert len(out.payload["text"]) <= 4000
    assert out.payload["text"].endswith("…[truncated]")


def test_redact_event_does_not_mutate_input():
    ev = AgentStreamEvent(
        stream_sequence=0,
        session_id="s1",
        tab_id="t1",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TEXT_DELTA,
        payload={"env": {"A": "b"}},
        created_at=datetime.now(timezone.utc),
    )
    redact_event(ev)
    assert ev.payload["env"] == {"A": "b"}
    assert ev.redacted is False


# ── store ────────────────────────────────────────────────────────────────────


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> AgentStreamStore:
    import importlib

    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")

    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(wm_pkg, "STATE_ROOT", Path(tmp))
    return AgentStreamStore("ws1", "s1")


def _event(seq: int = 0, **payload: Any) -> AgentStreamEvent:
    return AgentStreamEvent(
        stream_sequence=seq,
        session_id="s1",
        tab_id="t1",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TEXT_DELTA,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def test_store_assigns_monotonic_sequences(store: AgentStreamStore):
    async def run() -> None:
        a = await store.append(_event(text="a"))
        b = await store.append(_event(text="b"))
        assert a.stream_sequence == 0
        assert b.stream_sequence == 1

    asyncio.run(run())


def test_store_read_since_pagination(store: AgentStreamStore):
    async def run() -> None:
        for i in range(5):
            await store.append(_event(text=str(i)))
        page = await store.read_since(-1, limit=2)
        assert len(page.events) == 2
        assert page.has_more is True
        page2 = await store.read_since(page.next_sequence, limit=10)
        assert len(page2.events) == 3
        assert page2.has_more is False

    asyncio.run(run())


def test_store_count_and_last_event_at(store: AgentStreamStore):
    async def run() -> None:
        assert await store.count() == 0
        assert await store.last_event_at() is None
        await store.append(_event(text="a"))
        await store.append(_event(text="b"))
        assert await store.count() == 2
        assert await store.last_event_at() is not None

    asyncio.run(run())


def test_store_recovers_sequence_after_restart(store: AgentStreamStore):
    async def run() -> None:
        await store.append(_event(text="a"))
        await store.append(_event(text="b"))
        # New store instance pointing at the same file.
        store2 = AgentStreamStore("ws1", "s1")
        c = await store2.append(_event(text="c"))
        assert c.stream_sequence == 2

    asyncio.run(run())


def test_store_clear_removes_event_log_and_cursor_checkpoint(store: AgentStreamStore):
    async def run() -> None:
        await store.append(_event(text="old"))
        store.cursor_path.write_text("{}", encoding="utf-8")

        await store.clear()

        assert not store.path.exists()
        assert not store.cursor_path.exists()
        fresh = await store.append(_event(text="new"))
        assert fresh.stream_sequence == 0

    asyncio.run(run())


class _SnapshotAdapter(AgentStreamAdapter):
    """Small whole-file source used to exercise append-only reconciliation."""

    adapter_id = "test-snapshot"

    def __init__(self, snapshot: TranscriptSnapshot) -> None:
        self.snapshot = snapshot

    def discover_source(self, session: ManagedSession) -> Path:
        return Path("/tmp/test-snapshot.jsonl")

    def supports_snapshot(self, session: ManagedSession) -> bool:
        return True

    def read_snapshot(self, path: Path, session: ManagedSession) -> TranscriptSnapshot:
        return self.snapshot

    def normalize_line(self, raw: Dict[str, Any], ctx: NormalizeContext) -> list[AgentStreamEvent]:
        if raw["kind"] == "user":
            return [ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": raw["text"]})]
        if raw["kind"] == "turn_ended":
            return [ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "completed"})]
        return [ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": raw["text"]})]


def _snapshot(*rows: tuple[str, str, str]) -> TranscriptSnapshot:
    return TranscriptSnapshot(
        digest="|".join(row[0] for row in rows),
        records=tuple(
            SnapshotRecord(
                source_id=source_id,
                raw={"kind": kind, "text": text},
                source_kind=kind,
            )
            for source_id, kind, text in rows
        ),
    )


def _snapshot_session() -> ManagedSession:
    now = datetime.now(timezone.utc)
    return ManagedSession(
        id="s1",
        workspace_id="ws1",
        tab_id="t1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CURSOR,
        status=ManagedSessionStatus.WORKING,
        title="cursor",
        workspace_path="/tmp",
        tmux_session="tmux-s1",
        created_at=now,
        updated_at=now,
    )


def test_snapshot_tailer_appends_only_new_suffix_and_fans_out(store: AgentStreamStore):
    async def run() -> None:
        session = _snapshot_session()
        adapter = _SnapshotAdapter(_snapshot(("u1", "user", "hello")))
        tailer = SessionTailer("ws1", "s1", adapter, lambda: session, store=store)

        await tailer._tail_snapshot(Path("/ignored"), session)
        first = await store.read_since(-1)
        assert [event.stream_sequence for event in first.events] == [0]
        assert [event.run_epoch for event in first.events] == [1]

        queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
        tailer._subscribers.add(queue)
        adapter.snapshot = _snapshot(
            ("u1", "user", "hello"),
            ("a1", "assistant", "world"),
        )
        await tailer._tail_snapshot(Path("/ignored"), session)

        second = await store.read_since(-1)
        assert [event.stream_sequence for event in second.events] == [0, 1]
        assert [event.payload["text"] for event in second.events[1:]] == ["world"]
        assert second.events[1].run_epoch == 1
        assert (await queue.get()).stream_sequence == 1

    asyncio.run(run())


def test_snapshot_tailer_fails_closed_on_rewritten_history(store: AgentStreamStore):
    async def run() -> None:
        session = _snapshot_session()
        adapter = _SnapshotAdapter(_snapshot(("u1", "user", "hello")))
        tailer = SessionTailer("ws1", "s1", adapter, lambda: session, store=store)

        await tailer._tail_snapshot(Path("/ignored"), session)
        adapter.snapshot = _snapshot(("u2", "user", "rewritten"))
        await tailer._tail_snapshot(Path("/ignored"), session)

        assert tailer.hard_failed is True
        page = await store.read_since(-1)
        assert [event.payload["summary"] for event in page.events] == ["hello"]

    asyncio.run(run())


def test_snapshot_tailer_allows_cursor_style_replacement_of_tail_end_marker(
    store: AgentStreamStore,
):
    async def run() -> None:
        session = _snapshot_session()
        adapter = _SnapshotAdapter(
            _snapshot(
                ("u1", "user", "first"),
                ("end-1", "turn_ended", ""),
            )
        )
        tailer = SessionTailer("ws1", "s1", adapter, lambda: session, store=store)

        await tailer._tail_snapshot(Path("/ignored"), session)
        adapter.snapshot = _snapshot(
            ("u1", "user", "first"),
            ("u2", "user", "second"),
        )
        await tailer._tail_snapshot(Path("/ignored"), session)

        assert tailer.hard_failed is False
        page = await store.read_since(-1)
        assert [event.type for event in page.events] == [
            AgentStreamEventType.TURN_STARTED,
            AgentStreamEventType.TURN_COMPLETED,
            AgentStreamEventType.TURN_STARTED,
        ]
        assert [event.run_epoch for event in page.events] == [1, 1, 2]

    asyncio.run(run())


# ── registry / fail-closed ───────────────────────────────────────────────────


def test_registry_claude_and_codex_supported():
    assert supports_structured(AgentType.CLAUDE) is True
    assert supports_structured(AgentType.CODEX) is True


def test_registry_cursor_has_a_structured_adapter():
    assert supports_structured(AgentType.CURSOR) is True
    assert get_adapter(AgentType.CURSOR) is not None


def test_get_adapter_for_session_cursor_terminal_transport_fail_closed():
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="s1",
        workspace_id="ws1",
        tab_id="t1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CURSOR,
        status=ManagedSessionStatus.IDLE,
        title="cursor",
        workspace_path="/tmp",
        tmux_session="tmux-s1",
        cursor_transport="terminal",
        created_at=now,
        updated_at=now,
    )
    assert get_adapter_for_session(session) is None


# ── Claude adapter normalization ─────────────────────────────────────────────


def _ctx() -> NormalizeContext:
    return NormalizeContext(
        session_id="s1",
        tab_id="t1",
        agent_type=AgentType.CLAUDE,
        run_epoch=0,
    )


def test_claude_adapter_user_prompt_emits_turn_started():
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    raw = {"type": "user", "message": {"content": "hello"}}
    events = adapter.normalize_line(raw, _ctx())
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_STARTED
    assert events[0].payload["summary"] == "hello"


def test_claude_adapter_assistant_text_and_tool_use():
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    raw = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "thinking", "thinking": "let me think"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "Read",
                    "input": {"file": "x.py"},
                },
            ]
        },
    }
    events = adapter.normalize_line(raw, _ctx())
    types = [e.type for e in events]
    assert AgentStreamEventType.TEXT_DELTA in types
    assert AgentStreamEventType.THINKING_DELTA in types
    assert AgentStreamEventType.TOOL_CALL_STARTED in types
    tool_event = next(e for e in events if e.type == AgentStreamEventType.TOOL_CALL_STARTED)
    assert tool_event.call_id == "tu_1"
    assert tool_event.payload["name"] == "Read"


def test_claude_adapter_skips_sidechain_and_meta():
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    raw = {"type": "user", "isSidechain": True, "message": {"content": "x"}}
    assert adapter.normalize_line(raw, _ctx()) == []


# ── Codex adapter normalization ──────────────────────────────────────────────


def test_codex_adapter_user_message_emits_turn_started():
    from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter

    adapter = CodexJsonlAdapter()
    raw = {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}
    events = adapter.normalize_line(raw, _ctx())
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_STARTED
    assert events[0].payload["summary"] == "hi"


def test_codex_adapter_function_call_and_output():
    from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter

    adapter = CodexJsonlAdapter()
    call = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec",
            "call_id": "fc_1",
            "arguments": '{"cmd": "ls"}',
        },
    }
    out = {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "fc_1",
            "output": "file.txt",
        },
    }
    events = adapter.normalize_line(call, _ctx()) + adapter.normalize_line(out, _ctx())
    types = [e.type for e in events]
    assert AgentStreamEventType.TOOL_CALL_STARTED in types
    assert AgentStreamEventType.TOOL_CALL_COMPLETED in types
    started = next(e for e in events if e.type == AgentStreamEventType.TOOL_CALL_STARTED)
    assert started.payload["args"] == {"cmd": "ls"}


def test_codex_adapter_task_complete_emits_turn_completed():
    from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter

    adapter = CodexJsonlAdapter()
    raw = {
        "type": "event_msg",
        "payload": {"type": "task_complete", "last_agent_message": "done"},
    }
    events = adapter.normalize_line(raw, _ctx())
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_COMPLETED
    assert events[0].payload["summary"] == "done"
