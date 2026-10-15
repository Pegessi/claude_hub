"""Cross-layer regression coverage for managed Chat controls."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from claude_hub.api import agent_stream as stream_api
from claude_hub.api import tabs as tabs_api
from claude_hub.models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    SwitchEnvRequest,
    User,
    Workspace,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.native import CodexNativeSession
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.agent_stream.tailer import SessionTailer, TailerManager
from claude_hub.services.ttyd_manager import TTYDManager
from claude_hub.services.workspace_manager import WorkspaceManager


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        return None


@pytest.fixture
def managed_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    wm_module = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_module, "STATE_ROOT", tmp_path / "workspace-state")
    monkeypatch.setattr(WorkspaceManager, "_load_state", lambda self: None)
    monkeypatch.setattr(WorkspaceManager, "_load_scheduled_tasks", lambda self: None)
    manager = WorkspaceManager()
    now = datetime.now(timezone.utc)
    workspace = Workspace(
        id=f"workspace-{tmp_path.name}",
        name="Managed controls",
        path=str(tmp_path),
        default_branch="main",
        session_prefix="managed-controls",
        created_at=now,
        updated_at=now,
    )
    manager.workspaces[workspace.id] = workspace
    monkeypatch.setattr(manager, "_assert_session_seat", lambda _session: "seat")
    return manager, workspace


def _managed_session(
    workspace: Workspace,
    *,
    agent_type: AgentType = AgentType.CODEX,
    tab_id: str = "managed-tab",
    env: dict[str, str] | None = None,
) -> ManagedSession:
    now = datetime.now(timezone.utc)
    return ManagedSession(
        id=f"managed-{agent_type.value}-{tab_id}",
        workspace_id=workspace.id,
        tab_id=tab_id,
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=agent_type,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Managed Chat",
        workspace_path=workspace.path,
        tmux_session="",
        target=ExecutionTarget.LOCAL,
        env=dict(env or {}),
        created_at=now,
        updated_at=now,
    )


def _answer_text(value: str = "red") -> str:
    return json.dumps(
        {
            "type": "ask_question_response",
            "answers": [{"questionId": "q1", "selected": [value]}],
        }
    )


def _approval_event(session: ManagedSession, *, turn_id: str, call_id: str) -> AgentStreamEvent:
    return AgentStreamEvent(
        stream_sequence=0,
        session_id=session.id,
        tab_id=session.tab_id,
        agent_type=session.agent_type,
        type=AgentStreamEventType.APPROVAL_REQUIRED,
        turn_id=turn_id,
        call_id=call_id,
        payload={
            "tool_call_id": call_id,
            "kind": "ask_question",
            "questions": [{"id": "q1", "prompt": "Pick", "options": []}],
        },
        created_at=datetime.now(timezone.utc),
    )


async def _real_tailer_manager(
    manager: WorkspaceManager,
    session: ManagedSession,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TailerManager, SessionTailer]:
    manager.sessions[session.id] = session
    tailers = TailerManager(session_getter=lambda session_id: manager.sessions.get(session_id))
    monkeypatch.setattr(stream_api, "workspace_manager", manager)
    monkeypatch.setattr(stream_api, "_tailer_manager", tailers)
    tailer = await tailers._get_or_create(session)
    assert tailer.native_transport is not None
    return tailers, tailer


async def _stop_tailers(tailers: TailerManager, transport: object | None = None) -> None:
    if transport is not None:
        setattr(transport, "_turn_in_flight", False)
        setattr(transport, "_process", None)
    await tailers.stop_all()


async def _simulate_managed_delete(
    manager: WorkspaceManager, session: ManagedSession, tailer: SessionTailer
) -> None:
    """Commit ownership removal and clear its stream under production lock order."""
    async with manager.workspace_mutation_lock(session.workspace_id):
        pump_lock = manager._pump_locks.setdefault(session.id, asyncio.Lock())
        async with pump_lock:
            live = manager.sessions.get(session.id)
            if live is None or live.created_at != session.created_at:
                raise RuntimeError("managed session incarnation changed before simulated delete")
            manager.sessions.pop(session.id)
            await tailer.store.clear()


@pytest.mark.asyncio
async def test_managed_blocking_question_uses_exact_provider_request(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace)
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = cast(Any, tailer.native_transport)
    assert transport is not None
    stdin = _FakeStdin()
    transport._process = SimpleNamespace(stdin=stdin)
    transport._turn_in_flight = True
    transport._pending_questions[42] = {
        "itemId": "question-1",
        "questions": [{"id": "q1", "question": "Pick", "options": []}],
    }
    tailer._active_turn_id = "turn-1"
    tailer._record_approval_card(_approval_event(session, turn_id="turn-1", call_id="question-1"))

    result = await stream_api.send_stream_input(
        session.id,
        stream_api.AgentStreamSendRequest(
            text=_answer_text(),
            client_turn_id="answer-attempt",
            question_turn_id="turn-1",
            question_call_id="question-1",
        ),
        User(open_id="user", name="User", email="user@example.com"),
    )

    assert result == {"ok": True, "answered_question": True}
    response = json.loads(stdin.writes[-1])
    assert response == {
        "jsonrpc": "2.0",
        "id": 42,
        "result": {"answers": {"q1": {"answers": ["red"]}}},
    }
    assert not session.pending_call_ids
    assert "question-1" not in tailer._pending_approvals

    transport._pending_questions[43] = {
        "itemId": "question-2",
        "questions": [{"id": "q1", "question": "Pick again", "options": []}],
    }
    tailer._record_approval_card(_approval_event(session, turn_id="turn-1", call_id="question-2"))
    with pytest.raises(HTTPException) as duplicate:
        await stream_api.send_stream_input(
            session.id,
            stream_api.AgentStreamSendRequest(
                text=_answer_text(),
                client_turn_id="lost-response-retry",
                question_turn_id="turn-1",
                question_call_id="question-1",
            ),
            User(open_id="user", name="User", email="user@example.com"),
        )
    assert duplicate.value.status_code == 409
    assert 43 in transport._pending_questions
    assert len(stdin.writes) == 1
    await _stop_tailers(tailers, transport)


@pytest.mark.asyncio
async def test_exact_question_publish_is_serialized_before_managed_delete(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="answer-delete")
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = cast(Any, tailer.native_transport)
    assert transport is not None
    transport._process = SimpleNamespace(stdin=_FakeStdin())
    transport._turn_in_flight = True
    transport._pending_questions[42] = {
        "itemId": "question-delete",
        "questions": [{"id": "q1", "question": "Pick", "options": []}],
    }
    tailer._active_turn_id = "turn-delete"
    tailer._record_approval_card(
        _approval_event(session, turn_id="turn-delete", call_id="question-delete")
    )
    publish_entered = asyncio.Event()
    release_publish = asyncio.Event()
    original_publish = tailer._emit_approval_resolved

    async def blocked_publish(*args: Any, **kwargs: Any) -> None:
        publish_entered.set()
        await release_publish.wait()
        await original_publish(*args, **kwargs)

    monkeypatch.setattr(tailer, "_emit_approval_resolved", blocked_publish)
    answer_task = asyncio.create_task(
        stream_api.send_stream_input(
            session.id,
            stream_api.AgentStreamSendRequest(
                text=_answer_text(),
                client_turn_id="answer-delete-attempt",
                question_turn_id="turn-delete",
                question_call_id="question-delete",
            ),
            User(open_id="user", name="User", email="user@example.com"),
        )
    )
    await asyncio.wait_for(publish_entered.wait(), timeout=2)
    delete_task = asyncio.create_task(_simulate_managed_delete(manager, session, tailer))
    await asyncio.sleep(0.02)
    assert not delete_task.done(), "delete must wait for exact-card publication"

    release_publish.set()
    assert await asyncio.wait_for(answer_task, timeout=2) == {
        "ok": True,
        "answered_question": True,
    }
    await asyncio.wait_for(delete_task, timeout=2)
    assert session.id not in manager.sessions
    assert not tailer.store.path.exists()

    await _stop_tailers(tailers, transport)
    await tailer.store.clear()


@pytest.mark.asyncio
async def test_managed_question_rejects_wrong_turn_and_malformed_answer(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="wrong-turn")
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = cast(Any, tailer.native_transport)
    assert transport is not None
    transport._process = SimpleNamespace(stdin=_FakeStdin())
    transport._turn_in_flight = True
    transport._pending_questions[42] = {
        "itemId": "question-1",
        "questions": [{"id": "q1", "question": "Pick", "options": []}],
    }
    tailer._active_turn_id = "turn-1"
    tailer._record_approval_card(_approval_event(session, turn_id="turn-1", call_id="question-1"))
    user = User(open_id="user", name="User", email="user@example.com")

    for text, turn_id in ((_answer_text(), "turn-2"), ("not a structured answer", "turn-1")):
        with pytest.raises(HTTPException) as raised:
            await stream_api.send_stream_input(
                session.id,
                stream_api.AgentStreamSendRequest(
                    text=text,
                    client_turn_id=f"attempt-{turn_id}",
                    question_turn_id=turn_id,
                    question_call_id="question-1",
                ),
                user,
            )
        assert raised.value.status_code == 409
    assert 42 in transport._pending_questions
    assert "question-1" in tailer._pending_approvals
    await _stop_tailers(tailers, transport)


@pytest.mark.asyncio
async def test_idle_managed_question_followup_uses_workspace_mailbox(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, agent_type=AgentType.CLAUDE, tab_id="followup")
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = tailer.native_transport
    assert transport is not None
    send_text = AsyncMock()
    monkeypatch.setattr(transport, "_send_text", send_text)
    tailer._active_turn_id = "question-turn"
    tailer._record_approval_card(
        _approval_event(session, turn_id="question-turn", call_id="claude-question")
    )

    result = await stream_api.send_stream_input(
        session.id,
        stream_api.AgentStreamSendRequest(
            text=_answer_text(),
            client_turn_id="followup-call",
            question_turn_id="question-turn",
            question_call_id="claude-question",
        ),
        User(open_id="user", name="User", email="user@example.com"),
    )

    assert result == {"ok": True}
    send_text.assert_awaited_once()
    assert "followup-call" in manager.sessions[session.id].processing_call_ids
    assert "claude-question" not in tailer._pending_approvals
    await tailers.stop_all()


@pytest.mark.asyncio
async def test_idle_question_resolution_is_serialized_before_managed_delete(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, agent_type=AgentType.CLAUDE, tab_id="followup-delete")
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = tailer.native_transport
    assert transport is not None
    monkeypatch.setattr(transport, "_send_text", AsyncMock())
    tailer._active_turn_id = "question-turn-delete"
    tailer._record_approval_card(
        _approval_event(
            session,
            turn_id="question-turn-delete",
            call_id="claude-question-delete",
        )
    )
    publish_entered = asyncio.Event()
    release_publish = asyncio.Event()
    original_publish = tailer._emit_approval_resolved

    async def blocked_publish(*args: Any, **kwargs: Any) -> None:
        publish_entered.set()
        await release_publish.wait()
        await original_publish(*args, **kwargs)

    monkeypatch.setattr(tailer, "_emit_approval_resolved", blocked_publish)
    answer_task = asyncio.create_task(
        stream_api.send_stream_input(
            session.id,
            stream_api.AgentStreamSendRequest(
                text=_answer_text(),
                client_turn_id="followup-delete-call",
                question_turn_id="question-turn-delete",
                question_call_id="claude-question-delete",
            ),
            User(open_id="user", name="User", email="user@example.com"),
        )
    )
    await asyncio.wait_for(publish_entered.wait(), timeout=2)
    delete_task = asyncio.create_task(_simulate_managed_delete(manager, session, tailer))
    await asyncio.sleep(0.02)
    assert not delete_task.done(), "delete must wait for follow-up card resolution"

    release_publish.set()
    assert await asyncio.wait_for(answer_task, timeout=2) == {"ok": True}
    await asyncio.wait_for(delete_task, timeout=2)
    assert session.id not in manager.sessions
    assert not tailer.store.path.exists()

    await _stop_tailers(tailers, transport)
    await tailer.store.clear()


@pytest.mark.asyncio
async def test_idle_question_does_not_enqueue_into_recreated_session(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, agent_type=AgentType.CLAUDE, tab_id="followup-reuse")
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = tailer.native_transport
    assert transport is not None
    send_text = AsyncMock()
    monkeypatch.setattr(transport, "_send_text", send_text)
    tailer._active_turn_id = "question-turn-reuse"
    tailer._record_approval_card(
        _approval_event(
            session,
            turn_id="question-turn-reuse",
            call_id="claude-question-reuse",
        )
    )
    send_entered = asyncio.Event()
    release_send = asyncio.Event()
    original_send = manager.send_chat_session_message

    async def blocked_send(*args: Any, **kwargs: Any) -> None:
        send_entered.set()
        await release_send.wait()
        await original_send(*args, **kwargs)

    monkeypatch.setattr(manager, "send_chat_session_message", blocked_send)
    answer_task = asyncio.create_task(
        stream_api.send_stream_input(
            session.id,
            stream_api.AgentStreamSendRequest(
                text=_answer_text(),
                client_turn_id="followup-reuse-call",
                question_turn_id="question-turn-reuse",
                question_call_id="claude-question-reuse",
            ),
            User(open_id="user", name="User", email="user@example.com"),
        )
    )
    await asyncio.wait_for(send_entered.wait(), timeout=2)
    replacement = session.model_copy(
        update={
            "created_at": session.created_at + timedelta(seconds=1),
            "updated_at": session.updated_at + timedelta(seconds=1),
        }
    )
    async with manager.workspace_mutation_lock(session.workspace_id):
        pump_lock = manager._pump_locks.setdefault(session.id, asyncio.Lock())
        async with pump_lock:
            manager.sessions[session.id] = replacement
    release_send.set()

    with pytest.raises(HTTPException) as stale:
        await asyncio.wait_for(answer_task, timeout=2)
    assert stale.value.status_code == 409
    send_text.assert_not_awaited()
    assert manager.sessions[session.id].created_at == replacement.created_at

    manager.sessions[session.id] = session
    await _stop_tailers(tailers, transport)
    await tailer.store.clear()


@pytest.mark.asyncio
async def test_direct_tab_switch_env_preserves_legacy_runtime_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_manager = SimpleNamespace(
        switch_env=AsyncMock(side_effect=RuntimeError("direct restart failed"))
    )
    monkeypatch.setattr(tabs_api, "ttyd_manager", direct_manager)
    monkeypatch.setattr(tabs_api, "_managed_chat_session_for_tab", lambda _tab_id: None)

    with pytest.raises(HTTPException) as raised:
        await tabs_api.switch_tab_env(
            "direct-tab",
            SwitchEnvRequest(env={}),
            User(open_id="user", name="User", email="user@example.com"),
        )

    assert raised.value.status_code == 400
    assert raised.value.detail == "direct restart failed"


@pytest.mark.asyncio
async def test_managed_codex_model_reasoning_update_reaches_real_transport(
    managed_workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    ttyd_module = importlib.import_module("claude_hub.services.ttyd_manager")

    monkeypatch.setattr(ttyd_module, "STATE_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(ttyd_module, "ORDER_FILE", tmp_path / "order.json")
    monkeypatch.setattr(ttyd_module, "LAUNCH_ENV_DIR", tmp_path / "launch-env")
    tabs = TTYDManager()
    old_env = {
        "CODEX_MODEL": "old-model",
        "CODEX_REASONING_EFFORT": "medium",
        "UNCHANGED": "yes",
    }
    session_id = "managed-codex-config"
    tab = await tabs.create_tab(
        name="Managed Codex",
        cwd=workspace.path,
        agent_type=AgentType.CODEX,
        session_kind=SessionKind.CHAT,
        workspace_id=workspace.id,
        workspace_role=WorkspaceSessionRole.ORCHESTRATOR,
        managed_session_id=session_id,
        env=old_env,
    )
    session = _managed_session(workspace, tab_id=tab.id, env=old_env)
    session.id = session_id
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = cast(CodexNativeSession, tailer.native_transport)
    assert transport is not None
    monkeypatch.setattr(tabs_api, "ttyd_manager", tabs)
    monkeypatch.setattr(tabs_api, "workspace_manager", manager)
    monkeypatch.setattr(tabs_api, "_get_tailer_manager", lambda: tailers)

    new_env = {
        "CODEX_MODEL": "new-model",
        "CODEX_REASONING_EFFORT": "high",
        "UNCHANGED": "yes",
    }
    updated_tab = await tabs_api.switch_tab_env(
        tab.id,
        SwitchEnvRequest(env=new_env),
        User(open_id="user", name="User", email="user@example.com"),
    )

    assert updated_tab.env == new_env
    assert manager.sessions[session_id].env == new_env
    assert transport.current_model() == "new-model"
    assert transport._selected_reasoning_effort() == "high"

    with pytest.raises(HTTPException) as unsupported:
        await tabs_api.switch_tab_env(
            tab.id,
            SwitchEnvRequest(env={**new_env, "HTTP_PROXY": "http://proxy"}),
            User(open_id="user", name="User", email="user@example.com"),
        )
    assert unsupported.value.status_code == 409
    assert manager.sessions[session_id].env == new_env
    await _stop_tailers(tailers)


@pytest.mark.asyncio
async def test_stale_managed_stop_conflicts_but_terminal_retry_is_idempotent(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="stop")
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    transport = tailer.native_transport
    assert transport is not None
    tailer._active_turn_id = "newer-turn"
    transport._turn_in_flight = True
    monkeypatch.setattr(stream_api, "_get_tailer_manager", lambda: tailers)

    with pytest.raises(HTTPException) as stale:
        await stream_api.cancel_stream_turn(
            session.id,
            stream_api.StreamCancelRequest(expected_turn_id="older-turn"),
            User(open_id="user", name="User", email="user@example.com"),
        )
    assert stale.value.status_code == 409
    assert manager.sessions[session.id].chat_paused is False
    assert transport.turn_in_flight

    transport._turn_in_flight = False
    tailer._active_turn_id = None
    terminal = await stream_api.cancel_stream_turn(
        session.id,
        stream_api.StreamCancelRequest(expected_turn_id="older-turn"),
        User(open_id="user", name="User", email="user@example.com"),
    )
    assert terminal == {"ok": True, "cancelled": False, "outcome": "already_terminal"}
    await _stop_tailers(tailers, transport)


@pytest.mark.asyncio
async def test_stale_stop_classifier_never_creates_or_reuses_an_owner(
    managed_workspace,
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="classifier-owner")
    manager.sessions[session.id] = session
    tailers = TailerManager(session_getter=lambda session_id: manager.sessions.get(session_id))

    assert await tailers.active_turn_id(session) is None
    assert tailers._tailers == {}

    manager.sessions[session.id] = session.model_copy(
        update={"created_at": session.created_at + timedelta(seconds=1)}
    )
    assert await tailers.active_turn_id(session) is None
    assert tailers._tailers == {}


@pytest.mark.asyncio
async def test_managed_history_and_wait_reject_friendly_id_replacement(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="history-owner")
    manager.sessions[session.id] = session
    tailers = TailerManager(session_getter=lambda session_id: manager.sessions.get(session_id))
    monkeypatch.setattr(stream_api, "workspace_manager", manager)
    monkeypatch.setattr(stream_api, "_tailer_manager", tailers)
    page = stream_api.AgentStreamEventPage(events=[], next_sequence=-1, has_more=False)
    user = User(open_id="user", name="User", email="user@example.com")

    def replace_incarnation() -> None:
        manager.sessions[session.id] = session.model_copy(
            update={"created_at": session.created_at + timedelta(seconds=1)}
        )

    async def replaced_history(*_args: Any, **_kwargs: Any) -> stream_api.AgentStreamEventPage:
        replace_incarnation()
        return page

    monkeypatch.setattr(stream_api, "_stream_events_for", replaced_history)
    with pytest.raises(HTTPException) as history:
        await stream_api.get_stream_events(session.id, -1, 200, False, user)
    assert history.value.status_code == 409

    manager.sessions[session.id] = session

    async def replaced_wait(*_args: Any, **_kwargs: Any) -> stream_api.AgentStreamEventPage:
        replace_incarnation()
        return page

    monkeypatch.setattr(stream_api, "_wait_stream_events_for", replaced_wait)
    with pytest.raises(HTTPException) as wait:
        await stream_api.wait_stream_events(
            session.id,
            stream_api.AgentStreamWaitRequest(since_sequence=-1, timeout_seconds=0),
            user,
        )
    assert wait.value.status_code == 409


@pytest.mark.asyncio
async def test_managed_capability_preparation_is_serialized_before_replacement(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="capability-owner")
    manager.sessions[session.id] = session
    tailers = TailerManager(session_getter=lambda session_id: manager.sessions.get(session_id))
    monkeypatch.setattr(stream_api, "workspace_manager", manager)
    monkeypatch.setattr(stream_api, "_tailer_manager", tailers)
    preparation_entered = asyncio.Event()
    release_preparation = asyncio.Event()

    async def blocked_capabilities(
        _session: ManagedSession, _manager: TailerManager
    ) -> stream_api.StreamCapabilities:
        preparation_entered.set()
        await release_preparation.wait()
        return stream_api.StreamCapabilities(structured=True)

    monkeypatch.setattr(stream_api, "_capabilities_for", blocked_capabilities)
    capability_task = asyncio.create_task(
        stream_api.get_stream_capabilities(
            session.id,
            User(open_id="user", name="User", email="user@example.com"),
        )
    )
    await asyncio.wait_for(preparation_entered.wait(), timeout=2)
    replacement = session.model_copy(
        update={"created_at": session.created_at + timedelta(seconds=1)}
    )

    async def replace() -> None:
        async with manager.workspace_mutation_lock(session.workspace_id):
            pump_lock = manager._pump_locks.setdefault(session.id, asyncio.Lock())
            async with pump_lock:
                manager.sessions[session.id] = replacement

    replacement_task = asyncio.create_task(replace())
    await asyncio.sleep(0.02)
    assert not replacement_task.done(), "replacement must wait for capability preparation"
    release_preparation.set()
    capabilities = await asyncio.wait_for(capability_task, timeout=2)
    assert capabilities.structured is True
    await asyncio.wait_for(replacement_task, timeout=2)
    assert manager.sessions[session.id].created_at == replacement.created_at


@pytest.mark.asyncio
async def test_managed_diagnostics_preparation_is_serialized_before_replacement(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="diagnostics-owner")
    manager.sessions[session.id] = session
    tailers = TailerManager(session_getter=lambda session_id: manager.sessions.get(session_id))
    monkeypatch.setattr(stream_api, "workspace_manager", manager)
    monkeypatch.setattr(stream_api, "_tailer_manager", tailers)
    preparation_entered = asyncio.Event()
    release_preparation = asyncio.Event()

    async def blocked_capabilities(
        _session: ManagedSession, _manager: TailerManager
    ) -> stream_api.StreamCapabilities:
        preparation_entered.set()
        await release_preparation.wait()
        return stream_api.StreamCapabilities(structured=True)

    monkeypatch.setattr(stream_api, "_capabilities_for", blocked_capabilities)
    diagnostics_task = asyncio.create_task(
        stream_api.get_stream_diagnostics(
            session.id,
            User(open_id="user", name="User", email="user@example.com"),
        )
    )
    await asyncio.wait_for(preparation_entered.wait(), timeout=2)
    replacement = session.model_copy(
        update={"created_at": session.created_at + timedelta(seconds=1)}
    )

    async def replace() -> None:
        async with manager.workspace_mutation_lock(session.workspace_id):
            pump_lock = manager._pump_locks.setdefault(session.id, asyncio.Lock())
            async with pump_lock:
                manager.sessions[session.id] = replacement

    replacement_task = asyncio.create_task(replace())
    await asyncio.sleep(0.02)
    assert not replacement_task.done(), "replacement must wait for diagnostics preparation"
    release_preparation.set()
    diagnostics = await asyncio.wait_for(diagnostics_task, timeout=2)
    assert diagnostics["structured"] is True
    await asyncio.wait_for(replacement_task, timeout=2)


@pytest.mark.asyncio
async def test_managed_sse_closes_before_serving_replaced_incarnation(
    managed_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, agent_type=AgentType.CLAUDE, tab_id="sse-owner")
    tailers, tailer = await _real_tailer_manager(manager, session, monkeypatch)
    response = await stream_api.stream_live(
        session.id,
        -1,
        User(open_id="user", name="User", email="user@example.com"),
    )
    manager.sessions[session.id] = session.model_copy(
        update={"created_at": session.created_at + timedelta(seconds=1)}
    )

    iterator = cast(
        AsyncGenerator[str | bytes | memoryview, None], response.body_iterator.__aiter__()
    )
    first = await asyncio.wait_for(anext(iterator), timeout=2)
    text = first.decode() if isinstance(first, bytes) else first
    assert "event: error" in text
    assert "session was deleted or replaced" in text
    assert "event: hello" not in text
    await iterator.aclose()

    manager.sessions[session.id] = session
    await _stop_tailers(tailers, tailer.native_transport)


@pytest.mark.asyncio
async def test_managed_agent_images_are_scoped_to_session_cwd_and_history(
    managed_workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    session = _managed_session(workspace, tab_id="image-owner")
    manager.sessions[session.id] = session
    monkeypatch.setattr(stream_api, "workspace_manager", manager)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    inside = Path(workspace.path) / "inside.png"
    inside.write_bytes(png)
    referenced = tmp_path.parent / f"{tmp_path.name}-referenced.png"
    referenced.write_bytes(png)
    unrelated = tmp_path.parent / f"{tmp_path.name}-unrelated.png"
    unrelated.write_bytes(png)
    await AgentStreamStore(session.workspace_id, session.id).append(
        AgentStreamEvent(
            stream_sequence=0,
            session_id=session.id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            type=AgentStreamEventType.TEXT_DELTA,
            turn_id="image-turn",
            message_id="image-turn:assistant",
            payload={"role": "assistant", "text": f"![preview]({referenced})"},
            created_at=datetime.now(timezone.utc),
        )
    )
    user = User(open_id="user", name="User", email="user@example.com")

    cwd_response = await stream_api.get_session_agent_image(session.id, "inside.png", user)
    history_response = await stream_api.get_session_agent_image(session.id, str(referenced), user)
    assert cwd_response.body == png
    assert history_response.body == png
    with pytest.raises(HTTPException) as denied:
        await stream_api.get_session_agent_image(session.id, str(unrelated), user)
    assert denied.value.status_code == 404


@pytest.fixture
async def isolated_tab_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[TTYDManager, None]:
    ttyd_module = importlib.import_module("claude_hub.services.ttyd_manager")
    monkeypatch.setattr(ttyd_module, "STATE_FILE", tmp_path / "guard-tabs.json")
    monkeypatch.setattr(ttyd_module, "ORDER_FILE", tmp_path / "guard-order.json")
    monkeypatch.setattr(ttyd_module, "LAUNCH_ENV_DIR", tmp_path / "guard-launch")
    tabs = TTYDManager()
    try:
        yield tabs
    finally:
        # Direct Chat still owns a ttyd process even when it only carries
        # Workspace display metadata. Reap every test-owned tab on failures too.
        results = await asyncio.gather(
            *(tabs.delete_tab(tab_id) for tab_id in tuple(tabs.processes)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
            assert result
        assert not tabs.processes


@pytest.mark.asyncio
async def test_managed_tab_rejects_direct_stream_and_destructive_tab_controls(
    managed_workspace, isolated_tab_manager: TTYDManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace = managed_workspace
    tabs = isolated_tab_manager
    tab = await tabs.create_tab(
        name="Managed",
        cwd=workspace.path,
        agent_type=AgentType.CODEX,
        session_kind=SessionKind.CHAT,
        workspace_id=workspace.id,
        workspace_role=WorkspaceSessionRole.ORCHESTRATOR,
        managed_session_id="managed-guard",
    )
    session = _managed_session(workspace, tab_id=tab.id)
    session.id = "managed-guard"
    manager.sessions[session.id] = session
    monkeypatch.setattr(stream_api, "ttyd_manager", tabs)
    monkeypatch.setattr(tabs_api, "ttyd_manager", tabs)
    monkeypatch.setattr(tabs_api, "workspace_manager", manager)

    with pytest.raises(HTTPException) as stream_guard:
        await stream_api.get_tab_stream_capabilities(
            tab.id, User(open_id="user", name="User", email="user@example.com")
        )
    assert stream_guard.value.status_code == 409

    with pytest.raises(HTTPException) as image_guard:
        await stream_api.get_tab_agent_image(
            tab.id,
            "inside.png",
            User(open_id="user", name="User", email="user@example.com"),
        )
    assert image_guard.value.status_code == 409

    with pytest.raises(HTTPException) as delete_guard:
        await tabs_api.delete_tab(
            tab.id, User(open_id="user", name="User", email="user@example.com")
        )
    assert delete_guard.value.status_code == 409
    assert tabs.get_tab(tab.id) is not None

    direct = await tabs.create_tab(
        name="Direct Chat with workspace metadata",
        cwd=workspace.path,
        agent_type=AgentType.CODEX,
        session_kind=SessionKind.CHAT,
        workspace_id=workspace.id,
    )
    direct_session = stream_api._terminal_tab_session_or_404(direct.id)
    assert direct_session.tab_id == direct.id
    assert tabs_api._managed_chat_session_for_tab(direct.id) is None
