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
_REAL_CHAT_SEND = managed_chat.send_message
_REAL_CHAT_DELIVERY_STATE = managed_chat.delivery_state
_REAL_CHAT_IS_RUNNING = managed_chat.is_turn_running


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
        _, _, identities = managed_chat._prepare_attachments(attachments)
        evidence = managed_chat._read_attempts(current)
        evidence["attempts"][call_id] = {
            "state": "accepted",
            "turn_id": call_id,
            "payload_sha256": managed_chat._payload_digest(text, identities),
        }
        managed_chat._write_attempts(current, evidence)
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


async def _terminal_reconciliation_case(
    mailbox, outcome="failed", *, provenance=True, message="quota-blocked review"
):
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType
    from claude_hub.models.schema_domains.requests import ReconcileTerminalDeliveryRequest
    from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
    from claude_hub.services.agent_stream.store import AgentStreamStore
    from claude_hub.services.task_graph import task_inbox_consumer_key

    manager, session_id, receipts, sender = mailbox
    task_id = _bind_task(manager, session_id)
    task = manager.tasks[task_id]
    if provenance:
        manager.task_mailbox.append_event(
            workspace_id=task.workspace_id,
            task_id=task.id,
            actor_role=TaskActorRole.SUPERVISOR,
            event_type=TaskEventType.FOLLOWUP,
            call_id="terminal-call",
            action="followup",
            consumer_key=task_inbox_consumer_key(task),
            target=task.id,
            review_cycle=task.review_cycle,
            payload=manager._canonical_followup_payload(
                task_id=task.id,
                message=message,
                actor_role=TaskActorRole.SUPERVISOR,
                actor_session_id=None,
                review_cycle=task.review_cycle,
            ),
            persist=True,
        )
    await manager.send_session_message(session_id, message, call_id="terminal-call")
    session = manager.sessions[session_id]
    task = manager.tasks[task_id]
    manager.tasks[task_id] = task.model_copy(update={"processing_call_ids": ["terminal-call"]})
    store = AgentStreamStore(session.workspace_id, session_id)
    for sequence, kind in enumerate(
        [AgentStreamEventType.TURN_STARTED, AgentStreamEventType.TURN_COMPLETED]
    ):
        await store.append(
            AgentStreamEvent(
                stream_sequence=sequence,
                session_id=session_id,
                tab_id=session.tab_id,
                agent_type=session.agent_type,
                type=kind,
                turn_id="terminal-call",
                run_epoch=1,
                created_at=datetime.now(timezone.utc),
                payload={"status": outcome} if sequence else {},
            )
        )
    request = ReconcileTerminalDeliveryRequest(
        operation_id="operator-reconcile-1",
        call_id="terminal-call",
        reason="Provider quota failure inspected",
        expected_created_at=session.created_at,
        expected_task_id=task_id,
        expected_review_cycle=task.review_cycle,
        terminal_sequence=1,
    )
    return manager, session_id, request, store, sender


@pytest.mark.parametrize("outcome", ["failed", "completed"])
async def test_terminal_operator_reconciliation_is_not_worker_ack(mailbox, outcome):
    manager, session_id, request, _store, sender = await _terminal_reconciliation_case(
        mailbox, outcome
    )
    task_before = manager.tasks[request.expected_task_id]
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    session = manager.sessions[session_id]
    assert record.outcome == outcome
    assert record.actor == "operator-user"
    assert record.terminal_sequence == 1
    assert session.processing_call_ids == []
    assert session.delivered_call_ids == []
    assert session.pending_messages[request.call_id] == "quota-blocked review"
    assert request.call_id in session.call_payload_fingerprints
    assert manager.tasks[task_before.id].model_dump(
        exclude={"processing_call_ids", "reconciled_call_ids"}
    ) == (task_before.model_dump(exclude={"processing_call_ids", "reconciled_call_ids"}))
    assert manager.tasks[task_before.id].processing_call_ids == []
    assert manager.tasks[task_before.id].reconciled_call_ids == [request.call_id]
    assert manager.reports == {}
    assert sender.await_count == 1
    # Reload through the real persisted workspace document, then retry both
    # the operator request and original delivery without starting another turn.
    restored = WorkspaceManager()
    assert restored.sessions[session_id].reconciled_deliveries[request.call_id] == record
    assert (
        await restored.reconcile_terminal_delivery(session_id, request, actor="operator-user")
        == record
    )
    await manager.send_session_message(session_id, "quota-blocked review", call_id=request.call_id)
    assert sender.await_count == 1
    with pytest.raises(ValueError, match="different payload"):
        await manager.send_session_message(session_id, "new work", call_id=request.call_id)
    with pytest.raises(ValueError, match="conflicts"):
        await manager.reconcile_terminal_delivery(session_id, request, actor="another-user")
    async with manager.managed_chat_configuration_guard(session_id):
        pass


@pytest.mark.parametrize(
    "failure",
    [
        "active",
        "unaccepted",
        "epoch",
        "sequence",
        "incarnation",
        "assignment",
        "cycle",
        "unknown",
        "uncertain",
        "missing-start",
        "duplicate-end",
        "rejected",
        "corrupt",
    ],
)
async def test_terminal_reconciliation_rejects_unproven_deliveries(mailbox, monkeypatch, failure):
    from datetime import timedelta

    from claude_hub.models import AgentStreamEventType

    manager, session_id, request, store, _sender = await _terminal_reconciliation_case(mailbox)
    session = manager.sessions[session_id]
    if failure == "active":
        monkeypatch.setattr(managed_chat, "is_turn_running", lambda _session: True)
    elif failure == "unaccepted":
        monkeypatch.setattr(manager, "_chat_delivery_state", lambda *_args: "uncertain")
    elif failure == "sequence":
        request = request.model_copy(update={"terminal_sequence": 2})
    elif failure == "incarnation":
        request = request.model_copy(
            update={"expected_created_at": session.created_at - timedelta(seconds=1)}
        )
    elif failure == "assignment":
        request = request.model_copy(update={"expected_task_id": "another-task"})
    elif failure == "cycle":
        request = request.model_copy(update={"expected_review_cycle": 99})
    elif failure == "unknown":
        request = request.model_copy(update={"call_id": "unknown-call"})
    elif failure == "uncertain":
        manager.sessions[session_id] = session.model_copy(
            update={"processing_call_ids": [], "uncertain_call_ids": [request.call_id]}
        )
    else:
        events = list(store.iter_events())
        if failure == "epoch":
            events[-1] = events[-1].model_copy(update={"run_epoch": 2})
        elif failure == "missing-start":
            events = events[1:]
        elif failure == "duplicate-end":
            events.append(events[-1].model_copy(update={"stream_sequence": 2}))
        elif failure == "rejected":
            events[-1] = events[-1].model_copy(
                update={"payload": {"status": "failed", "delivery_rejected": True}}
            )
        elif failure == "corrupt":
            events[-1] = events[-1].model_copy(update={"type": AgentStreamEventType.ERROR})
        await store.replace_all(events)
    before_session = manager.sessions[session_id].model_dump()
    before_task = manager.tasks[session.task_id].model_dump()
    with pytest.raises(ValueError):
        await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert manager.sessions[session_id].model_dump() == before_session
    assert manager.tasks[session.task_id].model_dump() == before_task


async def test_terminal_reconciliation_rolls_back_failed_persistence(mailbox, monkeypatch):
    manager, session_id, request, _store, sender = await _terminal_reconciliation_case(mailbox)
    before_session = manager.sessions[session_id]
    before_task = manager.tasks[request.expected_task_id]
    monkeypatch.setattr(manager, "_save_workspace_state", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert manager.sessions[session_id] == before_session
    assert manager.tasks[before_task.id] == before_task
    assert sender.await_count == 1


async def test_terminal_reconciliation_api_uses_authenticated_actor(mailbox):
    from types import SimpleNamespace

    from fastapi import APIRouter, FastAPI
    from httpx import ASGITransport, AsyncClient

    from claude_hub.api.routes.workspaces.execution import register_execution_routes
    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.models import User

    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    app = FastAPI()
    router = APIRouter(prefix="/api/workspaces")
    register_execution_routes(router, SimpleNamespace(workspace_manager=lambda: manager))
    app.include_router(router)
    user = User(
        open_id="authenticated-controller", name="Controller", email="operator@example.test"
    )
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated") as client:
        path = f"/api/workspaces/sessions/{session_id}/reconcile-terminal"
        forged = request.model_dump(mode="json") | {"actor": "reviewer"}
        assert (await client.post(path, json=forged)).status_code == 422
        response = await client.post(path, json=request.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        assert response.json()["actor"] == user.open_id
        assert response.json()["outcome"] == "failed"
        assert manager.sessions[session_id].delivered_call_ids == []
        assert manager.reports == {}


async def test_terminal_reconciliation_handles_post_commit_failure(mailbox, monkeypatch):
    manager, session_id, request, _store, sender = await _terminal_reconciliation_case(mailbox)
    save = manager._save_workspace_state

    def fail_after_commit(workspace_id):
        save(workspace_id)
        raise OSError("response failed after commit")

    monkeypatch.setattr(manager, "_save_workspace_state", fail_after_commit)
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert manager.sessions[session_id].reconciled_deliveries[request.call_id] == record
    assert await manager.resume_existing_call(session_id, request.call_id)
    assert sender.await_count == 1


async def test_terminal_reconciliation_serializes_concurrent_operator_requests(mailbox):
    manager, session_id, request, _store, sender = await _terminal_reconciliation_case(mailbox)
    one, two = await asyncio.gather(
        manager.reconcile_terminal_delivery(session_id, request, actor="operator-user"),
        manager.reconcile_terminal_delivery(session_id, request, actor="operator-user"),
    )
    assert one == two
    assert len(manager.sessions[session_id].reconciled_deliveries) == 1
    assert sender.await_count == 1


async def test_terminal_reconciliation_supports_existing_naive_session_time(mailbox):
    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    session = manager.sessions[session_id]
    naive_created = session.created_at.astimezone().replace(tzinfo=None)
    manager.sessions[session_id] = session.model_copy(update={"created_at": naive_created})
    request = request.model_copy(update={"expected_created_at": naive_created})
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert record.outcome == "failed"


async def test_terminal_reconciliation_rejects_other_task_mailbox_call(mailbox):
    from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
    from claude_hub.services.task_graph import task_inbox_consumer_key

    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(
        mailbox, provenance=False
    )
    session = manager.sessions[session_id]
    other = manager.create_task(
        session.workspace_id, WorkspaceTaskCreate(title="Unrelated", prompt="Do not touch")
    )
    manager.task_mailbox.append_event(
        workspace_id=session.workspace_id,
        task_id=other.id,
        actor_role=TaskActorRole.HUMAN,
        event_type=TaskEventType.FOLLOWUP,
        call_id=request.call_id,
        action="followup",
        consumer_key=task_inbox_consumer_key(other),
        target=other.id,
        payload={"message": "Unrelated task input"},
        persist=True,
    )
    with pytest.raises(ValueError, match="Another Task"):
        await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert manager.tasks[other.id] == other
    assert manager.sessions[session_id].processing_call_ids == [request.call_id]


@pytest.mark.parametrize("state", ["working", "review"])
@pytest.mark.parametrize("reassign", [False, True])
async def test_reconciled_followup_retry_preserves_task_after_reload(mailbox, state, reassign):
    from claude_hub.models import WorkspaceTaskStatus
    from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
    from claude_hub.services.task_graph import task_inbox_consumer_key

    manager, session_id, request, _store, sender = await _terminal_reconciliation_case(mailbox)
    task = manager.tasks[request.expected_task_id]
    # Register the actual canonical TaskMailbox envelope before reconciliation.
    message = "quota-blocked review"
    event, _ = manager.task_mailbox.append_event(
        workspace_id=task.workspace_id,
        task_id=task.id,
        actor_role=TaskActorRole.SUPERVISOR,
        event_type=TaskEventType.FOLLOWUP,
        call_id=request.call_id,
        action="followup",
        consumer_key=task_inbox_consumer_key(task),
        review_cycle=task.review_cycle,
        target=task.id,
        payload=manager._canonical_followup_payload(
            task_id=task.id,
            message=message,
            actor_role=TaskActorRole.SUPERVISOR,
            actor_session_id=None,
            review_cycle=task.review_cycle,
        ),
        persist=True,
    )
    await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(
        update={"status": WorkspaceTaskStatus(state)}
    )
    if reassign:
        manager.tasks[task.id] = manager.tasks[task.id].model_copy(
            update={"session_id": "replacement-worker"}
        )
    manager._save_state()
    restored = WorkspaceManager()
    before = restored.tasks[task.id].model_dump()
    reports_before = dict(restored.reports)
    receipt = await restored.followup_task(task.workspace_id, task.id, message, request.call_id)
    assert receipt == event
    assert restored.tasks[task.id].model_dump() == before
    assert restored.reports == reports_before
    assert sender.await_count == 1
    with pytest.raises(ValueError):
        await restored.followup_task(task.workspace_id, task.id, "different input", request.call_id)


async def test_reconciled_chat_retry_keeps_paused_runtime_unchanged(mailbox):
    manager, session_id, request, _store, sender = await _terminal_reconciliation_case(mailbox)
    await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"chat_paused": True, "status": ManagedSessionStatus.NEEDS_INPUT}
    )
    before = manager.sessions[session_id].model_dump()
    await manager.send_chat_session_message(
        session_id, "quota-blocked review", call_id=request.call_id
    )
    await manager.send_chat_session_message_locked(
        session_id, "quota-blocked review", call_id=request.call_id
    )
    assert manager.sessions[session_id].model_dump() == before
    assert sender.await_count == 1


@pytest.mark.parametrize(
    "message,outcome",
    [
        ("Wait for assigned tasks", "completed"),
        ("Review original historical Task; preserve its evidence", "completed"),
        ("Review current Task candidate", "failed"),
    ],
)
async def test_legacy_session_delivery_closes_without_current_task_attribution(
    mailbox, message, outcome
):
    manager, session_id, request, _store, sender = await _terminal_reconciliation_case(
        mailbox, outcome, provenance=False, message=message
    )
    session = manager.sessions[session_id]
    # This historical call is not mirrored in the currently assigned Task.
    task = manager.tasks[request.expected_task_id]
    manager.tasks[task.id] = task.model_copy(update={"processing_call_ids": []})
    manager.sessions[session_id] = session.model_copy(
        update={"role": WorkspaceSessionRole.REVIEWER}
    )
    before_task = manager.tasks[task.id].model_dump()
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert record.provenance == "session_delivery"
    assert record.task_id is None and record.review_cycle is None
    assert record.guard_task_id == task.id
    assert manager.tasks[task.id].model_dump() == before_task
    assert manager.sessions[session_id].pending_messages == session.pending_messages
    assert manager.sessions[session_id].delivered_call_ids == []
    assert sender.await_count == 1


async def test_terminal_delivery_preserves_prior_review_cycle_provenance(mailbox):
    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    task = manager.tasks[request.expected_task_id]
    manager.tasks[task.id] = task.model_copy(update={"review_cycle": task.review_cycle + 1})
    request = request.model_copy(update={"expected_review_cycle": task.review_cycle + 1})
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert record.review_cycle == task.review_cycle
    assert record.guard_review_cycle == task.review_cycle + 1
    assert manager.tasks[task.id].review_cycle == task.review_cycle + 1


async def test_terminal_reconciliation_rejects_corrupt_durable_history(mailbox):
    manager, session_id, request, store, _sender = await _terminal_reconciliation_case(mailbox)
    with store._read_database() as (_db, name):
        pass
    with store._database() as db, db:
        db.execute("INSERT INTO stream_errors VALUES (?,?,?)", (name, 100, b"corrupt fixture"))
    with pytest.raises(ValueError, match="unreadable events"):
        await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert manager.sessions[session_id].processing_call_ids == [request.call_id]


async def test_terminal_reconciliation_rejects_inconsistent_retained_envelope(mailbox):
    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    manager.sessions[session_id].pending_messages[request.call_id] = "changed stored envelope"
    with pytest.raises(ValueError, match="inconsistent"):
        await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")


async def _real_reconciliation_config_case(mailbox, monkeypatch, tmp_path):
    from claude_hub.api import tabs as tabs_api
    from claude_hub.services.agent_stream.tailer import TailerManager
    from claude_hub.services.ttyd_manager import TTYDManager

    manager, session_id, request, store, sender = await _terminal_reconciliation_case(mailbox)
    session = manager.sessions[session_id]
    ttyd_module = import_module("claude_hub.services.ttyd_manager")
    monkeypatch.setattr(ttyd_module, "STATE_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(ttyd_module, "ORDER_FILE", tmp_path / "order.json")
    monkeypatch.setattr(ttyd_module, "LAUNCH_ENV_DIR", tmp_path / "launch-env")
    tabs = TTYDManager()
    tab = await tabs.create_tab(
        name="Isolated config",
        cwd=session.workspace_path,
        agent_type=session.agent_type,
        session_kind=SessionKind.CHAT,
        workspace_id=session.workspace_id,
        workspace_role=session.role,
        managed_session_id=session_id,
        env={},
    )
    manager.sessions[session_id] = session.model_copy(update={"tab_id": tab.id})
    await store.replace_all(
        [event.model_copy(update={"tab_id": tab.id}) for event in store.iter_events()]
    )
    tailers = TailerManager(session_getter=lambda sid: manager.sessions.get(sid))
    monkeypatch.setattr(tabs_api, "ttyd_manager", tabs)
    monkeypatch.setattr(tabs_api, "workspace_manager", manager)
    monkeypatch.setattr(tabs_api, "_get_tailer_manager", lambda: tailers)
    return manager, session_id, request, sender, tab, tailers


@pytest.mark.parametrize("other_blocker", [False, True])
async def test_reconciliation_reaches_original_guarded_config_path(
    mailbox, monkeypatch, tmp_path, other_blocker
):
    from fastapi import HTTPException

    from claude_hub.api import tabs as tabs_api
    from claude_hub.models import SwitchEnvRequest, User

    manager, session_id, request, _sender, tab, tailers = await _real_reconciliation_config_case(
        mailbox, monkeypatch, tmp_path
    )
    user = User(open_id="controller", name="Controller", email="controller@example.test")
    change = SwitchEnvRequest(env={"ANTHROPIC_MODEL": "simulated-model"})
    if other_blocker:
        session = manager.sessions[session_id]
        manager.sessions[session_id] = session.model_copy(
            update={"processing_call_ids": session.processing_call_ids + ["unproven-call"]}
        )
    try:
        with pytest.raises(HTTPException) as old_guard:
            await tabs_api.switch_tab_env(tab.id, change, user)
        assert old_guard.value.status_code == 409
        assert manager.sessions[session_id].env == {}
        before = manager.tasks[request.expected_task_id].model_dump(
            exclude={"processing_call_ids", "reconciled_call_ids"}
        )
        await manager.reconcile_terminal_delivery(session_id, request, actor=user.open_id)
        if other_blocker:
            with pytest.raises(HTTPException) as still_blocked:
                await tabs_api.switch_tab_env(tab.id, change, user)
            assert still_blocked.value.status_code == 409
            assert manager.sessions[session_id].processing_call_ids == ["unproven-call"]
            assert manager.sessions[session_id].env == {}
        else:
            updated = await tabs_api.switch_tab_env(tab.id, change, user)
            assert updated.env == change.env
            assert manager.sessions[session_id].env == change.env
            tailer = tailers.get_tailer(session_id)
            assert tailer is not None and tailer.native_transport is not None
            assert tailer.native_transport.session.env == change.env
        assert (
            manager.tasks[request.expected_task_id].model_dump(
                exclude={"processing_call_ids", "reconciled_call_ids"}
            )
            == before
        )
        assert manager.sessions[session_id].delivered_call_ids == []
        assert manager.reports == {}
    finally:
        await tailers.stop_all()


async def test_send_switch_and_reconciliation_serialize_on_real_session_guard(
    mailbox, monkeypatch, tmp_path
):
    from claude_hub.api import tabs as tabs_api
    from claude_hub.models import SwitchEnvRequest, User

    manager, session_id, request, sender, tab, tailers = await _real_reconciliation_config_case(
        mailbox, monkeypatch, tmp_path
    )
    user = User(open_id="controller", name="Controller", email="controller@example.test")
    pump = manager._pump_locks.setdefault(session_id, asyncio.Lock())
    await pump.acquire()
    try:
        reconcile = asyncio.create_task(
            manager.reconcile_terminal_delivery(session_id, request, actor=user.open_id)
        )
        await asyncio.sleep(0)
        switch = asyncio.create_task(
            tabs_api.switch_tab_env(
                tab.id, SwitchEnvRequest(env={"ANTHROPIC_MODEL": "simulated-model"}), user
            )
        )
        await asyncio.sleep(0)
        send = asyncio.create_task(
            manager.send_chat_session_message(
                session_id, "new authorized input", call_id="new-input"
            )
        )
        await asyncio.sleep(0)
        assert not reconcile.done() and not switch.done() and not send.done()
    finally:
        pump.release()
    try:
        result, updated, _ = await asyncio.wait_for(
            asyncio.gather(reconcile, switch, send), timeout=5
        )
        assert result.outcome == "failed"
        assert updated.env == {"ANTHROPIC_MODEL": "simulated-model"}
        assert manager.sessions[session_id].processing_call_ids == ["new-input"]
        assert manager.sessions[session_id].delivered_call_ids == []
        assert sender.await_count == 2  # original terminal call and new input only
        assert manager.reports == {}
    finally:
        await tailers.stop_all()


async def test_reconciliation_uses_real_durable_handoff_and_terminal_guard(mailbox, monkeypatch):
    from types import SimpleNamespace

    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    session = manager.sessions[session_id]
    provider = SimpleNamespace(send_message=AsyncMock(), get_tailer=lambda _sid: None)
    monkeypatch.setattr(managed_chat, "_manager", lambda: provider)
    monkeypatch.setattr(managed_chat, "send_message", _REAL_CHAT_SEND)
    monkeypatch.setattr(managed_chat, "delivery_state", _REAL_CHAT_DELIVERY_STATE)
    monkeypatch.setattr(managed_chat, "is_turn_running", _REAL_CHAT_IS_RUNNING)
    managed_chat._write_attempts(session, {"version": 1, "attempts": {}})
    # Only the external provider boundary is simulated. Production handoff
    # writes its real immutable sending/accepted ledger; terminal guard reads
    # the real SQLite lifecycle rather than a mocked reconciliation verdict.
    await managed_chat.send_message(
        session, f"[call_id:{request.call_id}]\nquota-blocked review", [], request.call_id
    )
    assert managed_chat.delivery_state(session, request.call_id) == "accepted"
    assert not managed_chat.is_turn_running(session)
    with pytest.raises(RuntimeError, match="delivery to become idle"):
        async with manager.managed_chat_configuration_guard(session_id):
            pass
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="controller")
    assert record.outcome == "failed"
    async with manager.managed_chat_configuration_guard(session_id):
        pass
    restored = WorkspaceManager()
    assert managed_chat.delivery_state(restored.sessions[session_id], request.call_id) == "accepted"
    await restored.send_chat_session_message(
        session_id, "quota-blocked review", call_id=request.call_id
    )
    assert provider.send_message.await_count == 1
    assert restored.sessions[session_id].delivered_call_ids == []


async def test_late_mailbox_record_cannot_fabricate_terminal_task_provenance(mailbox):
    from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
    from claude_hub.services.task_graph import task_inbox_consumer_key

    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(
        mailbox, provenance=False
    )
    task = manager.tasks[request.expected_task_id]
    manager.task_mailbox.append_event(
        workspace_id=task.workspace_id,
        task_id=task.id,
        actor_role=TaskActorRole.SUPERVISOR,
        event_type=TaskEventType.FOLLOWUP,
        call_id=request.call_id,
        action="followup",
        consumer_key=task_inbox_consumer_key(task),
        target=task.id,
        review_cycle=task.review_cycle,
        payload={"message": "late origin"},
        persist=True,
    )
    with pytest.raises(ValueError, match="Terminal evidence"):
        await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert manager.sessions[session_id].processing_call_ids == [request.call_id]


async def test_resident_reviewer_keeps_original_mailbox_provenance(mailbox):
    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    task = manager.tasks[request.expected_task_id]
    manager.tasks[task.id] = task.model_copy(
        update={"session_id": "actual-worker", "review_session_id": session_id}
    )
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert record.task_id == task.id
    assert record.provenance == "task_mailbox"
    assert manager.tasks[task.id].session_id == "actual-worker"
    assert manager.tasks[task.id].review_session_id == session_id


async def test_terminal_reconciliation_refuses_task_control_handoff(mailbox):
    from claude_hub.models.schemas import TaskExecutionControl

    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    task = manager.tasks[request.expected_task_id]
    manager.tasks[task.id] = task.model_copy(
        update={"execution_control": TaskExecutionControl.INITIATOR}
    )
    with pytest.raises(RuntimeError):
        await manager.reconcile_terminal_delivery(session_id, request, actor="operator-user")
    assert manager.sessions[session_id].processing_call_ids == [request.call_id]


@pytest.mark.parametrize("field", ["turn_id", "payload_sha256", "missing_turn", "missing_digest"])
async def test_reconciliation_rejects_contradictory_durable_handoff(mailbox, field):
    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(mailbox)
    session = manager.sessions[session_id]
    evidence = managed_chat._read_attempts(session)
    record = evidence["attempts"][request.call_id]
    if field.startswith("missing"):
        record.pop("turn_id" if field == "missing_turn" else "payload_sha256")
    else:
        record[field] = "contradictory-evidence"
    managed_chat._write_attempts(session, evidence)
    # The existing coarse state alone still accepts this record; the new
    # operator proof must reject it without closing any processing envelope.
    assert _REAL_CHAT_DELIVERY_STATE(session, request.call_id) == "accepted"
    with pytest.raises(ValueError, match="handoff identity or payload"):
        await manager.reconcile_terminal_delivery(session_id, request, actor="controller")
    assert manager.sessions[session_id].processing_call_ids == [request.call_id]
    assert manager.sessions[session_id].reconciled_deliveries == {}
    assert manager.tasks[request.expected_task_id].processing_call_ids == [request.call_id]


async def test_historical_mailbox_origin_is_retained_without_current_task_adoption(mailbox):
    from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
    from claude_hub.services.task_graph import task_inbox_consumer_key

    manager, session_id, _receipts, _sender = mailbox
    original_id = _bind_task(manager, session_id)
    original = manager.tasks[original_id]
    manager.task_mailbox.append_event(
        workspace_id=original.workspace_id,
        task_id=original.id,
        actor_role=TaskActorRole.SUPERVISOR,
        event_type=TaskEventType.FOLLOWUP,
        call_id="terminal-call",
        action="followup",
        consumer_key=task_inbox_consumer_key(original),
        target=original.id,
        review_cycle=original.review_cycle,
        payload={"message": "original historical review"},
        persist=True,
    )
    manager, session_id, request, _store, _sender = await _terminal_reconciliation_case(
        mailbox, "completed", provenance=False
    )
    current = manager.tasks[request.expected_task_id]
    manager.tasks[current.id] = current.model_copy(update={"processing_call_ids": []})
    before = manager.tasks[current.id].model_dump()
    record = await manager.reconcile_terminal_delivery(session_id, request, actor="controller")
    assert record.task_id == original.id
    assert record.review_cycle == original.review_cycle
    assert record.guard_task_id == current.id
    assert record.provenance == "task_mailbox"
    assert manager.tasks[original.id] == original
    assert manager.tasks[current.id].model_dump() == before
