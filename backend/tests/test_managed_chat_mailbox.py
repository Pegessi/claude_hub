"""Managed Chat mailbox preserves durable envelopes without terminal side effects."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceAttachment,
    WorkspaceAttachmentCreate,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
)
from claude_hub.services import managed_chat
from claude_hub.services.workspace_manager import DeliveryUncertain, WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture()
def mailbox(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    manager = WorkspaceManager()
    workspace = manager.create_workspace(
        WorkspaceCreate(name="Chat mailbox", path=str(tmp_path), target=ExecutionTarget.LOCAL)
    )
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="chat-mailbox-session",
        workspace_id=workspace.id,
        tab_id="chat-mailbox-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        title="Chat worker",
        workspace_path=str(tmp_path),
        tmux_session="",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    manager._save_state()
    monkeypatch.setattr(manager, "_assert_session_seat", Mock(return_value=""))
    for name in (
        "_send_tmux_message",
        "_send_tmux_message_with_receipt",
        "_query_tmux_receipt",
        "_ensure_submitted_without_repaste",
        "_capture_tmux_output",
        "_run_tmux",
    ):
        monkeypatch.setattr(manager, name, AsyncMock(side_effect=AssertionError("Chat used tmux")))
    receipts = {}

    async def accepted(current, text, attachments, call_id):
        # Intent and the immutable envelope must exist before native handoff.
        saved = manager.sessions[current.id]
        assert call_id in saved.processing_call_ids
        assert call_id not in saved.pending_call_ids
        assert call_id in saved.pending_messages
        assert call_id in saved.call_payload_fingerprints
        receipts[call_id] = "accepted"

    sender = AsyncMock(side_effect=accepted)
    monkeypatch.setattr(managed_chat, "send_message", sender)
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda current: False)
    monkeypatch.setattr(
        managed_chat, "delivery_state", lambda current, call_id: receipts.get(call_id, "not_sent")
    )
    return manager, session.id, receipts, sender


def _bind_task(manager, session_id):
    session = manager.sessions[session_id]
    task = manager.create_task(
        session.workspace_id,
        WorkspaceTaskCreate(title="Chat assignment", prompt="Work", agent_type=session.agent_type),
    )
    manager.tasks[task.id] = task.model_copy(update={"session_id": session_id})
    manager.sessions[session_id] = session.model_copy(update={"task_id": task.id})
    manager._save_state()
    return task.id


async def test_chat_handoff_requires_report_ack_and_preserves_fingerprint(mailbox):
    manager, session_id, _receipts, sender = mailbox
    await manager.send_session_message(session_id, "hello", call_id="call-one")
    session = manager.sessions[session_id]
    assert session.processing_call_ids == ["call-one"]
    assert session.delivered_call_ids == []
    assert session.pending_messages["call-one"] == "hello"
    assert sender.await_args.args[1] == "[call_id:call-one]\nhello"

    await manager.send_session_message(session_id, "hello", call_id="call-one")
    assert sender.await_count == 1
    with pytest.raises(ValueError, match="different payload"):
        await manager.send_session_message(session_id, "changed", call_id="call-one")
    manager._ack_call_ids(None, session_id, ["call-one"])
    manager._save_state()
    session = manager.sessions[session_id]
    assert session.processing_call_ids == []
    assert session.delivered_call_ids == ["call-one"]
    assert "call-one" not in session.pending_messages
    await manager.send_session_message(session_id, "hello", call_id="call-one")
    assert sender.await_count == 1


async def test_chat_busy_keeps_fifo_envelopes_pending_until_next_pump(mailbox, monkeypatch):
    manager, session_id, _receipts, sender = mailbox
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda current: True)
    await manager.send_session_message(session_id, "first", call_id="first")
    await manager.send_session_message(session_id, "second", call_id="second")
    session = manager.sessions[session_id]
    assert session.pending_call_ids == ["first", "second"]
    assert session.processing_call_ids == session.uncertain_call_ids == []
    sender.assert_not_awaited()

    monkeypatch.setattr(managed_chat, "is_turn_running", lambda current: False)
    await manager._pump_session_messages(session_id)
    assert sender.await_count == 1
    assert sender.await_args.args[3] == "first"
    assert manager.sessions[session_id].pending_call_ids == ["second"]
    await manager._pump_session_messages(session_id)
    assert sender.await_count == 2
    assert sender.await_args.args[3] == "second"


async def test_chat_unkeyed_input_is_durable_and_does_not_unpause(mailbox):
    manager, session_id, _receipts, sender = mailbox
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"chat_paused": True}
    )
    await manager.send_session_message(session_id, "bootstrap")
    session = manager.sessions[session_id]
    assert session.chat_paused
    assert len(session.pending_call_ids) == 1
    call_id = session.pending_call_ids[0]
    assert call_id.startswith("chat:")
    assert session.pending_messages[call_id] == "bootstrap"
    assert call_id in session.call_payload_fingerprints
    sender.assert_not_awaited()


async def test_chat_definite_failure_persists_unsent_before_later_queue(mailbox, monkeypatch):
    manager, session_id, _receipts, sender = mailbox
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda current: True)
    await manager.send_session_message(session_id, "first", call_id="first")
    await manager.send_session_message(session_id, "later", call_id="later")
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda current: False)
    sender.side_effect = ValueError("invalid before native attempt")
    with pytest.raises(ValueError, match="before native attempt"):
        await manager._pump_session_messages(session_id)
    session = manager.sessions[session_id]
    assert session.pending_call_ids == ["first", "later"]
    assert session.processing_call_ids == session.uncertain_call_ids == []


@pytest.mark.parametrize("receipt_state", ["sending", "uncertain"])
async def test_chat_ambiguous_send_never_replays_or_sends_later_input(mailbox, receipt_state):
    manager, session_id, receipts, sender = mailbox

    async def ambiguous(current, text, attachments, call_id):
        receipts[call_id] = receipt_state
        raise RuntimeError("provider side effect may have started")

    sender.side_effect = ambiguous
    with pytest.raises(DeliveryUncertain):
        await manager.send_session_message(session_id, "first", call_id="first")
    await manager.send_session_message(session_id, "later", call_id="later")
    await manager._pump_session_messages(session_id)
    session = manager.sessions[session_id]
    assert session.uncertain_call_ids == ["first"]
    assert session.pending_call_ids == ["later"]
    assert session.delivered_call_ids == []
    assert sender.await_count == 1
    with pytest.raises(DeliveryUncertain):
        await manager.retry_uncertain_delivery(session_id, "first", reason="retry", actor="user")
    assert sender.await_count == 1


async def test_chat_cancelled_send_records_uncertainty_and_propagates(mailbox):
    manager, session_id, receipts, sender = mailbox

    async def cancelled(current, text, attachments, call_id):
        receipts[call_id] = "sending"
        raise asyncio.CancelledError()

    sender.side_effect = cancelled
    with pytest.raises(asyncio.CancelledError):
        await manager.send_session_message(session_id, "first", call_id="first")
    assert manager.sessions[session_id].uncertain_call_ids == ["first"]


async def test_chat_report_ack_wins_over_late_send_failure(mailbox):
    manager, session_id, receipts, sender = mailbox

    async def reported_then_failed(current, text, attachments, call_id):
        manager._ack_call_ids(None, session_id, [call_id])
        manager._save_state()
        receipts[call_id] = "uncertain"
        raise RuntimeError("late transport failure")

    sender.side_effect = reported_then_failed
    await manager.send_session_message(session_id, "first", call_id="first")
    session = manager.sessions[session_id]
    assert session.delivered_call_ids == ["first"]
    assert session.uncertain_call_ids == session.processing_call_ids == []


@pytest.mark.parametrize(
    ("receipt", "pending", "processing", "uncertain"),
    [
        ("not_sent", ["first"], [], []),
        ("accepted", [], ["first"], []),
        ("sending", [], [], ["first"]),
        ("uncertain", [], [], ["first"]),
    ],
)
async def test_chat_recovery_uses_provider_receipt_not_ui_history(
    mailbox, receipt, pending, processing, uncertain
):
    manager, session_id, receipts, sender = mailbox
    await manager.send_session_message(session_id, "first", call_id="first")
    receipts["first"] = receipt
    await manager._recover_processing_via_receipt(session_id)
    session = manager.sessions[session_id]
    assert session.pending_call_ids == pending
    assert session.processing_call_ids == processing
    assert session.uncertain_call_ids == uncertain
    assert session.delivered_call_ids == []
    assert sender.await_count == 1


@pytest.mark.parametrize("receipt", ["accepted", "not_sent", "uncertain"])
async def test_chat_mailbox_cold_load_preserves_envelope_and_native_recovery(mailbox, receipt):
    manager, session_id, receipts, sender = mailbox
    await manager.send_session_message(session_id, "original", call_id="first")
    receipts["first"] = receipt
    manager._save_state()

    fresh = WorkspaceManager()
    assert fresh.sessions[session_id].session_kind == SessionKind.CHAT
    assert fresh.sessions[session_id].pending_messages["first"] == "original"
    await fresh._recover_processing_via_receipt(session_id)
    session = fresh.sessions[session_id]
    assert session.pending_call_ids == (["first"] if receipt == "not_sent" else [])
    assert session.processing_call_ids == (["first"] if receipt == "accepted" else [])
    assert session.uncertain_call_ids == (["first"] if receipt == "uncertain" else [])
    assert sender.await_count == 1


async def test_chat_concurrent_same_call_claims_once(mailbox):
    manager, session_id, receipts, sender = mailbox
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting(current, text, attachments, call_id):
        receipts[call_id] = "sending"
        entered.set()
        await release.wait()
        receipts[call_id] = "accepted"

    sender.side_effect = waiting
    sending = asyncio.create_task(
        manager.send_session_message(session_id, "original", call_id="first")
    )
    await asyncio.wait_for(entered.wait(), 1)
    await manager.send_session_message(session_id, "original", call_id="first")
    assert sender.await_count == 1
    release.set()
    await asyncio.wait_for(sending, 1)
    assert manager.sessions[session_id].processing_call_ids == ["first"]


async def test_chat_unreadable_delivery_evidence_fails_closed(mailbox, monkeypatch):
    manager, session_id, _receipts, _sender = mailbox
    await manager.send_session_message(session_id, "first", call_id="first")
    monkeypatch.setattr(
        managed_chat, "delivery_state", Mock(side_effect=RuntimeError("bad evidence"))
    )
    await manager._recover_processing_via_receipt(session_id)
    assert manager.sessions[session_id].uncertain_call_ids == ["first"]
    with pytest.raises(DeliveryUncertain):
        await manager.retry_uncertain_delivery(session_id, "first", reason="retry", actor="user")


async def test_chat_operator_retry_with_acceptance_receipt_never_resubmits(mailbox):
    manager, session_id, _receipts, sender = mailbox
    _bind_task(manager, session_id)
    await manager.send_session_message(session_id, "first", call_id="first")
    manager._mark_processing_as_uncertain(session_id, ["first"])
    await manager.retry_uncertain_delivery(session_id, "first", reason="verified", actor="user")
    session = manager.sessions[session_id]
    assert session.processing_call_ids == ["first"]
    assert session.uncertain_call_ids == session.delivered_call_ids == []
    assert sender.await_count == 1


async def test_chat_operator_retry_proven_unsent_reuses_original_envelope(mailbox):
    manager, session_id, receipts, sender = mailbox
    _bind_task(manager, session_id)
    await manager.send_session_message(session_id, "original", call_id="first")
    manager._mark_processing_as_uncertain(session_id, ["first"])
    receipts.pop("first")
    await manager.retry_uncertain_delivery(session_id, "first", reason="verified", actor="user")
    assert sender.await_count == 2
    assert sender.await_args.args[1] == "[call_id:first]\noriginal"
    assert manager.sessions[session_id].processing_call_ids == ["first"]


async def test_chat_retry_audit_failure_restores_uncertain_without_send(mailbox, monkeypatch):
    manager, session_id, receipts, sender = mailbox
    await manager.send_session_message(session_id, "original", call_id="first")
    manager._mark_processing_as_uncertain(session_id, ["first"])
    receipts.pop("first")
    monkeypatch.setattr(
        manager, "_emit_delivery_retry_requested", Mock(side_effect=OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        await manager.retry_uncertain_delivery(session_id, "first", reason="verified", actor="user")
    assert manager.sessions[session_id].uncertain_call_ids == ["first"]
    assert manager.sessions[session_id].pending_call_ids == []
    assert sender.await_count == 1


async def test_chat_native_images_keep_attachment_fingerprints(mailbox):
    manager, session_id, _receipts, sender = mailbox
    image = WorkspaceAttachmentCreate(
        filename="image.png",
        mime_type="image/png",
        data_url="data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode(),
    )
    await manager.send_session_message(session_id, "inspect", [image], call_id="attachments")
    delivered_text = sender.await_args.args[1]
    native_images = sender.await_args.args[2]
    assert [attachment.filename for attachment in native_images] == ["image.png"]
    assert "image.png" not in delivered_text
    assert len(manager.sessions[session_id].pending_attachments["attachments"]) == 1
    changed_image = image.model_copy(
        update={
            "data_url": "data:image/png;base64,"
            + base64.b64encode(b"\x89PNG\r\n\x1a\nchanged").decode()
        }
    )
    with pytest.raises(ValueError, match="different payload"):
        await manager.send_session_message(
            session_id, "inspect", [changed_image], call_id="attachments"
        )
    assert sender.await_count == 1


async def test_chat_existing_document_manifest_remains_in_text(mailbox, tmp_path):
    manager, session_id, _receipts, sender = mailbox
    document_path = tmp_path / "notes.txt"
    document_path.write_text("notes")
    document = WorkspaceAttachment(
        id="document",
        filename="notes.txt",
        mime_type="text/plain",
        path=str(document_path),
        size_bytes=5,
    )
    session = manager.sessions[session_id]
    manager.sessions[session_id] = session.model_copy(
        update={
            "pending_call_ids": ["document"],
            "pending_messages": {"document": "inspect"},
            "pending_attachments": {"document": [document]},
            "call_payload_fingerprints": {
                "document": manager._compute_persisted_payload_fingerprint("inspect", [document])
            },
        }
    )
    await manager._pump_session_messages(session_id)
    assert "notes.txt" in sender.await_args.args[1]
    assert sender.await_args.args[2] == []


async def test_chat_readiness_checks_seat_without_terminal_probes(mailbox):
    manager, session_id, _receipts, _sender = mailbox
    await manager._ensure_session_ready_for_send(manager.sessions[session_id])
    await manager._wait_for_agent_prompt(manager.sessions[session_id])
    assert manager._assert_session_seat.call_count == 2


@pytest.mark.asyncio
async def test_exact_question_followup_bypasses_only_unsent_fifo(mailbox, monkeypatch):
    manager, session_id, receipts, sender = mailbox
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda current: True)
    await manager.send_chat_session_message(session_id, "earlier-one", call_id="one")
    await manager.send_chat_session_message(session_id, "earlier-two", call_id="two")
    assert manager.sessions[session_id].pending_call_ids == ["one", "two"]
    sender.assert_not_called()
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda current: False)
    await manager._send_session_message_locked(
        session_id, "structured answer", call_id="answer", question_key='["turn","call"]'
    )
    assert sender.await_args_list[0].args[-1] == "answer"
    assert manager.sessions[session_id].pending_call_ids == ["one", "two"]
    assert manager.sessions[session_id].chat_question_answer_calls['["turn","call"]'] == "answer"
