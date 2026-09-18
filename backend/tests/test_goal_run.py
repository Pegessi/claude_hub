import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_hub.models import (
    HARD_GOAL_MAX_TURNS,
    GoalDispatchState,
    GoalRunCreate,
    GoalRunStatus,
    GoalUsageQuality,
)
from claude_hub.models.agent_stream import normalize_provider_usage
from claude_hub.services.goal_run import GoalPolicyError, GoalRunController, GoalRunStore
from claude_hub.services.goal_run.controller import build_continuation_prompt


def response_with_checkpoint(
    state: str = "continue", *, next_step: str = "Run integration tests"
) -> str:
    return f"""Progress report.
<goal-checkpoint>
{{"verified_progress":[{{"item":"Goal API","evidence":"12 tests passed"}}],
 "decisions":["Keep Goal separate from ChatMode"],
 "remaining":["Provider verification"],
 "blocker":null,"next_step":"{next_step}"}}
</goal-checkpoint>
<goal-status state="{state}">checkpoint recorded</goal-status>"""


def controller(tmp_path: Path) -> GoalRunController:
    return GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"))


def test_codex_usage_uses_per_turn_last_and_non_cached_total() -> None:
    usage = normalize_provider_usage(
        {
            "tokenUsage": {
                "last": {
                    "inputTokens": 100,
                    "cachedInputTokens": 40,
                    "outputTokens": 20,
                    "reasoningOutputTokens": 5,
                },
                "total": {"inputTokens": 1000, "outputTokens": 200},
            }
        },
        "codex",
    )
    assert usage == {
        "input": 100,
        "cached": 40,
        "output": 20,
        "reasoning": 5,
        "total": 80,
        "source": "codex",
    }


def request(request_id: str = "create-1", **kwargs: object) -> GoalRunCreate:
    return GoalRunCreate(
        objective="Ship the requested feature", client_request_id=request_id, **kwargs
    )


def arm_goal(manager: GoalRunController, goal_id: str, turn_id: str) -> None:
    goal = manager.get(goal_id)
    goal.current_turn_id = turn_id
    goal.dispatch_state = GoalDispatchState.DISPATCHED
    manager.store.put(goal)


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
async def test_completed_goal_remains_visible_until_cleared(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    completed = await manager.complete(goal.id, "complete-1")
    assert manager.current("tab-1") == completed
    assert (await manager.clear(goal.id, "clear-complete")).status == GoalRunStatus.CANCELLED
    assert manager.current("tab-1") is None
    replacement = manager.create("tab-1", request("create-2"))
    assert manager.current("tab-1") == replacement


@pytest.mark.asyncio
async def test_turn_signal_continues_only_after_state_is_persisted(tmp_path: Path) -> None:
    observations: list[tuple[str, str]] = []

    async def dispatch(goal, prompt):
        cold = GoalRunStore(tmp_path / "goal_runs.json").get(goal.id)
        observations.append((cold.dispatch_state.value, prompt))
        return "turn-2"

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    goal = manager.create("tab-1", request(token_budget=1000))
    arm_goal(manager, goal.id, "turn-1")
    result = await manager.on_turn_completed(
        "tab-1",
        "turn-1",
        "complete",
        response_with_checkpoint(),
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
    assert result.checkpoint is not None
    assert result.checkpoint.verified_progress[0].evidence == "12 tests passed"
    assert result.checkpoint_history == [result.checkpoint]


@pytest.mark.asyncio
async def test_turn_missing_signal_fails_closed_and_usage_is_unavailable(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "turn-1")
    result = await manager.on_turn_completed("tab-1", "turn-1", "complete", "plain prose")
    assert result is not None
    assert result.status == GoalRunStatus.FAILED
    assert result.usage_quality == GoalUsageQuality.UNAVAILABLE
    assert manager.get(goal.id).turns_completed == 1


@pytest.mark.asyncio
async def test_unrelated_completion_is_ignored_and_stop_pauses_goal(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "goal-turn")
    unrelated = await manager.on_turn_completed(
        "tab-1", "user-turn", "complete", '<goal-status state="complete">x</goal-status>'
    )
    assert unrelated is not None
    assert unrelated.status == GoalRunStatus.ACTIVE
    assert unrelated.turns_completed == 0
    stopped = await manager.on_turn_completed("tab-1", "goal-turn", "cancelled", "")
    assert stopped is not None
    assert stopped.status == GoalRunStatus.PAUSED
    assert stopped.turns_completed == 1


@pytest.mark.asyncio
async def test_usage_quality_conservatively_degrades_across_turns(tmp_path: Path) -> None:
    async def dispatch(goal, prompt):
        return "turn-2"

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "turn-1")
    first = await manager.on_turn_completed(
        "tab-1", "turn-1", "complete", response_with_checkpoint(), {"total": 100}
    )
    assert first is not None and first.usage_quality == GoalUsageQuality.EXACT
    second = await manager.on_turn_completed(
        "tab-1", "turn-2", "complete", response_with_checkpoint(state="complete")
    )
    assert second is not None
    assert second.token_usage == 100
    assert second.usage_quality == GoalUsageQuality.UNAVAILABLE


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
        "turn-resumed",
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
    goal = manager.create("tab-1", request(token_budget=10, max_turns=1))
    arm_goal(manager, goal.id, "turn-1")
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
    stored.dispatch_state = GoalDispatchState.PENDING
    manager.store.put(stored)
    recovered = manager.recover()
    assert recovered[0].status == GoalRunStatus.PAUSED
    assert recovered[0].dispatch_state.value == "uncertain"


@pytest.mark.asyncio
async def test_resume_reconciles_uncertain_turn_and_dispatches_once(tmp_path: Path) -> None:
    dispatched: list[str] = []
    cancelled: list[str | None] = []

    async def dispatch(goal, prompt):
        dispatched.append(goal.pending_step_id)
        return "turn-recovered"

    async def cancel(goal):
        cancelled.append(goal.current_turn_id)

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goal_runs.json"), dispatch=dispatch, cancel=cancel
    )
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "turn-uncertain")
    manager.recover()

    resumed = await manager.resume(goal.id, "resume-recovered")

    assert resumed.status == GoalRunStatus.ACTIVE
    assert resumed.dispatch_state == GoalDispatchState.DISPATCHED
    assert resumed.current_turn_id == "turn-recovered"
    assert cancelled == ["turn-uncertain"]
    assert len(dispatched) == 1
    replay = await manager.resume(goal.id, "resume-recovered")
    assert replay.current_turn_id == "turn-recovered"
    assert len(dispatched) == 1


def test_continuation_prompt_keeps_full_objective(tmp_path: Path) -> None:
    goal = controller(tmp_path).create("tab-1", request())
    prompt = build_continuation_prompt(goal)
    assert goal.objective in prompt
    assert "verify relevant" in prompt
    assert "<goal-status" in prompt
    assert "<goal-checkpoint>" in prompt


@pytest.mark.asyncio
async def test_latest_valid_checkpoint_is_injected_and_invalid_update_is_non_destructive(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []

    async def dispatch(goal, prompt):
        prompts.append(prompt)
        return f"turn-{len(prompts) + 1}"

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "turn-1")
    first = await manager.on_turn_completed(
        "tab-1", "turn-1", "complete", response_with_checkpoint()
    )
    assert first is not None and first.checkpoint is not None
    assert '"next_step":"Run integration tests"' in prompts[-1]

    invalid = await manager.on_turn_completed(
        "tab-1",
        "turn-2",
        "complete",
        '<goal-checkpoint>{"remaining":[""]}</goal-checkpoint>\n'
        '<goal-status state="continue">keep going</goal-status>',
    )
    assert invalid is not None and invalid.checkpoint is not None
    assert invalid.checkpoint.turn_id == "turn-1"
    assert invalid.checkpoint_warning is not None
    assert len(invalid.checkpoint_history) == 1


@pytest.mark.asyncio
async def test_final_status_parser_ignores_earlier_unclosed_opener(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "turn-1")

    result = await manager.on_turn_completed(
        "tab-1",
        "turn-1",
        "complete",
        'quoted <goal-status state="complete">noise\n'
        '<goal-status state="continue">real final state</goal-status>',
    )

    assert result is not None
    assert result.status == GoalRunStatus.PAUSED
    assert result.status_message == "automatic continuation is not connected"


@pytest.mark.asyncio
async def test_checkpoint_rejects_provider_owned_metadata_and_nested_extras(
    tmp_path: Path,
) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "turn-1")
    response = (
        '<goal-checkpoint>{"turn_id":"forged","created_at":"2000-01-01T00:00:00Z",'
        '"verified_progress":[{"item":"x","evidence":"y","objective":"z"}]}</goal-checkpoint>\n'
        '<goal-status state="complete">done</goal-status>'
    )

    result = await manager.on_turn_completed("tab-1", "turn-1", "complete", response)

    assert result is not None and result.status == GoalRunStatus.COMPLETE
    assert result.checkpoint is None
    assert "Hub-managed" in (result.checkpoint_warning or "")


@pytest.mark.asyncio
async def test_checkpoint_history_is_bounded(tmp_path: Path) -> None:
    async def dispatch(goal, prompt):
        return f"next-{goal.turns_completed}"

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    goal = manager.create("tab-1", request(max_turns=20))
    arm_goal(manager, goal.id, "turn-0")
    for index in range(12):
        current = manager.get(goal.id)
        if current.current_turn_id != f"turn-{index}":
            arm_goal(manager, goal.id, f"turn-{index}")
        result = await manager.on_turn_completed(
            "tab-1",
            f"turn-{index}",
            "complete",
            response_with_checkpoint(next_step=f"step {index}"),
        )
    assert result is not None
    assert len(result.checkpoint_history) == 10
    assert result.checkpoint_history[0].turn_id == "turn-2"
    assert result.checkpoint_history[-1].turn_id == "turn-11"


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


@pytest.mark.asyncio
async def test_completion_during_pending_dispatch_is_not_lost(tmp_path: Path) -> None:
    manager: GoalRunController
    observed_states: list[GoalDispatchState] = []

    async def dispatch(goal, prompt):
        observed_states.append(manager.get(goal.id).dispatch_state)
        completed = await manager.on_turn_completed(
            goal.tab_id,
            goal.pending_step_id,
            "complete",
            response_with_checkpoint(state="complete"),
        )
        assert completed is not None
        assert completed.status == GoalRunStatus.COMPLETE
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)

    result = await manager.create_and_start("tab-1", request())

    assert observed_states == [GoalDispatchState.PENDING]
    assert result.status == GoalRunStatus.COMPLETE
    assert result.dispatch_state == GoalDispatchState.IDLE
    assert result.current_turn_id is None
    assert result.turns_completed == 1


@pytest.mark.asyncio
async def test_pause_waits_for_pending_dispatch_acceptance_before_cancel(tmp_path: Path) -> None:
    dispatch_entered = asyncio.Event()
    release_dispatch = asyncio.Event()
    operations: list[str] = []

    async def dispatch(goal, prompt):
        operations.append("dispatch-entered")
        dispatch_entered.set()
        await release_dispatch.wait()
        operations.append("dispatch-accepted")
        return goal.pending_step_id

    async def cancel(goal):
        operations.append(f"cancel:{goal.current_turn_id}")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goal_runs.json"), dispatch=dispatch, cancel=cancel
    )
    start_task = asyncio.create_task(manager.create_and_start("tab-1", request()))
    await dispatch_entered.wait()
    goal = manager.current("tab-1")
    assert goal is not None

    pause_task = asyncio.create_task(manager.pause(goal.id, "pause-race"))
    await asyncio.sleep(0)
    assert not pause_task.done()
    assert manager.get(goal.id).status == GoalRunStatus.PAUSED

    release_dispatch.set()
    started, paused = await asyncio.gather(start_task, pause_task)

    assert started.status == GoalRunStatus.PAUSED
    assert paused.status == GoalRunStatus.PAUSED
    assert paused.dispatch_state == GoalDispatchState.IDLE
    assert paused.current_turn_id is None
    assert operations == [
        "dispatch-entered",
        "dispatch-accepted",
        f"cancel:{goal.pending_step_id}",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "complete", "clear"])
async def test_terminal_mutation_cancel_failure_is_uncertain_and_retriable(
    tmp_path: Path, operation: str
) -> None:
    attempts = 0

    async def cancel(goal):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("provider unavailable")

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), cancel=cancel)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "running-turn")

    first = await getattr(manager, operation)(goal.id, f"{operation}-1")

    assert first.dispatch_state == GoalDispatchState.UNCERTAIN
    assert first.current_turn_id == "running-turn"
    assert first.pending_step_id is None
    assert first.status_message == "turn cancellation failed: provider unavailable"

    retried = await getattr(manager, operation)(goal.id, f"{operation}-1")

    assert attempts == 2
    assert retried.dispatch_state == GoalDispatchState.IDLE
    assert retried.current_turn_id is None


@pytest.mark.asyncio
async def test_resume_does_not_dispatch_until_failed_pause_cancel_is_reconciled(
    tmp_path: Path,
) -> None:
    cancel_attempts = 0
    dispatched: list[str] = []

    async def cancel(goal):
        nonlocal cancel_attempts
        cancel_attempts += 1
        if cancel_attempts < 3:
            raise RuntimeError("provider unavailable")

    async def dispatch(goal, prompt):
        dispatched.append(goal.pending_step_id)
        return goal.pending_step_id

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goal_runs.json"), dispatch=dispatch, cancel=cancel
    )
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "running-turn")
    paused = await manager.pause(goal.id, "pause-1")
    assert paused.dispatch_state == GoalDispatchState.UNCERTAIN

    with pytest.raises(GoalPolicyError, match="turn cancellation failed"):
        await manager.resume(goal.id, "resume-1")
    assert dispatched == []

    resumed = await manager.resume(goal.id, "resume-2")
    assert resumed.dispatch_state == GoalDispatchState.DISPATCHED
    assert len(dispatched) == 1


def test_budget_update_idempotency_includes_payload(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request(token_budget=10))
    assert manager.update_budget(goal.id, "budget-1", 20).token_budget == 20
    with pytest.raises(GoalPolicyError, match="another mutation"):
        manager.update_budget(goal.id, "budget-1", 30)


@pytest.mark.asyncio
async def test_duplicate_dispatch_and_unrelated_completion_do_not_resend(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def dispatch(goal, prompt):
        dispatched.append(goal.pending_step_id)
        entered.set()
        await release.wait()
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    start = asyncio.create_task(manager.create_and_start("tab-1", request()))
    await entered.wait()
    goal = manager.current("tab-1")
    duplicates = [
        asyncio.create_task(manager.create_and_start("tab-1", request())),
        asyncio.create_task(manager.dispatch_next(goal.id)),
        asyncio.create_task(manager.on_turn_completed("tab-1", "unrelated", "complete", "")),
    ]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(start, *duplicates)
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_old_pause_replay_cannot_cancel_resumed_turn(tmp_path: Path) -> None:
    cancelled = []

    async def cancel(goal):
        cancelled.append(goal.current_turn_id)

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), lambda goal, prompt: goal.pending_step_id, cancel
    )
    goal = await manager.create_and_start("tab-1", request())
    await manager.pause(goal.id, "pause")
    resumed = await manager.resume(goal.id, "resume")
    replay = await manager.pause(goal.id, "pause")
    assert replay.current_turn_id == resumed.current_turn_id
    assert replay.dispatch_state == GoalDispatchState.DISPATCHED
    assert cancelled == [goal.current_turn_id]


@pytest.mark.asyncio
async def test_clear_failure_stays_visible_and_blocks_replacement(tmp_path: Path) -> None:
    async def cancel(goal):
        raise RuntimeError("unreachable")

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), cancel=cancel)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "running")
    cleared = await manager.clear(goal.id, "clear")
    assert manager.current("tab-1") == cleared
    with pytest.raises(ValueError, match="unfinished"):
        manager.create("tab-1", request("replacement"))


@pytest.mark.asyncio
async def test_clearing_latest_goal_does_not_resurrect_previous_goal(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    first = manager.create("tab-1", request())
    await manager.complete(first.id, "complete")
    second = manager.create("tab-1", request("second"))
    await manager.clear(second.id, "clear")
    assert manager.current("tab-1") is None


def test_create_replay_uses_original_budget_after_budget_edit(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request(token_budget=10))
    manager.update_budget(goal.id, "budget", 20)
    cold = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"))
    assert cold.replay_create("tab-1", request(token_budget=10)).id == goal.id


@pytest.mark.asyncio
async def test_lower_budget_during_turn_keeps_completion_accounting(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request(token_budget=100))
    arm_goal(manager, goal.id, "running")
    stored = manager.get(goal.id)
    stored.token_usage = 20
    manager.store.put(stored)
    manager.update_budget(goal.id, "budget", 10)
    result = await manager.on_turn_completed(
        "tab-1", "running", "complete", response_with_checkpoint(), {"total": 5}
    )
    assert result.token_usage == 25
    assert result.status == GoalRunStatus.BUDGET_LIMITED
    assert result.current_turn_id is None


@pytest.mark.asyncio
async def test_resume_reconciliation_allows_synchronous_completion_callback(tmp_path: Path) -> None:
    async def cancel(goal):
        await manager.on_turn_completed(goal.tab_id, "old", "cancelled", "")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), lambda goal, prompt: goal.pending_step_id, cancel
    )
    goal = manager.create("tab-1", request())
    goal.status = GoalRunStatus.PAUSED
    goal.dispatch_state = GoalDispatchState.UNCERTAIN
    manager.store.put(goal)
    result = await asyncio.wait_for(manager.resume(goal.id, "resume"), timeout=1)
    assert result.dispatch_state == GoalDispatchState.DISPATCHED


@pytest.mark.asyncio
async def test_failed_reconciliation_without_turn_id_cannot_resume(tmp_path: Path) -> None:
    async def cancel(goal):
        raise RuntimeError("offline")

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), cancel=cancel)
    goal = manager.create("tab-1", request())
    goal.status = GoalRunStatus.PAUSED
    goal.dispatch_state = GoalDispatchState.UNCERTAIN
    manager.store.put(goal)
    with pytest.raises(GoalPolicyError, match="offline"):
        await manager.resume(goal.id, "resume")


@pytest.mark.asyncio
async def test_pause_waits_only_for_acceptance_not_dispatch_callers_lifetime(
    tmp_path: Path,
) -> None:
    entered, release, caller_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancelled = []

    async def dispatch(goal, prompt):
        entered.set()
        await release.wait()
        return "provider-turn"

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch,
        lambda goal: cancelled.append(goal.current_turn_id),
    )

    async def caller():
        await manager.create_and_start("tab-1", request())
        await caller_finished.wait()

    task = asyncio.create_task(caller())
    await entered.wait()
    goal = manager.current("tab-1")
    pause = asyncio.create_task(manager.pause(goal.id, "pause"))
    await asyncio.sleep(0)
    release.set()
    try:
        result = await asyncio.wait_for(pause, timeout=1)
        assert result.dispatch_state == GoalDispatchState.IDLE
        assert cancelled == ["provider-turn"]
    finally:
        caller_finished.set()
        await task
