"""Native Goal lifecycle tests without a provider process or live runtime."""

import copy
from typing import Any

import pytest

from claude_hub.models.goal_run import GoalRunCreate, GoalRunStatus
from claude_hub.services.goal_run import GoalRunController, GoalRunStore
from claude_hub.services.goal_run.native import NativeGoalConnection, NativeGoalError
from claude_hub.services.goal_run.policy import GoalPolicyError


class FakeGoalProvider:
    def __init__(self) -> None:
        self.goal: dict[str, Any] | None = None
        self.thread = "native-thread"
        self.turn: str | None = None
        self.clock = 100
        self.calls: list[Any] = []
        self.read_error = False
        self.stop_error = False
        self.set_error = False
        self.accept_then_error = False
        self.supported = True

    async def read(self):
        if self.read_error:
            raise RuntimeError("provider unavailable")
        if not self.supported:
            return None
        return {"thread_id": self.thread, "goal": copy.deepcopy(self.goal)}

    async def set_goal(self, updates):
        self.calls.append(("set", updates.copy()))
        if self.set_error:
            raise RuntimeError("set rejected")
        if self.goal is None:
            self.clock += 1
            self.goal = {
                "threadId": self.thread,
                "objective": updates["objective"],
                "status": "active",
                "createdAt": self.clock,
                "updatedAt": self.clock,
                "tokensUsed": 0,
                "timeUsedSeconds": 0,
                "tokenBudget": None,
            }
        self.goal.update(updates)
        self.clock += 1
        self.goal["updatedAt"] = self.clock
        if self.accept_then_error:
            raise RuntimeError("response lost")
        return copy.deepcopy(self.goal)

    async def clear(self):
        self.calls.append("clear")
        cleared = self.goal is not None
        self.goal = None
        return cleared

    async def stop(self):
        self.calls.append("stop")
        if self.stop_error:
            raise RuntimeError("interrupt not confirmed")
        self.turn = None

    async def active_turn(self):
        return self.turn

    async def send(self, goal, text, turn_id):
        self.calls.append(("input", text, turn_id))
        self.turn = turn_id

    async def connect(self, tab_id):
        snapshot = await self.read()
        if snapshot is None:
            return None
        return NativeGoalConnection(
            snapshot=snapshot,
            read=self.read,
            set_goal=self.set_goal,
            clear_goal=self.clear,
            stop_turn=self.stop,
            active_turn_id=self.active_turn,
            send_input=self.send,
            turn_in_flight=lambda: self.turn is not None,
        )


@pytest.fixture
def native_controller(tmp_path):
    dispatched = []

    async def dispatch(goal, prompt):
        dispatched.append(prompt)
        return "hub-turn"

    controller = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    provider = FakeGoalProvider()
    controller.native.resolver = provider.connect
    return controller, provider, dispatched


async def create(controller):
    return await controller.create_and_start(
        "tab", GoalRunCreate(objective="Finish the work", client_request_id="create")
    )


@pytest.mark.asyncio
async def test_native_create_pause_resume_clear_and_no_hub_continuation(native_controller):
    controller, provider, dispatched = native_controller
    goal = await create(controller)
    assert goal.execution_owner == "provider_native"
    assert goal.status == GoalRunStatus.ACTIVE
    assert provider.calls == [("set", {"objective": "Finish the work", "status": "active"})]
    provider.turn = "automatic-turn"
    await controller.refresh_current("tab")
    paused = await controller.pause(goal.id, "pause")
    assert paused.status == GoalRunStatus.PAUSED
    assert paused.current_turn_id is None
    assert provider.calls[-2:] == [("set", {"status": "paused"}), "stop"]
    await controller.resume(goal.id, "resume")
    completed_turn = await controller.on_turn_completed(
        "tab", "automatic-turn", "completed", "Done"
    )
    assert completed_turn.status == GoalRunStatus.ACTIVE
    assert completed_turn.turns_completed == 1
    assert not dispatched
    assert await controller.dispatch_next(goal.id) == controller.get(goal.id)
    cleared = await controller.clear(goal.id, "clear")
    assert cleared.status == GoalRunStatus.CANCELLED
    assert provider.goal is None
    assert await controller.refresh_current("tab") is None
    assert not dispatched


@pytest.mark.asyncio
async def test_native_existing_goal_is_adopted_and_null_is_not_unsupported(native_controller):
    controller, provider, dispatched = native_controller
    assert await controller.refresh_current("tab") is None
    await provider.set_goal({"objective": "Already running", "status": "active"})
    provider.turn = "native-auto"
    adopted = await controller.refresh_current("tab")
    assert adopted.current_turn_id == "native-auto"
    assert adopted.execution_owner == "provider_native"
    provider.goal["status"] = "complete"
    assert (await controller.refresh_current("tab")).status == GoalRunStatus.COMPLETE
    provider.goal = None
    assert await controller.refresh_current("tab") is None
    assert not dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"]
)
async def test_native_status_and_usage_remain_authoritative(native_controller, state):
    controller, provider, _ = native_controller
    await provider.set_goal({"objective": "Native work", "status": state})
    provider.goal["tokensUsed"] = 1234
    goal = await controller.refresh_current("tab")
    assert goal.status.value == state
    assert goal.provider_status == state
    assert goal.token_usage == 1234
    assert goal.usage_quality.value == "exact"
    assert goal.checkpoint is None


@pytest.mark.asyncio
async def test_old_hub_goal_never_dispatches_once_native_owner_is_discovered(native_controller):
    controller, provider, dispatched = native_controller
    old = controller.create("tab", GoalRunCreate(objective="Native Goal", client_request_id="old"))
    await provider.set_goal({"objective": "Native Goal", "status": "active"})
    adopted = await controller.refresh_current("tab")
    assert adopted.id == old.id
    assert adopted.objective == "Native Goal"
    assert adopted.pending_step_id is None
    await controller.on_turn_completed(
        "tab", "old-hub-turn", "completed", '<goal-status state="continue">go</goal-status>'
    )
    await controller.dispatch_next(old.id)
    assert not dispatched


@pytest.mark.asyncio
async def test_native_transient_read_failure_never_falls_back_to_hub(native_controller):
    controller, provider, dispatched = native_controller
    provider.read_error = True
    with pytest.raises(NativeGoalError, match="provider unavailable"):
        await create(controller)
    assert controller.current("tab") is None
    assert not dispatched


@pytest.mark.asyncio
async def test_confirmed_unsupported_provider_keeps_hub_scheduler(native_controller):
    controller, provider, dispatched = native_controller
    provider.supported = False
    goal = await create(controller)
    assert goal.execution_owner == "hub_managed"
    assert goal.current_turn_id == "hub-turn"
    assert "<goal-checkpoint>" in dispatched[0]
    assert not provider.calls


@pytest.mark.asyncio
async def test_existing_native_owner_cannot_silently_fall_back(native_controller):
    controller, provider, dispatched = native_controller
    goal = await create(controller)
    provider.supported = False
    with pytest.raises(NativeGoalError, match="requires its native"):
        await controller.resume(goal.id, "resume")
    assert not dispatched
    assert controller.get(goal.id).status == GoalRunStatus.ACTIVE


@pytest.mark.asyncio
async def test_native_stop_failure_preserves_uncertainty_until_execution_stops(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    provider.turn = "running"
    provider.stop_error = True
    with pytest.raises(NativeGoalError, match="interrupt not confirmed"):
        await controller.pause(goal.id, "pause")
    refreshed = await controller.refresh_current("tab")
    assert refreshed.status == GoalRunStatus.PAUSED
    assert refreshed.dispatch_state.value == "uncertain"
    assert refreshed.provider_stop_pending
    provider.turn = None
    refreshed = await controller.refresh_current("tab")
    assert refreshed.dispatch_state.value == "idle"
    assert not refreshed.provider_stop_pending


@pytest.mark.asyncio
async def test_native_create_lost_response_reconciles_without_second_start(native_controller):
    controller, provider, dispatched = native_controller
    provider.accept_then_error = True
    with pytest.raises(NativeGoalError, match="response lost"):
        await create(controller)
    provider.accept_then_error = False
    goal = await create(controller)
    assert goal.status == GoalRunStatus.ACTIVE
    assert len(provider.calls) == 1
    assert not dispatched


@pytest.mark.asyncio
async def test_native_pause_replay_does_not_cancel_a_resumed_turn(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    await controller.pause(goal.id, "pause")
    await controller.resume(goal.id, "resume")
    provider.turn = "new-turn"
    calls = len(provider.calls)
    replay = await controller.pause(goal.id, "pause")
    assert replay.status == GoalRunStatus.ACTIVE
    assert provider.turn == "new-turn"
    assert len(provider.calls) == calls
    with pytest.raises(GoalPolicyError, match="another mutation"):
        await controller.clear(goal.id, "pause")


@pytest.mark.asyncio
async def test_native_steer_preserves_user_text_and_uses_no_checkpoint_prompt(native_controller):
    controller, provider, dispatched = native_controller
    goal = await create(controller)
    provider.turn = "previous-turn"
    result = await controller.steer(goal.id, "steer", "Use a different approach", "user-steer")
    assert provider.calls[-4:] == [
        ("set", {"status": "paused"}),
        "stop",
        ("input", "Use a different approach", "user-steer"),
        ("set", {"status": "active"}),
    ]
    assert result.current_turn_id == "user-steer"
    assert result.status == GoalRunStatus.ACTIVE
    assert not dispatched


@pytest.mark.asyncio
async def test_native_generation_fences_old_goal_actions(native_controller):
    controller, provider, _ = native_controller
    goal = await create(controller)
    await provider.clear()
    await provider.set_goal({"objective": "Replacement Goal", "status": "active"})
    replacement = await controller.refresh_current("tab")
    assert replacement.id != goal.id
    calls = len(provider.calls)
    with pytest.raises(GoalPolicyError, match="changed or was cleared"):
        await controller.pause(goal.id, "old-pause")
    assert len(provider.calls) == calls
    assert provider.goal["status"] == "active"


@pytest.mark.asyncio
async def test_native_recovery_does_not_pause_or_start_provider_goal(native_controller):
    controller, provider, dispatched = native_controller
    goal = await create(controller)
    calls = len(provider.calls)
    assert controller.recover() == []
    await controller.resume(
        goal.id, "restart", step_id="restart-step", dispatch=lambda goal, prompt: "bad"
    )
    assert len(provider.calls) == calls
    assert not dispatched
    assert controller.get(goal.id).status == GoalRunStatus.ACTIVE
