"""Recovery and consistency tests for edit-resend.

Covers the critical data-loss bugs fixed in the edit-resend path:

1. Send-failure recovery  — a failed send restores both the Hub event store
   and the provider transcript to their pre-edit state (byte-identical).
2. Fork-failure recovery  — a raised ``fork_transcript`` restores both files.
3. Truncation boundary    — on success, turns after the edited turn are
   removed and the edited text is delivered.
4. Concurrent edit        — two simultaneous edit-resend calls are serialized
   by a per-session lock; they do not interleave.
5. Unmappable turn        — a turn whose provider delivery failed has no
   matching transcript user message and is rejected before truncation.

The tailer creation (``_get_or_create``) and transcript discovery
(``discover_source_cached``) are mocked so the tests exercise the
truncate/fork/send/recovery logic without spawning real provider subprocesses.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import TailerManager
from claude_hub.services.agent_stream.transcript_fork import TranscriptForkError

# ``claude_hub.services.workspace_manager`` is shadowed on the package by the
# ``workspace_manager`` singleton (see claude_hub/services/__init__.py), so the
# module must be fetched via importlib to reach its ``STATE_ROOT`` attribute.
wm_module = importlib.import_module("claude_hub.services.workspace_manager")


# ── helpers ──────────────────────────────────────────────────────────────────


def _session(session_id: str = "sess-1") -> ManagedSession:
    return ManagedSession(
        id=session_id,
        workspace_id="ws-1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="test",
        workspace_path="/tmp",
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


async def _append_turn(
    store: AgentStreamStore,
    turn_id: str,
    text: str,
    event_type: AgentStreamEventType = AgentStreamEventType.TURN_STARTED,
) -> AgentStreamEvent:
    """Append one ``turn_started`` (or other) event to the Hub store."""
    event = AgentStreamEvent(
        stream_sequence=0,  # overwritten by store.append
        session_id=store.session_id,
        tab_id="tab-1",
        agent_type=AgentType.CLAUDE,
        type=event_type,
        turn_id=turn_id,
        message_id=f"{turn_id}:user",
        payload={"summary": text, "attachments": []},
        created_at=datetime.now(timezone.utc),
    )
    return await store.append(event)


def _claude_user_line(text: str) -> str:
    """A Claude-format genuine user message line for the provider transcript."""
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def _write_transcript(path: Path, texts: List[str]) -> None:
    """Write a Claude transcript with one genuine user message per text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for text in texts:
            f.write(_claude_user_line(text) + "\n")


def _patch_discovery(monkeypatch: pytest.MonkeyPatch, transcript_path: Path) -> None:
    """Point ``discover_source_cached`` at ``transcript_path`` in both modules."""
    monkeypatch.setattr(
        "claude_hub.services.agent_stream.tailer.discover_source_cached",
        lambda adapter, session: transcript_path,
    )
    monkeypatch.setattr(
        "claude_hub.services.agent_stream.transcript_fork.discover_source_cached",
        lambda adapter, session: transcript_path,
    )


def _make_manager(
    session: ManagedSession,
    send_side_effect: Optional[BaseException] = None,
) -> tuple[TailerManager, MagicMock]:
    """Create a manager whose tailer factory returns a mock tailer.

    ``send_side_effect`` makes the mock tailer's ``send_message`` raise when
    awaited.  Both ``send_message`` and ``stop`` are ``AsyncMock`` so they are
    awaitable and record calls.
    """
    manager = TailerManager(session_getter=lambda _sid: session)
    mock_tailer = MagicMock()
    mock_tailer.send_message = AsyncMock(side_effect=send_side_effect)
    mock_tailer.stop = AsyncMock()

    async def _fake_get_or_create(sess: ManagedSession) -> MagicMock:
        return mock_tailer

    manager._get_or_create = _fake_get_or_create  # type: ignore[assignment]
    return manager, mock_tailer


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the durable event store at ``tmp_path`` for the test."""
    monkeypatch.setattr(wm_module, "STATE_ROOT", tmp_path)
    return tmp_path


# ── 1. send-failure recovery ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_failure_restores_store_and_transcript(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed send must restore both planes to their pre-edit bytes."""
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    # Populate the Hub store with three turns.
    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    await _append_turn(store, "t2", "msg2")

    store_before = _read_bytes(store.path)
    transcript_before = _read_bytes(transcript_path)

    manager, _mock_tailer = _make_manager(session, send_side_effect=RuntimeError("send failed"))

    with pytest.raises(RuntimeError, match="send failed"):
        await manager.edit_resend(session, "edited msg1", "c-new", "t1")

    # Both planes must be byte-identical to before the edit.
    assert _read_bytes(store.path) == store_before, "event store was not restored"
    assert _read_bytes(transcript_path) == transcript_before, "transcript was not restored"

    # No snapshot sidecars left behind.
    assert not Path(str(store.path) + ".edit-bak").exists()
    assert not Path(str(transcript_path) + ".edit-bak").exists()


# ── 2. fork-failure recovery ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_failure_restores_store_and_transcript(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A raised ``fork_transcript`` must restore both planes."""
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    await _append_turn(store, "t2", "msg2")

    store_before = _read_bytes(store.path)
    transcript_before = _read_bytes(transcript_path)

    # Make fork_transcript raise.
    monkeypatch.setattr(
        "claude_hub.services.agent_stream.tailer.fork_transcript",
        _raising_fork,
    )

    manager, _mock_tailer = _make_manager(session)

    with pytest.raises(TranscriptForkError, match="fork exploded"):
        await manager.edit_resend(session, "edited msg1", "c-new", "t1")

    assert _read_bytes(store.path) == store_before, "event store was not restored"
    assert _read_bytes(transcript_path) == transcript_before, "transcript was not restored"


def _raising_fork(*args: Any, **kwargs: Any) -> int:
    raise TranscriptForkError("fork exploded")


# ── 3. truncation boundary (success) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_success_truncates_after_edited_turn(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On success, turns after the edited turn are removed and the edit is sent."""
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    await _append_turn(store, "t2", "msg2")

    manager, mock_tailer = _make_manager(session)

    await manager.edit_resend(session, "edited msg1", "c-new", "t1")

    # The edited text was delivered.
    mock_tailer.send_message.assert_called_once_with("edited msg1", [], "c-new")

    # Hub store: only t0's turn_started remains (t1 and t2 were truncated).
    page = await store.read_since(-1, limit=100)
    summaries = [
        e.payload.get("summary") for e in page.events if e.type == AgentStreamEventType.TURN_STARTED
    ]
    assert summaries == ["msg0"], f"expected only t0, got {summaries}"

    # Transcript: only the first user message remains.
    kept_texts: List[str] = []
    with transcript_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)
            content = obj["message"]["content"]
            kept_texts.append(content[0]["text"])
    assert kept_texts == ["msg0"], f"expected only msg0, got {kept_texts}"


# ── 4. concurrent edit ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_edits_are_serialized(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two simultaneous edit-resend calls must not interleave.

    The first edit blocks inside ``send_message``; the second must wait for
    the per-session lock rather than starting its own send.  After the first
    completes, the second proceeds (and fails, because the first edit already
    truncated the turn).
    """
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    await _append_turn(store, "t2", "msg2")

    manager = TailerManager(session_getter=lambda _sid: session)

    events: List[str] = []
    send_started = asyncio.Event()
    send_proceed = asyncio.Event()

    async def _blocking_send(text: str, images: List[bytes], client_turn_id: str) -> None:
        events.append(f"send-start:{client_turn_id}")
        send_started.set()
        await send_proceed.wait()
        events.append(f"send-end:{client_turn_id}")

    mock_tailer = MagicMock()
    mock_tailer.send_message = _blocking_send
    mock_tailer.stop = AsyncMock()

    async def _fake_get_or_create(sess: ManagedSession) -> MagicMock:
        return mock_tailer

    manager._get_or_create = _fake_get_or_create  # type: ignore[assignment]

    # Start the first edit; it blocks inside send.
    task1 = asyncio.create_task(manager.edit_resend(session, "edit1", "c1", "t1"))
    await send_started.wait()

    # Start the second edit; it must wait for the lock.
    task2 = asyncio.create_task(manager.edit_resend(session, "edit2", "c2", "t1"))
    await asyncio.sleep(0.1)

    # The second edit must not have started sending while the first is in send.
    assert not any(e == "send-start:c2" for e in events), "second edit interleaved with first"

    # Unblock the first send and let it complete.
    send_proceed.set()
    await task1

    # The second edit now proceeds; the turn was already truncated by the
    # first edit, so it fails with ValueError (turn not found).
    with pytest.raises(ValueError):
        await task2

    # Only the first edit's send happened, and it completed.
    assert events == ["send-start:c1", "send-end:c1"]


# ── 5. unmappable turn ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unmappable_turn_fails_fast_without_truncating(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A turn whose provider delivery failed must be rejected before truncation.

    The Hub store has three turns, but the provider transcript only has user
    messages for t0 and t2 (t1's delivery failed).  Editing t1 must raise
    ``TranscriptForkError`` and leave both planes untouched.
    """
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    # t1's user message is missing from the transcript (delivery failed).
    _write_transcript(transcript_path, ["msg0", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    await _append_turn(store, "t2", "msg2")

    store_before = _read_bytes(store.path)
    transcript_before = _read_bytes(transcript_path)

    manager, _mock_tailer = _make_manager(session)

    with pytest.raises(TranscriptForkError, match="no matching provider user message"):
        await manager.edit_resend(session, "edited msg1", "c-new", "t1")

    # Neither plane was truncated (the store truncation that ran before the
    # fork was rolled back from the snapshot).
    assert _read_bytes(store.path) == store_before, "event store was modified"
    assert _read_bytes(transcript_path) == transcript_before, "transcript was modified"
