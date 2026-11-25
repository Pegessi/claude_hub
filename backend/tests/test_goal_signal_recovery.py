"""Sanitized incident shape: interrupted work, resumed delivery, protocol repair."""

from pathlib import Path

import pytest

from claude_hub.models.goal_run import GoalRunCreate, GoalRunStatus
from claude_hub.services.goal_run import GoalRunController, GoalRunStore, StrictReviewOutcome

DELIVERY = "Implementation and local checks passed. Please choose the next evaluation target."
CHECKPOINT = (
    '<goal-checkpoint>{"verified_progress":[{"item":"implementation",'
    '"evidence":"isolated checks passed"}],"remaining":[],"blocker":null}'
    "</goal-checkpoint>"
)


def envelope(state: str = "complete") -> str:
    return CHECKPOINT + f'\n<goal-status state="{state}">Verified outcome</goal-status>'


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["complete", "needs_input", "continue"])
async def test_interrupted_delivery_repairs_once_and_preserves_strict_gate(tmp_path, signal):
    sent = []
    reviewed = []

    async def dispatch(goal, prompt):
        sent.append((goal, prompt))
        return goal.pending_step_id

    async def review(goal, assistant_text, round_number):
        reviewed.append((goal, round_number, assistant_text))
        return StrictReviewOutcome(
            verdict="PASS", summary="Verified", findings=[], reviewer_provider="other"
        )

    path = tmp_path / "goals.json"
    manager = GoalRunController(GoalRunStore(path), dispatch, strict_reviewer=review)
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(
            objective="Deliver and verify the feature", client_request_id="new", strict_review=True
        ),
    )
    first = goal.current_turn_id
    await manager.on_turn_completed("tab", first, "cancelled", "")
    assert manager.get(goal.id).status == GoalRunStatus.PAUSED
    goal = await manager.resume(goal.id, "restart-recovery")
    second = goal.current_turn_id
    goal = await manager.on_turn_completed("tab", second, "completed", DELIVERY)
    assert goal.status == GoalRunStatus.ACTIVE
    assert goal.strict_review_rounds == 0
    assert len(sent) == 3
    assert "Do not repeat implementation" in sent[-1][1]
    assert DELIVERY in sent[-1][1]
    assert "signal_recovery_text" not in goal.model_dump()
    # Duplicate original completion and a cold store must not grant another repair.
    await manager.on_turn_completed("tab", second, "completed", DELIVERY)
    assert len(sent) == 3
    manager = GoalRunController(GoalRunStore(path), dispatch, strict_reviewer=review)
    repaired = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", envelope(signal)
    )
    assert (
        repaired.status
        == {
            "complete": GoalRunStatus.COMPLETE,
            "needs_input": GoalRunStatus.BLOCKED,
            "continue": GoalRunStatus.ACTIVE,
        }[signal]
    )
    assert bool(reviewed) == (signal == "complete")
    if reviewed:
        assert DELIVERY in reviewed[0][2]
        assert repaired.strict_review_rounds == 1


@pytest.mark.asyncio
async def test_repair_exhaustion_parks_for_human_without_losing_delivery(tmp_path: Path):
    sent = []

    async def dispatch(goal, prompt):
        sent.append(prompt)
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new")
    )
    assert goal.current_turn_id is not None
    repair = await manager.on_turn_completed("tab", goal.current_turn_id, "completed", DELIVERY)
    assert repair is not None and repair.current_turn_id is not None
    result = await manager.on_turn_completed(
        "tab", repair.current_turn_id, "completed", "Still no signal."
    )
    assert result is not None and result.status == GoalRunStatus.BLOCKED
    assert result.status_message is not None and "signal" in result.status_message
    assert result.completed_at is None
    assert len(sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,text",
    [
        ("failed", envelope()),
        ("cancelled", envelope()),
        ("completed", ""),
        ("completed", " \n\u200b"),
    ],
)
async def test_real_failure_or_empty_result_never_repairs_or_completes(tmp_path, status, text):
    sent = []

    async def dispatch(goal, prompt):
        sent.append(prompt)
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new")
    )
    goal = await manager.on_turn_completed("tab", goal.current_turn_id, status, text)
    assert goal.status in {GoalRunStatus.PAUSED, GoalRunStatus.FAILED}
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["plain", "fenced", "separate"])
async def test_protocol_markdown_wrapping_is_accepted(tmp_path, wrapper):
    text = envelope()
    if wrapper == "fenced":
        text = f"```xml\n{text}\n```"
    elif wrapper == "separate":
        text = f"```json\n{CHECKPOINT}\n```\n```xml\n<goal-status state='complete'>Done</goal-status>\n```"
    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"))
    goal = manager.create("tab", GoalRunCreate(objective="Verify", client_request_id="new"))
    from claude_hub.models.goal_run import GoalDispatchState

    goal.current_turn_id = "turn"
    goal.dispatch_state = GoalDispatchState.DISPATCHED
    manager.store.put(goal)
    result = await manager.on_turn_completed(
        "tab", "turn", "completed", "Delivery.\n" + text + " \n"
    )
    assert result.status == GoalRunStatus.COMPLETE
    assert result.checkpoint.verified_progress[0].item == "implementation"


@pytest.mark.parametrize(
    "text",
    [
        'Quoted `<goal-status state="complete">example</goal-status>`',
        '> <goal-status state="complete">example</goal-status>',
        'Example: <goal-status state="complete">example</goal-status>',
        '```xml\nExample text\n<goal-status state="complete">example</goal-status>\n```',
        '```xml\n<goal-status state="complete">example</goal-status>',
        envelope() + "\nAdditional note.",
        '<goal-status state="complete">outer <goal-status state="complete">inner</goal-status>',
    ],
    ids=[
        "inline-code",
        "blockquote",
        "inline-example",
        "fenced-prose",
        "open-fence",
        "trailing-note",
        "nested",
    ],
)
def test_quoted_or_ambiguous_status_is_not_a_signal(text):
    from claude_hub.services.goal_signals import parse_goal_signal

    assert parse_goal_signal(text) is None


@pytest.mark.asyncio
async def test_repair_dispatch_failure_and_restart_preserve_budget(tmp_path):
    sent = []

    async def dispatch(goal, prompt):
        sent.append(prompt)
        if len(sent) > 1:
            raise RuntimeError("transport uncertain")
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new")
    )
    goal = await manager.on_turn_completed("tab", goal.current_turn_id, "completed", DELIVERY)
    assert goal.status == GoalRunStatus.PAUSED
    assert goal.dispatch_state.value == "uncertain"
    assert goal.signal_recovery_attempts == 1
    manager.recover()
    assert manager.get(goal.id).signal_recovery_attempts == 1
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_sanitized_incident_events_through_the_real_completion_observer(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from claude_hub.api import agent_stream as api
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType
    from claude_hub.services import goal_run

    async def dispatch(goal, prompt):
        return goal.pending_step_id

    review = AsyncMock(
        return_value=StrictReviewOutcome(
            verdict="PASS", summary="Verified", findings=[], reviewer_provider="other"
        )
    )
    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=review
    )
    monkeypatch.setattr(goal_run, "get_goal_manager", lambda: manager)
    monkeypatch.setattr(api.workspace_manager, "on_scheduled_chat_turn_completed", AsyncMock())
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(objective="Verify the feature", client_request_id="new", strict_review=True),
    )

    async def observe(turn_id, payload):
        await api._notify_goal_turn_completed(
            AgentStreamEvent(
                stream_sequence=1,
                session_id="terminal-tab-tab",
                tab_id="tab",
                agent_type=AgentType.CURSOR,
                type=AgentStreamEventType.TURN_COMPLETED,
                turn_id=turn_id,
                payload=payload,
                created_at=datetime.now(timezone.utc),
            )
        )

    # Persisted production shape: no assistant text on runtime cancellation;
    # then restart recovery's completed event carries a substantial plain answer.
    await observe(goal.current_turn_id, {"status": "cancelled"})
    goal = await manager.resume(goal.id, "restart-recovery")
    await observe(goal.current_turn_id, {"status": "completed", "assistant_text": DELIVERY})
    goal = manager.get(goal.id)
    assert goal.status == GoalRunStatus.ACTIVE and goal.turns_completed == 2
    review.assert_not_awaited()
    # Raw protocol is private observer input, not part of the stored visible answer.
    await observe(
        goal.current_turn_id,
        {
            "status": "completed",
            "assistant_text": "Verified.",
            "_goal_protocol_text": "```xml\n" + envelope() + "\n```",
        },
    )
    assert manager.get(goal.id).status == GoalRunStatus.COMPLETE
    review.assert_awaited_once()
    assert DELIVERY in review.call_args.args[1]


@pytest.mark.asyncio
async def test_recovered_completion_still_blocks_on_strict_verdict(tmp_path):
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def review(goal, text, round_number):
        return StrictReviewOutcome(
            verdict="BLOCK", summary="Work is not verified", findings=[], reviewer_provider="other"
        )

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=review
    )
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new", strict_review=True)
    )
    goal = await manager.on_turn_completed("tab", goal.current_turn_id, "completed", DELIVERY)
    goal = await manager.on_turn_completed("tab", goal.current_turn_id, "completed", envelope())
    assert goal.status == GoalRunStatus.ACTIVE
    assert goal.strict_review_history[-1].verdict == "BLOCK"
    assert goal.completed_at is None


@pytest.mark.asyncio
async def test_duplicate_repair_and_old_completion_cannot_start_a_second_reviewer(tmp_path):
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()
    reviews = []

    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def review(goal, text, round_number):
        reviews.append(text)
        started.set()
        await release.wait()
        return StrictReviewOutcome(
            verdict="PASS", summary="Verified", findings=[], reviewer_provider="other"
        )

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=review
    )
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new", strict_review=True)
    )
    original = goal.current_turn_id
    goal = await manager.on_turn_completed("tab", original, "completed", DELIVERY)
    repair = goal.current_turn_id
    completing = asyncio.create_task(
        manager.on_turn_completed("tab", repair, "completed", envelope())
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(
            manager.on_turn_completed("tab", repair, "completed", envelope()), timeout=1
        )
        await asyncio.wait_for(
            manager.on_turn_completed("tab", original, "completed", DELIVERY), timeout=1
        )
        assert len(reviews) == 1
    finally:
        release.set()
        await completing
    assert manager.get(goal.id).status == GoalRunStatus.COMPLETE
