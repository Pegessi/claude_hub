"""Workspace workflow invariants for native Chat execution."""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    DispatchDecisionRequest,
    EnsureWorkspaceAgentRequest,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    StartTaskRequest,
    TerminalAgentStatus,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
)
from claude_hub.services import managed_chat
from claude_hub.services.agent_stream.base import NativeInputNotSubmittedError
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.workspace_manager import WorkspaceManager

wm_module = importlib.import_module("claude_hub.services.workspace_manager")
_real_managed_chat_delivery_state = managed_chat.delivery_state


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkspaceManager:
    monkeypatch.setattr(wm_module, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(WorkspaceManager, "_load_state", lambda self: None)
    monkeypatch.setattr(WorkspaceManager, "_load_scheduled_tasks", lambda self: None)
    manager = WorkspaceManager()
    now = datetime.now()
    workspace = Workspace(
        id="workspace-chat",
        name="Chat tests",
        path=str(tmp_path),
        default_branch="main",
        session_prefix="chat-tests",
        created_at=now,
        updated_at=now,
    )
    manager.workspaces[workspace.id] = workspace
    session = ManagedSession(
        id="chat-worker",
        workspace_id=workspace.id,
        tab_id="chat-tab",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Chat worker",
        workspace_path=str(tmp_path),
        tmux_session="",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    task = WorkspaceTask(
        id="chat-task",
        workspace_id=workspace.id,
        title="Implement a feature",
        prompt="Implement the requested feature and submit a report.",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=WorkspaceTaskStatus.QUEUED,
        session_id=session.id,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    monkeypatch.setattr(manager, "_assert_session_seat", lambda session: "chat-seat")
    monkeypatch.setattr(
        manager, "_rename_session_for_task", AsyncMock(side_effect=lambda s, *a, **k: s)
    )
    monkeypatch.setattr(manager, "_lesson_context_payload", lambda *args: {})
    monkeypatch.setattr(managed_chat, "is_turn_running", lambda session: False)
    monkeypatch.setattr(managed_chat, "runtime_status", lambda session: AgentRuntimeStatus.IDLE)
    monkeypatch.setattr(managed_chat, "delivery_state", lambda session, call_id: "accepted")
    return manager


@pytest.mark.asyncio
async def test_chat_dispatch_claims_assignment_before_native_send(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def send(session, text, attachments, call_id):
        assert manager.tasks["chat-task"].session_id == session.id
        assert manager.sessions[session.id].task_id == "chat-task"
        assert call_id in manager.sessions[session.id].processing_call_ids
        calls.append(call_id)

    monkeypatch.setattr(managed_chat, "send_message", send)
    forbidden = AsyncMock(side_effect=AssertionError("Chat must not use tmux"))
    monkeypatch.setattr(manager, "_send_tmux_message_with_receipt", forbidden)
    await manager._dispatch_task_to_session_workspace(
        manager.tasks["chat-task"], manager.sessions["chat-worker"]
    )
    assert calls == ["dispatch:chat-task:0"]
    assert manager.tasks["chat-task"].status == WorkspaceTaskStatus.WORKING
    assert manager.sessions["chat-worker"].delivered_call_ids == []
    assert manager.sessions["chat-worker"].processing_call_ids == calls

    # Reconciliation must retain the original dispatch envelope without replay.
    await manager._send_dispatch_message("chat-worker", calls[0], "a rebuilt prompt")
    assert len(calls) == 1
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_provider_shutdown_rejection_rolls_mailbox_back_to_pending(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_id = "managed-restart-call"
    attempts = 0

    async def send(session, text, attachments, delivered_call_id):
        nonlocal attempts
        attempts += 1
        assert delivered_call_id == call_id
        if attempts == 1:
            raise NativeInputNotSubmittedError("service is shutting down")

    delivery_states = iter(["not_sent"])
    monkeypatch.setattr(managed_chat, "send_message", send)
    monkeypatch.setattr(
        managed_chat,
        "delivery_state",
        lambda session, delivered_call_id: next(delivery_states),
    )

    with pytest.raises(NativeInputNotSubmittedError, match="shutting down"):
        await manager.send_session_message("chat-worker", "continue", call_id=call_id)
    session = manager.sessions["chat-worker"]
    assert session.pending_call_ids == [call_id]
    assert session.processing_call_ids == []
    assert session.uncertain_call_ids == []
    fingerprint = session.call_payload_fingerprints[call_id]

    monkeypatch.setattr(
        managed_chat, "delivery_state", lambda session, delivered_call_id: "accepted"
    )
    await manager.send_session_message("chat-worker", "continue", call_id=call_id)
    session = manager.sessions["chat-worker"]
    assert attempts == 2
    assert session.pending_call_ids == []
    assert session.processing_call_ids == [call_id]
    assert session.uncertain_call_ids == []
    assert session.call_payload_fingerprints[call_id] == fingerprint


@pytest.mark.asyncio
async def test_lazy_api_shutdown_rejection_rolls_workspace_call_back_to_pending(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.agent_stream import tailer as tailer_module

    call_id = "lazy-manager-restart-call"
    providers: list[ProviderSession] = []
    provider_inputs: list[str] = []

    class MemoryProvider(ProviderSession):
        def _build_command(self) -> list[str]:
            return []

        async def _send_text(self, text: str) -> None:
            provider_inputs.append(text)

    def factory(*args: Any, **kwargs: Any) -> ProviderSession:
        provider = MemoryProvider(*args, **kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(tailer_module, "create_native_session", factory)
    monkeypatch.setattr(agent_stream_api, "workspace_manager", manager)
    monkeypatch.setattr(agent_stream_api, "_tailer_manager", None)
    monkeypatch.setattr(agent_stream_api, "_native_input_shutting_down", True)
    monkeypatch.setattr(managed_chat, "delivery_state", _real_managed_chat_delivery_state)

    with pytest.raises(NativeInputNotSubmittedError, match="shutting down"):
        await manager.send_session_message("chat-worker", "continue", call_id=call_id)
    session = manager.sessions["chat-worker"]
    assert session.pending_call_ids == [call_id]
    assert session.processing_call_ids == []
    assert session.uncertain_call_ids == []
    assert managed_chat.delivery_state(session, call_id) == "not_sent"
    assert providers == []

    agent_stream_api._reset_tailer_manager()
    try:
        await manager.send_session_message("chat-worker", "continue", call_id=call_id)
        session = manager.sessions["chat-worker"]
        assert session.pending_call_ids == []
        assert session.processing_call_ids == [call_id]
        assert session.uncertain_call_ids == []
        assert managed_chat.delivery_state(session, call_id) == "accepted"
        assert len(providers) == 1
        assert len(provider_inputs) == 1
    finally:
        await agent_stream_api._stop_all_tailer_managers()


@pytest.mark.asyncio
async def test_chat_stop_pauses_only_matching_session_without_completing_task(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = manager.tasks["chat-task"]
    manager.tasks[task.id] = task.model_copy(update={"status": WorkspaceTaskStatus.WORKING})
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(managed_chat, "cancel", cancel)
    session_before_stop = manager.sessions["chat-worker"]
    assert await manager.cancel_chat_session("chat-worker", expected_turn_id="turn-1")
    cancel.assert_awaited_once_with(session_before_stop, expected_turn_id="turn-1")
    assert manager.sessions["chat-worker"].chat_paused
    assert manager.sessions["chat-worker"].status == ManagedSessionStatus.NEEDS_INPUT
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING
    assert manager.tasks[task.id].human_accepted_at is None


@pytest.mark.asyncio
async def test_stale_chat_stop_does_not_pause_newer_turn(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_chat, "cancel", AsyncMock(return_value=False))
    assert not await manager.cancel_chat_session("chat-worker", expected_turn_id="old")
    assert not manager.sessions["chat-worker"].chat_paused


@pytest.mark.asyncio
async def test_explicit_user_message_resumes_chat_but_invalid_message_does_not(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = manager.sessions["chat-worker"]
    manager.sessions[session.id] = session.model_copy(update={"chat_paused": True})
    send = AsyncMock()
    monkeypatch.setattr(manager, "_send_session_message_locked", send)
    with pytest.raises(ValueError, match="message or attachment"):
        await manager.send_chat_session_message(session.id, "", call_id="resume")
    assert manager.sessions[session.id].chat_paused
    send.assert_not_awaited()
    await manager.send_chat_session_message(session.id, "continue", call_id="resume")
    assert not manager.sessions[session.id].chat_paused
    send.assert_awaited_once_with(session.id, "continue", None, "resume", question_key=None)


@pytest.mark.asyncio
async def test_uncertain_delivery_cannot_be_unpaused_by_user_message(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = manager.sessions["chat-worker"]
    manager.sessions[session.id] = session.model_copy(
        update={"chat_paused": True, "uncertain_call_ids": ["unknown"]}
    )
    send = AsyncMock()
    monkeypatch.setattr(manager, "_send_session_message_locked", send)
    with pytest.raises(RuntimeError, match="uncertain"):
        await manager.send_chat_session_message(session.id, "continue", call_id="resume")
    assert manager.sessions[session.id].chat_paused
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_reset_uses_native_lifecycle_not_clear_text(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = manager.sessions["chat-worker"]
    reset = AsyncMock()
    send = AsyncMock(side_effect=AssertionError("/clear must not be sent as Chat text"))
    monkeypatch.setattr(managed_chat, "reset_context", reset)
    monkeypatch.setattr(manager, "send_session_message", send)
    await manager._reset_session_context(session)
    reset.assert_awaited_once_with(session)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_reloads_identity_before_dispatch_claim(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = manager.tasks["chat-task"].model_copy(update={"clear_context": True})
    manager.tasks[task.id] = task

    async def reset(session):
        manager.sessions[session.id] = session.model_copy(
            update={"agent_session_id": "fresh-provider-context", "agent_session_id_verified": True}
        )

    monkeypatch.setattr(managed_chat, "reset_context", reset)
    monkeypatch.setattr(managed_chat, "send_message", AsyncMock())
    await manager._dispatch_task_to_session_workspace(task, manager.sessions["chat-worker"])
    assert manager.sessions["chat-worker"].agent_session_id == "fresh-provider-context"


@pytest.mark.asyncio
async def test_chat_monitor_never_uses_terminal_status_or_auto_review(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = manager.sessions["chat-worker"]
    manager.sessions[session.id] = session.model_copy(
        update={"task_id": "chat-task", "current_task_id": "chat-task"}
    )
    manager.tasks["chat-task"] = manager.tasks["chat-task"].model_copy(
        update={"status": WorkspaceTaskStatus.WORKING}
    )
    terminal_status = AsyncMock(side_effect=AssertionError("No terminal polling for Chat"))
    monkeypatch.setattr(wm_module.ttyd_manager, "list_tab_agent_statuses", terminal_status)
    review = AsyncMock(side_effect=AssertionError("A model ending is not a review request"))
    monkeypatch.setattr(manager, "_request_task_review", review)
    await manager._refresh_session_statuses(run_auto_continue=True)
    assert manager.tasks["chat-task"].status == WorkspaceTaskStatus.WORKING
    assert manager.sessions[session.id].status == ManagedSessionStatus.NEEDS_INPUT
    terminal_status.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_paused_status_survives_monitor_refresh(
    manager: WorkspaceManager,
) -> None:
    session = manager.sessions["chat-worker"]
    manager.sessions[session.id] = session.model_copy(update={"chat_paused": True})
    await manager._refresh_session_statuses(run_auto_continue=True)
    assert manager.sessions[session.id].chat_paused
    assert manager.sessions[session.id].runtime_status == AgentRuntimeStatus.ATTENTION


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_friendly_id", [False, True])
async def test_chat_monitor_snapshot_cannot_resurrect_deleted_or_reused_session(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
    reuse_friendly_id: bool,
) -> None:
    stale = manager.sessions["chat-worker"]
    terminal = stale.model_copy(
        update={
            "id": "terminal-monitor-barrier",
            "tab_id": "terminal-monitor-tab",
            "session_kind": SessionKind.TERMINAL,
        }
    )
    manager.sessions[terminal.id] = terminal
    status_waiting = asyncio.Event()
    release_status = asyncio.Event()

    async def blocked_statuses(*, tab_ids):
        assert tab_ids == [terminal.tab_id]
        status_waiting.set()
        await release_status.wait()
        return []

    monkeypatch.setattr(wm_module.ttyd_manager, "list_tab_agent_statuses", blocked_statuses)
    refresh = asyncio.create_task(manager._refresh_session_statuses())
    await asyncio.wait_for(status_waiting.wait(), timeout=1)

    manager.sessions.pop(stale.id)
    replacement = None
    if reuse_friendly_id:
        replacement = stale.model_copy(
            update={
                "tab_id": "fresh-tab",
                "created_at": stale.created_at + timedelta(seconds=1),
                "env": {"FRESH": "1"},
                "chat_paused": True,
                "pending_call_ids": ["fresh-call"],
            }
        )
        manager.sessions[replacement.id] = replacement
    release_status.set()
    await asyncio.wait_for(refresh, timeout=1)

    if replacement is None:
        assert stale.id not in manager.sessions
    else:
        assert manager.sessions[stale.id] == replacement


@pytest.mark.asyncio
async def test_terminal_monitor_snapshot_cannot_resurrect_deleted_session(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = manager.sessions["chat-worker"].model_copy(
        update={
            "id": "terminal-monitor-victim",
            "tab_id": "terminal-monitor-victim-tab",
            "session_kind": SessionKind.TERMINAL,
        }
    )
    manager.sessions[terminal.id] = terminal
    status_waiting = asyncio.Event()
    release_status = asyncio.Event()
    sampled_at = datetime.now(timezone.utc)

    async def blocked_statuses(*, tab_ids):
        assert tab_ids == [terminal.tab_id]
        status_waiting.set()
        await release_status.wait()
        return [
            TerminalAgentStatus(
                tab_id=terminal.tab_id,
                tab_name="victim",
                agent_type=terminal.agent_type,
                status=AgentRuntimeStatus.WORKING,
                status_text="Working",
                detail=None,
                tmux_session=terminal.tmux_session,
                last_changed_at=sampled_at,
                sampled_at=sampled_at,
            )
        ]

    monkeypatch.setattr(wm_module.ttyd_manager, "list_tab_agent_statuses", blocked_statuses)
    refresh = asyncio.create_task(manager._refresh_session_statuses())
    await asyncio.wait_for(status_waiting.wait(), timeout=1)
    manager.sessions.pop(terminal.id)
    release_status.set()
    await asyncio.wait_for(refresh, timeout=1)

    assert terminal.id not in manager.sessions


@pytest.mark.asyncio
async def test_prompt_retry_after_workspace_delete_cannot_resurrect_session(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = manager.sessions["chat-worker"]
    task = manager.tasks["chat-task"]
    tmux_entered = asyncio.Event()
    release_tmux = asyncio.Event()

    async def blocked_tmux(*_args: str) -> None:
        tmux_entered.set()
        await release_tmux.wait()

    async def fake_delete_tab(_tab_id: str) -> bool:
        return True

    monkeypatch.setattr(manager, "_run_tmux", blocked_tmux)
    monkeypatch.setattr(wm_module.ttyd_manager, "delete_tab", fake_delete_tab)
    retrying = asyncio.create_task(
        manager._retry_pending_prompt_submit(session, task, datetime.now(timezone.utc))
    )
    await asyncio.wait_for(tmux_entered.wait(), timeout=1)
    await manager.delete_workspace(session.workspace_id)
    release_tmux.set()
    await asyncio.wait_for(retrying, timeout=1)

    assert session.workspace_id not in manager.workspaces
    assert task.id not in manager.tasks
    assert session.id not in manager.sessions


@pytest.mark.asyncio
async def test_prompt_stall_capture_cannot_corrupt_reused_session(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    original = manager.sessions["chat-worker"].model_copy(
        update={
            "session_kind": SessionKind.TERMINAL,
            "tab_id": "old-tab",
            "tmux_session": "old-tmux",
            "prompt_retry_task_id": "chat-task",
            "created_at": now,
            "updated_at": now,
        }
    )
    task = manager.tasks["chat-task"].model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "started_at": now - timedelta(hours=1),
            "updated_at": now,
        }
    )
    manager.sessions[original.id] = original
    manager.tasks[task.id] = task
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()

    async def blocked_capture(_tmux_session: str) -> str:
        capture_entered.set()
        await release_capture.wait()
        return "> New workspace task assigned."

    monkeypatch.setattr(manager, "_capture_tmux_output", blocked_capture)
    detecting = asyncio.create_task(manager._detect_prompt_dispatch_stall(original, task, now))
    await asyncio.wait_for(capture_entered.wait(), timeout=1)
    replacement = original.model_copy(
        update={
            "tab_id": "replacement-tab",
            "tmux_session": "replacement-tmux",
            "created_at": now + timedelta(seconds=1),
            "updated_at": now + timedelta(seconds=1),
        }
    )
    manager.sessions[original.id] = replacement
    release_capture.set()

    assert await asyncio.wait_for(detecting, timeout=1) is None
    assert manager.sessions[original.id] == replacement
    assert not manager.reports


@pytest.mark.asyncio
async def test_auto_continue_capture_after_workspace_delete_cannot_resurrect_task(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    session = manager.sessions["chat-worker"].model_copy(
        update={
            "session_kind": SessionKind.TERMINAL,
            "tab_id": "terminal-tab",
            "tmux_session": "terminal-tmux",
            "status": ManagedSessionStatus.IDLE,
            "runtime_status": AgentRuntimeStatus.IDLE,
            "auto_continue_task_id": "chat-task",
            "auto_continue_attempts": 999,
            "hard_recovery_task_id": "chat-task",
            "hard_recovery_attempts": 999,
            "last_activity_at": now - timedelta(hours=1),
            "updated_at": now,
        }
    )
    task = manager.tasks["chat-task"].model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "session_id": session.id,
            "started_at": now - timedelta(hours=1),
            "updated_at": now,
        }
    )
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()

    async def blocked_capture(_tmux_session: str) -> str:
        capture_entered.set()
        await release_capture.wait()
        return "API error"

    async def fake_delete_tab(_tab_id: str) -> bool:
        return True

    monkeypatch.setattr(manager, "_capture_tmux_output", blocked_capture)
    monkeypatch.setattr(wm_module.ttyd_manager, "delete_tab", fake_delete_tab)
    continuing = asyncio.create_task(manager._auto_continue_stopped_task(session, task, now))
    await asyncio.wait_for(capture_entered.wait(), timeout=1)
    await manager.delete_workspace(session.workspace_id)
    release_capture.set()

    assert await asyncio.wait_for(continuing, timeout=1) is None
    assert session.workspace_id not in manager.workspaces
    assert task.id not in manager.tasks
    assert session.id not in manager.sessions


@pytest.mark.asyncio
async def test_delete_first_recreate_rejects_stale_message_waiter(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = manager.sessions["chat-worker"]
    lock = manager._pump_locks.setdefault(stale.id, asyncio.Lock())
    await lock.acquire()
    pumped: list[str] = []

    async def record_pump(session_id: str) -> None:
        pumped.append(session_id)

    monkeypatch.setattr(manager, "_pump_session_messages_locked", record_pump)
    waiter = asyncio.create_task(manager.send_session_message(stale.id, "old", call_id="old-call"))
    await asyncio.sleep(0)
    manager.sessions.pop(stale.id)
    fresh = stale.model_copy(
        update={
            "tab_id": "fresh-tab",
            "created_at": stale.created_at + timedelta(seconds=1),
            "pending_call_ids": [],
            "pending_messages": {},
            "call_payload_fingerprints": {},
        }
    )
    manager.sessions[fresh.id] = fresh
    lock.release()

    with pytest.raises(KeyError):
        await asyncio.wait_for(waiter, timeout=1)
    assert pumped == []
    assert manager.sessions[fresh.id].pending_call_ids == []


@pytest.mark.asyncio
async def test_chat_sender_rejects_terminal_duplicate_fast_path(
    manager: WorkspaceManager,
) -> None:
    terminal = manager.sessions["chat-worker"].model_copy(
        update={
            "session_kind": SessionKind.TERMINAL,
            "call_payload_fingerprints": {
                "same-call": manager._compute_payload_fingerprint("x", [])
            },
            "processing_call_ids": ["same-call"],
        }
    )
    manager.sessions[terminal.id] = terminal

    with pytest.raises(ValueError, match="requires a managed Chat"):
        await manager.send_chat_session_message(terminal.id, "x", call_id="same-call")


def test_persisted_chat_surface_and_pause_are_preserved(manager: WorkspaceManager) -> None:
    session = manager.sessions["chat-worker"].model_copy(update={"chat_paused": True})
    normalized = manager._normalize_session_item(session.model_dump(mode="json"))
    restored = ManagedSession(**normalized)
    assert restored.session_kind == SessionKind.CHAT
    assert restored.chat_paused
    normalized.pop("session_kind")
    assert manager._normalize_session_item(normalized)["session_kind"] == "terminal"


@pytest.mark.asyncio
async def test_auto_reviewer_inherits_chat_provider_and_environment(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = manager.sessions["chat-worker"].model_copy(
        update={
            "agent_type": AgentType.MASO,
            "env": {"MASO_BASE_URL": "http://127.0.0.1:19001", "MASO_MODEL": "test-model"},
        }
    )
    manager.sessions[worker.id] = worker
    created: list[EnsureWorkspaceAgentRequest] = []

    async def create(workspace_id, payload):
        created.append(payload)
        return worker.model_copy(update={"id": "reviewer", "role": WorkspaceSessionRole.REVIEWER})

    monkeypatch.setattr(manager, "ensure_workspace_agent", create)
    reviewer = await manager._select_or_create_reviewer(
        manager.workspaces[worker.workspace_id], manager.tasks["chat-task"]
    )
    assert reviewer.id != worker.id
    assert created[0].session_kind == SessionKind.CHAT
    assert created[0].agent_type == AgentType.MASO
    assert created[0].env == worker.env
    assert created[0].ephemeral


@pytest.mark.asyncio
@pytest.mark.parametrize("has_available_reviewer", [True, False])
async def test_stopped_chat_reviewer_is_not_reused_after_worker_continues(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
    has_available_reviewer: bool,
) -> None:
    worker = manager.sessions["chat-worker"]
    task = manager.tasks["chat-task"]
    previous = worker.model_copy(
        update={
            "id": "previous-reviewer",
            "tab_id": "previous-reviewer-tab",
            "role": WorkspaceSessionRole.REVIEWER,
            "task_id": task.id,
            "current_task_id": task.id,
            "last_review_task_id": task.id,
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
        }
    )
    replacement = worker.model_copy(
        update={
            "id": "replacement-reviewer",
            "tab_id": "replacement-reviewer-tab",
            "role": WorkspaceSessionRole.REVIEWER,
        }
    )
    manager.sessions[previous.id] = previous
    manager.tasks[task.id] = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.REVIEW,
            "review_session_id": previous.id,
            "review_requested_at": datetime.now(),
            "review_cycle": 1,
        }
    )
    if has_available_reviewer:
        manager.sessions[replacement.id] = replacement

    async def create_reviewer(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
        assert workspace_id == task.workspace_id
        assert payload.role == WorkspaceSessionRole.REVIEWER
        assert payload.session_kind == SessionKind.CHAT
        manager.sessions[replacement.id] = replacement
        return replacement

    create = AsyncMock(side_effect=create_reviewer)
    send = AsyncMock()
    monkeypatch.setattr(manager, "ensure_workspace_agent", create)
    monkeypatch.setattr(managed_chat, "cancel", AsyncMock(return_value=True))
    monkeypatch.setattr(managed_chat, "send_message", send)

    assert await manager.cancel_chat_session(previous.id, expected_turn_id="review-turn")
    assert manager.sessions[previous.id].chat_paused
    continued = await manager.continue_task(task.id, call_id="continue-worker")
    assert continued.status == WorkspaceTaskStatus.WORKING
    assert continued.review_session_id == previous.id
    assert continued.review_requested_at is None
    assert manager.sessions[previous.id].chat_paused

    result = await manager.request_task_review(task.id)
    assert result.status == WorkspaceTaskStatus.REVIEW
    assert result.review_session_id == replacement.id
    assert manager.sessions[previous.id].chat_paused
    assert manager.sessions[previous.id].pending_call_ids == []
    assert manager.sessions[replacement.id].pending_call_ids == []
    assert len(manager.sessions[replacement.id].processing_call_ids) == 1
    assert [call.args[0].id for call in send.await_args_list] == [worker.id, replacement.id]
    assert create.await_count == (0 if has_available_reviewer else 1)


@pytest.mark.asyncio
async def test_unpaused_previous_chat_reviewer_keeps_same_task_context(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = manager.sessions["chat-worker"]
    task = manager.tasks["chat-task"]
    reviewer = worker.model_copy(
        update={
            "id": "previous-reviewer",
            "tab_id": "previous-reviewer-tab",
            "role": WorkspaceSessionRole.REVIEWER,
            "last_review_task_id": task.id,
        }
    )
    manager.sessions[reviewer.id] = reviewer
    task = task.model_copy(update={"review_session_id": reviewer.id})
    manager.tasks[task.id] = task
    create = AsyncMock(side_effect=AssertionError("reuse the eligible previous reviewer"))
    monkeypatch.setattr(manager, "ensure_workspace_agent", create)

    selected = await manager._select_or_create_reviewer(manager.workspaces[task.workspace_id], task)
    assert selected is reviewer
    assert selected.last_review_task_id == task.id
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_task_chat_review_resets_context_before_prompt(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = manager.sessions["chat-worker"]
    reviewer = worker.model_copy(
        update={
            "id": "chat-reviewer",
            "role": WorkspaceSessionRole.REVIEWER,
            "last_review_task_id": "previous-task",
        }
    )
    manager.sessions[reviewer.id] = reviewer
    monkeypatch.setattr(manager, "_select_or_create_reviewer", AsyncMock(return_value=reviewer))
    calls: list[str] = []

    async def reset(session):
        calls.append("reset")

    async def send(session_id, text, *args, **kwargs):
        assert manager.sessions[session_id].last_review_task_id == "chat-task"
        assert text != "/clear"
        calls.append("send")

    monkeypatch.setattr(managed_chat, "reset_context", reset)
    monkeypatch.setattr(manager, "send_session_message", send)
    now = datetime.now()
    report = AgentReport(
        id="trigger",
        workspace_id=worker.workspace_id,
        task_id="chat-task",
        session_id=worker.id,
        state=AgentReportState.READY_FOR_REVIEW,
        message="Ready for independent review",
        created_at=now,
    )
    await manager._request_task_review_workspace_report(manager.tasks["chat-task"], report)
    assert calls == ["reset", "send"]
    assert manager.tasks["chat-task"].status == WorkspaceTaskStatus.REVIEW
    assert manager.tasks["chat-task"].human_accepted_at is None


def test_task_creation_accepts_and_persists_frontend_surface_contract(
    manager: WorkspaceManager,
) -> None:
    payload = WorkspaceTaskCreate.model_validate(
        {
            "title": "New Chat task",
            "prompt": "Do the work",
            "session_kind": "chat",
            "agent_type": "claude",
            "execution_control": "workspace",
        }
    )
    task = manager.create_task("workspace-chat", payload)
    assert task.session_kind == SessionKind.CHAT
    bound = manager.create_task(
        "workspace-chat",
        WorkspaceTaskCreate(title="Bound", prompt="Continue work", session_id="chat-worker"),
    )
    assert bound.session_kind == SessionKind.CHAT
    with pytest.raises(ValueError, match="execution surface"):
        manager.create_task(
            "workspace-chat",
            payload.model_copy(
                update={"session_kind": SessionKind.TERMINAL, "session_id": "chat-worker"}
            ),
        )


@pytest.mark.asyncio
async def test_dispatch_surface_filters_mixed_pool_and_rejects_wrong_target(
    manager: WorkspaceManager,
) -> None:
    chat = manager.sessions["chat-worker"]
    terminal = chat.model_copy(
        update={"id": "terminal-worker", "session_kind": SessionKind.TERMINAL}
    )
    manager.sessions[terminal.id] = terminal
    task = manager.tasks["chat-task"]
    workspace = manager.workspaces[task.workspace_id]
    decision = await manager._choose_dispatch_target(workspace, task, StartTaskRequest())
    assert decision is not None and decision[0].id == chat.id
    with pytest.raises(ValueError, match="execution surface"):
        await manager._choose_dispatch_target(
            workspace, task, StartTaskRequest(target_session_id=terminal.id)
        )
    with pytest.raises(ValueError, match="execution surface"):
        await manager._apply_dispatch_decision_workspace(
            task.id, DispatchDecisionRequest(target_session_id=terminal.id)
        )


@pytest.mark.asyncio
async def test_chat_task_creates_matching_agent_when_only_terminal_exists(
    manager: WorkspaceManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = manager.sessions.pop("chat-worker")
    manager.sessions["terminal-worker"] = chat.model_copy(
        update={"id": "terminal-worker", "session_kind": SessionKind.TERMINAL}
    )
    task = manager.tasks["chat-task"].model_copy(
        update={"status": WorkspaceTaskStatus.TODO, "session_id": None}
    )
    manager.tasks[task.id] = task
    created: list[EnsureWorkspaceAgentRequest] = []

    async def create(workspace_id, payload):
        created.append(payload)
        manager.sessions[chat.id] = chat
        return chat

    monkeypatch.setattr(manager, "ensure_workspace_agent", create)
    monkeypatch.setattr(manager, "_refresh_session_statuses", AsyncMock())
    monkeypatch.setattr(manager, "dispatch_workspace", AsyncMock())
    result = await manager.start_task(task.id)
    assert len(created) == 1 and created[0].session_kind == SessionKind.CHAT
    assert result.session_id == chat.id
    assert result.session_kind == SessionKind.CHAT


def test_queue_rebalancing_cannot_move_chat_task_to_terminal(manager: WorkspaceManager) -> None:
    chat = manager.sessions["chat-worker"]
    manager.sessions[chat.id] = chat.model_copy(
        update={"runtime_status": AgentRuntimeStatus.WORKING}
    )
    terminal = chat.model_copy(update={"id": "free-terminal", "session_kind": SessionKind.TERMINAL})
    manager.sessions[terminal.id] = terminal
    assert manager._next_reassignable_queued_task(terminal.id, chat.workspace_id) is None


def test_task_surface_edits_preserve_provider_and_active_task_contracts(
    manager: WorkspaceManager,
) -> None:
    task = manager.tasks["chat-task"].model_copy(
        update={
            "status": WorkspaceTaskStatus.TODO,
            "agent_type": AgentType.MASO,
            "session_id": None,
        }
    )
    with pytest.raises(ValueError, match="MASO"):
        manager._prepare_task_edit(
            task, WorkspaceTaskUpdate(session_kind=SessionKind.TERMINAL), datetime.now()
        )
    active = task.model_copy(update={"status": WorkspaceTaskStatus.WORKING})
    with pytest.raises(ValueError, match="Only todo tasks"):
        manager._prepare_task_edit(
            active, WorkspaceTaskUpdate(session_kind=SessionKind.CHAT), datetime.now()
        )
