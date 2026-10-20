"""Isolated durable inbox contract and crash-boundary tests."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from claude_hub.models.chat_message import ChatMessageCreate, utc_now
from claude_hub.services.chat_message_inbox import (
    ChatMessageInbox,
    InboxError,
    InboxEvidence,
    InboxTarget,
)


class Harness:
    def __init__(self, path: Path, **limits: int) -> None:
        self.target_value = InboxTarget("inc-1", "conversation-1")
        self.calls: list[str] = []
        self.proof = InboxEvidence("absent")
        self.mode = "ok"
        self.inbox = ChatMessageInbox(path, self.target, self.submit, self.evidence, **limits)

    def target(self, tab: str) -> InboxTarget:
        if self.mode == "deleted":
            raise InboxError(404, "deleted")
        return self.target_value

    async def evidence(self, item):
        return self.proof

    async def submit(self, item, before):
        self.calls.append(item.call_id)
        if self.mode == "busy":
            raise InboxError(409, "busy")
        before()
        if self.mode == "ambiguous":
            raise RuntimeError("lost provider ack")
        if self.mode == "fast_complete":
            await self.inbox.completed(item.tab_id, item.turn_id, "completed")

    async def enqueue(self, call: str = "call-1", text: str = "hello", principal: str = "actor"):
        return await self.inbox.enqueue(
            "tab", principal, principal, ChatMessageCreate(message=text, call_id=call)
        )


@pytest.mark.asyncio
async def test_durable_reload_and_concurrent_idempotency(tmp_path):
    h = Harness(tmp_path / "inbox.json")
    receipts = await asyncio.gather(*(h.enqueue() for _ in range(20)))
    assert len({r.message_id for r in receipts}) == 1
    assert json.loads(h.inbox.path.read_text())["messages"][0]["message"] == "hello"
    clone = Harness(h.inbox.path)
    assert (await clone.enqueue()).message_id == receipts[0].message_id
    with pytest.raises(InboxError) as exc:
        await clone.enqueue(text="different")
    assert exc.value.status == 409
    assert (await clone.enqueue(principal="other")).message_id != receipts[0].message_id
    assert len(list(tmp_path.iterdir())) == 1  # call ids never become paths


@pytest.mark.asyncio
async def test_source_hint_part_of_fingerprint_and_private_receipts(tmp_path):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()
    with pytest.raises(InboxError) as exc:
        await h.inbox.enqueue(
            "tab",
            "actor",
            "actor",
            ChatMessageCreate(message="hello", call_id="call-1", source_tab_id="spoof"),
        )
    assert exc.value.status == 409
    for tab, principal in [("other", "actor"), ("tab", "other")]:
        with pytest.raises(InboxError) as exc:
            await h.inbox.get(tab, r.message_id, principal)
        assert exc.value.status == 404


@pytest.mark.parametrize("limits", [{"max_pending": 1}, {"max_bytes": 5}])
@pytest.mark.asyncio
async def test_caps_and_retry_when_full(tmp_path, limits):
    h = Harness(tmp_path / "store.json", **limits)
    r = await h.enqueue()
    assert (await h.enqueue()).message_id == r.message_id
    with pytest.raises(InboxError) as exc:
        await h.enqueue("call-2")
    assert exc.value.status == 429
    await h.inbox.cancel("tab", r.message_id, "actor")
    await h.enqueue("call-2")


@pytest.mark.asyncio
async def test_fifo_busy_no_coalescing_and_completion(tmp_path):
    h = Harness(tmp_path / "store.json")
    a, b = await h.enqueue(), await h.enqueue("call-2")
    h.mode = "busy"
    await h.inbox.pump("tab")
    assert (await h.inbox.get("tab", a.message_id, "actor")).state == "waiting"
    assert h.calls == ["call-1"]
    h.mode = "ok"
    await asyncio.gather(h.inbox.pump("tab"), h.inbox.pump("tab"))
    assert h.calls == ["call-1", "call-1"]
    assert (await h.inbox.get("tab", a.message_id, "actor")).state == "accepted"
    await h.inbox.completed("tab", a.turn_id, "completed")
    await h.inbox.pump("tab")
    assert h.calls[-1] == "call-2"
    assert (await h.inbox.get("tab", b.message_id, "actor")).state == "accepted"


@pytest.mark.asyncio
async def test_cancel_queued_waiting_and_reject_submitted(tmp_path):
    h = Harness(tmp_path / "store.json")
    a = await h.enqueue()
    h.target_value = InboxTarget("inc-1", "conversation-1", "goal_active")
    await h.inbox.pump("tab")
    assert (await h.inbox.cancel("tab", a.message_id, "actor")).state == "cancelled"
    assert (await h.inbox.cancel("tab", a.message_id, "actor")).state == "cancelled"
    b = await h.enqueue("next")
    h.target_value = InboxTarget("inc-1", "conversation-1")
    await h.inbox.pump("tab")
    with pytest.raises(InboxError) as exc:
        await h.inbox.cancel("tab", b.message_id, "actor")
    assert exc.value.status == 409


@pytest.mark.parametrize(
    "proof,started,state,expected",
    [
        ("absent", False, "dispatching", "queued"),
        ("absent", True, "dispatching", "uncertain"),
        ("started", False, "dispatching", "uncertain"),
        ("absent", False, "accepted", "uncertain"),
        ("live", True, "accepted", "accepted"),
        ("completed", True, "accepted", "completed"),
        ("failed", True, "uncertain", "failed"),
        ("cancelled", True, "dispatching", "cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_restart_evidence_matrix(tmp_path, proof, started, state, expected):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()
    h.inbox.messages[r.message_id].state = state
    h.inbox.messages[r.message_id].submission_started = started
    h.inbox._save()
    clone = Harness(h.inbox.path)
    clone.proof = InboxEvidence(proof)
    await clone.inbox.reconcile(restarting=True)
    assert (await clone.inbox.get("tab", r.message_id, "actor")).state == expected
    assert clone.calls == []


@pytest.mark.asyncio
async def test_uncertain_blocks_following_and_terminal_evidence_releases(tmp_path):
    h = Harness(tmp_path / "store.json")
    a = await h.enqueue()
    await h.enqueue("next")
    h.mode = "ambiguous"
    await h.inbox.pump("tab")
    await h.inbox.pump("tab")
    assert h.calls == ["call-1"]
    assert (await h.inbox.get("tab", a.message_id, "actor")).state == "uncertain"
    h.proof = InboxEvidence("completed")
    await h.inbox.reconcile()
    h.mode = "ok"
    await h.inbox.pump("tab")
    assert h.calls == ["call-1", "next"]


@pytest.mark.asyncio
async def test_completion_before_ack_wins(tmp_path):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()
    h.mode = "fast_complete"
    await h.inbox.pump("tab")
    assert (await h.inbox.get("tab", r.message_id, "actor")).state == "completed"


@pytest.mark.asyncio
async def test_archive_reject_hold_restore(tmp_path):
    h = Harness(tmp_path / "store.json")
    h.target_value = InboxTarget("inc-1", "conversation-1", "archived")
    with pytest.raises(InboxError) as exc:
        await h.enqueue()
    assert exc.value.status == 409
    h.target_value = InboxTarget("inc-1", "conversation-1")
    r = await h.enqueue()
    h.target_value = InboxTarget("inc-1", "conversation-1", "archived")
    await h.inbox.pump("tab")
    assert (await h.inbox.get("tab", r.message_id, "actor")).waiting_reason == "archived"
    h.target_value = InboxTarget("inc-1", "conversation-1")
    await h.inbox.pump("tab")
    assert h.calls == ["call-1"]


@pytest.mark.parametrize(
    "change,expected",
    [("deleted", "cancelled"), ("incarnation", "failed"), ("conversation", "failed")],
)
@pytest.mark.asyncio
async def test_target_fences(tmp_path, change, expected):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()
    if change == "deleted":
        h.mode = "deleted"
    else:
        h.target_value = InboxTarget(
            "inc-2" if change == "incarnation" else "inc-1", "new-conversation"
        )
    await h.inbox.pump("tab")
    assert (await h.inbox.get("tab", r.message_id, "actor")).state == expected
    assert h.calls == []


@pytest.mark.asyncio
async def test_save_failure_does_not_ack_and_fails_closed(tmp_path, monkeypatch):
    h = Harness(tmp_path / "store.json")
    monkeypatch.setattr(
        "claude_hub.services.chat_message_inbox.os.replace", Mock(side_effect=OSError("disk full"))
    )
    with pytest.raises(OSError):
        await h.enqueue()
    assert h.inbox.messages == {}
    with pytest.raises(InboxError) as exc:
        await h.enqueue()
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_expiry_keeps_nonreplayable_tombstone(tmp_path):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()
    await h.inbox.completed("tab", r.turn_id, "completed")
    h.inbox.messages[r.message_id].updated_at = utc_now() - timedelta(days=31)
    await h.inbox.reconcile()
    assert h.inbox.messages[r.message_id].message == ""
    clone = Harness(h.inbox.path)
    for operation in [clone.enqueue(), clone.inbox.get("tab", r.message_id, "actor")]:
        with pytest.raises(InboxError) as exc:
            await operation
        assert exc.value.status == 410


@pytest.mark.parametrize(
    "payload",
    [
        {"message": " ", "call_id": "x"},
        {"message": "x", "call_id": " "},
        {"message": "界" * 21846, "call_id": "x"},
        {"message": "x", "call_id": "x", "origin": "feishu"},
        {"message": "x", "call_id": "x", "steer": True},
        {"message": "x", "call_id": "x", "source_tab_id": " "},
    ],
)
def test_strict_public_contract(payload):
    with pytest.raises(ValidationError):
        ChatMessageCreate.model_validate(payload)


def test_corrupt_or_unknown_store_fails_closed(tmp_path):
    path = tmp_path / "store.json"
    path.write_text('{"version":999}')
    with pytest.raises(RuntimeError):
        Harness(path)
    path.write_text("not-json")
    with pytest.raises(json.JSONDecodeError):
        Harness(path)


@pytest.mark.asyncio
async def test_cancel_race_dispatching_cannot_cancel(tmp_path):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(item, before):
        entered.set()
        await release.wait()
        before()

    h.inbox.submit = gated
    task = asyncio.create_task(h.inbox.pump("tab"))
    await entered.wait()
    with pytest.raises(InboxError) as exc:
        await h.inbox.cancel("tab", r.message_id, "actor")
    assert exc.value.status == 409
    release.set()
    await task
    assert (await h.inbox.get("tab", r.message_id, "actor")).state == "accepted"


@pytest.mark.asyncio
async def test_goal_changed_between_dispatch_and_delivery_holds(tmp_path):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()

    async def changed(item, before):
        h.target_value = InboxTarget("inc-1", "conversation-1", "goal_active")
        before()
        pytest.fail("must not deliver after Goal ownership changes")

    h.inbox.submit = changed
    await h.inbox.pump("tab")
    item = h.inbox.messages[r.message_id]
    assert item.state == "waiting"
    assert not item.submission_started


@pytest.mark.asyncio
async def test_evidence_read_error_never_replays(tmp_path):
    h = Harness(tmp_path / "store.json")
    r = await h.enqueue()
    h.inbox.messages[r.message_id].state = "dispatching"

    async def unavailable(item):
        raise OSError("transcript unreadable")

    h.inbox.evidence = unavailable
    await h.inbox.reconcile(restarting=True)
    await h.inbox.pump("tab")
    assert h.inbox.messages[r.message_id].state == "uncertain"
    assert h.calls == []


@pytest.mark.asyncio
async def test_conversation_reset_allows_new_intent_without_delivering_old(tmp_path):
    h = Harness(tmp_path / "store.json")
    old = await h.enqueue()
    h.target_value = InboxTarget("inc-1", "conversation-2")
    new = await h.enqueue("new-intent")
    await h.inbox.pump("tab")
    assert h.inbox.messages[old.message_id].state == "failed"
    assert h.inbox.messages[new.message_id].state == "accepted"
    assert h.calls == ["new-intent"]


@pytest.mark.asyncio
async def test_reset_then_initial_provider_id_binds_same_new_epoch(tmp_path):
    h = Harness(tmp_path / "store.json")
    old = await h.enqueue()
    h.target_value = InboxTarget("inc-1", None)
    new = await h.enqueue("post-reset-intent")
    new_epoch = h.inbox.messages[new.message_id].incarnation

    async def assign_provider_id(item, before):
        h.calls.append(item.call_id)
        # Native readiness may persist its initial provider ID only after the
        # new intent was accepted. This binds the reset epoch, not a new one.
        h.target_value = InboxTarget("inc-1", "conversation-2")
        before()

    h.inbox.submit = assign_provider_id
    await h.inbox.pump("tab")
    assert h.inbox.messages[old.message_id].state == "failed"
    assert h.inbox.messages[new.message_id].state == "accepted"
    assert h.inbox.messages[new.message_id].incarnation == new_epoch
    assert h.calls == ["post-reset-intent"]
