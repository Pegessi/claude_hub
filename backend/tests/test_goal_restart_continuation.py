"""Planned restart keeps Goal authority while reusing the native restart guards."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from claude_hub.api import goal_runs as goal_api
from claude_hub.models import (
    GoalCheckpoint,
    GoalDispatchState,
    GoalMutationRequest,
    GoalRunCreate,
    GoalRunStatus,
)
from claude_hub.services import goal_run
from claude_hub.services import restart_continuation as recovery
from claude_hub.services.goal_run.controller import GoalRunController
from claude_hub.services.goal_run.store import GoalRunStore
from tests.test_restart_continuation import MemoryProvider, setup  # noqa: F401


def connect_goals() -> GoalRunController:
    manager = goal_run.get_goal_manager()
    manager.set_dispatch_callback(goal_api._dispatch_goal_turn)
    manager.set_cancel_callback(goal_api._cancel_goal_turn)
    return manager


async def start_goal(h: Any, *, deferred: bool = False):
    manager = connect_goals()
    goal = await manager.create_and_start(
        h.tab.id,
        GoalRunCreate(objective="Verify the whole requested outcome", client_request_id="create"),
    )
    assert goal.dispatch_state == GoalDispatchState.DISPATCHED
    if deferred:
        await manager.pause(goal.id, "pause")
        await h.work("Finish this ordinary turn first", turn_id="ordinary-turn")
        goal = await manager.resume(goal.id, "defer", after_current_turn=True)
        assert goal.resume_after_turn
    return goal


async def cold_restart(h: Any) -> GoalRunController:
    path = goal_run.get_goal_manager().store.path
    await h.replace()
    manager = GoalRunController(
        GoalRunStore(path), dispatch=goal_api._dispatch_goal_turn, cancel=goal_api._cancel_goal_turn
    )
    h.monkeypatch.setattr(goal_run, "goal_run_controller", manager)
    manager.recover()
    return manager


def ticket(h: Any) -> dict[str, Any]:
    targets = h.mailbox.read()["operation"]["chat_recovery"]["targets"]
    assert len(targets) == 1
    return dict(targets[0])


async def finish_ordinary(h: Any, text: str = "Ordinary work completed") -> None:
    provider = h.providers[-1]
    await provider._stdout_queue.put(
        (provider._stdout_generation, {"type": "result", "subtype": "success", "result": text})
    )
    for _ in range(100):
        if len(provider.inputs) > 1:
            return
        await asyncio.sleep(0.01)
    pytest.fail("Goal did not continue after the ordinary turn completed")


async def test_goal_restart_resumes_same_goal_with_one_guarded_protocol_step(setup):
    h = setup
    original = await start_goal(h)
    original.checkpoint = GoalCheckpoint(turn_id="verified-step", remaining=["Run the final tests"])
    goal_run.get_goal_manager().store.put(original)
    manager = await cold_restart(h)
    assert manager.get(original.id).status == GoalRunStatus.PAUSED
    captured = ticket(h)
    assert captured["goal_id"] == original.id
    assert not captured["goal_deferred"]

    await asyncio.gather(
        recovery.continue_interrupted_chats(), recovery.continue_interrupted_chats()
    )
    await recovery.continue_interrupted_chats()

    resumed = manager.get(original.id)
    assert resumed.status == GoalRunStatus.ACTIVE
    assert resumed.current_turn_id == captured["continuation_id"]
    assert resumed.idempotency[captured["continuation_id"]] == "resume"
    assert resumed.objective == original.objective
    assert resumed.checkpoint == original.checkpoint
    assert "Run the final tests" in h.providers[1].inputs[0]
    assert manager.dispatch is goal_api._dispatch_goal_turn
    assert len(h.providers) == 2
    assert len(h.providers[1].inputs) == 1
    assert "Continue the active Goal." in h.providers[1].inputs[0]
    assert original.objective in h.providers[1].inputs[0]
    assert h.providers[1]._conversation_id == "verified-conversation"
    assert ticket(h)["state"] == "started"
    store = h.api._tab_tailer_manager.get_tailer(f"terminal-tab-{h.tab.id}").store
    turn = await store.find_turn_with_metadata(captured["continuation_id"])
    assert turn is not None
    assert turn[4]["origin"] == "goal"
    assert turn[4]["protocol"] == "goal-continuation-v1"


async def test_deferred_goal_finishes_ordinary_work_before_normal_goal_dispatch(setup):
    h = setup
    original = await start_goal(h, deferred=True)
    manager = await cold_restart(h)
    captured = ticket(h)
    assert captured["goal_deferred"]
    await recovery.continue_interrupted_chats()
    assert h.providers[-1].inputs == ["Please continue."]
    assert manager.get(original.id).resume_after_turn

    await finish_ordinary(h)
    assert len(h.providers[-1].inputs) == 2
    assert "Continue the active Goal." in h.providers[-1].inputs[1]
    assert not manager.get(original.id).resume_after_turn
    assert manager.get(original.id).current_turn_id != captured["continuation_id"]


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("stop_original", [False, True])
async def test_native_stop_does_not_release_goal_after_restart(setup, deferred, stop_original):
    h = setup
    original = await start_goal(h, deferred=deferred)
    manager = await cold_restart(h)
    await recovery.continue_interrupted_chats()
    captured = ticket(h)
    expected = captured["turn_id"] if stop_original else captured["continuation_id"]
    result = await h.api.cancel_tab_stream_turn(
        h.tab.id, h.api.StreamCancelRequest(expected_turn_id=expected)
    )
    assert result["cancelled"]
    tailer = h.api._tab_tailer_manager.get_tailer(f"terminal-tab-{h.tab.id}")
    await tailer._await_turn_teardown()
    assert manager.get(original.id).status == GoalRunStatus.PAUSED
    assert not manager.get(original.id).resume_after_turn
    await recovery.continue_interrupted_chats()
    assert len(h.providers[-1].inputs) == 1


@pytest.mark.parametrize(
    "reason", ["paused", "blocked", "uncertain", "question", "capacity", "opt_out", "unplanned"]
)
async def test_ineligible_goal_never_auto_resumes(setup, reason):
    h = setup
    original = await start_goal(h)
    manager = goal_run.get_goal_manager()
    tailer = h.api._tab_tailer_manager.get_tailer(f"terminal-tab-{h.tab.id}")
    if reason == "paused":
        await manager.pause(original.id, "user-pause")
    elif reason in {"blocked", "uncertain"}:
        goal = manager.get(original.id)
        if reason == "blocked":
            goal.status = GoalRunStatus.BLOCKED
        else:
            goal.dispatch_state = GoalDispatchState.UNCERTAIN
        manager.store.put(goal)
    elif reason == "question":
        tailer._blocking_approval_call_ids.add("user-question")
    elif reason == "capacity":
        tailer._waiting_for_model_capacity = True
    else:
        with h.mailbox.locked() as state:
            if reason == "opt_out":
                state["operation"]["resume_running_chats"] = False
            else:
                state["operation"] = None
    await cold_restart(h)
    await recovery.continue_interrupted_chats()
    operation = h.mailbox.read().get("operation") or {}
    assert operation.get("chat_recovery", {}).get("targets", []) == []
    assert len(h.providers) == 1


@pytest.mark.parametrize("action", ["pause", "resume", "complete", "clear"])
async def test_goal_control_revokes_before_waiting_for_restart_admission(setup, action):
    h = setup
    original = await start_goal(h)
    manager = await cold_restart(h)
    entered, release = asyncio.Event(), asyncio.Event()
    start = MemoryProvider.start

    async def blocked_start(provider):
        entered.set()
        await release.wait()
        await start(provider)

    h.monkeypatch.setattr(MemoryProvider, "start", blocked_start)
    continuing = asyncio.create_task(recovery.continue_interrupted_chats())
    controlling = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        endpoint = getattr(goal_api, f"{action}_goal")
        controlling = asyncio.create_task(
            endpoint(original.id, GoalMutationRequest(client_request_id="human-control"))
        )
        for _ in range(100):
            if ticket(h)["state"] == "skipped":
                break
            await asyncio.sleep(0.01)
        assert ticket(h)["state"] == "skipped"
        assert not controlling.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(continuing, controlling), 3)
        assert ticket(h)["continuation_id"] not in manager.get(original.id).idempotency
        if action == "resume":
            assert len(h.providers[-1].inputs) == 1
            assert manager.get(original.id).current_turn_id != ticket(h)["continuation_id"]
        else:
            assert h.providers[-1].inputs == []
    finally:
        release.set()
        await asyncio.gather(
            continuing, *([controlling] if controlling else []), return_exceptions=True
        )


@pytest.mark.parametrize("action", ["pause", "resume"])
async def test_old_goal_control_replay_does_not_revoke_restart(setup, action):
    h = setup
    original = await start_goal(h)
    manager = goal_run.get_goal_manager()
    await manager.pause(original.id, "old-pause")
    await manager.resume(original.id, "old-resume")
    manager = await cold_restart(h)
    captured = ticket(h)
    endpoint = getattr(goal_api, f"{action}_goal")
    # The exact prior request is not a new human control.
    await endpoint(original.id, GoalMutationRequest(client_request_id=f"old-{action}"))
    assert ticket(h)["state"] == "pending"
    await recovery.continue_interrupted_chats()
    assert ticket(h)["state"] == "started"
    assert manager.get(original.id).current_turn_id == captured["continuation_id"]
    assert len(h.providers[-1].inputs) == 1


@pytest.mark.parametrize("deferred", [False, True])
async def test_failed_goal_restart_never_claims_started_or_replays(setup, deferred):
    h = setup
    original = await start_goal(h, deferred=deferred)
    manager = await cold_restart(h)

    async def rejected_send(provider, text, images):
        raise RuntimeError("injected provider handoff failure")

    h.monkeypatch.setattr(MemoryProvider, "send_message", rejected_send)
    await recovery.continue_interrupted_chats()
    assert ticket(h)["state"] == "failed"
    assert manager.get(original.id).status != GoalRunStatus.ACTIVE
    assert not manager.get(original.id).resume_after_turn
    count = len(h.providers)
    await recovery.continue_interrupted_chats()
    assert len(h.providers) == count
    assert h.providers[-1].inputs == []


async def test_archive_veto_before_capture_publication_is_private_and_effective(setup):
    h = setup
    await start_goal(h)
    store = h.api._tab_tailer_manager.get_tailer(f"terminal-tab-{h.tab.id}").store
    find = store.find_turn_with_metadata
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_find(turn_id):
        entered.set()
        await release.wait()
        return await find(turn_id)

    h.monkeypatch.setattr(store, "find_turn_with_metadata", blocked_find)
    capture = asyncio.create_task(recovery.capture_interrupted_chats())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert "chat_recovery" not in h.mailbox.read()["operation"]
        recovery.cancel_pending_continuation(h.tab.id, None, veto_capture=True)
        # Restore the same visible tab before capture publishes its local list.
        h.tab.archived = True
        h.tab.archived = False
        release.set()
        await asyncio.wait_for(capture, 2)
        assert h.mailbox.read()["operation"]["chat_recovery"]["targets"] == []
        assert (
            "chat_recovery_vetoes"
            not in recovery.restart.public_status(h.mailbox.read())["operation"]
        )
    finally:
        release.set()
        await asyncio.gather(capture, return_exceptions=True)


async def test_goal_ticket_cannot_be_downgraded_to_plain_chat(setup):
    h = setup
    original = await start_goal(h)
    manager = await cold_restart(h)
    await manager.clear(original.id, "human-clear")
    with h.mailbox.locked() as state:
        row = state["operation"]["chat_recovery"]["targets"][0]
        row.update(goal_id=None, goal_control_digest=None, goal_deferred=False)
    await recovery.continue_interrupted_chats()
    assert ticket(h)["state"] == "skipped"
    assert len(h.providers) == 1


async def test_fast_resumed_goal_completion_uses_normal_next_step(setup):
    h = setup
    original = await start_goal(h)
    manager = await cold_restart(h)
    send = MemoryProvider.send_message

    async def complete_first_send(provider, text, images):
        await send(provider, text, images)
        if len(provider.inputs) == 1:
            await provider._stdout_queue.put(
                (
                    provider._stdout_generation,
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": '<goal-status state="continue">Next verified step</goal-status>',
                                }
                            ],
                        },
                    },
                )
            )
            await provider._stdout_queue.put(
                (
                    provider._stdout_generation,
                    {
                        "type": "result",
                        "subtype": "success",
                        "result": '<goal-status state="continue">Next verified step</goal-status>',
                    },
                )
            )
            await asyncio.sleep(0)

    h.monkeypatch.setattr(MemoryProvider, "send_message", complete_first_send)
    await recovery.continue_interrupted_chats()
    for _ in range(100):
        if len(h.providers[-1].inputs) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(h.providers[-1].inputs) == 2
    assert ticket(h)["state"] == "started"
    assert manager.get(original.id).current_turn_id != ticket(h)["continuation_id"]
    assert manager.get(original.id).status == GoalRunStatus.ACTIVE


async def test_expiry_after_deferred_resume_does_not_leave_future_automation_armed(setup):
    h = setup
    original = await start_goal(h, deferred=True)
    manager = await cold_restart(h)
    resume = manager.resume

    async def expire_after_resume(*args, **kwargs):
        result = await resume(*args, **kwargs)
        with h.mailbox.locked() as state:
            state["operation"]["chat_recovery"]["captured_at"] = 0
        return result

    h.monkeypatch.setattr(manager, "resume", expire_after_resume)
    await recovery.continue_interrupted_chats()
    assert ticket(h)["state"] == "skipped"
    assert manager.get(original.id).status == GoalRunStatus.PAUSED
    assert not manager.get(original.id).resume_after_turn
    assert h.providers[-1].inputs == []


async def test_archive_records_capture_veto_before_runtime_stop(setup, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from claude_hub.services.agent_stream import tailer as stream_module
    from claude_hub.services.ttyd_manager import TTYDManager

    h = setup
    await start_goal(h)

    async def stop(**kwargs):
        assert h.tab.id in h.mailbox.read()["operation"]["chat_recovery_vetoes"]

    process = SimpleNamespace(
        archived=False,
        archived_at=None,
        name="Chat",
        stop=stop,
        is_active=True,
        to_schema=lambda: h.tab,
    )

    @asynccontextmanager
    async def locked(tab_id):
        yield process

    manager = SimpleNamespace(_locked_current_tab=locked, _save_state=lambda: None)
    monkeypatch.setattr(stream_module, "stop_session_stream", AsyncMock())
    await TTYDManager.archive_tab(manager, h.tab.id)
    assert process.archived
    assert not process.is_active


async def test_each_planned_restart_can_continue_the_same_goal_once(setup):
    h = setup
    original = await start_goal(h)
    await cold_restart(h)
    await recovery.continue_interrupted_chats()
    first_id = ticket(h)["continuation_id"]
    with h.mailbox.locked() as state:
        state["operation"] = {
            "id": "second-restart",
            "instance_id": recovery.restart.INSTANCE_ID,
            "status": "restarting",
            "resume_running_chats": True,
        }
    manager = await cold_restart(h)
    h.monkeypatch.setattr(recovery.restart, "INSTANCE_ID", "second-new-instance")
    second_id = ticket(h)["continuation_id"]
    assert second_id != first_id
    await recovery.continue_interrupted_chats()
    await recovery.continue_interrupted_chats()
    assert ticket(h)["state"] == "started"
    assert manager.get(original.id).current_turn_id == second_id
    assert manager.get(original.id).idempotency[first_id] == "resume"
    assert manager.get(original.id).idempotency[second_id] == "resume"
    assert len(h.providers) == 3
    assert len(h.providers[-1].inputs) == 1


async def test_claimed_goal_attempt_from_prior_instance_is_not_replayed(setup):
    h = setup
    original = await start_goal(h)
    manager = await cold_restart(h)
    with h.mailbox.locked() as state:
        row = state["operation"]["chat_recovery"]["targets"][0]
        row.update(state="claimed", claimed_by_instance_id="lost-attempt-instance")
    await recovery.continue_interrupted_chats()
    assert ticket(h)["state"] == "failed"
    assert manager.get(original.id).status == GoalRunStatus.PAUSED
    assert len(h.providers) == 1


@pytest.mark.parametrize("deferred", [False, True])
async def test_stop_in_capturing_instance_targets_original_turn(setup, deferred):
    h = setup
    original = await start_goal(h, deferred=deferred)
    await recovery.capture_interrupted_chats()
    captured = ticket(h)
    result = await h.api.cancel_tab_stream_turn(
        h.tab.id, h.api.StreamCancelRequest(expected_turn_id=captured["turn_id"])
    )
    assert result["cancelled"]
    await h.api._tab_tailer_manager.get_tailer(f"terminal-tab-{h.tab.id}")._await_turn_teardown()
    assert goal_run.get_goal_manager().get(original.id).status == GoalRunStatus.PAUSED
    assert ticket(h)["state"] == "skipped"
    await cold_restart(h)
    await recovery.continue_interrupted_chats()
    assert len(h.providers) == 1


async def test_accepted_deferred_wait_resume_is_idempotent_after_restart(setup):
    h = setup
    original = await start_goal(h, deferred=True)
    await goal_api.resume_goal(original.id, GoalMutationRequest(client_request_id="accepted-wait"))
    assert goal_run.get_goal_manager().get(original.id).idempotency["accepted-wait"] == "resume"
    await cold_restart(h)
    assert ticket(h)["state"] == "pending"
    await goal_api.resume_goal(original.id, GoalMutationRequest(client_request_id="accepted-wait"))
    assert ticket(h)["state"] == "pending"
    assert len(h.providers) == 1


async def test_human_deferred_release_retry_does_not_revoke_next_restart(setup):
    h = setup
    original = await start_goal(h, deferred=True)
    await cold_restart(h)
    entered, release = asyncio.Event(), asyncio.Event()
    accepted = []

    async def late_send(provider, text):
        entered.set()
        await release.wait()
        provider.check_delivery_admission()
        accepted.append(text)

    h.monkeypatch.setattr(MemoryProvider, "_send_text", late_send)
    continuing = asyncio.create_task(recovery.continue_interrupted_chats())
    human = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        human = asyncio.create_task(
            goal_api.resume_goal(
                original.id, GoalMutationRequest(client_request_id="accepted-release")
            )
        )
        for _ in range(100):
            if ticket(h)["state"] == "skipped":
                break
            await asyncio.sleep(0.01)
        assert ticket(h)["state"] == "skipped"
        release.set()
        await asyncio.wait_for(asyncio.gather(continuing, human), 3)
        goal = goal_run.get_goal_manager().get(original.id)
        assert goal.status == GoalRunStatus.ACTIVE
        assert goal.idempotency["accepted-release"] == "resume"
        assert len(accepted) == 1

        with h.mailbox.locked() as state:
            state["operation"] = {
                "id": "second-after-release",
                "instance_id": recovery.restart.INSTANCE_ID,
                "status": "restarting",
                "resume_running_chats": True,
            }
        await cold_restart(h)
        h.monkeypatch.setattr(recovery.restart, "INSTANCE_ID", "second-after-release-instance")
        assert ticket(h)["state"] == "pending"
        await goal_api.resume_goal(
            original.id, GoalMutationRequest(client_request_id="accepted-release")
        )
        assert ticket(h)["state"] == "pending"
        assert len(accepted) == 1
    finally:
        release.set()
        await asyncio.gather(continuing, *([human] if human else []), return_exceptions=True)
