"""Goal formatting instructions expire without filtering assistant output."""

from unittest.mock import AsyncMock

import pytest

from claude_hub.models.goal_run import GoalRun
from claude_hub.services.agent_stream.native import (
    HUB_RUNTIME_GUIDANCE,
    CodexNativeSession,
    strip_hub_runtime_guidance,
    wrap_hub_runtime_guidance,
)
from claude_hub.services.goal_run import build_continuation_prompt
from tests.test_native_goal_transport import _session


def test_hub_continuation_formatting_is_explicitly_scoped_to_its_response():
    prompt = build_continuation_prompt(GoalRun(tab_id="tab", objective="Verify the change"))
    assert "requirements in this message apply only to this response" in prompt
    assert "only when that turn supplies fresh Hub Goal continuation instructions" in prompt
    assert "<goal-checkpoint>" in prompt
    assert '<goal-status state="continue|complete|blocked|needs_input">' in prompt


def test_runtime_guidance_revokes_historical_formatting_without_nested_frames():
    text = "Explain the last result"
    wrapped = wrap_hub_runtime_guidance(text, context="Current runtime context")
    assert "earlier requests for <goal-checkpoint> or <goal-status> formatting expire" in wrapped
    assert (
        "For ordinary Chat or provider-native Goals, ignore those historical formatting requirements"
        in wrapped
    )
    assert "quoting or discussing the protocol when the user asks" in HUB_RUNTIME_GUIDANCE
    assert wrapped.count("<claude_hub_instructions>") == 1
    assert strip_hub_runtime_guidance(wrapped) == text


def test_runtime_strip_preserves_visible_protocol_text_outside_input_guidance():
    text = '<goal-checkpoint>{"verified_progress": []}</goal-checkpoint>'
    assert strip_hub_runtime_guidance(text) == text
    assert strip_hub_runtime_guidance(wrap_hub_runtime_guidance(text)) == text


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_resumed_transport_scopes_first_text_after_image_only_input(
    monkeypatch, continuation
):
    session = _session()
    session.agent_session_id = "existing-thread"
    session.agent_session_id_verified = True
    transport = CodexNativeSession(session)
    monkeypatch.setattr(transport, "_prepare_turn_context", AsyncMock(return_value=""))
    assert await transport._with_first_turn_prefixes("") == ("", False)
    assert not transport._hub_runtime_guidance_injected
    text = (
        build_continuation_prompt(GoalRun(tab_id="tab", objective="Verify the change"))
        if continuation
        else "Explain the last result"
    )
    wrapped, seeded = await transport._with_first_turn_prefixes(text)
    assert not seeded
    assert wrapped.count("<claude_hub_instructions>") == 1
    assert "historical formatting requirements" in wrapped
    assert strip_hub_runtime_guidance(wrapped) == text
    if continuation:
        assert "Before the final status, emit this bounded JSON working-memory block" in wrapped
    second, _ = await transport._with_first_turn_prefixes("Another ordinary message")
    assert second == "Another ordinary message"
