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
    format_task_brief,
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
    assert "Work like a secretary: reply fast" in TURN_RULES
    assert "Chat apps show only the last message of your turn" in TURN_RULES
    assert "Start agent work with `claude-hub dot delegate`" in TURN_RULES
    # Decisions belong on the question card, not in a closing prose question.
    assert "ask it with the question tool" in TURN_RULES
    assert "2–4 concrete options" in TURN_RULES
    assert "one-line tradeoff" in TURN_RULES
    assert "[--session <agent-session-id>]" in TOOL_GUIDE
    assert "[--recipe standard|triage_diagnose|spec_implement_review|eval_and_report]" in TOOL_GUIDE
    for std_recipe in (None, "standard", "eval_and_report"):
        rendered_std = format_task_brief(
            goal="G",
            context="C",
            done_when="1. D",
            output="O",
            out_of_scope="N",
            constraints="K",
            recipe=std_recipe,
        )
        assert "Post your plan (Goal Packet) before substantial work" in rendered_std
    assert "--title" in TOOL_GUIDE and "--tone" in WAKE_MESSAGE
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


def test_tool_guide_is_compact_and_preserves_workspace_lessons_and_safety_rules() -> None:
    assert 800 <= len(TOOL_GUIDE.strip()) <= 1100
    for token in (
        "--help",
        "CLAUDE_HUB_URL",
        "dot show|remember|forget|cards|journal|inbox|lessons|health|status|identity|rotate|patrol|scout",
        "workspace list",
        "task create|dispatch|get|continue|followup",
        "agent list|status|create|run|send|result|close",
        "session logs",
        "lessons list|get|create",
        "claude-hub lessons create <workspace_id> --summary ... --do ... --avoid ...",
        "dot cards",
        "dot lessons",
        "memory/<kind>/",
        "may be read directly",
        "never store or leak internal paths or secrets",
    ):
        assert token in TOOL_GUIDE, token
    for bogus in ("dot card/", "dot files", "dot digest", "agent output", "create/send/output"):
        assert bogus not in TOOL_GUIDE, bogus


def test_format_task_brief_recipe_acceptance_checklists() -> None:
    eval_brief = format_task_brief(
        goal="Benchmark serving throughput",
        context="Compare candidate vs baseline",
        done_when="1. Report written",
        output="reports/bench.md",
        out_of_scope="Code edits",
        constraints="Read-only",
        recipe="eval_and_report",
    )
    assert "## Recipe acceptance checklist" in eval_brief
    assert "sweep axis, load, and batch coverage across the intended operating range" in eval_brief
    assert "not bunched at one point" in eval_brief
    assert (
        "Normalize per-replica or unit-resource throughput against an apples-to-apples "
        "baseline distribution"
    ) in eval_brief
    assert (
        "Update all affected tables, charts, and conclusions coherently throughout the "
        "report body rather than appending an unintegrated tail section"
    ) in eval_brief

    triage_brief = format_task_brief(
        goal="Diagnose timeout regression",
        context="Inspect logs and runtime flags",
        done_when="1. Root cause identified",
        output="Reply in report",
        out_of_scope="Code changes",
        constraints="Read-only",
        recipe="triage_diagnose",
    )
    assert "Post your plan (Goal Packet) before substantial work" not in triage_brief
    assert "Perform read-only investigation directly within scope" in triage_brief
    assert "## Recipe acceptance checklist" in triage_brief
    assert (
        "Trace back to the primary root-cause error log and config/parameter derivation "
        "chain (verifying actual effective runtime values vs defaults), rather than "
        "repeating secondary or cascading symptoms"
    ) in triage_brief
    assert "State concrete verification commands and evidence" in triage_brief

    std_brief = format_task_brief(
        goal="G",
        context="C",
        done_when="1. D",
        output="O",
        out_of_scope="N",
        constraints="K",
        recipe="standard",
    )
    assert "## Recipe acceptance checklist" not in std_brief


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
    store.update_state(tab_id="tab-dot")
    first = service._prepare_turn("hi", None)
    assert service.associate_conversation(
        "provider-1", prepared_generation=first.prepared_generation, tab_id="tab-dot"
    )
    assert "Custom role first version" in first.system_pack
    assert TURN_RULES.strip() in first.system_pack
    store.role_path.write_text("Custom role second version.\n")
    same = service._prepare_turn("again", "provider-1")
    assert same.system_pack == first.system_pack
    assert not same.rotate
    service.request_rotation()
    fresh = service._prepare_turn("new topic", "provider-1")
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


def test_schedules_use_durable_hub_targets_even_with_a_custom_dot_role(tmp_path: Path) -> None:
    from claude_hub.services.agent_stream.providers.guidance import (
        HUB_RUNTIME_GUIDANCE,
        SUBAGENT_HUB_RUNTIME_GUIDANCE,
    )

    store = DotStore(tmp_path / "dot")
    store.ensure()
    store.role_path.write_text("A customized Dot role.")
    pack = build_session_pack(store)
    for guidance in (TURN_RULES, pack, HUB_RUNTIME_GUIDANCE, SUBAGENT_HUB_RUNTIME_GUIDANCE):
        assert "Never use provider-native" in guidance
        assert "CreateCronJob" in guidance
        assert (
            'claude-hub schedule create --kind chat_turn --tab-id "$CLAUDE_HUB_TAB_ID"' in guidance
        )
        assert "ephemeral provider conversation" in guidance
    assert "A customized Dot role." in pack
