"""Dot guidance stays concise without weakening ownership or stored customization."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.dot.context import build_session_pack
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
    assert "when useful" in TURN_RULES
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
