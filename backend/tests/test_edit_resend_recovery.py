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
6. Turn-in-flight guard   — editing while a turn is running raises
   ``EditResendTurnInFlightError`` (mapped to 409); editing when idle
   succeeds.

The tailer creation (``_get_or_create``) and transcript discovery
(``discover_source_cached``) are mocked so the tests exercise the
truncate/fork/send/recovery logic without spawning real provider subprocesses.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast
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
from claude_hub.models.chat_image import ChatImageAttachment
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import SessionTailer, TailerManager
from claude_hub.services.agent_stream.transcript_fork import TranscriptForkError
from tests.test_agent_stream_attachments import _png as _attachment_png
from tests.test_ttyd_stream_lifecycle import DirectChat, direct_chat  # noqa: F401

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
    *,
    metadata: Optional[Dict[str, Any]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> AgentStreamEvent:
    """Append one ``turn_started`` (or other) event to the Hub store."""
    payload: Dict[str, Any] = {"summary": text, "attachments": list(attachments or [])}
    if metadata is not None:
        payload["metadata"] = dict(metadata)
    event = AgentStreamEvent(
        stream_sequence=0,  # overwritten by store.append
        session_id=store.session_id,
        tab_id="tab-1",
        agent_type=AgentType.CLAUDE,
        type=event_type,
        turn_id=turn_id,
        message_id=f"{turn_id}:user",
        payload=payload,
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
    if path.parent.name == "agent_streams":
        store = AgentStreamStore(path.parent.parent.name, path.stem)
        return json.dumps([event.model_dump(mode="json") for event in store._read_rows()]).encode()
    return path.read_bytes()


def _png(width: int = 1, height: int = 1) -> bytes:
    """A minimal valid PNG (used to seed an attachment preview)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return sig + ihdr + iend


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


@pytest.mark.asyncio
async def test_web_edit_resend_rebuilds_feishu_provider_text_and_resets_source(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Web edit locates the Feishu provider input, then creates a Web turn."""

    from claude_hub.services.agent_stream.turn_source import (
        FEISHU_PROVIDER_TEXT_FORMAT_V1,
        format_feishu_provider_text_v1,
    )

    session = _session()
    visible_text = "message from Feishu"
    metadata = {
        "origin": "feishu",
        "provider_text_format": FEISHU_PROVIDER_TEXT_FORMAT_V1,
        "feishu": {
            "app_id": "cli-bot",
            "chat_id": "oc-old",
            "message_id": "om-old",
            "sender_open_id": "ou-owner",
        },
    }
    provider_text = format_feishu_provider_text_v1(
        visible_text,
        app_id="cli-bot",
        chat_id="oc-old",
        message_id="om-old",
        sender_open_id="ou-owner",
    )
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", provider_text, "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", visible_text, metadata=metadata)
    await _append_turn(store, "t2", "msg2")
    legacy_result = await store.find_turn("t1")
    assert legacy_result is not None
    assert len(legacy_result) == 4
    detailed_result = await store.find_turn_with_metadata("t1")
    assert detailed_result is not None
    assert detailed_result[4] == metadata

    manager, mock_tailer = _make_manager(session)
    await manager.edit_resend(
        session,
        "edited in Web",
        "web-edit-turn",
        "t1",
        turn_metadata={"origin": "web"},
    )

    mock_tailer.send_message.assert_called_once_with(
        "edited in Web",
        [],
        "web-edit-turn",
        turn_metadata={"origin": "web"},
    )
    kept_texts: List[str] = []
    with transcript_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            kept_texts.append(value["message"]["content"][0]["text"])
    assert kept_texts == ["msg0"]


# ── 3b. attachment preservation (success) ──────────────────────────────────


@pytest.mark.asyncio
async def test_edit_resend_preserves_attachments(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Edit-resend must re-send the original turn's image attachments.

    The original turn carries one image attachment whose preview is persisted
    in the attachment store.  After edit-resend, ``send_message`` must receive
    the preview bytes as ``images`` and the original attachment metas via
    ``reuse_attachments`` so the new turn re-references the same thumbnails.
    """
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    # Persist a preview for the original turn's image attachment.
    att_store = AgentStreamAttachmentStore(session.workspace_id, session.id)
    png_bytes = _png(width=8, height=8)
    meta = await att_store.save("image/png", png_bytes)
    assert meta["id"], "save must return a non-empty attachment id"

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    # t1 carries the image attachment.
    t1 = AgentStreamEvent(
        stream_sequence=0,
        session_id=store.session_id,
        tab_id="tab-1",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TURN_STARTED,
        turn_id="t1",
        message_id="t1:user",
        payload={"summary": "msg1", "attachments": [meta]},
        created_at=datetime.now(timezone.utc),
    )
    await store.append(t1)
    await _append_turn(store, "t2", "msg2")

    manager, mock_tailer = _make_manager(session)

    removed = await manager.edit_resend(session, "edited msg1", "c-new", "t1")
    assert removed == 1, "editing t1 removes the one turn after it"

    # The edited text was delivered WITH the original image re-sent and the
    # original attachment metas re-referenced.  Under the current
    # ``reuse_attachments`` rule only the leading images are reused; this edit
    # adds no image, so ``images`` and the metas stay aligned one-to-one and
    # nothing new is persisted.
    mock_tailer.send_message.assert_called_once_with(
        "edited msg1",
        [png_bytes],
        "c-new",
        reuse_attachments=[meta],
    )

    # The preview is still readable (edit-resend must not delete it).
    data, _mime = await att_store.read(meta["id"])
    assert data == png_bytes, "original preview was deleted by edit-resend"


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

    async def _blocking_send(
        text: str, images: List[bytes], client_turn_id: str, **kwargs: Any
    ) -> None:
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


# ── 6. turn-in-flight guard ─────────────────────────────────────────────────


def _tailer_with_transport(
    session: ManagedSession,
    turn_in_flight: bool,
) -> SessionTailer:
    """A real SessionTailer whose mock transport reports ``turn_in_flight``.

    The tailer creates its own real ``AgentStreamStore`` (same path as the
    test's), so the manager's ``get_store`` -> ``find_turn`` reads the same
    file the test populated.  ``stop`` is replaced with an ``AsyncMock`` so
    the test does not cancel a real run task or flush a real coalescer.
    """
    transport = MagicMock()
    transport.turn_in_flight = turn_in_flight
    tailer = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=MagicMock(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    tailer.stop = AsyncMock()  # type: ignore[assignment]
    return tailer


@pytest.mark.asyncio
async def test_edit_resend_rejected_while_turn_running(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Editing while a turn is running must raise (mapped to 409).

    The tailer's transport reports ``turn_in_flight=True``; edit_resend must
    raise ``EditResendTurnInFlightError``, leave both planes untouched, and
    restore the tailer to the manager so the in-flight turn is not orphaned.
    """
    from claude_hub.services.agent_stream.tailer import EditResendTurnInFlightError

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

    manager = TailerManager(session_getter=lambda _sid: session)
    # Populate the manager with a tailer whose turn is in flight.
    manager._tailers[session.id] = _tailer_with_transport(session, turn_in_flight=True)

    with pytest.raises(EditResendTurnInFlightError, match="turn is currently running"):
        await manager.edit_resend(session, "edited msg1", "c-new", "t1")

    # Neither plane was modified.
    assert _read_bytes(store.path) == store_before, "event store was modified"
    assert _read_bytes(transcript_path) == transcript_before, "transcript was modified"

    # The tailer was restored to the manager (not orphaned).
    assert session.id in manager._tailers, "tailer was not restored after rejection"
    # The tailer's stop was NOT called (the in-flight turn was not killed).
    restored = manager._tailers[session.id]
    cast(AsyncMock, restored.stop).assert_not_called()


@pytest.mark.asyncio
async def test_edit_resend_succeeds_when_idle(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Editing when no turn is running must succeed.

    The tailer's transport reports ``turn_in_flight=False``; edit_resend must
    stop the tailer, truncate after the edited turn, and deliver the edit.
    """
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    await _append_turn(store, "t2", "msg2")

    manager, mock_tailer = _make_manager(session)
    # Populate the manager with an idle tailer (turn_in_flight=False).
    idle_tailer = _tailer_with_transport(session, turn_in_flight=False)
    manager._tailers[session.id] = idle_tailer

    await manager.edit_resend(session, "edited msg1", "c-new", "t1")

    # The idle tailer was stopped (its stop was called once).
    cast(AsyncMock, idle_tailer.stop).assert_awaited_once()

    # The edited text was delivered via the (mocked) fresh tailer.
    mock_tailer.send_message.assert_called_once_with("edited msg1", [], "c-new")

    # Hub store: only t0 remains.
    page = await store.read_since(-1, limit=100)
    summaries = [
        e.payload.get("summary") for e in page.events if e.type == AgentStreamEventType.TURN_STARTED
    ]
    assert summaries == ["msg0"], f"expected only t0, got {summaries}"


@pytest.mark.asyncio
@pytest.mark.parametrize("send_fails", [False, True])
async def test_edit_resend_cleans_hub_and_external_provider_snapshots(
    isolated_state_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, send_fails: bool
) -> None:
    """Provider JSONL cleanup must not route through the Hub SQLite engine."""
    from claude_hub.services.local_storage import document_exists

    monkeypatch.setattr(wm_module, "STATE_ROOT", tmp_path / "hub-workspaces")
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub-home"))
    provider_root = tmp_path / "provider-home"
    transcript = provider_root / "transcripts" / "conversation.jsonl"
    _write_transcript(transcript, ["msg0", "msg1"])
    before = transcript.read_bytes()
    _patch_discovery(monkeypatch, transcript)
    session = _session()
    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    manager, _ = _make_manager(
        session, send_side_effect=RuntimeError("send failed") if send_fails else None
    )
    if send_fails:
        with pytest.raises(RuntimeError, match="send failed"):
            await manager.edit_resend(session, "edited", "c-new", "t1")
        assert transcript.read_bytes() == before
        assert await store.find_turn("t1") is not None
    else:
        await manager.edit_resend(session, "edited", "c-new", "t1")
        assert await store.find_turn("t1") is None
    # Exercise real snapshot, rollback and finally cleanup (only transport is mocked).
    assert sorted(
        path.relative_to(provider_root) for path in provider_root.rglob("*") if path.is_file()
    ) == [Path("transcripts/conversation.jsonl")]
    assert not document_exists(store.path.with_name(store.path.name + ".edit-bak"))


# ── 7. digest matching for redacted / long messages ─────────────────────────


def _redacted_summary(text: str) -> str:
    """The ``summary`` the Hub persists for ``text`` (masked, then truncated)."""
    from claude_hub.services.agent_stream.redaction import MAX_FIELD_CHARS, mask_secrets

    masked = mask_secrets(text)
    marker = "…[truncated]"
    if len(masked) > MAX_FIELD_CHARS:
        return masked[: MAX_FIELD_CHARS - len(marker)] + marker
    return masked


@pytest.mark.asyncio
async def test_edit_resend_matches_redacted_message_by_recorded_digest(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A long or secret-bearing message is matched by its recorded digest.

    The stored ``summary`` is redacted (secret-masked and truncated at 4000
    chars), so comparing it with the full provider text can never match.  The
    digest recorded on ``turn_started`` identifies the real provider message;
    a turn without one (historical) still fails cleanly and destructively-free.
    """
    from claude_hub.services.agent_stream.transcript_fork import hash_provider_text

    provider_text = "sk-abcd1234efgh5678 " + ("x" * 4200)
    stored_summary = _redacted_summary(provider_text)
    assert stored_summary != provider_text, "precondition: the stored copy is redacted"

    # Historical turn: no digest recorded, redacted summary cannot match.
    legacy = _session("sess-legacy")
    legacy_transcript = tmp_path / "legacy.jsonl"
    _write_transcript(legacy_transcript, ["msg0", provider_text, "msg2"])
    _patch_discovery(monkeypatch, legacy_transcript)
    legacy_store = AgentStreamStore(legacy.workspace_id, legacy.id)
    await _append_turn(legacy_store, "t0", "msg0")
    await _append_turn(legacy_store, "t1", stored_summary)
    await _append_turn(legacy_store, "t2", "msg2")
    legacy_manager, _ = _make_manager(legacy)
    with pytest.raises(TranscriptForkError, match="no matching provider user message"):
        await legacy_manager.edit_resend(legacy, "edited", "c-new", "t1")
    assert await legacy_store.count_turns() == 3, "legacy refusal must not truncate"

    # Modern turn: the digest recorded at send time makes the edit matchable.
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", provider_text, "msg2"])
    _patch_discovery(monkeypatch, transcript_path)
    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(
        store,
        "t1",
        stored_summary,
        metadata={"provider_text_sha256": hash_provider_text(provider_text)},
    )
    await _append_turn(store, "t2", "msg2")
    manager, mock_tailer = _make_manager(session)
    removed = await manager.edit_resend(session, "edited", "c-new", "t1")
    assert removed == 1
    mock_tailer.send_message.assert_called_once_with("edited", [], "c-new")


# ── 8. Hub-authored turns are not editable ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["goal", "workspace", "restart_continuation"])
async def test_hub_authored_turn_is_refused_before_truncating(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    origin: str,
) -> None:
    """Server-authored turns are refused with no truncation and no snapshot left."""
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    provider_text = "Goal continuation instruction the user never typed"
    _write_transcript(transcript_path, ["msg0", provider_text, "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(
        store, "t1", "Continue active Goal", metadata={"origin": origin, "protocol": "p"}
    )
    await _append_turn(store, "t2", "msg2")
    store_before = _read_bytes(store.path)
    transcript_before = _read_bytes(transcript_path)

    manager, mock_tailer = _make_manager(session)
    with pytest.raises(TranscriptForkError, match="written by the Goal/Hub"):
        await manager.edit_resend(session, "edited", "c-new", "t1")

    mock_tailer.send_message.assert_not_called()
    mock_tailer.stop.assert_not_called()
    assert _read_bytes(store.path) == store_before, "event store was modified"
    assert _read_bytes(transcript_path) == transcript_before, "transcript was modified"
    assert not transcript_path.with_name(transcript_path.name + ".edit-bak").exists()
    assert not store.path.with_name(store.path.name + ".edit-bak").exists()


# ── 9. images added while editing ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_added_images_join_the_reused_prefix(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """New images ride past the reused prefix, one preview slot each.

    ``send_message`` owns persisting their previews and building their metadata
    (single writer for the merge rule), so this asserts the contract it is
    handed: the original turn's metadata as the reused prefix, the added
    originals after it, and a preview slot per image.
    """
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    att_store = AgentStreamAttachmentStore(session.workspace_id, session.id)
    original_preview = _png(width=8, height=8)
    original_meta = await att_store.save("image/png", original_preview)
    added_original = _png(width=4, height=4)
    added_preview = _png(width=3, height=3)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1", attachments=[original_meta])
    await _append_turn(store, "t2", "msg2")

    manager, mock_tailer = _make_manager(session)
    await manager.edit_resend(
        session,
        "edited msg1",
        "c-new",
        "t1",
        added_images=[added_original],
        added_previews=[added_preview],
    )

    kwargs = mock_tailer.send_message.call_args.kwargs
    call_images = mock_tailer.send_message.call_args.args[1]
    # Only the original turn's metadata is reused: the new image must not
    # inherit an existing id.
    assert kwargs["reuse_attachments"] == [original_meta]
    # Images line up with the metadata: reused preview first, then the original
    # bytes of the added image (the provider receives originals, not previews).
    assert call_images == [original_preview, added_original]
    # One preview slot per image; the reused slot is ignored, the added image
    # brings its own bounded preview.
    assert kwargs["previews"] == [None, added_preview]
    # A rolled-back edit must not keep the previews it made the tailer persist.
    assert kwargs["retain_previews_on_delivery_failure"] is False
    # Nothing is persisted here: that happens inside ``send_message``.
    assert await att_store.read(original_meta["id"]) == (original_preview, "image/png")


@pytest.mark.asyncio
async def test_added_image_rollback_leaves_the_cache_untouched(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed edit restores the store and persists no preview for the new image.

    The added image's preview is written by ``send_message`` once the edit has
    truncated both planes, so a failure before that point (this send fails)
    leaves the cache exactly as it was; ``send_message`` owns deleting anything
    it wrote itself.
    """
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    att_store = AgentStreamAttachmentStore(session.workspace_id, session.id)
    original_preview = _png(width=8, height=8)
    original_meta = await att_store.save("image/png", original_preview)
    added_preview = _png(width=3, height=3)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1", attachments=[original_meta])
    await _append_turn(store, "t2", "msg2")

    manager, _mock_tailer = _make_manager(session, send_side_effect=RuntimeError("send failed"))
    with pytest.raises(RuntimeError, match="send failed"):
        await manager.edit_resend(
            session,
            "edited msg1",
            "c-new",
            "t1",
            added_images=[_png(width=4, height=4)],
            added_previews=[added_preview],
        )

    # The edit was rolled back: every turn is still present.
    assert await store.count_turns() == 3
    # The reused preview is untouched and nothing was written for the new image.
    assert await att_store.read(original_meta["id"]) == (original_preview, "image/png")
    cached = {entry["id"] for entry in AgentStreamAttachmentStore._read_index()}
    assert cached == {original_meta["id"]}, "a preview was persisted for an edit that failed"


# ── 10. the resend reports what it removed ─────────────────────────────────


@pytest.mark.asyncio
async def test_edit_resend_reports_removed_turn_count(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The caller learns how many Hub turns the rewrite discarded."""
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, ["msg0", "msg1", "msg2"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "msg1")
    await _append_turn(store, "t2", "msg2")
    manager, _mock_tailer = _make_manager(session)

    # Two turns follow the edited one.
    assert await manager.edit_resend(session, "edited msg1", "c-new", "t1") == 1
    # The edited turn is now the last one: nothing further is removed.
    assert await manager.edit_resend(session, "edited msg0", "c-new-2", "t0") == 0


# ── 11. shifted matches are visible ────────────────────────────────────────


@pytest.mark.asyncio
async def test_shifted_duplicate_match_logs_warning(
    isolated_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A duplicate/drifted match logs the requested and matched ordinals."""
    session = _session()
    transcript_path = tmp_path / "transcript.jsonl"
    # The provider only saw one "dup", at ordinal 1; the Hub requested ordinal 2.
    _write_transcript(transcript_path, ["msg0", "dup"])
    _patch_discovery(monkeypatch, transcript_path)

    store = AgentStreamStore(session.workspace_id, session.id)
    await _append_turn(store, "t0", "msg0")
    await _append_turn(store, "t1", "dup")
    await _append_turn(store, "t2", "dup")
    manager, _mock_tailer = _make_manager(session)

    with caplog.at_level(logging.WARNING, logger="claude_hub"):
        await manager.edit_resend(session, "edited dup", "c-new", "t2")

    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    shifted = [message for message in warnings if "shifted match" in message]
    assert shifted, f"expected a shifted-match warning, got {warnings}"
    assert "requested turn_index=2" in shifted[0]
    assert "ordinal=1" in shifted[0]
    assert "removed 1 transcript lines" in shifted[0]


# ── 12. tab edit-resend route contract ─────────────────────────────────────


@pytest.mark.asyncio
async def test_tab_edit_resend_route_forwards_images_and_reports_removed_count(
    direct_chat: DirectChat,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tab route validates images like a normal send and reports the rewrite."""
    from claude_hub.api import agent_stream as stream_api
    from claude_hub.models import StreamCapabilities

    tabs, process, _manager, _tailer, _transport = direct_chat
    monkeypatch.setattr(stream_api, "ttyd_manager", tabs)
    goal = importlib.import_module("claude_hub.services.goal_run")
    monkeypatch.setattr(goal, "get_goal_manager", lambda: MagicMock(current=lambda *_: None))

    fake_manager = MagicMock()
    fake_manager.edit_resend = AsyncMock(return_value=2)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: fake_manager)
    monkeypatch.setattr(
        stream_api,
        "_capabilities_for",
        AsyncMock(return_value=StreamCapabilities(supports_images=True)),
    )
    user = MagicMock()

    png = _attachment_png(2, 2)
    encoded = base64.b64encode(png).decode()
    payload = stream_api.AgentStreamEditResendRequest(
        text="edited",
        client_turn_id="c-new",
        turn_id="t1",
        attachments=[
            ChatImageAttachment(
                filename="added.png",
                mime_type="image/png",
                data_url=f"data:image/png;base64,{encoded}",
            )
        ],
    )
    result = await stream_api.edit_resend_tab_stream(process.tab_id, payload, current_user=user)
    assert result == {"ok": True, "removed_turn_count": 2}
    kwargs = fake_manager.edit_resend.call_args.kwargs
    assert kwargs["added_images"] == [png]
    assert kwargs["turn_metadata"] == {"origin": "web"}
    assert fake_manager.edit_resend.call_args.args[1] == "edited"

    # Images the provider cannot accept are refused before any rewrite.
    fake_manager.edit_resend.reset_mock()
    monkeypatch.setattr(
        stream_api,
        "_capabilities_for",
        AsyncMock(return_value=StreamCapabilities(supports_images=False)),
    )
    with pytest.raises(stream_api.HTTPException) as refused:
        await stream_api.edit_resend_tab_stream(process.tab_id, payload, current_user=user)
    assert refused.value.status_code == 400
    fake_manager.edit_resend.assert_not_called()

    # Malformed image data fails closed in decoding, before the manager runs.
    bad = stream_api.AgentStreamEditResendRequest(
        text="edited",
        client_turn_id="c-new",
        turn_id="t1",
        attachments=[
            ChatImageAttachment(
                filename="bad.png", mime_type="image/png", data_url="data:image/png;base64,AAAA"
            )
        ],
    )
    with pytest.raises(stream_api.HTTPException) as malformed:
        await stream_api.edit_resend_tab_stream(process.tab_id, bad, current_user=user)
    assert malformed.value.status_code == 400
    fake_manager.edit_resend.assert_not_called()
