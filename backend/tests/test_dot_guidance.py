"""Dot guidance stays concise without weakening ownership or stored customization."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.dot.context import TURN_PACK_CHARS, build_session_pack, build_turn_pack
from claude_hub.services.dot.prompts import (
    BRIEF_TEMPLATE,
    DEFAULT_ROLE,
    QUIET_REPLY,
    RETIRED_TIDY_MESSAGES,
    RETIRED_WAKE_MESSAGES,
    TIDY_MESSAGE,
    TOOL_GUIDE,
    TURN_RULES,
    WAKE_MESSAGE,
)


def test_guidance_preserves_choices_and_explicit_tool_contracts() -> None:
    assert "Handle small, bounded requests directly" in DEFAULT_ROLE
    assert "A targeted check can help" in DEFAULT_ROLE
    assert "Do not store secrets or passwords" in DEFAULT_ROLE
    assert "Delegate long or many-step work" in DEFAULT_ROLE
    assert "Delegate long or many-step work to Hub Tasks" in TURN_RULES
    assert "when useful" not in TURN_RULES
    assert "lengthy test runs" in TURN_RULES
    assert "tracked tasks (delegated or watched)" in TOOL_GUIDE
    assert "not a watched task or human acceptance" in TOOL_GUIDE
    assert "A redo does not restart work" in TOOL_GUIDE
    assert "once the user agrees" in TOOL_GUIDE
    assert "open gap" in BRIEF_TEMPLATE
    assert "Follow the repository rules" in BRIEF_TEMPLATE
    active = "\n".join(
        (DEFAULT_ROLE, TURN_RULES, TOOL_GUIDE, BRIEF_TEMPLATE, WAKE_MESSAGE, TIDY_MESSAGE)
    )
    for absolute in ("Never poll", "do not rerun", "two or three", "Judge each returned task"):
        assert absolute not in active
    for message, prefix in ((WAKE_MESSAGE, "[Dot wake]"), (TIDY_MESSAGE, "[Dot tidy]")):
        assert message.startswith(prefix)
        assert f"replying exactly {QUIET_REPLY} keeps this turn hidden" in message
    assert WAKE_MESSAGE not in RETIRED_WAKE_MESSAGES
    assert TIDY_MESSAGE not in RETIRED_TIDY_MESSAGES


@pytest.mark.parametrize("role", [b"Custom role: ask before changing memory.\n", b"", b"\xff\xfe"])
def test_existing_role_is_not_rewritten(tmp_path: Path, role: bytes) -> None:
    home = tmp_path / "dot"
    home.mkdir()
    path = home / "DOT.md"
    path.write_bytes(role)
    store = DotStore(home)
    store.ensure()
    build_session_pack(store)
    assert path.read_bytes() == role
    DotStore(home).ensure()
    assert path.read_bytes() == role


def test_new_home_uses_guidance_and_existing_session_keeps_its_snapshot(tmp_path: Path) -> None:
    store = DotStore(tmp_path / "dot")
    store.ensure()
    assert store.role_path.read_text() == DEFAULT_ROLE
    store.role_path.write_text("Custom role first version.\n")
    service = DotService(store)
    first = service._prepare_turn("hi", None)
    assert "Custom role first version" in first.system_pack
    assert TURN_RULES.strip() in first.system_pack
    store.role_path.write_text("Custom role second version.\n")
    same = service._prepare_turn("again", first.conversation_id)
    assert same.system_pack == first.system_pack
    assert not same.rotate
    service.request_rotation()
    fresh = service._prepare_turn("new topic", first.conversation_id)
    assert fresh.rotate
    assert "Custom role second version" in fresh.system_pack
    assert "Custom role first version" not in fresh.system_pack
    assert store.role_path.read_text() == "Custom role second version.\n"


@pytest.mark.parametrize("wake", [True, False], ids=["wake", "user"])
def test_tracked_work_distinguishes_ownership_without_changing_review_state(
    tmp_path: Path,
    wake: bool,
) -> None:
    store = DotStore(tmp_path / "dot")
    for task_id, watched in (("delegated", False), ("watched", True)):
        store.upsert_work(
            task_id,
            title=task_id,
            workspace_id="ws",
            status="review",
            watched=watched,
            review_due=True,
        )
    # Older delegated entries omit the optional watched flag.
    store.upsert_work("legacy", title="legacy", workspace_id="ws", status="working")
    before = store.list_work()
    pack = build_turn_pack(store, None, "", first_turn=False, session_turn=1, wake=wake)
    assert "## Tracked work" in pack.text and "## Delegated work" not in pack.text
    assert "[delegated] delegated — review (delegated; workspace ws)" in pack.text
    assert "[watched] watched — review (watched; workspace ws)" in pack.text
    assert "[legacy] legacy — working (delegated; workspace ws)" in pack.text
    reviews = pack.text.split("## Waiting for your review", 1)[1]
    assert "[delegated]" in reviews and "[watched]" not in reviews
    assert store.list_work() == before
    assert len(pack.text) <= TURN_PACK_CHARS


@pytest.mark.parametrize("watched", [True, False], ids=["watched", "delegated"])
def test_result_card_includes_tracking_without_taking_review_ownership(
    tmp_path: Path,
    watched: bool,
) -> None:
    store = DotStore(tmp_path / "dot")
    item = store.upsert_work(
        "task-1",
        title="Result",
        workspace_id="ws",
        status="review",
        watched=watched,
    )
    card = DotService(store)._result_card(item, "review", None)
    tracking = "watched" if watched else "delegated"
    assert f"Tracking: {tracking} by Dot" in card.body
    if watched:
        assert "task ownership is unchanged" in card.body
    assert store.get_work("task-1") == item
