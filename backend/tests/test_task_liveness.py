"""One recovery budget covers idle workers, reviews, loss and prompt failures."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceSessionRole,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.models.schema_domains.lifecycle import TaskLiveness
from claude_hub.services import managed_chat
from claude_hub.services.workspace_manager import WorkspaceManager
from claude_hub.services.workspace_manager.monitoring.liveness import (
    LivenessFacts,
    evaluate_liveness,
)
from tests.test_review_delivery import delivery_env, tick  # noqa: F401


def as_worker(manager, *, provider=AgentType.CODEX, kind=SessionKind.CHAT):
    session = manager.sessions.pop("reviewer").model_copy(
        update={
            "id": "worker",
            "role": WorkspaceSessionRole.ORCHESTRATOR,
            "agent_type": provider,
            "session_kind": kind,
            "tmux_session": "" if kind == SessionKind.CHAT else "claude-hub-tab",
        }
    )
    manager.sessions[session.id] = session
    task = manager.tasks["t"].model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "review_session_id": None,
            "review_requested_at": None,
            "session_kind": kind,
            "agent_type": provider,
        }
    )
    manager.tasks[task.id] = task
    return task, session


@pytest.mark.parametrize(
    "provider", [AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR, AgentType.TRAEX]
)
def test_detached_job_turn_end_gets_bounded_chat_wake(delivery_env, monkeypatch, provider):
    manager, clock, _ = delivery_env
    task, session = as_worker(manager, provider=provider)
    monkeypatch.setattr(managed_chat, "runtime_status", lambda s: AgentRuntimeStatus.IDLE)
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda s: False)
    monkeypatch.setattr(managed_chat, "has_pending_question", lambda s: False)
    monkeypatch.setattr(managed_chat, "question_input_blocked", lambda *a: False)

    async def accept(session_id, message, *, call_id):
        current = manager.sessions[session_id]
        manager.sessions[session_id] = current.model_copy(
            update={"processing_call_ids": current.processing_call_ids + [call_id]}
        )

    send = AsyncMock(side_effect=accept)
    monkeypatch.setattr(manager, "send_session_message", send)
    capture = AsyncMock(side_effect=AssertionError("Chat must not inspect tmux"))
    monkeypatch.setattr(manager, "_capture_tmux_output", capture)
    # The job itself is unregistered in phase 0. An ended turn with WORKING
    # Task is the observable violation; exact job-exit delivery is phase 2.
    for _ in range(3):
        asyncio.run(tick(manager))
        clock[0] += timedelta(seconds=61)
    assert send.await_count == 2
    for call in send.await_args_list:
        assert "/api/workspaces/sessions/worker/reports" in call.args[1]
        assert "liveness-progress-cycle-1-attempt-" in call.args[1]
        assert call.kwargs["call_id"] in call.args[1]
    assert manager.tasks[task.id].liveness.escalated_at is not None
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    assert len({c.kwargs["call_id"] for c in send.await_args_list}) == 2
    capture.assert_not_awaited()


def test_plan_review_gate_idle_reviewer_is_nudged(delivery_env):
    from claude_hub.models import GoalPacket, GoalPacketStatus

    manager, _, sent = delivery_env
    task = manager.tasks["t"]
    manager.tasks[task.id] = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "goal_packet": GoalPacket(objective="work", status=GoalPacketStatus.PENDING_REVIEW),
        }
    )
    asyncio.run(tick(manager))
    assert len(sent) == 1
    assert "SAME review round" in sent[0][0]
    assert manager.tasks[task.id].review_cycle == 1
    assert manager.tasks[task.id].reviewed_cycle == 0


def test_codex_needs_input_reviewer_routes_without_fabricating_verdict(delivery_env):
    manager, clock, sent = delivery_env
    reviewer = manager.sessions["reviewer"]
    manager.sessions[reviewer.id] = reviewer.model_copy(
        update={
            "runtime_status": AgentRuntimeStatus.ATTENTION,
            "status": ManagedSessionStatus.NEEDS_INPUT,
        }
    )
    asyncio.run(tick(manager))
    clock[0] += timedelta(seconds=181)
    asyncio.run(tick(manager))
    task = manager.tasks["t"]
    assert task.liveness.escalated_at is not None
    assert "not yet established" in task.liveness.detail
    assert task.review_completed_at is None and task.reviewed_cycle == 0
    assert not sent


@pytest.mark.parametrize("case", ["active", "paused", "uncertain", "question", "unknown"])
def test_chat_safety_fences_never_send_or_reset(delivery_env, monkeypatch, case):
    manager, clock, _ = delivery_env
    _, session = as_worker(manager)
    monkeypatch.setattr(
        managed_chat,
        "runtime_status",
        lambda s: AgentRuntimeStatus.WORKING if case == "active" else AgentRuntimeStatus.IDLE,
    )
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda s: case in {"active", "unknown"})
    monkeypatch.setattr(managed_chat, "has_pending_question", lambda s: case == "question")
    monkeypatch.setattr(managed_chat, "question_input_blocked", lambda *a: False)
    if case == "paused":
        manager.sessions[session.id] = session.model_copy(update={"chat_paused": True})
    if case == "uncertain":
        manager.sessions[session.id] = session.model_copy(
            update={"uncertain_call_ids": ["maybe-sent"]}
        )
    send, reset = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "send_session_message", send)
    monkeypatch.setattr(manager, "_reset_session_context", reset)
    asyncio.run(tick(manager))
    clock[0] += timedelta(hours=4)
    asyncio.run(tick(manager))
    send.assert_not_awaited()
    reset.assert_not_awaited()
    assert bool(manager.tasks["t"].liveness.escalated_at) == (case != "active")


def test_session_replacement_does_not_reset_budget(delivery_env):
    manager, clock, sent = delivery_env
    for _ in range(2):
        asyncio.run(tick(manager))
        clock[0] += timedelta(seconds=181)
        session = manager.sessions["reviewer"]
        manager.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.IDLE,
                "runtime_status": AgentRuntimeStatus.IDLE,
            }
        )
    replacement = manager.sessions["reviewer"].model_copy(update={"created_at": clock[0]})
    manager.sessions[replacement.id] = replacement
    asyncio.run(tick(manager))
    assert len(sent) == 2
    assert manager.tasks["t"].liveness.attempts == 2
    assert manager.tasks["t"].liveness.escalated_at is not None


def test_pending_prompt_retries_enter_once_then_routes(delivery_env, monkeypatch):
    manager, clock, sent = delivery_env

    async def capture(*a):
        return "> Review workspace task."

    monkeypatch.setattr(manager, "_capture_tmux_output", capture)
    enter = AsyncMock()
    monkeypatch.setattr(manager, "_run_tmux", enter)
    asyncio.run(tick(manager))
    clock[0] += timedelta(seconds=11)
    asyncio.run(tick(manager))
    assert enter.await_count == 1
    assert not sent
    assert manager.tasks["t"].liveness.escalated_at is not None


def test_claude_error_reset_consumes_second_shared_attempt(delivery_env, monkeypatch):
    manager, clock, sent = delivery_env
    task, session = as_worker(manager, provider=AgentType.CLAUDE, kind=SessionKind.TERMINAL)

    async def capture(*a):
        return "API error"

    monkeypatch.setattr(manager, "_capture_tmux_output", capture)
    reset = AsyncMock()
    monkeypatch.setattr(manager, "_reset_session_context", reset)
    for _ in range(3):
        asyncio.run(tick(manager))
        clock[0] += timedelta(seconds=21)
    assert len(sent) == 2 and reset.await_count == 1
    assert manager.tasks[task.id].liveness.attempts == 2
    assert manager.tasks[task.id].liveness.escalated_at is not None


@pytest.mark.parametrize(
    "mode,dead,action",
    [
        (WorkspaceTaskMode.SUBAGENT, True, "fail"),
        (WorkspaceTaskMode.SUBAGENT, False, "fail"),
        (WorkspaceTaskMode.REVIEWED, True, "fail"),
    ],
)
def test_loss_and_timeout_share_evaluator(delivery_env, mode, dead, action):
    manager, clock, _ = delivery_env
    task, session = as_worker(manager, kind=SessionKind.TERMINAL)
    task = task.model_copy(update={"task_mode": mode, "timeout_seconds": 30})
    state = TaskLiveness(
        generation="1:1", reason="agent_gone", since=clock[0] - timedelta(minutes=6)
    )
    facts = LivenessFacts(task, session, clock[0], clock[0] - timedelta(minutes=6), dead=dead)
    assert evaluate_liveness(facts, state).action == action
    assert evaluate_liveness(replace(facts, active=True), state).action == (
        "fail" if mode == WorkspaceTaskMode.SUBAGENT else "wait"
    )


def test_dot_watch_threshold_is_input_not_a_second_timer(delivery_env):
    manager, clock, sent = delivery_env
    task, _ = as_worker(manager, kind=SessionKind.TERMINAL)
    manager.tasks[task.id] = task.model_copy(update={"status": WorkspaceTaskStatus.QUEUED})
    manager.configure_liveness_watch(lambda task: (clock[0] - timedelta(hours=2), 0.5))
    clock[0] += timedelta(hours=1)
    asyncio.run(tick(manager))
    assert not sent
    assert manager.tasks[task.id].liveness.reason == "watch_stalled"
    assert manager.tasks[task.id].liveness.escalated_at is not None


def test_registered_future_wake_is_a_fact_on_same_evaluator(delivery_env):
    manager, clock, _ = delivery_env
    task, session = as_worker(manager)
    facts = LivenessFacts(
        task, session, clock[0], task.created_at, wake_deadline=clock[0] + timedelta(minutes=1)
    )
    state = TaskLiveness(generation="1:1", since=clock[0])
    assert evaluate_liveness(facts, state).reason == "registered_wake"
    assert (
        evaluate_liveness(replace(facts, now=clock[0] + timedelta(minutes=2)), state).action
        == "nudge"
    )


def test_new_owner_loss_gets_its_own_grace(delivery_env):
    manager, clock, sent = delivery_env
    task, session = as_worker(manager, kind=SessionKind.TERMINAL)
    manager.tasks[task.id] = task.model_copy(
        update={
            "liveness": TaskLiveness(
                generation="1:1", reason="active_turn", since=clock[0] - timedelta(hours=4)
            )
        }
    )
    manager.sessions[session.id] = session.model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    asyncio.run(tick(manager))
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    clock[0] += timedelta(seconds=301)
    asyncio.run(tick(manager))
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.FAILED
    assert not sent


def test_legacy_budget_is_imported_only_once(delivery_env):
    manager, clock, sent = delivery_env
    session = manager.sessions["reviewer"]
    manager.sessions[session.id] = session.model_copy(
        update={
            "auto_continue_task_id": "t",
            "auto_continue_attempts": 10,
        }
    )
    asyncio.run(tick(manager))
    assert manager.tasks["t"].liveness.escalated_at is not None
    task = manager.tasks["t"]
    manager.tasks[task.id] = task.model_copy(update={"review_cycle": 2})
    asyncio.run(tick(manager))
    assert len(sent) == 1
    assert manager.tasks["t"].liveness.attempts == 1
    assert manager.tasks["t"].liveness.escalated_at is None


def test_claim_cancelled_before_side_effect_is_not_uncertain(delivery_env, monkeypatch):
    manager, clock, sent = delivery_env
    original = manager._liveness_facts
    count = 0

    async def facts(task, **kwargs):
        nonlocal count
        result = await original(task, **kwargs)
        count += 1
        return replace(result, active=True) if count == 2 else result

    monkeypatch.setattr(manager, "_liveness_facts", facts)
    asyncio.run(tick(manager))
    state = manager.tasks["t"].liveness
    assert state.attempts == 0 and state.action_id is None
    assert not sent
    asyncio.run(tick(manager))
    assert len(sent) == 1
    assert manager.tasks["t"].liveness.escalated_at is None


def test_resolved_blocker_closes_attention_and_keeps_budget(delivery_env):
    manager, clock, sent = delivery_env
    task, session = manager.tasks["t"], manager.sessions["reviewer"]
    manager.tasks[task.id] = task.model_copy(
        update={
            "liveness": TaskLiveness(
                generation="1:1",
                reason="blocked_runtime",
                since=clock[0],
                attempts=1,
                escalated_at=clock[0],
                detail="permission needed",
            )
        }
    )
    manager.sessions[session.id] = session.model_copy(
        update={"runtime_status": AgentRuntimeStatus.WORKING}
    )
    asyncio.run(tick(manager))
    state = manager.tasks[task.id].liveness
    assert state.escalated_at is None and state.attempts == 1 and state.incident == 2
    assert manager.task_lifecycles([manager.tasks[task.id]])[task.id].reason != "liveness_attention"
    assert not sent


def test_replacement_provisioning_cannot_overwrite_resumed_worker(delivery_env, monkeypatch):
    manager, clock, _ = delivery_env
    task, session = as_worker(manager, kind=SessionKind.TERMINAL)
    manager.tasks[task.id] = task.model_copy(
        update={"system_internal": True, "internal_kind": "scheduled"}
    )
    manager.sessions[session.id] = session.model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    replacement = session.model_copy(update={"id": "new-worker"})

    async def provision(*args):
        manager.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
            }
        )
        manager.sessions[replacement.id] = replacement
        return replacement

    monkeypatch.setattr(manager, "ensure_workspace_agent", provision)
    cleanup, dispatch = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_best_effort_delete_session", cleanup)
    monkeypatch.setattr(manager, "_dispatch_task_to_session", dispatch)
    asyncio.run(tick(manager))
    dispatch.assert_not_awaited()
    cleanup.assert_awaited_once_with(replacement.id)
    assert manager.tasks[task.id].session_id == session.id
    assert manager.tasks[task.id].liveness.attempts == 0


@pytest.mark.parametrize("control", ["workspace", "initiator"])
def test_dot_watch_rearms_after_progress_with_no_active_session(delivery_env, control):
    from claude_hub.models.schemas import TaskExecutionControl

    manager, clock, _ = delivery_env
    task, _ = as_worker(manager)
    manager.tasks[task.id] = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.QUEUED,
            "execution_control": TaskExecutionControl(control),
            "session_id": None,
        }
    )
    watched_at = clock[0] - timedelta(hours=2)
    manager.configure_liveness_watch(lambda task: (watched_at, 0.5))
    clock[0] += timedelta(hours=1)
    asyncio.run(tick(manager))
    first = manager.tasks[task.id].liveness
    assert first.escalated_at is not None
    # New report activity closes the incident without a managed active turn.
    clock[0] += timedelta(seconds=1)
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(update={"updated_at": clock[0]})
    asyncio.run(tick(manager))
    rearmed = manager.tasks[task.id].liveness
    assert rearmed.escalated_at is None and rearmed.incident == first.incident + 1
    assert rearmed.attempts == first.attempts
    clock[0] += timedelta(minutes=31)
    asyncio.run(tick(manager))
    second = manager.tasks[task.id].liveness
    assert second.escalated_at is not None and second.escalated_at > first.escalated_at
    assert second.incident == rearmed.incident
    asyncio.run(tick(manager))
    assert manager.tasks[task.id].liveness == second


def prepare_recovery_envelope(manager, clock, monkeypatch, kind=SessionKind.CHAT):
    task, session = as_worker(manager, kind=kind)
    monkeypatch.setattr(manager, "_assert_session_seat", lambda s: "")
    monkeypatch.setattr(managed_chat, "runtime_status", lambda s: AgentRuntimeStatus.IDLE)
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda s: False)
    monkeypatch.setattr(managed_chat, "has_pending_question", lambda s: False)
    monkeypatch.setattr(managed_chat, "question_input_blocked", lambda *a: False)
    monkeypatch.setattr(manager, "_chat_delivery_state", lambda *a: "not_sent")
    call_id = f"liveness:{task.id}:{task.execution_epoch}:{task.review_cycle}:1"
    state = TaskLiveness(
        generation=f"{task.execution_epoch}:{task.review_cycle}",
        since=clock[0],
        action="nudge",
        action_id=call_id,
        action_at=clock[0],
        attempts=1,
        session_id=session.id,
        session_created_at=session.created_at,
        message="resume",
    )
    manager.tasks[task.id] = task.model_copy(update={"liveness": state})
    return task, session, call_id


def supersede_recovery(manager, task, session, monkeypatch, reason):
    if reason == "cycle":
        current = manager.tasks[task.id]
        manager.tasks[task.id] = current.model_copy(
            update={"review_cycle": current.review_cycle + 1}
        )
    elif reason == "question":
        monkeypatch.setattr(managed_chat, "has_pending_question", lambda s: True)
    elif reason == "paused":
        current = manager.sessions[session.id]
        manager.sessions[session.id] = current.model_copy(update={"chat_paused": True})
    else:
        monkeypatch.setattr(managed_chat, "runtime_status", lambda s: AgentRuntimeStatus.WORKING)
        current = manager.sessions[session.id]
        manager.sessions[session.id] = current.model_copy(
            update={"runtime_status": AgentRuntimeStatus.WORKING}
        )


@pytest.mark.parametrize("reason", ["active", "question", "paused", "cycle"])
@pytest.mark.parametrize("boundary", ["mailbox_lock", "ready", "pending_retry"])
def test_recovery_is_cancelled_at_actual_delivery_boundary(
    delivery_env, monkeypatch, reason, boundary
):
    manager, clock, sent = delivery_env
    task, session, call_id = prepare_recovery_envelope(manager, clock, monkeypatch)
    native_send = AsyncMock()
    monkeypatch.setattr(managed_chat, "send_message", native_send)

    async def run():
        if boundary == "mailbox_lock":
            lock = manager._pump_locks.setdefault(session.id, asyncio.Lock())
            await lock.acquire()
            sender = asyncio.create_task(
                manager.send_session_message(session.id, "resume", call_id=call_id)
            )
            await asyncio.sleep(0)
            supersede_recovery(manager, task, session, monkeypatch, reason)
            lock.release()
            await sender
        elif boundary == "ready":

            async def ready(_session):
                supersede_recovery(manager, task, session, monkeypatch, reason)

            monkeypatch.setattr(manager, "_ensure_session_ready_for_send", ready)
            await manager.send_session_message(session.id, "resume", call_id=call_id)
        else:
            # Simulate a durable enqueue whose first submission failed before any side effect.
            pump = manager._pump_session_messages_locked
            monkeypatch.setattr(manager, "_pump_session_messages_locked", AsyncMock())
            await manager.send_session_message(session.id, "resume", call_id=call_id)
            monkeypatch.setattr(manager, "_pump_session_messages_locked", pump)
            supersede_recovery(manager, task, session, monkeypatch, reason)
            await manager._pump_session_messages(session.id)
        await manager._pump_session_messages(session.id)
        # A stale caller/restart replay must not resurrect a cancelled wake.
        await manager.send_session_message(session.id, "resume", call_id=call_id)

    asyncio.run(run())
    assert not sent
    native_send.assert_not_awaited()
    current = manager.sessions[session.id]
    assert (
        call_id
        not in current.pending_call_ids + current.processing_call_ids + current.delivered_call_ids
    )
    assert call_id not in current.call_payload_fingerprints
    assert manager.tasks[task.id].liveness.action_finished


@pytest.mark.parametrize("kind", [SessionKind.CHAT, SessionKind.TERMINAL])
def test_pending_recovery_cannot_reach_new_cycle_or_block_normal_message(
    delivery_env, monkeypatch, kind
):
    manager, clock, sent = delivery_env
    task, session, call_id = prepare_recovery_envelope(manager, clock, monkeypatch, kind)
    native_send = AsyncMock()
    monkeypatch.setattr(managed_chat, "send_message", native_send)

    async def run():
        pump = manager._pump_session_messages_locked
        monkeypatch.setattr(manager, "_pump_session_messages_locked", AsyncMock())
        await manager.send_session_message(session.id, "resume", call_id=call_id)
        monkeypatch.setattr(manager, "_pump_session_messages_locked", pump)
        supersede_recovery(manager, task, session, monkeypatch, "cycle")
        await manager.send_session_message(
            session.id, "ordinary user follow-up", call_id="user:next"
        )

    asyncio.run(run())
    assert call_id not in manager.sessions[session.id].pending_call_ids
    assert "user:next" in manager.sessions[session.id].processing_call_ids
    assert (native_send.await_count if kind == SessionKind.CHAT else len(sent)) == 1


@pytest.mark.parametrize(
    "receipt", ["processing_call_ids", "uncertain_call_ids", "delivered_call_ids"]
)
def test_recovery_cancellation_preserves_receipt_evidence(delivery_env, monkeypatch, receipt):
    manager, clock, _ = delivery_env
    _, session, call_id = prepare_recovery_envelope(manager, clock, monkeypatch)
    manager.sessions[session.id] = session.model_copy(update={receipt: [call_id]})
    manager._retire_liveness_delivery(session.id, call_id)
    assert call_id in getattr(manager.sessions[session.id], receipt)


def test_final_native_callback_rechecks_question(delivery_env, monkeypatch):
    from claude_hub.services.agent_stream.base import NativeInputNotSubmittedError

    manager, clock, _ = delivery_env
    task, session, call_id = prepare_recovery_envelope(manager, clock, monkeypatch)

    async def native_send(_session, _message, _attachments, _call_id, *, before_delivery):
        supersede_recovery(manager, task, session, monkeypatch, "question")
        before_delivery()
        pytest.fail("superseded input reached provider")

    monkeypatch.setattr(managed_chat, "send_message", native_send)
    with pytest.raises(NativeInputNotSubmittedError):
        asyncio.run(manager.send_session_message(session.id, "resume", call_id=call_id))
    assert call_id not in manager.sessions[session.id].pending_call_ids
    assert call_id not in manager.sessions[session.id].uncertain_call_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [AgentType.CLAUDE, AgentType.CODEX])
async def test_native_preparation_cannot_outlive_recovery_admission(
    delivery_env, monkeypatch, provider
):
    from claude_hub.services.agent_stream import get_adapter_for_session
    from claude_hub.services.agent_stream.base import NativeInputNotSubmittedError
    from claude_hub.services.agent_stream.native import ClaudeNativeSession, CodexNativeSession
    from claude_hub.services.agent_stream.tailer import SessionTailer

    manager, clock, _ = delivery_env
    task, session, call_id = prepare_recovery_envelope(manager, clock, monkeypatch)
    session = session.model_copy(update={"agent_type": provider})
    manager.sessions[session.id] = session
    native = (ClaudeNativeSession if provider == AgentType.CLAUDE else CodexNativeSession)(session)
    native._started = True
    previous = lambda: None
    native.require_resumed_conversation(None, before_delivery=previous)
    writes = AsyncMock()
    monkeypatch.setattr(native, "_send_text", writes)

    async def prepare():
        await asyncio.sleep(0)
        supersede_recovery(manager, task, session, monkeypatch, "question")

    monkeypatch.setattr(native, "_prepare_for_send", prepare)
    tailer = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=get_adapter_for_session(session),
        session_getter=lambda: session,
        native_transport=native,
    )
    try:
        with pytest.raises(NativeInputNotSubmittedError):
            await tailer.send_message(
                "resume",
                [],
                client_turn_id=call_id,
                delivery_guard=manager._liveness_native_guard(session.id, call_id),
                allow_question_answer=False,
            )
        writes.assert_not_awaited()
        assert native._delivery_admission is previous
    finally:
        await tailer.stop()


@pytest.mark.parametrize("action", ["submit_prompt", "reset_context"])
@pytest.mark.parametrize("reason", ["active", "cycle"])
def test_control_waiting_for_workspace_lock_yields_to_new_turn(
    delivery_env, monkeypatch, action, reason
):
    manager, clock, _ = delivery_env
    task, session, _call_id = prepare_recovery_envelope(
        manager, clock, monkeypatch, SessionKind.TERMINAL
    )
    current = manager.tasks[task.id]
    state = current.liveness.model_copy(update={"action": action})
    current = current.model_copy(update={"liveness": state})
    manager.tasks[task.id] = current
    enter, reset = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_run_tmux", enter)
    monkeypatch.setattr(manager, "_reset_session_context", reset)

    async def run():
        facts = await manager._liveness_facts(current)
        async with manager.workspace_mutation_lock(task.workspace_id):
            recovery = asyncio.create_task(manager._execute_liveness_action(current, facts, state))
            await asyncio.sleep(0)
            supersede_recovery(manager, task, session, monkeypatch, reason)
        await recovery

    asyncio.run(run())
    enter.assert_not_awaited()
    reset.assert_not_awaited()
    assert manager.tasks[task.id].liveness.action_id is None
    assert manager.tasks[task.id].liveness.attempts == 0
