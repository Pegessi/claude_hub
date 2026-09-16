from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_hub.models import (
    HARD_GOAL_MAX_TURNS,
    GoalRunCreate,
    GoalRunStatus,
    GoalUsageQuality,
)
from claude_hub.services.goal_run import GoalPolicyError, GoalRunController, GoalRunStore
from claude_hub.services.goal_run.controller import build_continuation_prompt


def controller(tmp_path: Path) -> GoalRunController:
    return GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"))


def request(request_id: str = "create-1", **kwargs: object) -> GoalRunCreate:
    return GoalRunCreate(
        objective="Ship the requested feature", client_request_id=request_id, **kwargs
    )


def test_contract_validation_and_create_idempotency(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    first = manager.create("tab-1", request())
    assert manager.create("tab-1", request()).id == first.id
    with pytest.raises(ValueError, match="unfinished"):
        manager.create("tab-1", request("create-2"))
    with pytest.raises(ValidationError):
        request("too-long", max_turns=HARD_GOAL_MAX_TURNS + 1)
    with pytest.raises(ValidationError):
        GoalRunCreate(objective="x" * 4001, client_request_id="large")


@pytest.mark.asyncio
async def test_mutations_are_idempotent_and_persisted(tmp_path: Path) -> None:
    snapshot = tmp_path / "goal_runs.json"
    manager = GoalRunController(GoalRunStore(snapshot))
    goal = manager.create("tab-1", request())
    paused = await manager.pause(goal.id, "pause-1")
    assert paused.status == GoalRunStatus.PAUSED
    assert (await manager.pause(goal.id, "pause-1")).status == GoalRunStatus.PAUSED
    with pytest.raises(GoalPolicyError, match="another mutation"):
        await manager.resume(goal.id, "pause-1")
    cold = GoalRunController(GoalRunStore(snapshot))
    assert cold.get(goal.id).status == GoalRunStatus.PAUSED
    # Without a connected transport, resume fails closed in paused state.
    resumed = await cold.resume(goal.id, "resume-1")
    assert resumed.status == GoalRunStatus.PAUSED
    assert (await cold.clear(goal.id, "clear-1")).status == GoalRunStatus.CANCELLED
    assert cold.current("tab-1") is None


@pytest.mark.asyncio
async def test_turn_signal_continues_only_after_state_is_persisted(tmp_path: Path) -> None:
    observations: list[tuple[str, str]] = []

    async def dispatch(goal, prompt):
        cold = GoalRunStore(tmp_path / "goal_runs.json").get(goal.id)
        observations.append((cold.dispatch_state.value, prompt))
        return "turn-2"

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    goal = manager.create("tab-1", request(token_budget=1000))
    result = await manager.on_turn_completed(
        "tab-1",
        "turn-1",
        "complete",
        'work done\n<goal-status state="continue">tests pending</goal-status>',
        {"total_tokens": 100, "quality": "exact"},
    )
    assert result is not None
    assert result.current_turn_id == "turn-2"
    assert result.turns_completed == 1
    assert result.token_usage == 100
    assert result.usage_quality == GoalUsageQuality.EXACT
    assert observations[0][0] == "pending"
    assert "do not narrow" in observations[0][1]
    assert "provider-native subagents" in observations[0][1]


@pytest.mark.asyncio
async def test_turn_missing_signal_fails_closed_and_usage_is_unavailable(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    result = await manager.on_turn_completed("tab-1", "turn-1", "complete", "plain prose")
    assert result is not None
    assert result.status == GoalRunStatus.FAILED
    assert result.usage_quality == GoalUsageQuality.UNAVAILABLE
    assert manager.get(goal.id).turns_completed == 1


@pytest.mark.asyncio
async def test_blocked_signal_and_stale_completion_after_pause(tmp_path: Path) -> None:
    async def dispatch(goal, prompt):
        return "turn-resumed"

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    goal = manager.create("tab-1", request())
    await manager.pause(goal.id, "pause")
    stale = await manager.on_turn_completed(
        "tab-1", "turn-1", "complete", '<goal-status state="complete">done</goal-status>'
    )
    assert stale is not None and stale.status == GoalRunStatus.PAUSED
    await manager.resume(goal.id, "resume")
    blocked = await manager.on_turn_completed(
        "tab-1",
        "turn-2",
        "complete",
        '<goal-status state="needs_input">choose A or B</goal-status>',
    )
    assert blocked is not None and blocked.status == GoalRunStatus.BLOCKED


@pytest.mark.asyncio
async def test_budget_and_turn_limits_stop_continuation(tmp_path: Path) -> None:
    called = False

    async def dispatch(goal, prompt):
        nonlocal called
        called = True

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    manager.create("tab-1", request(token_budget=10, max_turns=1))
    result = await manager.on_turn_completed(
        "tab-1",
        "turn-1",
        "success",
        '<goal-status state="continue">more</goal-status>',
        {"total_tokens": 10},
    )
    assert result is not None and result.status == GoalRunStatus.BUDGET_LIMITED
    assert not called


def test_recovery_pauses_uncertain_dispatch(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    stored = manager.get(goal.id)
    from claude_hub.models import GoalDispatchState

    stored.dispatch_state = GoalDispatchState.PENDING
    manager.store.put(stored)
    recovered = manager.recover()
    assert recovered[0].status == GoalRunStatus.PAUSED
    assert recovered[0].dispatch_state.value == "uncertain"


def test_continuation_prompt_keeps_full_objective(tmp_path: Path) -> None:
    goal = controller(tmp_path).create("tab-1", request())
    prompt = build_continuation_prompt(goal)
    assert goal.objective in prompt
    assert "verify relevant" in prompt
    assert "<goal-status" in prompt


@pytest.mark.asyncio
async def test_create_dispatches_after_persist_and_pause_cancels(tmp_path: Path) -> None:
    observed: list[str] = []

    async def dispatch(goal, prompt):
        observed.append(GoalRunStore(tmp_path / "goal_runs.json").get(goal.id).dispatch_state.value)
        return "initial-turn"

    async def cancel(goal):
        observed.append(f"cancel:{goal.current_turn_id}:{goal.status.value}")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goal_runs.json"), dispatch=dispatch, cancel=cancel
    )
    goal = await manager.create_and_start("tab-1", request())
    assert goal.current_turn_id == "initial-turn"
    assert observed == ["pending"]
    paused = await manager.pause(goal.id, "pause")
    assert paused.status == GoalRunStatus.PAUSED
    assert observed[-1] == "cancel:initial-turn:paused"
    await manager.pause(goal.id, "pause")
    assert observed.count("cancel:initial-turn:paused") == 1


def test_budget_update_idempotency_includes_payload(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request(token_budget=10))
    assert manager.update_budget(goal.id, "budget-1", 20).token_budget == 20
    with pytest.raises(GoalPolicyError, match="another mutation"):
        manager.update_budget(goal.id, "budget-1", 30)
