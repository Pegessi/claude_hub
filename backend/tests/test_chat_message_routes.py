"""Isolated ASGI admission and native adapter contracts; no real providers."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI, HTTPException

from claude_hub.api import agent_stream as stream
from claude_hub.api.routes.agent_stream import messages
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    SessionKind,
    User,
)
from claude_hub.models.chat_message import ChatMessageCreate
from claude_hub.services import goal_run, ttyd_manager
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.chat_message_inbox import (
    ChatMessageInbox,
    InboxError,
    InboxEvidence,
    InboxTarget,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    async def submit(item, before):
        before()

    async def evidence(item):
        return InboxEvidence("absent")

    inbox = ChatMessageInbox(
        tmp_path / "inbox.json", lambda tab: InboxTarget("inc"), submit, evidence
    )
    monkeypatch.setattr(messages, "_inbox", inbox)
    app = FastAPI()
    router = APIRouter(prefix="/api/workspaces")
    messages.register_message_routes(router)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="alice", name="Alice", email="a@example.test"
    )
    return app, inbox


async def test_receipt_enqueue_status_cancel_and_privacy(setup):
    app, inbox = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/api/workspaces/tabs/t/messages"
        body = {"message": "hello", "call_id": "same", "source_tab_id": "unverified"}
        response = await client.post(url, json=body)
        assert response.status_code == 202
        receipt = response.json()
        assert receipt["state"] == "queued"
        assert "message" not in receipt and "principal_email" not in receipt
        status = receipt["status_url"]
        assert (await client.get(status)).json() == receipt
        assert (await client.post(url, json=body)).json() == receipt
        assert (await client.post(url, json={**body, "message": "changed"})).status_code == 409
        assert len((await client.get(url)).json()["messages"]) == 1
        assert (await client.delete(status)).json()["state"] == "cancelled"
        app.dependency_overrides[get_current_user] = lambda: User(
            open_id="bob", name="Bob", email="b@example.test"
        )
        assert (await client.get(status)).status_code == 404
        assert (await client.get(url)).json() == {"messages": []}


@pytest.mark.parametrize(
    "extra", ["origin", "delivery", "turn_metadata", "question_call_id", "interval"]
)
async def test_privileged_input_rejected(setup, extra):
    app, _ = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/workspaces/tabs/t/messages",
            json={"message": "x", "call_id": "id", extra: "spoof"},
        )
        assert response.status_code == 422


async def test_auth_dependency_is_required(setup):
    app, _ = setup

    def denied():
        raise HTTPException(401, "denied")

    app.dependency_overrides[get_current_user] = denied
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/api/workspaces/tabs/t/messages",
                json={"message": "x", "call_id": "id"},
            )
        ).status_code == 401
        assert (await client.get("/api/workspaces/tabs/t/messages")).status_code == 401
        assert (await client.delete("/api/workspaces/tabs/t/messages/id")).status_code == 401


async def test_capacity_archived_and_accepted_cancel(setup):
    app, inbox = setup
    inbox.max_pending = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/api/workspaces/tabs/t/messages"
        receipt = (await client.post(url, json={"message": "x", "call_id": "id"})).json()
        assert (await client.post(url, json={"message": "y", "call_id": "new"})).status_code == 429
        await inbox.pump("t")
        assert (await client.delete(receipt["status_url"])).status_code == 409
        inbox.target = lambda tab: InboxTarget("inc", waiting_reason="archived")
        assert (
            await client.post(url, json={"message": "z", "call_id": "archived"})
        ).status_code == 409


@pytest.mark.parametrize(
    "kind,native,managed,archived,expected",
    [
        (SessionKind.CHAT, True, False, False, None),
        (SessionKind.TERMINAL, False, False, False, 409),
        (SessionKind.CHAT, False, False, False, 409),
        (SessionKind.CHAT, True, True, False, 409),
        (SessionKind.CHAT, True, False, True, "archived"),
    ],
)
def test_actual_target_eligibility(monkeypatch, kind, native, managed, archived, expected):
    tab = SimpleNamespace(
        created_at=datetime.now(timezone.utc),
        agent_type=AgentType.CODEX,
        session_kind=kind,
        target=SimpleNamespace(value="local"),
        cwd="/tmp",
        env={},
        chat_mode=SimpleNamespace(value="default"),
        cursor_transport="acp",
        agent_session_id=None,
        agent_session_id_verified=False,
        archived=archived,
    )

    def resolve(tab_id):
        if managed:
            raise HTTPException(409, "managed session stream required")
        return SimpleNamespace(session_kind=kind)

    monkeypatch.setattr(stream, "_terminal_tab_session_or_404", resolve)
    monkeypatch.setattr(stream, "_is_chat_native", lambda session: native)
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tab_id: tab)
    from claude_hub.api.routes.agent_stream import dispatch

    monkeypatch.setattr(dispatch, "require_tab_input_available", lambda tab_id: None)
    if isinstance(expected, int):
        with pytest.raises(InboxError) as exc:
            messages._target("t")
        assert exc.value.status == expected
    else:
        assert messages._target("t").waiting_reason == expected


def test_missing_and_recovering_target(monkeypatch):
    def missing(tab_id):
        raise HTTPException(404, "missing")

    monkeypatch.setattr(stream, "_terminal_tab_session_or_404", missing)
    with pytest.raises(InboxError) as exc:
        messages._target("gone")
    assert exc.value.status == 404


@pytest.mark.parametrize("goal_status", ["active", "blocked"])
async def test_goal_holds_without_steering_or_answering(setup, monkeypatch, goal_status):
    _, inbox = setup
    row = await inbox.enqueue(
        "t",
        "local",
        "local CLI",
        ChatMessageCreate(message='{"answers":{}}', call_id="q"),
    )
    item = inbox.messages[row.message_id]
    goal = SimpleNamespace(
        status=SimpleNamespace(value=goal_status),
        dispatch_state=SimpleNamespace(value="idle"),
    )
    monkeypatch.setattr(
        goal_run, "get_goal_manager", lambda: SimpleNamespace(current=lambda tab: goal)
    )
    send = AsyncMock()
    monkeypatch.setattr(
        stream, "_get_tab_tailer_manager", lambda: SimpleNamespace(send_message=send)
    )
    with pytest.raises(InboxError) as exc:
        await messages._submit(item, lambda: None)
    assert exc.value.status == 409
    send.assert_not_awaited()


async def test_native_adapter_disables_approval_interception_and_records_metadata(
    setup, monkeypatch
):
    _, inbox = setup
    row = await inbox.enqueue(
        "t",
        "local",
        "local CLI",
        ChatMessageCreate(message="hello", call_id="q", source_tab_id="hint"),
    )
    item = inbox.messages[row.message_id]
    monkeypatch.setattr(
        goal_run,
        "get_goal_manager",
        lambda: SimpleNamespace(current=lambda tab: None, has_goal_history=lambda tab: False),
    )
    monkeypatch.setattr(messages, "_target", lambda tab: InboxTarget("inc"))
    manager = SimpleNamespace(
        turn_in_flight=AsyncMock(return_value=False),
        ensure_started=AsyncMock(return_value=SimpleNamespace(native_ready=True)),
        send_message=AsyncMock(),
    )
    monkeypatch.setattr(stream, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream, "_terminal_tab_session_or_404", lambda tab: SimpleNamespace())
    monkeypatch.setattr(messages, "ensure_chat_runtime", AsyncMock(return_value=True))
    await messages._submit(item, lambda: None)
    kwargs = manager.send_message.await_args.kwargs
    assert kwargs["allow_question_answer"] is False
    assert kwargs["turn_metadata"] == {
        "origin": "hub_message",
        "hub_message": {
            "message_id": row.message_id,
            "actor": "local CLI",
            "source_tab_id": "hint",
        },
    }
    assert "delivery" not in kwargs
    await kwargs["before_send"]()


async def test_revoked_external_sender_rejected_before_runtime(setup, monkeypatch):
    _, inbox = setup
    row = await inbox.enqueue(
        "t",
        "alice",
        "Alice",
        ChatMessageCreate(message="hello", call_id="q"),
        email="a@example.test",
    )
    from claude_hub.config import settings

    monkeypatch.setattr(settings, "feishu_app_id", "fake-app")
    monkeypatch.setattr(settings, "feishu_app_secret", "fake-secret")
    monkeypatch.setattr(settings, "auth_allowed_open_ids", "bob")
    with pytest.raises(InboxError) as exc:
        messages._require_actor(inbox.messages[row.message_id])
    assert exc.value.status == 403


async def test_evidence_synthetic_rejection_not_provider_completion(setup, monkeypatch, tmp_path):
    _, inbox = setup
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(tmp_path / "streams"))
    row = await inbox.enqueue(
        "t", "local", "local CLI", ChatMessageCreate(message="hello", call_id="q")
    )
    store = AgentStreamStore("terminal-tabs", "terminal-tab-t")
    for kind, payload in [
        (AgentStreamEventType.TURN_STARTED, {"summary": "hello"}),
        (
            AgentStreamEventType.TURN_COMPLETED,
            {"status": "failed", "delivery_rejected": True},
        ),
    ]:
        await store.append(
            AgentStreamEvent(
                stream_sequence=0,
                created_at=datetime.now(timezone.utc),
                session_id="terminal-tab-t",
                tab_id="t",
                agent_type=AgentType.CODEX,
                type=kind,
                turn_id=row.turn_id,
                run_epoch=1,
                payload=payload,
            )
        )
    assert (await messages._evidence(inbox.messages[row.message_id])).state == "started"
    await store.append(
        AgentStreamEvent(
            stream_sequence=0,
            created_at=datetime.now(timezone.utc),
            session_id="terminal-tab-t",
            tab_id="t",
            agent_type=AgentType.CODEX,
            type=AgentStreamEventType.TURN_COMPLETED,
            turn_id=row.turn_id,
            run_epoch=1,
            payload={"status": "completed"},
        )
    )
    assert (await messages._evidence(inbox.messages[row.message_id])).state == "completed"


async def test_synthetic_rejection_observer_cannot_release_fifo(setup):
    _, inbox = setup
    row = await inbox.enqueue(
        "t", "local", "local CLI", ChatMessageCreate(message="hello", call_id="q")
    )
    inbox._change(row.message_id, "dispatching")
    await messages.observe_completion(
        AgentStreamEvent(
            stream_sequence=0,
            created_at=datetime.now(timezone.utc),
            session_id="terminal-tab-t",
            tab_id="t",
            agent_type=AgentType.CODEX,
            type=AgentStreamEventType.TURN_COMPLETED,
            turn_id=row.turn_id,
            run_epoch=1,
            payload={"status": "failed", "delivery_rejected": True},
        )
    )
    assert (await inbox.get("t", row.message_id, "local")).state == "dispatching"


@pytest.mark.parametrize(
    "agent_type", [AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR, AgentType.MASO]
)
async def test_native_inbox_json_cannot_answer_an_active_approval(
    tmp_path, monkeypatch, agent_type
):
    import json

    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
    from claude_hub.services.agent_stream.tailer import SessionTailer
    from tests.test_agent_stream import (
        _as_provider_session,
        _FakeNativeTransport,
        _isolate_state_root,
        _native_session,
    )

    _isolate_state_root(monkeypatch, tmp_path)
    transport = _FakeNativeTransport()
    transport._turn_in_flight = True
    transport.answer_pending_question = AsyncMock(return_value=True)
    session = _native_session(agent_type)
    tailer = SessionTailer(
        workspace_id="ws-inbox-answer",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=_as_provider_session(transport),
    )
    answer = json.dumps(
        {"type": "ask_question_response", "answers": [{"questionId": "q", "selected": ["red"]}]}
    )
    with pytest.raises(RuntimeError, match="turn is already in flight"):
        await tailer.send_message(answer, [], "inbox-json", allow_question_answer=False)
    transport.answer_pending_question.assert_not_awaited()
    assert transport.sent_messages == []


async def test_native_inbox_persists_origin_and_submission_marker(tmp_path, monkeypatch):
    from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
    from claude_hub.services.agent_stream.tailer import SessionTailer
    from tests.test_agent_stream import (
        _as_provider_session,
        _FakeNativeTransport,
        _isolate_state_root,
        _native_session,
    )

    _isolate_state_root(monkeypatch, tmp_path)
    transport = _FakeNativeTransport()
    session = _native_session()
    tailer = SessionTailer(
        workspace_id="ws-inbox",
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=_as_provider_session(transport),
    )
    marker = []
    await tailer.send_message(
        "review",
        [],
        "hub-message-test",
        allow_question_answer=False,
        turn_metadata={"origin": "hub_message"},
        before_delivery=lambda: marker.append("possible"),
    )
    page = await tailer.store.read_since(-1, limit=10)
    assert page.events[0].payload["metadata"]["origin"] == "hub_message"
    assert marker == ["possible"]
    assert transport.sent_messages == [("review", [])]
    await tailer.stop()


async def test_inbox_observer_failure_cannot_block_other_completion_owners(monkeypatch):
    from claude_hub.services import workspace_manager

    observer = AsyncMock(side_effect=OSError("inbox disk unavailable"))
    goal = AsyncMock()
    scheduled = AsyncMock()
    monkeypatch.setattr(messages, "observe_completion", observer)
    monkeypatch.setattr(
        goal_run, "get_goal_manager", lambda: SimpleNamespace(on_turn_completed=goal)
    )
    monkeypatch.setattr(workspace_manager, "on_scheduled_chat_turn_completed", scheduled)
    event = AgentStreamEvent(
        stream_sequence=0,
        created_at=datetime.now(timezone.utc),
        session_id="terminal-tab-t",
        tab_id="t",
        agent_type=AgentType.CODEX,
        type=AgentStreamEventType.TURN_COMPLETED,
        turn_id="other-owner",
        run_epoch=1,
        payload={"status": "completed"},
    )
    await stream._notify_goal_turn_completed(event)
    goal.assert_awaited_once()
    scheduled.assert_awaited_once()


async def test_corrupt_inbox_startup_preserves_other_service_cleanup(monkeypatch):
    from contextlib import asynccontextmanager

    from claude_hub import main
    from claude_hub.api import feishu_bot
    from claude_hub.services import dot, restart_continuation

    @asynccontextmanager
    async def ownership():
        yield

    monkeypatch.setattr(main, "_backend_runtime_ownership", ownership)
    monkeypatch.setattr(main.ttyd_manager, "schedule_pending_stream_cleanup", lambda: None)
    monkeypatch.setattr(main.ttyd_manager, "start_all_tabs", AsyncMock())
    monkeypatch.setattr(main.ttyd_manager, "cleanup", AsyncMock())
    monkeypatch.setattr(main.workspace_manager, "start_background_monitor", lambda: None)
    monkeypatch.setattr(main.workspace_manager, "stop_background_monitor", AsyncMock())
    monkeypatch.setattr(stream, "_reset_tailer_manager", lambda: None)
    monkeypatch.setattr(stream, "_quiesce_tailer_managers", lambda: None)
    monkeypatch.setattr(stream, "_stop_all_tailer_managers", AsyncMock())
    monkeypatch.setattr(
        messages, "start_inbox", AsyncMock(side_effect=ValueError("corrupt evidence"))
    )
    stop = AsyncMock()
    monkeypatch.setattr(messages, "stop_inbox", stop)
    for name in ["start_feishu_websockets", "quiesce_feishu_websockets", "stop_feishu_websockets"]:
        monkeypatch.setattr(feishu_bot, name, AsyncMock())
    watcher = SimpleNamespace(start=lambda: None, stop=AsyncMock())
    monkeypatch.setattr(dot, "get_dot_service", lambda: watcher)
    monkeypatch.setattr(restart_continuation, "continue_interrupted_chats", AsyncMock())
    monkeypatch.setattr(restart_continuation, "capture_interrupted_chats", AsyncMock())
    # No provider/process startup helpers should run outside the owned mocks.
    async with main.lifespan(main.app):
        assert main.ttyd_manager.start_all_tabs.await_count == 1
    stop.assert_awaited_once()
    main.ttyd_manager.cleanup.assert_awaited_once()
    main.workspace_manager.stop_background_monitor.assert_awaited_once()


async def test_both_entry_modes_use_one_native_adapter_and_submission_fence(setup, monkeypatch):
    _, inbox = setup
    monkeypatch.setattr(
        goal_run,
        "get_goal_manager",
        lambda: SimpleNamespace(current=lambda tab: None, has_goal_history=lambda tab: False),
    )
    monkeypatch.setattr(messages, "_target", lambda tab: InboxTarget("inc"))
    monkeypatch.setattr(messages, "ensure_chat_runtime", AsyncMock(return_value=True))
    monkeypatch.setattr(stream, "_terminal_tab_session_or_404", lambda tab: SimpleNamespace())
    sent = []

    async def send(session, text, images, turn_id, **kwargs):
        await kwargs["before_send"]()
        kwargs["before_delivery"]()
        assert kwargs["allow_question_answer"] is False
        sent.append((turn_id, kwargs["turn_metadata"]))

    manager = SimpleNamespace(turn_in_flight=AsyncMock(return_value=False), send_message=send)
    monkeypatch.setattr(stream, "_get_tab_tailer_manager", lambda: manager)
    inbox.submit = messages._submit
    first = await inbox.enqueue(
        "t", "local", "CLI", ChatMessageCreate(message="first", call_id="a")
    )
    # Only the internal timer policy can create this trusted envelope.
    scheduled = inbox.messages[first.message_id].model_copy(
        deep=True,
        update={
            "message_id": "occurrence",
            "call_id": "occurrence",
            "turn_id": "scheduled-occurrence",
            "scheduled_run_id": "occurrence",
            "scheduled_task_id": "timer",
            "principal": "scheduler",
            "turn_metadata": {
                "origin": "scheduled_task",
                "scheduled_task_id": "timer",
                "yields_to_user": True,
            },
        },
    )
    inbox.admit_scheduled(scheduled)
    last = await inbox.enqueue("t", "local", "CLI", ChatMessageCreate(message="last", call_id="b"))
    await inbox.pump("t")
    await inbox.completed("t", first.turn_id, "completed")
    await inbox.pump("t")
    await inbox.completed("t", scheduled.turn_id, "completed")
    await inbox.pump("t")
    assert [turn for turn, _ in sent] == [first.turn_id, scheduled.turn_id, last.turn_id]
    assert [metadata["origin"] for _, metadata in sent] == [
        "hub_message",
        "scheduled_task",
        "hub_message",
    ]
    assert sent[1][1]["yields_to_user"] is True
    assert all(item.submission_started for item in inbox.messages.values())
