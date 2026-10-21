"""Regression coverage for native Goal intent, history and turn ownership."""

import pytest

from claude_hub.models.goal_run import GoalDispatchState, GoalRunCreate, GoalRunStatus
from claude_hub.services.goal_run import GoalRunController, GoalRunStore
from claude_hub.services.goal_run.native import NativeGoalError
from claude_hub.services.goal_run.policy import GoalPolicyError
from tests import test_native_goal_coordinator as goal_fixtures

native_controller = goal_fixtures.native_controller
create = goal_fixtures.create


@pytest.mark.asyncio
async def test_rejected_create_retry_stays_unconfirmed(native_controller):
    controller, provider, dispatched = native_controller
    provider.set_error = True
    with pytest.raises(NativeGoalError, match="set rejected"):
        await create(controller)
    provider.set_error = False
    assert await controller.refresh_current("tab") is None
    with pytest.raises(NativeGoalError, match="creation is not confirmed"):
        await create(controller)
    assert provider.goal is None
    assert len(provider.calls) == 1
    assert not dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "clear"])
async def test_rejected_mutation_retry_is_not_false_success(native_controller, operation):
    controller, provider, _ = native_controller
    goal = await create(controller)
    calls = []

    async def reject_clear():
        calls.append("clear")
        raise RuntimeError("clear rejected")

    if operation == "pause":
        provider.set_error = True
    else:
        provider.clear = reject_clear
    with pytest.raises(NativeGoalError, match="rejected"):
        await getattr(controller, operation)(goal.id, "operation")
    provider.set_error = False
    await controller.refresh_current("tab")
    original_calls = len(provider.calls)
    with pytest.raises(NativeGoalError, match="not confirmed"):
        await getattr(controller, operation)(goal.id, "operation")
    assert provider.goal is not None
    assert len(provider.calls) == original_calls
    assert calls == (["clear"] if operation == "clear" else [])


@pytest.mark.asyncio
async def test_accepted_clear_with_lost_response_reconciles(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    await controller.complete(goal.id, "complete")
    calls = []

    async def accepted_clear():
        calls.append("clear")
        provider.goal = None
        raise RuntimeError("clear response lost")

    provider.clear = accepted_clear
    with pytest.raises(NativeGoalError, match="response lost"):
        await controller.clear(goal.id, "clear")
    replay = await controller.clear(goal.id, "clear")
    assert replay.status == GoalRunStatus.CANCELLED
    assert provider.goal is None
    assert calls == ["clear"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [GoalRunStatus.COMPLETE, GoalRunStatus.FAILED, GoalRunStatus.CANCELLED]
)
async def test_native_absence_preserves_hub_terminal_history(native_controller, state):
    controller, provider, _ = native_controller
    goal = controller.create(
        "tab", GoalRunCreate(objective="Old Hub Goal", client_request_id="old")
    )
    goal.status = state
    goal.dispatch_state = GoalDispatchState.IDLE
    goal.current_turn_id = None
    controller.store.put(goal)
    historical = controller.get(goal.id).model_dump()
    assert await controller.refresh_current("tab") is None
    assert controller.get(goal.id).model_dump() == historical


@pytest.mark.asyncio
async def test_native_complete_then_absent_preserves_complete_history(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    await controller.complete(goal.id, "complete")
    historical = controller.get(goal.id).model_dump(exclude={"updated_at", "provider_goal_absent"})
    provider.goal = None
    assert await controller.refresh_current("tab") is None
    observed = controller.get(goal.id)
    assert observed.provider_goal_absent
    assert observed.model_dump(exclude={"updated_at", "provider_goal_absent"}) == historical


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [GoalRunStatus.COMPLETE, GoalRunStatus.FAILED])
async def test_terminal_native_history_allows_confirmed_unsupported_fallback(
    native_controller, state
):
    controller, provider, dispatched = native_controller
    goal = await create(controller)
    await controller.complete(goal.id, "complete")
    historical = controller.get(goal.id)
    historical.status = state
    controller.store.put(historical)
    provider.supported = False
    await controller.refresh_current("tab")
    replacement = await controller.create_and_start(
        "tab", GoalRunCreate(objective="New Hub Goal", client_request_id="new")
    )
    assert replacement.execution_owner == "hub_managed"
    assert len(dispatched) == 1
    assert controller.get(goal.id).status == state
    assert controller.get(goal.id).execution_owner == "provider_native"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "usageLimited", "budgetLimited"])
async def test_manual_turn_is_not_charged_to_inactive_native_goal(native_controller, state):
    controller, provider, _ = native_controller
    goal = await create(controller)
    provider.goal["status"] = state
    await controller.refresh_current("tab")
    provider.turn = "manual-turn"
    current = await controller.refresh_current("tab")
    assert current.current_turn_id is None
    assert "manual-turn" not in current.provider_owned_turn_ids
    provider.turn = None
    await controller.on_turn_completed("tab", "manual-turn", "completed", "Manual reply")
    assert controller.get(goal.id).turns_completed == 0
    assert "manual-turn" not in controller.get(goal.id).completed_turn_ids


@pytest.mark.asyncio
async def test_native_steer_turn_is_owned_and_counted_once(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    await controller.steer(goal.id, "steer", "Check this", "steer-turn")
    provider.turn = None
    for _ in range(2):
        await controller.on_turn_completed("tab", "steer-turn", "completed", "Checked")
    assert controller.get(goal.id).turns_completed == 1
    assert controller.get(goal.id).completed_turn_ids == ["steer-turn"]


@pytest.mark.asyncio
async def test_retry_stop_preserves_native_complete(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    provider.turn = "native-turn"
    provider.stop_error = True
    with pytest.raises(NativeGoalError, match="interrupt not confirmed"):
        await controller.complete(goal.id, "complete")
    current = await controller.refresh_current("tab")
    assert current.status == GoalRunStatus.COMPLETE
    assert current.provider_stop_pending
    assert current.dispatch_state == GoalDispatchState.UNCERTAIN
    provider.stop_error = False
    calls = len(provider.calls)
    retried = await controller.pause(goal.id, "retry-stop")
    assert retried.status == GoalRunStatus.COMPLETE
    assert provider.goal["status"] == "complete"
    assert provider.turn is None
    assert provider.calls[calls:] == ["stop"]
    assert not retried.provider_stop_pending
    assert retried.dispatch_state == GoalDispatchState.IDLE


@pytest.mark.asyncio
async def test_clear_limited_goal_does_not_interrupt_unrelated_manual_turn(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    provider.goal["status"] = "usageLimited"
    await controller.refresh_current("tab")
    provider.turn = "manual-turn"
    cleared = await controller.clear(goal.id, "clear")
    assert cleared.status == GoalRunStatus.CANCELLED
    assert provider.turn == "manual-turn"
    assert "stop" not in provider.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "complete"])
@pytest.mark.parametrize("reload_store", [False, True])
async def test_observed_absence_fences_replacement_with_same_provider_timestamp(
    native_controller, state, reload_store
):
    controller, provider, _ = native_controller
    await provider.set_goal({"objective": "First native Goal", "status": state})
    original = await controller.refresh_current("tab")
    await provider.clear()
    assert await controller.refresh_current("tab") is None
    if reload_store:
        controller = GoalRunController(GoalRunStore(controller.store.path))
        controller.native.resolver = provider.connect
    await provider.set_goal({"objective": "Replacement native Goal", "status": "active"})
    provider.goal["createdAt"] = original.provider_goal_created_at
    provider.goal["updatedAt"] = original.provider_updated_at
    replacement = await controller.refresh_current("tab")
    assert replacement.id != original.id
    assert replacement.objective == "Replacement native Goal"
    calls = len(provider.calls)
    with pytest.raises(GoalPolicyError, match="changed or was cleared"):
        await controller.pause(original.id, "stale-pause")
    assert len(provider.calls) == calls
    assert provider.goal["status"] == "active"
    assert controller.get(original.id).status.value == (
        "complete" if state == "complete" else "cancelled"
    )
