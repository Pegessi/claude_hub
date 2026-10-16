import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_hub.models import (
    GOAL_RECENT_TURN_IDS_LIMIT,
    GoalDispatchState,
    GoalRun,
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
    return GoalRunCreate.model_validate(
        {"objective": "Ship the requested feature", "client_request_id": request_id, **kwargs}
    )


def arm_goal(manager: GoalRunController, goal_id: str, turn_id: str) -> None:
    goal = manager.get(goal_id)
    goal.current_turn_id = turn_id
    goal.dispatch_state = GoalDispatchState.DISPATCHED
    manager.store.put(goal)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_response", [response_with_checkpoint("complete"), "Old answer"])
async def test_objective_edit_keeps_goal_and_ignores_old_completion(tmp_path, old_response):
    sent = []

    async def dispatch(goal, prompt):
        sent.append((goal, prompt))
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "edit.json"), dispatch)
    initial = await manager.create_and_start("tab", request())
    updated = await manager.update_objective(initial.id, "edit", "Verify the new outcome")
    assert updated.id == initial.id
    assert updated.current_turn_id == initial.current_turn_id
    assert len(sent) == 1
    await manager.on_turn_completed("tab", initial.current_turn_id, "completed", old_response)
    current = manager.get(initial.id)
    assert current.status == GoalRunStatus.ACTIVE
    assert len(sent) == 2
    assert "Verify the new outcome" in sent[-1][1]
    assert current.objective_revision == current.dispatched_objective_revision == 1
    assert (
        await manager.update_objective(initial.id, "edit", "Verify the new outcome")
    ).objective_revision == 1
    with pytest.raises(GoalPolicyError, match="another mutation"):
        await manager.update_objective(initial.id, "edit", "Different content")
    await manager.on_turn_completed(
        "tab", current.current_turn_id, "completed", response_with_checkpoint("complete")
    )
    assert manager.get(initial.id).status == GoalRunStatus.COMPLETE


def test_contract_validation_and_create_idempotency(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    first = manager.create("tab-1", request())
    assert manager.create("tab-1", request()).id == first.id
    with pytest.raises(ValueError, match="unfinished"):
        manager.create("tab-1", request("create-2"))
    assert request().model_dump(exclude={"attachments"}) == {
        "objective": "Ship the requested feature",
        "client_request_id": "create-1",
    }
    with pytest.raises(ValidationError):
        GoalRunCreate(objective="x" * 4001, client_request_id="large")


@pytest.mark.asyncio
async def test_goal_images_are_released_from_inherited_provider_context():
    import gc
    import weakref

    from claude_hub.models.chat_image import ChatImageAttachment
    from claude_hub.models.goal_run import GoalRun
    from claude_hub.services.goal_run.images import (
        goal_image_input,
        image_fingerprint,
        pending_goal_images,
    )

    image = ChatImageAttachment(filename="a.png", mime_type="image/png", data_url="original")
    reference = weakref.ref(image)
    goal = GoalRun(
        tab_id="tab",
        objective="Inspect reference",
        pending_image_count=1,
        pending_image_fingerprint=image_fingerprint([image]),
    )
    release = asyncio.Event()

    async def provider_reader():
        await release.wait()
        with pytest.raises(ValueError, match="Reattach"):
            pending_goal_images(goal)

    with goal_image_input("tab", [image]):
        assert pending_goal_images(goal)[0] is image
        reader = asyncio.create_task(provider_reader())
    del image
    gc.collect()
    assert reference() is None
    release.set()
    await reader


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
    goal = manager.create("tab-1", request())
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
async def test_goal_continues_past_legacy_limits_with_bounded_recent_ids(tmp_path: Path) -> None:
    dispatched = []

    async def dispatch(goal, prompt):
        dispatched.append(goal.pending_step_id)
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch)
    goal = manager.create("tab-1", request(token_budget=10, max_turns=1))
    result = await manager.dispatch_next(goal.id)
    assert result is not None
    first_turn = result.current_turn_id
    assert first_turn is not None
    for _ in range(125):
        current_turn_id = result.current_turn_id
        assert current_turn_id is not None
        updated = await manager.on_turn_completed(
            "tab-1",
            current_turn_id,
            "success",
            response_with_checkpoint(),
            {"total_tokens": 1000},
        )
        assert updated is not None
        result = updated
    assert result.status == GoalRunStatus.ACTIVE
    assert result.turns_completed == 125
    assert result.token_usage == 125000
    assert len(dispatched) == 126
    assert len(result.completed_turn_ids) == GOAL_RECENT_TURN_IDS_LIMIT
    assert len(result.checkpoint_history) == 10
    assert first_turn not in result.completed_turn_ids
    # A callback evicted from recent history is still rejected by turn identity.
    stale = await manager.on_turn_completed(
        "tab-1", first_turn, "success", response_with_checkpoint(state="complete")
    )
    assert stale == result
    assert len(dispatched) == 126


def test_recovery_pauses_uncertain_dispatch(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    stored = manager.get(goal.id)
    stored.dispatch_state = GoalDispatchState.PENDING
    manager.store.put(stored)
    recovered = manager.recover()
    assert recovered[0].status == GoalRunStatus.PAUSED
    assert recovered[0].dispatch_state.value == "uncertain"


def test_restart_between_turns_does_not_leave_goal_active_without_a_scheduler(
    tmp_path: Path,
) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    recovered = manager.recover()
    assert recovered[0].id == goal.id
    assert recovered[0].status == GoalRunStatus.PAUSED
    assert recovered[0].dispatch_state == GoalDispatchState.IDLE


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
    goal = manager.create("tab-1", request())
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
    assert goal is not None
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


@pytest.mark.asyncio
@pytest.mark.parametrize("has_create_inputs", [True, False])
async def test_legacy_limited_goal_migrates_without_autostart_and_can_resume(
    tmp_path: Path, has_create_inputs: bool
) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    path = tmp_path / "goal_runs.json"
    snapshot = json.loads(path.read_text())
    snapshot["version"] = 1
    snapshot["goals"][0].update(
        status="budget_limited",
        token_budget=10,
        max_turns=20,
        turns_completed=20,
        token_usage=100,
        status_message="maximum turn count (20) reached",
    )
    if has_create_inputs:
        snapshot["create_inputs"]["create-1"].update(token_budget=10, max_turns=20)
    else:
        snapshot.pop("create_inputs")
    path.write_text(json.dumps(snapshot))
    dispatched = []

    def dispatch(goal, prompt):
        dispatched.append(goal.pending_step_id)
        return goal.pending_step_id

    cold = GoalRunController(GoalRunStore(path), dispatch)
    migrated = cold.get(goal.id)
    assert migrated is not None
    assert migrated.status == GoalRunStatus.PAUSED
    assert migrated.status_message is not None
    assert "resume" in migrated.status_message
    assert not dispatched
    replayed_legacy = cold.replay_create("tab-1", request(token_budget=10, max_turns=20))
    assert replayed_legacy is not None
    assert replayed_legacy.id == goal.id
    replayed_current = cold.replay_create("tab-1", request())
    assert replayed_current is not None
    assert replayed_current.id == goal.id
    with pytest.raises(ValueError, match="another create"):
        cold.replay_create("tab-2", request())
    resumed = await cold.resume(goal.id, "resume")
    assert resumed.status == GoalRunStatus.ACTIVE
    assert len(dispatched) == 1
    persisted = json.loads(path.read_text())
    assert persisted["version"] == 2
    assert "token_budget" not in persisted["goals"][0]
    assert "max_turns" not in persisted["goals"][0]
    assert "token_budget" not in persisted["create_inputs"].get("create-1", {})


@pytest.mark.asyncio
async def test_goal_usage_is_accounting_only(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    arm_goal(manager, goal.id, "running")
    stored = manager.get(goal.id)
    stored.token_usage = 20
    manager.store.put(stored)
    result = await manager.on_turn_completed(
        "tab-1", "running", "complete", response_with_checkpoint(), {"total": 5}
    )
    assert result is not None
    assert result.token_usage == 25
    assert result.status == GoalRunStatus.PAUSED  # No connected dispatcher.
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
    assert goal is not None
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


@pytest.mark.asyncio
async def test_steer_replaces_running_goal_turn_and_keeps_goal_active(tmp_path: Path) -> None:
    prompts: list[tuple[str | None, str]] = []
    cancelled: list[str | None] = []

    async def dispatch(goal, prompt):
        prompts.append((goal.pending_step_id, prompt))
        return goal.pending_step_id

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch,
        lambda goal: cancelled.append(goal.current_turn_id),
    )
    goal = await manager.create_and_start("tab-1", request())
    first_turn = goal.current_turn_id

    steered = await manager.steer(goal.id, "steer-1", "Use the staging DB", "user-turn")

    assert cancelled == [first_turn]
    assert steered.status == GoalRunStatus.ACTIVE
    assert steered.current_turn_id == "user-turn"
    assert steered.dispatch_state == GoalDispatchState.DISPATCHED
    assert steered.pending_input is None
    assert prompts[-1][0] == "user-turn"
    assert "Use the staging DB" in prompts[-1][1]
    assert "Address it first" in prompts[-1][1]
    # The next continuation no longer repeats the delivered message.
    assert "Use the staging DB" not in build_continuation_prompt(steered)

    replay = await manager.steer(goal.id, "steer-1", "Use the staging DB", "user-turn")
    assert replay.current_turn_id == "user-turn"
    assert len(prompts) == 2
    with pytest.raises(GoalPolicyError, match="another mutation"):
        await manager.pause(goal.id, "steer-1")


@pytest.mark.asyncio
async def test_steer_requires_running_goal_and_keeps_input_when_dispatch_fails(
    tmp_path: Path,
) -> None:
    fail = False

    async def dispatch(goal, prompt):
        if fail:
            raise RuntimeError("transport offline")
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await manager.create_and_start("tab-1", request())
    await manager.pause(goal.id, "pause")
    with pytest.raises(GoalPolicyError, match="Resume the Goal"):
        await manager.steer(goal.id, "steer-paused", "hello", "turn-paused")

    await manager.resume(goal.id, "resume")
    # An unpersistable message is refused before it can corrupt the snapshot.
    with pytest.raises(ValueError, match="limited to"):
        await manager.steer(goal.id, "steer-long", "x" * 32_001, "turn-long")
    assert manager.get(goal.id).pending_input is None
    assert "steer-long" not in manager.get(goal.id).idempotency
    fail = True
    failed = await manager.steer(goal.id, "steer-1", "Keep me", "turn-1")
    assert failed.status == GoalRunStatus.PAUSED
    assert failed.dispatch_state == GoalDispatchState.UNCERTAIN
    assert failed.pending_input == "Keep me"
    assert "Keep me" in build_continuation_prompt(failed)


@pytest.mark.asyncio
async def test_blocked_goal_can_be_steered_with_the_user_reply(tmp_path: Path) -> None:
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await manager.create_and_start("tab-1", request())
    assert goal.current_turn_id is not None
    blocked = await manager.on_turn_completed(
        "tab-1", goal.current_turn_id, "completed", response_with_checkpoint("needs_input")
    )
    assert blocked is not None and blocked.status == GoalRunStatus.BLOCKED

    resumed = await manager.steer(goal.id, "reply", "Option B", "reply-turn")
    assert resumed.status == GoalRunStatus.ACTIVE
    assert resumed.current_turn_id == "reply-turn"


@pytest.mark.asyncio
async def test_resume_during_chat_turn_waits_for_that_turn_boundary(tmp_path: Path) -> None:
    dispatched: list[str | None] = []

    async def dispatch(goal, prompt):
        dispatched.append(goal.pending_step_id)
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await manager.create_and_start("tab-1", request())
    await manager.pause(goal.id, "pause")
    assert len(dispatched) == 1

    deferred = await manager.resume(goal.id, "resume", after_current_turn=True)
    assert deferred.status == GoalRunStatus.ACTIVE
    assert deferred.dispatch_state == GoalDispatchState.IDLE
    assert deferred.resume_after_turn is True
    assert deferred.status_message == "resumes when the current Chat turn finishes"
    assert len(dispatched) == 1

    released = await manager.on_turn_completed("tab-1", "manual-turn", "completed", "done")
    assert released is not None
    assert released.resume_after_turn is False
    assert released.dispatch_state == GoalDispatchState.DISPATCHED
    assert len(dispatched) == 2

    await manager.pause(goal.id, "pause-2")
    await manager.resume(goal.id, "resume-2", after_current_turn=True)
    paused = await manager.pause(goal.id, "pause-3")
    assert paused.resume_after_turn is False
    await manager.on_turn_completed("tab-1", "manual-turn-2", "completed", "done")
    assert len(dispatched) == 2

    await manager.resume(goal.id, "resume-3", after_current_turn=True)
    released = await manager.release_deferred_resume(goal.id)
    assert released.dispatch_state == GoalDispatchState.DISPATCHED
    assert len(dispatched) == 3
    # Releasing twice is a no-op.
    await manager.release_deferred_resume(goal.id)
    assert len(dispatched) == 3


@pytest.mark.asyncio
async def test_resume_fixed_step_override_is_persisted_and_only_used_once(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    snapshot = tmp_path / "goals.json"

    async def normal_dispatch(goal: GoalRun, prompt: str) -> str | None:
        calls.append(("normal", goal.pending_step_id))
        return goal.pending_step_id

    async def resumed_dispatch(goal: GoalRun, prompt: str) -> str | None:
        persisted = GoalRunStore(snapshot).get(goal.id)
        assert persisted.pending_step_id == persisted.current_turn_id == "restart-step"
        assert persisted.dispatch_state == GoalDispatchState.PENDING
        assert persisted.idempotency["restart-resume"] == "resume"
        assert goal.objective in prompt
        assert manager.dispatch is normal_dispatch
        calls.append(("restart", goal.pending_step_id))
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(snapshot), dispatch=normal_dispatch)
    goal = manager.create("tab-1", request())
    await manager.pause(goal.id, "pause")
    resumed = await manager.resume(
        goal.id, "restart-resume", step_id="restart-step", dispatch=resumed_dispatch
    )
    assert resumed.current_turn_id == "restart-step"
    assert calls == [("restart", "restart-step")]
    replay = await manager.resume(
        goal.id, "restart-resume", step_id="restart-step", dispatch=resumed_dispatch
    )
    assert replay.current_turn_id == "restart-step"
    assert calls == [("restart", "restart-step")]

    continued = await manager.on_turn_completed(
        "tab-1", "restart-step", "completed", response_with_checkpoint()
    )
    assert continued is not None
    assert continued.current_turn_id != "restart-step"
    assert calls == [("restart", "restart-step"), ("normal", continued.current_turn_id)]
    assert manager.dispatch is normal_dispatch


@pytest.mark.asyncio
async def test_fixed_step_resume_replay_cannot_override_a_later_pending_step(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def normal_dispatch(goal: GoalRun, prompt: str) -> str | None:
        calls.append("normal")
        return goal.pending_step_id

    async def resumed_dispatch(goal: GoalRun, prompt: str) -> str | None:
        calls.append("restart")
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch=normal_dispatch)
    goal = manager.create("tab-1", request())
    await manager.pause(goal.id, "pause")
    await manager.resume(goal.id, "restart", step_id="restart-step", dispatch=resumed_dispatch)
    # Reconstruct the durable boundary before a later ordinary Goal dispatch is
    # submitted; the old resume request has no authority over this step.
    later = manager.get(goal.id)
    later.current_turn_id = None
    later.pending_step_id = None
    later.dispatch_state = GoalDispatchState.IDLE
    manager.store.put(later)
    prepared = manager._prepare_dispatch_locked(goal.id)
    replay = await manager.resume(
        goal.id, "restart", step_id="restart-step", dispatch=resumed_dispatch
    )
    assert replay.pending_step_id == prepared.pending_step_id != "restart-step"
    assert replay.dispatch_state == GoalDispatchState.PENDING
    assert calls == ["restart"]
    await manager._dispatch_prepared(prepared)
    assert calls == ["restart", "normal"]


@pytest.mark.asyncio
async def test_duplicate_fixed_resume_claims_override_acceptance_once(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str | None] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        calls.append(goal.pending_step_id)
        entered.set()
        await release.wait()
        return goal.pending_step_id

    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    await manager.pause(goal.id, "pause")
    first = asyncio.create_task(
        manager.resume(goal.id, "restart", step_id="restart-step", dispatch=dispatch)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        replay = await manager.resume(goal.id, "restart", step_id="restart-step", dispatch=dispatch)
        assert replay.dispatch_state == GoalDispatchState.PENDING
        assert calls == ["restart-step"]
        release.set()
        assert (await first).dispatch_state == GoalDispatchState.DISPATCHED
        assert manager.dispatch is None
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    # The temporary callback cannot silently become the normal Goal scheduler.
    completed = await manager.on_turn_completed(
        "tab-1", "restart-step", "completed", response_with_checkpoint()
    )
    assert completed is not None and completed.status == GoalRunStatus.PAUSED
    assert completed.status_message == "automatic continuation is not connected"
    assert calls == ["restart-step"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_fixed_resume_retains_identity_and_does_not_retry_implicitly(
    tmp_path: Path, cancelled: bool
) -> None:
    attempts: list[str | None] = []
    cancellations: list[str | None] = []

    async def cancel(goal: GoalRun) -> None:
        cancellations.append(goal.current_turn_id)

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        attempts.append(goal.pending_step_id)
        if len(attempts) == 1:
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("provider acceptance is uncertain")
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), cancel=cancel)
    goal = manager.create("tab-1", request())
    await manager.pause(goal.id, "pause")
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await manager.resume(goal.id, "restart", step_id="restart-step", dispatch=dispatch)
    else:
        await manager.resume(goal.id, "restart", step_id="restart-step", dispatch=dispatch)
    uncertain = manager.get(goal.id)
    assert uncertain.status == GoalRunStatus.PAUSED
    assert uncertain.dispatch_state == GoalDispatchState.UNCERTAIN
    assert uncertain.pending_step_id == uncertain.current_turn_id == "restart-step"
    assert not manager._acceptances
    replay = await manager.resume(goal.id, "restart", step_id="restart-step", dispatch=dispatch)
    assert replay.dispatch_state == GoalDispatchState.UNCERTAIN
    assert attempts == ["restart-step"]
    retried = await manager.resume(
        goal.id, "explicit-retry", step_id="restart-step", dispatch=dispatch
    )
    assert cancellations == ["restart-step"]
    assert attempts == ["restart-step", "restart-step"]
    assert retried.dispatch_state == GoalDispatchState.DISPATCHED
    assert retried.current_turn_id == "restart-step"
    assert manager.dispatch is None


@pytest.mark.asyncio
async def test_fixed_resume_synchronous_completion_uses_normal_next_dispatch(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def normal_dispatch(goal: GoalRun, prompt: str) -> str | None:
        calls.append("normal")
        return goal.pending_step_id

    async def resumed_dispatch(goal: GoalRun, prompt: str) -> str | None:
        calls.append("restart")
        assert goal.current_turn_id == "restart-step"
        await manager.on_turn_completed(
            "tab-1", "restart-step", "completed", response_with_checkpoint()
        )
        return "restart-step"

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch=normal_dispatch)
    goal = manager.create("tab-1", request())
    await manager.pause(goal.id, "pause")
    resumed = await manager.resume(
        goal.id, "restart", step_id="restart-step", dispatch=resumed_dispatch
    )
    assert resumed.dispatch_state == GoalDispatchState.DISPATCHED
    assert resumed.current_turn_id != "restart-step"
    assert resumed.turns_completed == 1
    assert calls == ["restart", "normal"]
    assert manager.dispatch is normal_dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["complete", "completed", "success", "succeeded", "cancelled", "failed", "error"]
)
async def test_deferred_boundary_deduplicates_without_charging_goal_usage(
    tmp_path: Path, status: str
) -> None:
    calls: list[str | None] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        calls.append(goal.pending_step_id)
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch=dispatch)
    goal = manager.create("tab-1", request())
    goal.turns_completed = 7
    goal.token_usage = 100
    goal.usage_quality = GoalUsageQuality.EXACT
    goal.completed_turn_ids = [f"older-{index}" for index in range(GOAL_RECENT_TURN_IDS_LIMIT)]
    manager.store.put(goal)
    await manager.pause(goal.id, "pause")
    await manager.resume(goal.id, "defer", after_current_turn=True)
    result = await manager.on_turn_completed(
        "tab-1", "ordinary-turn", status, "ordinary answer", {"total_tokens": 999}
    )
    assert result is not None
    assert result.turns_completed == 7
    assert result.token_usage == 100
    assert result.usage_quality == GoalUsageQuality.EXACT
    assert not result.resume_after_turn
    assert result.completed_turn_ids[-1] == "ordinary-turn"
    assert len(result.completed_turn_ids) == GOAL_RECENT_TURN_IDS_LIMIT
    succeeded = status in {"complete", "completed", "success", "succeeded"}
    assert len(calls) == int(succeeded)
    if succeeded:
        assert result.status == GoalRunStatus.ACTIVE
        assert result.dispatch_state == GoalDispatchState.DISPATCHED
    else:
        assert result.status == GoalRunStatus.PAUSED
        assert result.dispatch_state == GoalDispatchState.IDLE
        assert result.paused_at is not None
        assert status in str(result.status_message)

    await manager.pause(goal.id, "pause-again")
    await manager.resume(goal.id, "defer-again", after_current_turn=True)
    # A duplicate successful notification cannot release the next deferral,
    # including when the first notification was cancelled or failed.
    replay = await manager.on_turn_completed("tab-1", "ordinary-turn", "completed", "late")
    assert replay is not None and replay.resume_after_turn
    assert replay.dispatch_state == GoalDispatchState.IDLE
    assert len(calls) == int(succeeded)
    assert replay.turns_completed == 7 and replay.token_usage == 100


@pytest.mark.asyncio
async def test_fixed_resume_options_reject_deferral_without_mutating_goal(tmp_path: Path) -> None:
    manager = controller(tmp_path)
    goal = manager.create("tab-1", request())
    await manager.pause(goal.id, "pause")
    with pytest.raises(GoalPolicyError, match="immediate"):
        await manager.resume(goal.id, "invalid", after_current_turn=True, step_id="fixed")
    with pytest.raises(GoalPolicyError, match="empty"):
        await manager.resume(goal.id, "empty", step_id=" ")
    with pytest.raises(GoalPolicyError, match="fixed step_id"):
        await manager.resume(goal.id, "missing-step", dispatch=lambda goal, prompt: None)
    unchanged = manager.get(goal.id)
    assert unchanged.status == GoalRunStatus.PAUSED
    assert unchanged.idempotency == {"pause": "pause"}
