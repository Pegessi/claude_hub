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
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentStreamEventPage,
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    StreamCapabilities,
    User,
    WorkspaceSessionRole,
)
from claude_hub.services import workspace_manager
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
from claude_hub.services.agent_stream.tailer import SessionTailer, TailerManager

# ── redaction ────────────────────────────────────────────────────────────────


def test_terminal_tab_stream_session_uses_tab_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct Chat tab is a stream source without a Workspace row."""

    from claude_hub.api import agent_stream as agent_stream_api

    tab = SimpleNamespace(
        id="tab-direct",
        name="Direct Claude",
        cwd="/tmp/direct",
        remote_cwd=None,
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_reconnect=True,
        solo_mode=True,
        env={"SAFE": "value"},
        agent_session_id="provider-session-id",
        cursor_transport="terminal",
        cursor_data_dir=None,
        cursor_cli_version=None,
        cursor_transcript_path=None,
        cursor_transcript_schema=None,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)

    session = agent_stream_api._terminal_tab_stream_session("tab-direct")

    assert session is not None
    assert session.id == "terminal-tab-tab-direct"
    assert session.workspace_id == "terminal-tabs"
    assert session.tab_id == "tab-direct"
    assert session.agent_type == AgentType.CLAUDE
    assert session.agent_session_id == "provider-session-id"
    assert session.workspace_path == "/tmp/direct"
    assert get_adapter_for_session(session) is not None


def test_terminal_tab_stream_session_returns_none_for_missing_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: None)
    assert agent_stream_api._terminal_tab_stream_session("missing") is None


def test_tab_mode_persistence_strips_stream_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    persisted: List[Tuple[str, str]] = []
    monkeypatch.setattr(
        agent_stream_api.ttyd_manager,
        "set_tab_chat_mode",
        lambda tab_id, mode: persisted.append((tab_id, mode)) or True,
    )

    agent_stream_api._persist_tab_chat_mode("terminal-tab-real-tab-id", "plan")

    assert persisted == [("real-tab-id", "plan")]


def test_terminal_tab_stream_session_uses_backend_cwd_when_tab_cwd_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    tab = SimpleNamespace(
        id="tab-inherited-cwd",
        name="Inherited cwd",
        cwd=None,
        remote_cwd=None,
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_reconnect=True,
        solo_mode=True,
        env={},
        agent_session_id="provider-session-id",
        cursor_transport="terminal",
        cursor_data_dir=None,
        cursor_cli_version=None,
        cursor_transcript_path=None,
        cursor_transcript_schema=None,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)
    monkeypatch.setattr(agent_stream_api.os, "getcwd", lambda: "/preview/backend")

    session = agent_stream_api._terminal_tab_stream_session("tab-inherited-cwd")

    assert session is not None
    assert session.workspace_path == "/preview/backend"


def test_verified_flag_survives_get_tab_to_native_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a persisted ``agent_session_id_verified=True`` must flow
    through ``TTYDManager.get_tab()`` -> ``TerminalTab`` schema ->
    ``_terminal_tab_stream_session`` -> ``ClaudeNativeSession`` so the first
    turn after a cold restart uses ``--resume`` (not ``--session-id``).

    This guards against the schema/to_schema gap that previously dropped
    ``agent_session_id_verified`` between the persisted ``TTYDProcess`` and
    the ``ManagedSession`` handed to the native provider.
    """
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.agent_stream.native import ClaudeNativeSession
    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    process = TTYDProcess(
        tab_id="tab-resume",
        port=12499,
        name="Resume Claude",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        agent_session_id="captured-conv-id",
        agent_session_id_verified=True,
    )

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {"tab-resume": process}

    tab = manager.get_tab("tab-resume")
    assert tab is not None
    # The schema must carry the verified flag through to_schema().
    assert tab.agent_session_id_verified is True
    assert tab.agent_session_id == "captured-conv-id"

    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)

    session = agent_stream_api._terminal_tab_stream_session("tab-resume")
    assert session is not None
    assert session.agent_session_id_verified is True
    assert session.agent_session_id == "captured-conv-id"

    native = ClaudeNativeSession(session)
    assert native._conversation_id_verified is True
    cmd = native._build_command()
    assert "--resume" in cmd
    assert "captured-conv-id" in cmd
    assert "--session-id" not in cmd


def test_unverified_flag_uses_session_id_not_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the above: an unverified (constructive) id must use
    ``--session-id`` even when it flows through the same get_tab -> schema ->
    session chain."""
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.agent_stream.native import ClaudeNativeSession
    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    process = TTYDProcess(
        tab_id="tab-constructive",
        port=12498,
        name="Constructive Claude",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        agent_session_id="constructive-id",
        agent_session_id_verified=False,
    )

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {"tab-constructive": process}

    tab = manager.get_tab("tab-constructive")
    assert tab is not None
    assert tab.agent_session_id_verified is False

    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)

    session = agent_stream_api._terminal_tab_stream_session("tab-constructive")
    assert session is not None
    assert session.agent_session_id_verified is False

    native = ClaudeNativeSession(session)
    assert native._conversation_id_verified is False
    cmd = native._build_command()
    assert "--session-id" in cmd
    assert "constructive-id" in cmd
    assert "--resume" not in cmd


# ── Cursor --model flag + live env propagation ──────────────────────────────


def test_cursor_native_build_command_forwards_model_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CURSOR_MODEL`` in the tab env must reach the Cursor CLI as a
    ``--model`` flag — the CLI ignores the env var, so the native transport
    must translate it into a CLI argument."""
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.agent_stream.native import CursorNativeSession
    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    process = TTYDProcess(
        tab_id="tab-cursor-model",
        port=12501,
        name="Cursor Model",
        agent_type=AgentType.CURSOR,
        session_kind=SessionKind.CHAT,
        env={"CURSOR_MODEL": "gpt-5.2"},
    )

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {"tab-cursor-model": process}

    tab = manager.get_tab("tab-cursor-model")
    assert tab is not None
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)

    session = agent_stream_api._terminal_tab_stream_session("tab-cursor-model")
    assert session is not None
    assert session.env.get("CURSOR_MODEL") == "gpt-5.2"

    native = CursorNativeSession(session)
    cmd = native._build_command()
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.2"


def test_cursor_native_build_command_omits_model_flag_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``CURSOR_MODEL`` in the tab env, no ``--model`` flag must be
    appended (the CLI picks its own default)."""
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.agent_stream.native import CursorNativeSession
    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    process = TTYDProcess(
        tab_id="tab-cursor-no-model",
        port=12502,
        name="Cursor No Model",
        agent_type=AgentType.CURSOR,
        session_kind=SessionKind.CHAT,
        env={},
    )

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {"tab-cursor-no-model": process}

    tab = manager.get_tab("tab-cursor-no-model")
    assert tab is not None
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)

    session = agent_stream_api._terminal_tab_stream_session("tab-cursor-no-model")
    assert session is not None

    native = CursorNativeSession(session)
    cmd = native._build_command()
    assert "--model" not in cmd


def test_provider_session_update_env_replaces_session_env() -> None:
    """``update_env`` must fully replace (not merge) the session env so the
    next turn picks up exactly the new values."""
    from claude_hub.services.agent_stream.native import ClaudeNativeSession

    session = _native_session()
    session.env = {"A": "1"}
    native = ClaudeNativeSession(session)

    native.update_env({"A": "2", "B": "3"})

    assert native.session.env == {"A": "2", "B": "3"}


def test_tailer_manager_set_env_propagates_to_live_transport() -> None:
    """``set_env`` must forward the new env to the tailer's live native
    transport so it takes effect on the next turn."""
    from claude_hub.services.agent_stream.tailer import TailerManager

    session = SimpleNamespace(id="sess-env-live")
    transport = MagicMock()
    tailer = SessionTailer(
        "ws-env",
        session.id,
        MagicMock(),
        lambda: None,
        native_transport=transport,
    )
    manager = TailerManager.__new__(TailerManager)
    manager._tailers = {session.id: tailer}

    manager.set_env(session, {"FOO": "bar"})

    transport.update_env.assert_called_once_with({"FOO": "bar"})


def test_tailer_manager_set_env_skips_when_tailer_missing_or_errored() -> None:
    """``set_env`` must no-op (neither raise nor call ``update_env``) when
    there is no tailer for the session, or when the tailer's native transport
    failed to start (``native_error`` set)."""
    from claude_hub.services.agent_stream.tailer import TailerManager

    # Case (a): no tailer registered for the session id — must not raise and
    # must not touch an unrelated session's transport.
    other_transport = MagicMock()
    other_tailer = SessionTailer(
        "ws-env",
        "sess-other",
        MagicMock(),
        lambda: None,
        native_transport=other_transport,
    )
    session_a = SimpleNamespace(id="sess-env-missing")
    manager_a = TailerManager.__new__(TailerManager)
    manager_a._tailers = {"sess-other": other_tailer}
    manager_a.set_env(session_a, {"FOO": "bar"})  # must not raise
    other_transport.update_env.assert_not_called()

    # Case (b): tailer exists but its native transport failed to start.
    session_b = SimpleNamespace(id="sess-env-errored")
    transport_b = MagicMock()
    tailer_b = SessionTailer(
        "ws-env",
        session_b.id,
        MagicMock(),
        lambda: None,
        native_transport=transport_b,
        native_error="boom",
    )
    manager_b = TailerManager.__new__(TailerManager)
    manager_b._tailers = {session_b.id: tailer_b}
    manager_b.set_env(session_b, {"FOO": "bar"})
    transport_b.update_env.assert_not_called()


@pytest.mark.asyncio
async def test_tab_capability_keeps_empty_claude_composer_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first prompt creates the transcript, so no source is not Raw-only."""

    from claude_hub.api import agent_stream as agent_stream_api

    session = _sse_session("terminal-tabs", "terminal-tab-empty")
    adapter = MagicMock()
    adapter.capabilities.return_value = StreamCapabilities(
        structured=False,
        adapter_id="claude-jsonl",
        schema_version=1,
        sources=[],
    )
    manager = MagicMock()
    manager.hard_failed.return_value = False
    monkeypatch.setattr(agent_stream_api, "get_adapter_for_session", lambda _session: adapter)

    caps = await agent_stream_api._tab_capabilities_for(session, manager)

    assert caps.structured is True
    assert caps.sources == []


@pytest.mark.asyncio
async def test_retry_stream_replaces_tailer_before_rechecking_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    session = _sse_session("terminal-tabs", "terminal-tab-retry")
    manager = MagicMock()
    manager.retry = AsyncMock()
    expected = StreamCapabilities(
        structured=True,
        adapter_id="codex-native",
        schema_version=1,
        sources=[],
    )
    capability_probe = AsyncMock(return_value=expected)
    monkeypatch.setattr(agent_stream_api, "_tab_capabilities_for", capability_probe)

    result = await agent_stream_api._retry_stream_for(session, manager, direct_tab=True)

    manager.retry.assert_awaited_once_with(session)
    capability_probe.assert_awaited_once_with(session, manager)
    assert result == expected


@pytest.mark.asyncio
async def test_set_stream_mode_returns_updated_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    session = _sse_session("terminal-tabs", "terminal-tab-mode").model_copy(
        update={"session_kind": SessionKind.CHAT}
    )
    manager = MagicMock()
    manager.set_mode = AsyncMock()
    expected = StreamCapabilities(
        structured=True,
        adapter_id="claude-native",
        schema_version=1,
        available_modes=[
            {"id": "default", "label": "Default", "description": "Normal"},
            {"id": "plan", "label": "Plan", "description": "Read only"},
        ],
        current_mode="plan",
        supports_dynamic_modes=True,
    )
    monkeypatch.setattr(
        agent_stream_api,
        "_tab_capabilities_for",
        AsyncMock(return_value=expected),
    )

    result = await agent_stream_api._set_stream_mode_for(
        session,
        manager,
        "plan",
        direct_tab=True,
    )

    manager.set_mode.assert_awaited_once_with(session, "plan")
    assert result == expected


@pytest.mark.asyncio
async def test_set_stream_mode_rejects_in_flight_turn() -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    session = _sse_session("terminal-tabs", "terminal-tab-busy").model_copy(
        update={"session_kind": SessionKind.CHAT}
    )
    manager = MagicMock()
    manager.set_mode = AsyncMock(
        side_effect=RuntimeError("cannot change Chat mode while a turn is in flight")
    )

    with pytest.raises(HTTPException) as exc_info:
        await agent_stream_api._set_stream_mode_for(session, manager, "plan")

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_workspace_terminal_session_rejects_chat_mode_mutation() -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    session = _sse_session("workspace-managed", "managed-terminal")
    manager = MagicMock()
    manager.set_mode = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await agent_stream_api._set_stream_mode_for(session, manager, "plan")

    assert exc_info.value.status_code == 400
    manager.set_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_reaped_tailer_restarts_before_setting_mode(
    monkeypatch: pytest.MonkeyPatch,
    store: AgentStreamStore,
) -> None:
    from claude_hub.services.agent_stream import native as native_module
    from claude_hub.services.agent_stream.native import ClaudeNativeSession
    from claude_hub.services.agent_stream.tailer import TailerManager

    session = _sse_session("ws-restart", "chat-restart").model_copy(
        update={"session_kind": SessionKind.CHAT}
    )
    transport = ClaudeNativeSession(session)
    adapter = MagicMock()
    tailer = SessionTailer(
        "ws-restart",
        "chat-restart",
        adapter,
        lambda: session,
        store=store,
        native_transport=transport,
    )
    tailer._stopped = True
    tailer._task = None
    persisted: List[Tuple[str, str]] = []
    manager = TailerManager(
        lambda _session_id: session,
        persist_mode=lambda session_id, mode: persisted.append((session_id, mode)),
    )
    manager._tailers[session.id] = tailer
    monkeypatch.setattr(native_module, "_help_confirms_plan_mode", lambda *_args: True)

    await manager.set_mode(session, "plan")

    assert tailer.is_running()
    assert transport.capabilities().current_mode == "plan"
    assert persisted == [(session.id, "plan")]
    await tailer.stop()


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


def test_store_sequential_pages_resume_from_cached_file_offset(
    store: AgentStreamStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later page must not scan and parse the JSONL prefix again."""

    async def run() -> None:
        for i in range(8):
            await store.append(_event(text=str(i)))

        original_open = Path.open
        readers: List[Any] = []

        class CountingReader:
            def __init__(self, handle: Any) -> None:
                self.handle = handle
                self.lines_read = 0
                self.seek_offsets: List[int] = []

            def __enter__(self) -> "CountingReader":
                self.handle.__enter__()
                return self

            def __exit__(self, *args: Any) -> Any:
                return self.handle.__exit__(*args)

            def __iter__(self) -> "CountingReader":
                return self

            def __next__(self) -> str:
                line = next(self.handle)
                self.lines_read += 1
                return line

            def readline(self, *args: Any) -> str:
                line = self.handle.readline(*args)
                if line:
                    self.lines_read += 1
                return line

            def seek(self, offset: int, *args: Any) -> int:
                self.seek_offsets.append(offset)
                return self.handle.seek(offset, *args)

            def __getattr__(self, name: str) -> Any:
                return getattr(self.handle, name)

        def tracked_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            handle = original_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == store.path and mode == "r":
                reader = CountingReader(handle)
                readers.append(reader)
                return reader
            return handle

        monkeypatch.setattr(Path, "open", tracked_open)

        first = await store.read_since(-1, limit=2)
        second = await store.read_since(first.next_sequence, limit=2)

        assert [event.payload["text"] for event in second.events] == ["2", "3"]
        assert len(readers) == 2
        assert readers[1].seek_offsets and readers[1].seek_offsets[0] > 0
        # Two returned rows plus one look-ahead row for has_more.
        assert readers[1].lines_read == 3

    asyncio.run(run())


def test_tailer_manager_reuses_active_tailer_store(store: AgentStreamStore) -> None:
    async def run() -> None:
        session = _snapshot_session()
        tailer = SessionTailer(
            session.workspace_id,
            session.id,
            MagicMock(),
            lambda: session,
            store=store,
        )
        manager = TailerManager(lambda _session_id: session)
        manager._tailers[session.id] = tailer

        assert manager.get_store(session.workspace_id, session.id) is store

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


def test_store_cached_read_offset_sees_events_appended_after_eof(store: AgentStreamStore) -> None:
    async def run() -> None:
        for i in range(3):
            await store.append(_event(text=str(i)))
        initial = await store.read_since(-1, limit=10)
        assert initial.next_sequence == 2

        appended = await store.append(_event(text="new"))
        resumed = await store.read_since(initial.next_sequence, limit=10)

        assert appended.stream_sequence == 3
        assert [event.payload["text"] for event in resumed.events] == ["new"]
        assert resumed.next_sequence == 3

    asyncio.run(run())


def test_store_replace_all_invalidates_cached_read_offsets(store: AgentStreamStore) -> None:
    async def run() -> None:
        for i in range(5):
            await store.append(_event(text=f"old-{i}"))
        await store.read_since(-1, limit=2)

        await store.replace_all([_event(text="replacement")])
        replaced = await store.read_since(-1, limit=10)

        assert [event.stream_sequence for event in replaced.events] == [0]
        assert [event.payload["text"] for event in replaced.events] == ["replacement"]

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


def test_snapshot_tailer_backfill_does_not_fan_out_then_live_delta_does(store: AgentStreamStore):
    """Cold-start backfill must persist without fanning out; subsequent live
    deltas must fan out.

    Subscribers replay history from the store, so fanning out backfill events
    would cause duplicate delivery. The coalescer must be drained before
    ``_is_live`` flips so buffered historical text is not emitted as live.
    """

    async def run() -> None:
        session = _snapshot_session()
        # Snapshot with a user turn and an assistant text delta.
        adapter = _SnapshotAdapter(
            _snapshot(
                ("u1", "user", "hello"),
                ("a1", "assistant", "world"),
            )
        )
        tailer = SessionTailer("ws1", "s1", adapter, lambda: session, store=store)

        queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
        tailer._subscribers.add(queue)

        # First call: cold-start backfill. Events must be persisted but NOT
        # fanned out to the subscriber.
        await tailer._tail_snapshot(Path("/ignored"), session)

        assert queue.empty(), "backfill events must not be fanned out"
        page = await store.read_since(-1)
        persisted_texts = [e.payload.get("text") for e in page.events if e.payload.get("text")]
        assert "world" in persisted_texts

        # Second call: a new assistant delta arrives. This is a live update and
        # must be fanned out to the subscriber.
        adapter.snapshot = _snapshot(
            ("u1", "user", "hello"),
            ("a1", "assistant", "world"),
            ("a2", "assistant", "!"),
        )
        await tailer._tail_snapshot(Path("/ignored"), session)

        fanned = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert fanned.payload.get("text") == "!"
        assert fanned.stream_sequence > 0

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


# ── Cursor native stream normalization ──────────────────────────────────────


def _cursor_ctx() -> NormalizeContext:
    return NormalizeContext(
        session_id="cursor-session",
        tab_id="cursor-tab",
        agent_type=AgentType.CURSOR,
        run_epoch=1,
        turn_id="cursor-turn",
    )


def _cursor_assistant(text: str, *, timestamped: bool = True) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    if timestamped:
        row["timestamp_ms"] = 1
    return row


def test_cursor_timestamped_full_message_replay_is_not_duplicated() -> None:
    """Resumed Cursor turns can timestamp both deltas and the full replay."""

    from claude_hub.services.agent_stream.cursor_cli_transcript import (
        CursorCliTranscriptAdapter,
    )

    adapter = CursorCliTranscriptAdapter()
    ctx = _cursor_ctx()
    chunks = ["**智涌今朝**\n\n", "千机灯火彻深更，  \n", "代码为诗一瞬成。"]

    emitted = []
    for chunk in chunks:
        emitted.extend(adapter.normalize_line(_cursor_assistant(chunk), ctx))
    replay = adapter.normalize_line(_cursor_assistant("".join(chunks)), ctx)

    assert "".join(event.payload["text"] for event in emitted) == "".join(chunks)
    assert replay == []


def test_cursor_multiple_assistant_message_snapshots_are_each_deduplicated() -> None:
    """One turn may contain separate poem and explanation assistant rows."""

    from claude_hub.services.agent_stream.cursor_cli_transcript import (
        CursorCliTranscriptAdapter,
    )

    adapter = CursorCliTranscriptAdapter()
    ctx = _cursor_ctx()
    poem = ["千机灯火", "彻深更"]
    explanation = ["这首诗写", "AI 时代"]

    for chunk in poem:
        assert len(adapter.normalize_line(_cursor_assistant(chunk), ctx)) == 1
    assert adapter.normalize_line(_cursor_assistant("".join(poem), timestamped=False), ctx) == []
    for chunk in explanation:
        assert len(adapter.normalize_line(_cursor_assistant(chunk), ctx)) == 1
    assert (
        adapter.normalize_line(_cursor_assistant("".join(explanation), timestamped=False), ctx)
        == []
    )


def test_cursor_repeated_single_delta_is_preserved() -> None:
    """A repeated token is content, not proof of a multi-chunk snapshot."""

    from claude_hub.services.agent_stream.cursor_cli_transcript import (
        CursorCliTranscriptAdapter,
    )

    adapter = CursorCliTranscriptAdapter()
    ctx = _cursor_ctx()
    first = adapter.normalize_line(_cursor_assistant("哈"), ctx)
    second = adapter.normalize_line(_cursor_assistant("哈"), ctx)

    assert [event.payload["text"] for event in first + second] == ["哈", "哈"]


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


def test_claude_two_assistant_messages_with_tool_no_reconcile_error() -> None:
    """A single turn can contain two assistant messages (pre-tool thinking+tool,
    post-tool thinking+text) separated by a user tool_result. The per-turn
    accumulator must reset at each message_start so the second message's
    thinking snapshot reconciles against its own deltas, not the first
    message's accumulated thinking."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    ctx = _ctx()
    lines = [
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg1"}}},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "pre-tool thinking"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "WebSearch",
                    "input": {},
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": '{"q":"poem"}'},
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "pre-tool thinking"},
                    {"type": "tool_use", "id": "tu1", "name": "WebSearch", "input": {"q": "poem"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": "search result"}
                ]
            },
        },
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg2"}}},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "post-tool thinking"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "final answer"},
            },
        },
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "post-tool thinking"},
                    {"type": "text", "text": "final answer"},
                ]
            },
        },
        {"type": "result", "subtype": "success"},
    ]

    events: List[Any] = []
    for line in lines:
        events.extend(adapter.normalize_line(line, ctx))

    types = [e.type for e in events]
    assert AgentStreamEventType.ERROR not in types
    assert AgentStreamEventType.TURN_COMPLETED in types
    # Both thinking segments and the final text are present.
    thinking_text = "".join(
        e.payload["text"] for e in events if e.type == AgentStreamEventType.THINKING_DELTA
    )
    assert "pre-tool thinking" in thinking_text
    assert "post-tool thinking" in thinking_text
    assistant_text = "".join(
        e.payload["text"] for e in events if e.type == AgentStreamEventType.TEXT_DELTA
    )
    assert assistant_text == "final answer"


def test_claude_genuine_text_mismatch_still_errors() -> None:
    """When the final assistant text snapshot neither matches nor extends the
    streamed deltas (a genuine protocol inconsistency), the adapter must still
    fail closed with an ERROR event rather than silently dropping or
    duplicating text."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    ctx = _ctx()
    lines = [
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg1"}}},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "streamed"},
            },
        },
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "different snapshot"}]},
        },
        {"type": "result", "subtype": "success"},
    ]

    events: List[Any] = []
    for line in lines:
        events.extend(adapter.normalize_line(line, ctx))

    assert any(e.type == AgentStreamEventType.ERROR for e in events)


def test_claude_streamed_tool_use_with_block_index_emits_single_tool_start() -> None:
    """A streaming tool_use block carries its content-block ``index`` on
    ``content_block_start``, every ``input_json_delta``, and
    ``content_block_stop``. The adapter must assemble the partial JSON into the
    tool's arguments and emit exactly one ``TOOL_CALL_STARTED`` at
    ``content_block_stop`` — not one per delta, and not a duplicate from the
    final assistant snapshot."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    ctx = _ctx()
    lines = [
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg1"}}},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "WebSearch",
                    "input": {},
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":"'},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": 'poem"}'},
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "WebSearch", "input": {"q": "poem"}},
                ]
            },
        },
        {"type": "result", "subtype": "success"},
    ]

    events: List[Any] = []
    for line in lines:
        events.extend(adapter.normalize_line(line, ctx))

    tool_starts = [e for e in events if e.type == AgentStreamEventType.TOOL_CALL_STARTED]
    assert len(tool_starts) == 1
    assert tool_starts[0].payload["name"] == "WebSearch"
    assert tool_starts[0].payload["args"] == {"q": "poem"}
    assert tool_starts[0].call_id == "tu1"


def test_claude_tool_snapshot_before_block_stop_emits_single_tool_start() -> None:
    """Claude 2.1.159 emits the top-level assistant tool_use snapshot before
    content_block_stop. The snapshot announces the call and the later block
    stop must be deduplicated; the inverse ordering is covered separately."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    ctx = _ctx()
    lines = [
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "msg1"}},
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "WebSearch",
                    "input": {},
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"q":"poem"}',
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "WebSearch",
                        "input": {"q": "poem"},
                    }
                ],
            },
        },
        {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 1},
        },
    ]

    events: List[Any] = []
    for line in lines:
        events.extend(adapter.normalize_line(line, ctx))

    tool_starts = [e for e in events if e.type == AgentStreamEventType.TOOL_CALL_STARTED]
    assert len(tool_starts) == 1
    assert tool_starts[0].payload["args"] == {"q": "poem"}
    assert tool_starts[0].call_id == "tu1"


def test_claude_malformed_streamed_tool_args_fall_back_to_final_snapshot() -> None:
    """A truncated input_json_delta is not authoritative. The adapter must
    wait for the final assistant snapshot instead of emitting raw partial JSON
    and suppressing the complete tool arguments."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    ctx = _ctx()
    lines = [
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "msg1"}},
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "WebSearch",
                    "input": {},
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":"poem'},
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {
            "type": "assistant",
            "message": {
                "id": "msg1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "WebSearch",
                        "input": {"q": "complete poem query"},
                    }
                ],
            },
        },
    ]

    events: List[Any] = []
    for line in lines:
        events.extend(adapter.normalize_line(line, ctx))

    tool_starts = [e for e in events if e.type == AgentStreamEventType.TOOL_CALL_STARTED]
    assert len(tool_starts) == 1
    assert tool_starts[0].payload["args"] == {"q": "complete poem query"}


def test_claude_final_only_two_assistant_messages_scoped_by_message_id() -> None:
    """Final-only transcripts (no ``stream_event`` wrappers) can contain two
    top-level ``assistant`` rows in one turn with different provider message
    ids — e.g. ``msg_389`` (thinking + tool_use) then ``msg_3b0`` (thinking +
    text) separated by a user ``tool_result``. The adapter must scope
    text/thinking accumulation to the provider message id so the second
    message's thinking snapshot reconciles against its own content, not the
    first message's accumulated thinking. Without this, the second message's
    thinking fails the ``startswith`` check and the turn errors out."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    ctx = _ctx()
    lines = [
        {
            "type": "assistant",
            "message": {
                "id": "msg_389",
                "content": [
                    {"type": "thinking", "thinking": "pre-tool thinking"},
                    {"type": "tool_use", "id": "tu1", "name": "WebSearch", "input": {"q": "poem"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "found"}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_3b0",
                "content": [
                    {"type": "thinking", "thinking": "post-tool thinking"},
                    {"type": "text", "text": "final answer"},
                ],
            },
        },
        {"type": "result", "subtype": "success"},
    ]

    events: List[Any] = []
    for line in lines:
        events.extend(adapter.normalize_line(line, ctx))

    types = [e.type for e in events]
    assert AgentStreamEventType.ERROR not in types
    thinking_text = "".join(
        e.payload["text"] for e in events if e.type == AgentStreamEventType.THINKING_DELTA
    )
    assert "pre-tool thinking" in thinking_text
    assert "post-tool thinking" in thinking_text
    assistant_text = "".join(
        e.payload["text"] for e in events if e.type == AgentStreamEventType.TEXT_DELTA
    )
    assert assistant_text == "final answer"


def test_claude_consecutive_same_message_id_rows_accumulate() -> None:
    """When two top-level ``assistant`` rows share the same provider message
    id (e.g. a thinking row followed by a tool_use row for the same message),
    the accumulator must NOT reset between them — they are fragments of one
    message. Resetting would drop the thinking before the tool_use snapshot
    reconciles."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    adapter = ClaudeJsonlAdapter()
    ctx = _ctx()
    lines = [
        {
            "type": "assistant",
            "message": {"id": "msg_same", "content": [{"type": "thinking", "thinking": "thought"}]},
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_same",
                "content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}}],
            },
        },
        {"type": "result", "subtype": "success"},
    ]

    events: List[Any] = []
    for line in lines:
        events.extend(adapter.normalize_line(line, ctx))

    types = [e.type for e in events]
    assert AgentStreamEventType.ERROR not in types
    thinking_text = "".join(
        e.payload["text"] for e in events if e.type == AgentStreamEventType.THINKING_DELTA
    )
    assert thinking_text == "thought"


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


# ── SSE live stream: session deletion ───────────────────────────────────────


def _sse_session(workspace_id: str, session_id: str) -> ManagedSession:
    now = datetime.now(timezone.utc)
    return ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id="tab-sse",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="sse",
        workspace_path="/tmp",
        tmux_session="tmux-sse",
        created_at=now,
        updated_at=now,
    )


def test_sse_live_stream_terminates_when_session_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live SSE connection must error out (not heartbeat forever) when its
    session is deleted.

    We drive the endpoint's async generator directly (TestClient buffers
    infinite SSE responses and would hang).
    """
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-sse-del"
    session_id = "s-sse-del"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    # Adapter reports structured=True so the stream enters the live loop.
    mock_adapter = MagicMock()
    mock_adapter.capabilities.return_value = StreamCapabilities(
        structured=True, adapter_id="mock", schema_version=1
    )
    monkeypatch.setattr(agent_stream_api, "get_adapter_for_session", lambda s: mock_adapter)

    manager = agent_stream_api._get_tailer_manager()

    async def _fake_subscribe(session: ManagedSession) -> asyncio.Queue:
        return asyncio.Queue()

    monkeypatch.setattr(manager, "subscribe", _fake_subscribe)

    class _EmptyStore:
        async def read_since(self, since: int, limit: int = 200) -> AgentStreamEventPage:
            return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    monkeypatch.setattr(manager, "get_store", lambda ws, sid: _EmptyStore())

    fake_user = User(open_id="local", name="Local", email="local@localhost", avatar_url=None)

    async def run() -> None:
        response = await agent_stream_api.stream_live(
            managed_session_id=session_id,
            since_sequence=-1,
            current_user=fake_user,
        )
        agen = response.body_iterator

        # First chunk is the "hello" event.
        hello = await agen.__anext__()
        assert hello.startswith("event: hello")

        # Delete the session out from under the stream.
        workspace_manager.sessions.pop(session_id, None)

        # The stream must emit an error event and then end (no endless
        # heartbeats). Guard with a timeout so a regression fails fast.
        error_chunk = await asyncio.wait_for(agen.__anext__(), timeout=5.0)
        assert error_chunk.startswith("event: error")
        assert "session was deleted" in error_chunk

        # Generator should terminate after the error event.
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()

    asyncio.run(run())


# ── long-poll wait: session deletion ─────────────────────────────────────────


def test_wait_stream_events_raises_structured_unavailable_when_session_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait_stream_events`` must detect a session removed after endpoint
    entry/subscription and raise the structured-unavailable HTTP error (409)
    promptly — it must not block for the full request timeout.

    Driven directly against the endpoint coroutine (no TestClient, which would
    serialize/deserialize and could mask the raise). The store's ``read_since``
    removes the session as a side effect so the deletion is observed on the
    first loop iteration, right after subscribe.
    """
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-del"
    session_id = "s-wait-del"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    mock_adapter = MagicMock()
    mock_adapter.capabilities.return_value = StreamCapabilities(
        structured=True, adapter_id="mock", schema_version=1
    )
    monkeypatch.setattr(agent_stream_api, "get_adapter_for_session", lambda s: mock_adapter)

    manager = agent_stream_api._get_tailer_manager()

    async def _fake_subscribe(session: ManagedSession) -> asyncio.Queue:
        return asyncio.Queue()

    monkeypatch.setattr(manager, "subscribe", _fake_subscribe)

    class _DeletingStore:
        """On the first read, remove the session out from under the waiter."""

        def __init__(self) -> None:
            self.calls = 0

        async def read_since(self, since: int, limit: int = 200) -> AgentStreamEventPage:
            self.calls += 1
            # Simulate the session being deleted after subscribe but while the
            # waiter is inside its poll loop.
            workspace_manager.sessions.pop(session_id, None)
            return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    deleting_store = _DeletingStore()
    monkeypatch.setattr(manager, "get_store", lambda ws, sid: deleting_store)

    fake_user = User(open_id="local", name="Local", email="local@localhost", avatar_url=None)
    payload = agent_stream_api.AgentStreamWaitRequest(
        since_sequence=-1,
        # Long timeout: if the waiter fails to detect deletion promptly, this
        # test would hang for the full duration. The deletion-detection branch
        # must short-circuit well before it.
        timeout_seconds=30.0,
    )

    async def run() -> None:
        with pytest.raises(HTTPException) as exc_info:
            await agent_stream_api.wait_stream_events(
                managed_session_id=session_id,
                payload=payload,
                current_user=fake_user,
            )
        assert exc_info.value.status_code == 409
        # The store must have been consulted at least once (proving we entered
        # the loop after subscribe) and the session must be gone.
        assert deleting_store.calls >= 1
        assert workspace_manager.sessions.get(session_id) is None

    # Guard against a regression that waits out the full timeout.
    asyncio.run(asyncio.wait_for(run(), timeout=5.0))


# ── Native transport push consumer (SessionTailer._run_native) ───────────────


class _FakeNativeTransport:
    """A minimal ProviderSession stand-in backed by an asyncio.Queue.

    Records pushed onto ``_records`` are returned by ``read_line``. The
    tailer's push consumer awaits ``read_line`` directly — no polling.
    """

    def __init__(self, eof_is_fatal: bool = False) -> None:
        self._started = True
        self._records: asyncio.Queue = asyncio.Queue()
        self.eof_is_fatal = eof_is_fatal
        self.stop_called = False
        self.sent_messages: List[Tuple[str, List[bytes]]] = []
        self._turn_in_flight = False
        # Per-turn exit error surfaced to the tailer on EOF. ``None`` means
        # the last turn exited cleanly.
        self.exit_error: Optional[str] = None
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self.stop_called = True

    async def read_line(self):
        return await self._records.get()

    async def send_message(self, text: str, images: List[bytes]) -> None:
        self._turn_in_flight = True
        self.sent_messages.append((text, images))

    @property
    def turn_in_flight(self) -> bool:
        return self._turn_in_flight

    def acknowledge_turn_complete(self) -> None:
        """Release the turn guard after the tailer consumes the turn-end signal."""
        self._turn_in_flight = False

    def _end_turn(self) -> None:
        self._turn_in_flight = False

    def maybe_capture_conversation_id(self, record) -> None:
        pass


def _native_session() -> ManagedSession:
    return ManagedSession(
        id="sess-native",
        workspace_id="ws-1",
        tab_id="tab-native",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="native",
        workspace_path="/tmp",
        tmux_session="tmux-native",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_native_subscriber_receives_delta_far_below_poll_interval() -> None:
    """A record pushed onto the native transport queue must reach the
    subscriber in well under ``POLL_INTERVAL_S`` (1s), proving the push
    consumer does not batch on a poll timer."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport()
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    # Let the push consumer task start and block on read_line.
    await asyncio.sleep(0.05)

    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    )

    # Must arrive in << 1s. If the tailer polled on POLL_INTERVAL_S this would
    # take up to a full second.
    evt = await asyncio.wait_for(queue.get(), timeout=0.2)
    assert evt.type == AgentStreamEventType.TEXT_DELTA
    assert evt.payload["text"] == "hello"
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_multiple_deltas_before_turn_completed() -> None:
    """Two deltas pushed before any turn_completed must both be delivered,
    proving the consumer does not wait for turn boundaries to fan out."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport()
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "foo"},
            },
        }
    )
    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "bar"},
            },
        }
    )

    first = await asyncio.wait_for(queue.get(), timeout=0.2)
    second = await asyncio.wait_for(queue.get(), timeout=0.2)
    assert first.payload["text"] == "foo"
    assert second.payload["text"] == "bar"
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_runtime_is_idle_on_completion_before_one_shot_process_eof() -> None:
    """Timeline completion is the runtime-status boundary even if Claude's
    one-shot process takes longer to exit.  The send guard is released at
    ``TURN_COMPLETED`` (the provider's final record) so a long-running tool
    call that keeps the subprocess alive does not block the next turn."""

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
    from claude_hub.services.agent_stream.tailer import (
        TailerManager,
        get_tab_native_runtime_snapshot,
    )

    transport = _FakeNativeTransport(eof_is_fatal=False)
    session = _native_session().model_copy(update={"session_kind": SessionKind.CHAT})
    transport.session = session
    tailer = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    manager = TailerManager(lambda _session_id: session)
    manager._tailers[session.id] = tailer
    queue = await tailer.subscribe()

    await tailer.send_message("hello", [], client_turn_id="turn-late-eof")
    assert (await asyncio.wait_for(queue.get(), timeout=0.5)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    transport._records.put_nowait({"type": "result", "subtype": "success"})

    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    # The turn guard is released at TURN_COMPLETED, not at EOF, so a lingering
    # subprocess (e.g. a long-running tool call) does not block the next send.
    assert transport.turn_in_flight is False
    snapshot = get_tab_native_runtime_snapshot(session.tab_id)
    assert snapshot is not None
    assert snapshot.status == AgentRuntimeStatus.IDLE

    # A late nonzero process exit does not overwrite the provider's already
    # persisted successful terminal outcome.
    transport.exit_error = "provider exited with code 1 after completion"
    transport._records.put_nowait(None)
    await asyncio.sleep(0.05)
    assert transport.turn_in_flight is False
    snapshot = get_tab_native_runtime_snapshot(session.tab_id)
    assert snapshot is not None
    assert snapshot.status == AgentRuntimeStatus.IDLE
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_runtime_needs_attention_on_failed_completion_before_eof() -> None:
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
    from claude_hub.services.agent_stream.tailer import (
        TailerManager,
        get_tab_native_runtime_snapshot,
    )

    transport = _FakeNativeTransport(eof_is_fatal=False)
    session = _native_session().model_copy(update={"session_kind": SessionKind.CHAT})
    transport.session = session
    tailer = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    manager = TailerManager(lambda _session_id: session)
    manager._tailers[session.id] = tailer
    queue = await tailer.subscribe()

    await tailer.send_message("hello", [], client_turn_id="turn-failed-late-eof")
    assert (await asyncio.wait_for(queue.get(), timeout=0.5)).type == (
        AgentStreamEventType.TURN_STARTED
    )
    transport._records.put_nowait(
        {"type": "result", "is_error": True, "result": "provider failure"}
    )

    assert (await asyncio.wait_for(queue.get(), timeout=0.5)).type == (AgentStreamEventType.ERROR)
    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    assert completed.payload["status"] == "failed"
    # The turn guard is released at TURN_COMPLETED even for failed turns.
    assert transport.turn_in_flight is False
    snapshot = get_tab_native_runtime_snapshot(session.tab_id)
    assert snapshot is not None
    assert snapshot.status == AgentRuntimeStatus.ATTENTION

    transport._records.put_nowait(None)
    await asyncio.sleep(0.05)
    assert transport.turn_in_flight is False
    snapshot = get_tab_native_runtime_snapshot(session.tab_id)
    assert snapshot is not None
    assert snapshot.status == AgentRuntimeStatus.ATTENTION
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_first_turn_is_fanned_out_not_swallowed_by_backfill() -> None:
    """Native sessions have no transcript backfill. The first turn's events
    must be fanned out to subscribers immediately, not dropped as 'historical'."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport()
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    # The authoritative turn_started is published by send_message before the
    # provider runs. The provider's own message_start (which also maps to
    # turn_started) is skipped so no duplicate turn is created.
    await tailer.send_message("hello", [], client_turn_id="turn-1")

    # Provider records: message_start (skipped), text_delta, message_stop,
    # then the top-level ``result`` record which emits TURN_COMPLETED.
    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"id": "m1", "role": "assistant"}},
        }
    )
    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "hi"},
            },
        }
    )
    transport._records.put_nowait({"type": "stream_event", "event": {"type": "message_stop"}})
    # The top-level ``result`` record is the provider's explicit turn-end
    # marker and is what emits TURN_COMPLETED (not message_stop).
    transport._records.put_nowait({"type": "result"})

    events = []
    for _ in range(3):
        events.append(await asyncio.wait_for(queue.get(), timeout=0.2))
    types = [e.type for e in events]
    assert AgentStreamEventType.TURN_STARTED in types
    assert AgentStreamEventType.TEXT_DELTA in types
    assert AgentStreamEventType.TURN_COMPLETED in types
    # The authoritative turn_started carries the frontend's client_turn_id.
    turn_started = next(e for e in events if e.type == AgentStreamEventType.TURN_STARTED)
    assert turn_started.turn_id == "turn-1"
    assert turn_started.message_id == "turn-1:user"
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_idle_reap_stops_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the tailer has zero subscribers for longer than IDLE_TTL_S, the
    native push consumer must call ``transport.stop()`` before exiting so the
    provider subprocess (e.g. Codex app-server) is reaped."""
    import claude_hub.services.agent_stream.tailer as tailer_mod

    # Make the idle TTL and the read tick near-instant so the test is fast.
    monkeypatch.setattr(tailer_mod, "IDLE_TTL_S", 0.0)
    monkeypatch.setattr(tailer_mod, "POLL_INTERVAL_S", 0.01)

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport()
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    # Subscribe then immediately unsubscribe so _subscribers is empty and the
    # idle timer can fire.
    q = await tailer.subscribe()
    tailer.unsubscribe(q)

    # Wait for the idle reap to fire and stop the transport.
    deadline = asyncio.get_event_loop().time() + 2.0
    while not transport.stop_called and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert transport.stop_called is True
    # The tailer task must have exited.
    assert not tailer.is_running()


@pytest.mark.asyncio
async def test_native_after_idle_stop_resubscribe_restarts_transport() -> None:
    """After an idle reap stops the transport, a fresh subscribe must restart
    it (the tailer re-creates its run loop and the transport is startable)."""
    import claude_hub.services.agent_stream.tailer as tailer_mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tailer_mod, "IDLE_TTL_S", 0.0)
    monkeypatch.setattr(tailer_mod, "POLL_INTERVAL_S", 0.01)

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport()
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )

    # First subscribe → idle reap stops transport.
    q1 = await tailer.subscribe()
    tailer.unsubscribe(q1)
    deadline = asyncio.get_event_loop().time() + 2.0
    while not transport.stop_called and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert transport.stop_called is True
    assert not tailer.is_running()

    # Reset the transport's stop flag and re-subscribe: the tailer must start
    # a new run loop and the transport must be startable again.
    transport.stop_called = False
    transport._started = False
    q2 = await tailer.subscribe()
    await asyncio.sleep(0.05)
    assert tailer.is_running()
    # A pushed record must still be delivered.
    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "restarted"},
            },
        }
    )
    evt = await asyncio.wait_for(q2.get(), timeout=0.2)
    assert evt.payload["text"] == "restarted"
    await tailer.stop()
    monkeypatch.undo()


# ── EOF / nonzero-exit / missing-completion handling ────────────────────────


@pytest.mark.asyncio
async def test_native_nonzero_exit_emits_error_and_failed_turn_completed_once() -> None:
    """A one-shot provider that exits nonzero must surface its bounded stderr
    as an ``error`` event and emit exactly one ``turn_completed(status=failed)``
    for the active turn. The turn guard is released only after the failed
    completion is persisted."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport(eof_is_fatal=False)
    transport.exit_error = "provider exited with code 1: boom"
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    # Start a turn: the tailer publishes an authoritative turn_started and
    # sets _active_turn_id.
    await tailer.send_message("hello", [], client_turn_id="turn-1")
    assert tailer._active_turn_id == "turn-1"

    # Drain the turn_started event from the subscriber queue.
    started = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started.type == AgentStreamEventType.TURN_STARTED

    # The provider exits nonzero without emitting any records (just EOF).
    transport._records.put_nowait(None)

    # The tailer must emit an error event and exactly one failed
    # turn_completed for the active turn.
    error_evt = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert error_evt.type == AgentStreamEventType.ERROR
    assert "provider exited with code 1" in error_evt.payload["message"]

    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    assert completed.payload["status"] == "failed"
    assert completed.turn_id == "turn-1"

    # The turn guard must be released after the failed completion.
    assert tailer._active_turn_id is None
    assert transport.turn_in_flight is False

    # No further events should be queued (exactly one failed completion).
    assert queue.empty()
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_clean_exit_without_completion_emits_failed_turn_completed() -> None:
    """A one-shot provider that exits cleanly (exit_error=None) but never
    emitted a terminal ``turn_completed`` record must still get a failed
    ``turn_completed`` synthesized so the frontend never leaves the turn
    pending."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport(eof_is_fatal=False)
    transport.exit_error = None  # clean exit
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    await tailer.send_message("hello", [], client_turn_id="turn-2")
    started = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started.type == AgentStreamEventType.TURN_STARTED

    # Provider emits a text delta but no turn_completed, then EOF.
    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "partial"},
            },
        }
    )
    transport._records.put_nowait(None)

    delta = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert delta.type == AgentStreamEventType.TEXT_DELTA

    # The tailer synthesizes an error + failed turn_completed because no
    # terminal completion record was seen.
    error_evt = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert error_evt.type == AgentStreamEventType.ERROR
    assert "without a completion record" in error_evt.payload["message"]

    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    assert completed.payload["status"] == "failed"
    assert completed.turn_id == "turn-2"

    assert tailer._active_turn_id is None
    assert transport.turn_in_flight is False
    assert queue.empty()
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_clean_exit_with_completion_does_not_synthesize_failure() -> None:
    """A one-shot provider that exits cleanly AND emitted a terminal
    ``turn_completed`` must NOT get a second (failed) completion synthesized.
    The provider's own completion is the single source of truth."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport(eof_is_fatal=False)
    transport.exit_error = None
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    await tailer.send_message("hello", [], client_turn_id="turn-3")
    started = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started.type == AgentStreamEventType.TURN_STARTED

    # Provider emits a text delta, then a result (turn_completed), then EOF.
    transport._records.put_nowait(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "done"},
            },
        }
    )
    transport._records.put_nowait({"type": "result"})
    transport._records.put_nowait(None)

    delta = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert delta.type == AgentStreamEventType.TEXT_DELTA

    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    # The provider's own completion status (not "failed").
    assert completed.payload.get("status") != "failed"

    # For one-shot providers, _active_turn_id is cleared at EOF (not at
    # turn_completed) so trailing records inherit the correct turn id. Wait
    # for the EOF to be processed.
    await asyncio.sleep(0.05)

    # No synthesized error or second completion.
    assert queue.empty()
    assert tailer._active_turn_id is None
    assert transport.turn_in_flight is False
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_provider_completion_then_nonzero_exit_emits_exactly_one_completion() -> None:
    """A provider that emits a terminal ``result``/``turn_completed`` and then
    exits nonzero must produce exactly ONE terminal completion — the provider's
    own (mapped from ``is_error``/``subtype``). The EOF branch must NOT
    synthesize a second failed ``turn_completed`` just because ``exit_error``
    is set."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport(eof_is_fatal=False)
    transport.exit_error = "provider exited with code 1 after completion"
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    await tailer.send_message("hello", [], client_turn_id="turn-double")
    started = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started.type == AgentStreamEventType.TURN_STARTED

    # Provider emits a successful result (turn_completed status=completed),
    # then the process exits nonzero.
    transport._records.put_nowait({"type": "result", "subtype": "success"})
    transport._records.put_nowait(None)

    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    # Provider's own status, not a synthesized "failed".
    assert completed.payload["status"] == "completed"
    assert completed.turn_id == "turn-double"

    # Wait for EOF processing; no second completion should be synthesized.
    await asyncio.sleep(0.05)
    assert queue.empty()
    assert tailer._active_turn_id is None
    assert transport.turn_in_flight is False
    await tailer.stop()


@pytest.mark.asyncio
async def test_native_provider_error_result_then_nonzero_exit_emits_exactly_one_failed_completion() -> (
    None
):
    """When the provider's ``result`` record carries ``is_error=True`` (or
    ``subtype=error``), the mapped ``turn_completed(status=failed)`` is the
    single terminal event. A subsequent nonzero exit must NOT add a second
    failed completion."""
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter

    transport = _FakeNativeTransport(eof_is_fatal=False)
    transport.exit_error = "provider exited with code 1"
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    await tailer.send_message("hello", [], client_turn_id="turn-err")
    started = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started.type == AgentStreamEventType.TURN_STARTED

    # Provider emits an error result, then exits nonzero.
    transport._records.put_nowait({"type": "result", "is_error": True, "result": "tool failure"})
    transport._records.put_nowait(None)

    # The error event from the result record.
    error_evt = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert error_evt.type == AgentStreamEventType.ERROR
    assert "tool failure" in error_evt.payload["message"]

    # Exactly one turn_completed, status=failed (from the result record).
    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    assert completed.payload["status"] == "failed"
    assert completed.turn_id == "turn-err"

    # No second completion synthesized from the nonzero exit.
    await asyncio.sleep(0.05)
    assert queue.empty()
    assert tailer._active_turn_id is None
    assert transport.turn_in_flight is False
    await tailer.stop()


@pytest.mark.asyncio
async def test_codex_fatal_eof_emits_error_and_failed_turn_completed_once() -> None:
    """For Codex (eof_is_fatal=True), the persistent app-server dying (EOF)
    is fatal. The in-flight turn must get an ``error`` event and exactly one
    failed ``turn_completed``, then the session fails closed."""
    from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter

    transport = _FakeNativeTransport(eof_is_fatal=True)
    session = _native_session()
    session.agent_type = AgentType.CODEX
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=CodexJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    await tailer.send_message("hello", [], client_turn_id="turn-codex-1")
    started = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started.type == AgentStreamEventType.TURN_STARTED

    # The app-server dies mid-turn: EOF with no turn/completed.
    transport._records.put_nowait(None)

    error_evt = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert error_evt.type == AgentStreamEventType.ERROR
    assert "native transport process exited" in error_evt.payload["message"]

    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    assert completed.payload["status"] == "failed"
    assert completed.turn_id == "turn-codex-1"

    # The session must be hard-failed after the app-server dies.
    assert tailer.hard_failed is True
    assert queue.empty()
    await tailer.stop()


@pytest.mark.asyncio
async def test_codex_turn_completed_ack_after_persistence_no_turn_ahead() -> None:
    """For Codex, ``turn/completed`` must clear ``_active_turn_id`` and
    release the turn guard ONLY after the completion event has been persisted
    and fanned out. Otherwise a concurrent ``send_message`` could publish a
    new ``turn_started`` that sequences ahead of the old turn's completion.

    We verify this by checking that the completion event is fanned out
    (arrives on the subscriber queue) before the turn guard is released."""
    from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter

    transport = _FakeNativeTransport(eof_is_fatal=True)
    session = _native_session()
    session.agent_type = AgentType.CODEX
    tailer = SessionTailer(
        workspace_id="ws-1",
        session_id=session.id,
        adapter=CodexJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    queue = await tailer.subscribe()
    await asyncio.sleep(0.05)

    await tailer.send_message("hello", [], client_turn_id="turn-codex-2")
    started = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started.type == AgentStreamEventType.TURN_STARTED
    assert tailer._active_turn_id == "turn-codex-2"

    # The provider emits turn/completed. The tailer must persist+fanout the
    # completion BEFORE clearing _active_turn_id and calling
    # acknowledge_turn_complete.
    transport._records.put_nowait(
        {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": "th-1",
                "turn": {"id": "tu-1", "status": "completed"},
            },
        }
    )

    completed = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert completed.type == AgentStreamEventType.TURN_COMPLETED
    assert completed.turn_id == "turn-codex-2"

    # After the completion is fanned out, the turn guard must be released.
    assert tailer._active_turn_id is None
    assert transport.turn_in_flight is False

    # A new send must now succeed (guard was released after persistence).
    await tailer.send_message("next", [], client_turn_id="turn-codex-3")
    started2 = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert started2.type == AgentStreamEventType.TURN_STARTED
    assert started2.turn_id == "turn-codex-3"
    await tailer.stop()


# ── long-poll wait: bounded queue-drain (no per-tick read_since) ─────────────


def _wait_event(session_id: str, seq: int) -> AgentStreamEvent:
    now = datetime.now(timezone.utc)
    return AgentStreamEvent(
        stream_sequence=seq,
        session_id=session_id,
        tab_id="tab-wait",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TEXT_DELTA,
        turn_id="turn-1",
        message_id="m-1",
        payload={"text": f"chunk-{seq}"},
        created_at=now,
    )


def _setup_wait_manager(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    queue: asyncio.Queue,
    read_since_impl,
):
    """Wire the tailer manager so subscribe returns ``queue`` and get_store
    returns a store whose ``read_since`` is ``read_since_impl``."""
    from claude_hub.api import agent_stream as agent_stream_api

    mock_adapter = MagicMock()
    mock_adapter.capabilities.return_value = StreamCapabilities(
        structured=True, adapter_id="mock", schema_version=1
    )
    monkeypatch.setattr(agent_stream_api, "get_adapter_for_session", lambda s: mock_adapter)

    manager = agent_stream_api._get_tailer_manager()

    async def _fake_subscribe(session: ManagedSession) -> asyncio.Queue:
        return queue

    monkeypatch.setattr(manager, "subscribe", _fake_subscribe)

    class _Store:
        async def read_since(self, since: int, limit: int = 200) -> AgentStreamEventPage:
            return await read_since_impl(since, limit)

    monkeypatch.setattr(manager, "get_store", lambda ws, sid: _Store())
    return manager


def test_wait_initial_read_since_then_no_reread_on_health_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the initial catch-up read_since returns empty, repeated health
    poll timeouts must NOT call read_since again. The store scan is O(rows)
    and must not run on every 1s tick."""
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-health"
    session_id = "s-wait-health"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    queue: asyncio.Queue = asyncio.Queue()
    read_calls = {"n": 0}

    async def _read_since(since: int, limit: int = 200) -> AgentStreamEventPage:
        read_calls["n"] += 1
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    _setup_wait_manager(monkeypatch, session_id, queue, _read_since)

    # Shorten the health poll so the test runs fast; we restore it after.
    original_poll = agent_stream_api._SSE_HEALTH_POLL_S
    agent_stream_api._SSE_HEALTH_POLL_S = 0.01
    try:
        payload = agent_stream_api.AgentStreamWaitRequest(
            since_sequence=-1,
            timeout_seconds=0.05,
        )

        async def run() -> AgentStreamEventPage:
            return await agent_stream_api.wait_stream_events(
                managed_session_id=session_id,
                payload=payload,
                current_user=User(open_id="local", name="L", email="l@l", avatar_url=None),
            )

        page = asyncio.run(asyncio.wait_for(run(), timeout=5.0))
    finally:
        agent_stream_api._SSE_HEALTH_POLL_S = original_poll

    # Exactly one read_since call: the initial catch-up. The ~5 health ticks
    # that fired during the 50ms window must not have triggered additional
    # scans.
    assert read_calls["n"] == 1
    assert page.events == []


def test_wait_consumes_queue_event_without_read_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the initial read_since is empty and a single contiguous event
    arrives on the subscriber queue, the waiter must return it directly
    without calling read_since again."""
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-queue"
    session_id = "s-wait-queue"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    queue: asyncio.Queue = asyncio.Queue()
    read_calls = {"n": 0}

    async def _read_since(since: int, limit: int = 200) -> AgentStreamEventPage:
        read_calls["n"] += 1
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    _setup_wait_manager(monkeypatch, session_id, queue, _read_since)

    # Push one event contiguous with the cursor (since=-1 → seq 0).
    queue.put_nowait(_wait_event(session_id, 0))

    payload = agent_stream_api.AgentStreamWaitRequest(
        since_sequence=-1,
        timeout_seconds=1.0,
    )

    async def run() -> AgentStreamEventPage:
        return await agent_stream_api.wait_stream_events(
            managed_session_id=session_id,
            payload=payload,
            current_user=User(open_id="local", name="L", email="l@l", avatar_url=None),
        )

    page = asyncio.run(asyncio.wait_for(run(), timeout=5.0))

    # Only the initial read_since ran; the queue event was consumed directly.
    assert read_calls["n"] == 1
    assert len(page.events) == 1
    assert page.events[0].stream_sequence == 0
    assert page.next_sequence == 0


def test_wait_skips_stale_overlap_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events on the queue whose sequence is <= since must be skipped
    (they were already delivered). The waiter must not return them and must
    not call read_since for them."""
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-stale"
    session_id = "s-wait-stale"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    queue: asyncio.Queue = asyncio.Queue()
    read_calls = {"n": 0}

    async def _read_since(since: int, limit: int = 200) -> AgentStreamEventPage:
        read_calls["n"] += 1
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    _setup_wait_manager(monkeypatch, session_id, queue, _read_since)

    # since=5; push stale events (seq 3, 5) then a fresh contiguous one (6).
    for seq in (3, 5, 6):
        queue.put_nowait(_wait_event(session_id, seq))

    payload = agent_stream_api.AgentStreamWaitRequest(
        since_sequence=5,
        timeout_seconds=1.0,
    )

    async def run() -> AgentStreamEventPage:
        return await agent_stream_api.wait_stream_events(
            managed_session_id=session_id,
            payload=payload,
            current_user=User(open_id="local", name="L", email="l@l", avatar_url=None),
        )

    page = asyncio.run(asyncio.wait_for(run(), timeout=5.0))

    assert read_calls["n"] == 1
    assert len(page.events) == 1
    assert page.events[0].stream_sequence == 6
    assert page.next_sequence == 6


def test_wait_drains_contiguous_queue_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the first queued event is contiguous (seq == since+1), the waiter
    must drain all further contiguous events already in the queue in one
    batch, up to the limit, without calling read_since again."""
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-drain"
    session_id = "s-wait-drain"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    queue: asyncio.Queue = asyncio.Queue()
    read_calls = {"n": 0}

    async def _read_since(since: int, limit: int = 200) -> AgentStreamEventPage:
        read_calls["n"] += 1
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    _setup_wait_manager(monkeypatch, session_id, queue, _read_since)

    # since=9 → contiguous events 10..14 already queued.
    for seq in range(10, 15):
        queue.put_nowait(_wait_event(session_id, seq))

    payload = agent_stream_api.AgentStreamWaitRequest(
        since_sequence=9,
        timeout_seconds=1.0,
    )

    async def run() -> AgentStreamEventPage:
        return await agent_stream_api.wait_stream_events(
            managed_session_id=session_id,
            payload=payload,
            current_user=User(open_id="local", name="L", email="l@l", avatar_url=None),
        )

    page = asyncio.run(asyncio.wait_for(run(), timeout=5.0))

    assert read_calls["n"] == 1
    assert [e.stream_sequence for e in page.events] == [10, 11, 12, 13, 14]
    assert page.next_sequence == 14


def test_wait_gap_falls_back_to_single_read_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the first queued event is NOT contiguous (seq > since+1), the
    waiter must reconcile by calling read_since exactly once and returning
    its page. This covers subscriber-queue overflow where events were
    dropped."""
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-gap"
    session_id = "s-wait-gap"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    queue: asyncio.Queue = asyncio.Queue()
    read_calls = {"n": 0}

    # The gap-fallback read_since returns the missing events. The initial
    # catch-up call must return empty so we reach the queue.
    async def _read_since(since: int, limit: int = 200) -> AgentStreamEventPage:
        read_calls["n"] += 1
        if read_calls["n"] == 1:
            return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)
        events = [_wait_event(session_id, since + 1)]
        return AgentStreamEventPage(events=events, next_sequence=since + 1, has_more=False)

    _setup_wait_manager(monkeypatch, session_id, queue, _read_since)

    # since=9 but the queue jumps to seq 12 (10 and 11 were dropped).
    queue.put_nowait(_wait_event(session_id, 12))

    payload = agent_stream_api.AgentStreamWaitRequest(
        since_sequence=9,
        timeout_seconds=1.0,
    )

    async def run() -> AgentStreamEventPage:
        return await agent_stream_api.wait_stream_events(
            managed_session_id=session_id,
            payload=payload,
            current_user=User(open_id="local", name="L", email="l@l", avatar_url=None),
        )

    page = asyncio.run(asyncio.wait_for(run(), timeout=5.0))

    # Initial read_since (empty) + one gap-fallback read_since = 2 calls.
    assert read_calls["n"] == 2
    assert len(page.events) == 1
    assert page.events[0].stream_sequence == 10


def test_wait_drains_full_batch_sets_has_more_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When more than 200 contiguous events are queued, the waiter must
    return exactly 200 and set ``has_more=True`` so consumers (e.g. the
    hydration loop) know more events are immediately available."""
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-fullbatch"
    session_id = "s-wait-fullbatch"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    queue: asyncio.Queue = asyncio.Queue()
    read_calls = {"n": 0}

    async def _read_since(since: int, limit: int = 200) -> AgentStreamEventPage:
        read_calls["n"] += 1
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    _setup_wait_manager(monkeypatch, session_id, queue, _read_since)

    # since=-1 → push 250 contiguous events (seq 0..249).
    for seq in range(250):
        queue.put_nowait(_wait_event(session_id, seq))

    payload = agent_stream_api.AgentStreamWaitRequest(
        since_sequence=-1,
        timeout_seconds=1.0,
    )

    async def run() -> AgentStreamEventPage:
        return await agent_stream_api.wait_stream_events(
            managed_session_id=session_id,
            payload=payload,
            current_user=User(open_id="local", name="L", email="l@l", avatar_url=None),
        )

    page = asyncio.run(asyncio.wait_for(run(), timeout=5.0))

    # Exactly the batch limit, has_more signals remaining queued events.
    assert read_calls["n"] == 1
    assert len(page.events) == 200
    assert page.events[0].stream_sequence == 0
    assert page.events[-1].stream_sequence == 199
    assert page.next_sequence == 199
    assert page.has_more is True


def test_wait_drain_skips_duplicate_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a duplicate sequence (== next_seq) appears mid-drain, the waiter
    must skip it and continue draining the remaining contiguous events
    rather than treating it as a gap."""
    from claude_hub.api import agent_stream as agent_stream_api

    workspace_id = "ws-wait-dup"
    session_id = "s-wait-dup"
    workspace_manager.sessions[session_id] = _sse_session(workspace_id, session_id)

    queue: asyncio.Queue = asyncio.Queue()
    read_calls = {"n": 0}

    async def _read_since(since: int, limit: int = 200) -> AgentStreamEventPage:
        read_calls["n"] += 1
        return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)

    _setup_wait_manager(monkeypatch, session_id, queue, _read_since)

    # since=9; push 10, 11, duplicate 11, 12, 13.
    for seq in (10, 11, 11, 12, 13):
        queue.put_nowait(_wait_event(session_id, seq))

    payload = agent_stream_api.AgentStreamWaitRequest(
        since_sequence=9,
        timeout_seconds=1.0,
    )

    async def run() -> AgentStreamEventPage:
        return await agent_stream_api.wait_stream_events(
            managed_session_id=session_id,
            payload=payload,
            current_user=User(open_id="local", name="L", email="l@l", avatar_url=None),
        )

    page = asyncio.run(asyncio.wait_for(run(), timeout=5.0))

    # Duplicate seq 11 is skipped; contiguous prefix 10..13 returned.
    assert read_calls["n"] == 1
    assert [e.stream_sequence for e in page.events] == [10, 11, 12, 13]
    assert page.next_sequence == 13
    assert page.has_more is False
