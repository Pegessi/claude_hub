"""Tests for the in-memory Feishu card-action result store."""

from __future__ import annotations

from datetime import datetime, timedelta

from claude_hub.services.feishu_card_results import (
    STATUS_PENDING,
    STATUS_RESOLVED,
    STATUS_UNKNOWN,
    CardResultStore,
)


def test_unknown_token_is_unknown() -> None:
    store = CardResultStore()
    result = store.get("nope")
    assert result["status"] == STATUS_UNKNOWN
    assert result["token"] == "nope"


def test_register_then_pending() -> None:
    store = CardResultStore()
    store.register("tok123", chat_id="oc_1", kind="approval")
    result = store.get("tok123")
    assert result["status"] == STATUS_PENDING
    assert result["chat_id"] == "oc_1"
    assert result["kind"] == "approval"


def test_submit_resolves() -> None:
    store = CardResultStore()
    store.register("tok123", chat_id="oc_1", kind="approval")
    ok = store.submit("tok123", action="approve", form={}, operator_id="ou_x")
    assert ok is True
    result = store.get("tok123")
    assert result["status"] == STATUS_RESOLVED
    assert result["action"] == "approve"
    assert result["operator_id"] == "ou_x"


def test_submit_unknown_token_fails() -> None:
    store = CardResultStore()
    assert store.submit("ghost", action="approve", form={}, operator_id=None) is False


def test_first_write_wins() -> None:
    store = CardResultStore()
    store.register("tok", chat_id="oc_1", kind="approval")
    assert store.submit("tok", action="approve", form={}, operator_id="a") is True
    # Second submit must not overwrite the recorded decision.
    assert store.submit("tok", action="reject", form={}, operator_id="b") is False
    assert store.get("tok")["action"] == "approve"


def test_register_is_idempotent() -> None:
    store = CardResultStore()
    store.register("tok", chat_id="oc_1", kind="approval")
    store.submit("tok", action="approve", form={}, operator_id="a")
    # Re-registering must not clobber the resolved decision.
    store.register("tok", chat_id="oc_2", kind="needs_input")
    result = store.get("tok")
    assert result["status"] == STATUS_RESOLVED
    assert result["action"] == "approve"


def test_form_values_round_trip() -> None:
    store = CardResultStore()
    store.register("tok", chat_id="oc_1", kind="needs_input")
    store.submit("tok", action="submit", form={"reply": "ship it"}, operator_id=None)
    assert store.get("tok")["form"] == {"reply": "ship it"}


def test_expired_unresolved_token_pruned() -> None:
    store = CardResultStore(ttl=timedelta(minutes=10))
    base = datetime(2026, 1, 1, 12, 0, 0)
    store.register("tok", chat_id="oc_1", kind="approval", now=base)
    later = base + timedelta(minutes=20)
    result = store.get("tok", now=later)
    assert result["status"] == STATUS_UNKNOWN


def test_resolved_token_readable_within_ttl() -> None:
    store = CardResultStore(ttl=timedelta(minutes=10))
    base = datetime(2026, 1, 1, 12, 0, 0)
    store.register("tok", chat_id="oc_1", kind="approval", now=base)
    store.submit("tok", action="approve", form={}, operator_id=None, now=base)
    soon = base + timedelta(minutes=5)
    assert store.get("tok", now=soon)["status"] == STATUS_RESOLVED


def test_discard_removes_token() -> None:
    store = CardResultStore()
    store.register("tok", chat_id="oc_1", kind="approval")
    store.discard("tok")
    assert store.get("tok")["status"] == STATUS_UNKNOWN


def test_prune_counts_expired() -> None:
    store = CardResultStore(ttl=timedelta(minutes=10))
    base = datetime(2026, 1, 1, 12, 0, 0)
    store.register("a", chat_id="oc_1", kind="approval", now=base)
    store.register("b", chat_id="oc_2", kind="approval", now=base)
    removed = store.prune(now=base + timedelta(minutes=20))
    assert removed == 2
