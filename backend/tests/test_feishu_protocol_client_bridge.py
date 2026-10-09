"""Feishu outbound client and Chat source bridge tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from claude_hub.api import agent_stream as stream_api
from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType
from claude_hub.services import goal_run
from claude_hub.services.agent_stream import ExternalTurnRetirement
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V1,
    format_feishu_provider_text_v1,
)
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuBotError,
    feishu_message_time_is_valid,
)

_NOW = 2_000_000_000


@pytest.fixture(autouse=True)
def isolated_runtime_and_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(runtime))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(state))
    monkeypatch.setenv("CLAUDE_HUB_TMUX_SOCKET", "test-feishu-protocol")

    def reject_real_transport(*_args, **_kwargs):
        pytest.fail("real outbound HTTP transport is forbidden")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_real_transport)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_real_transport)


@pytest.fixture
def bot_config() -> FeishuBotConfig:
    return FeishuBotConfig(
        app_id="cli-bot",
        app_secret="app-secret",
    )


@pytest.mark.asyncio
async def test_client_uses_only_injected_http_transport(bot_config: FeishuBotConfig) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om-reply"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://open.feishu.test",
    ) as http_client:
        client = FeishuBotClient(bot_config, client=http_client)
        await client.send_text("oc-chat", "assistant reply")
        await client.reply_text("om-inbound", "exact reply")

    assert [request.url.path for request in requests] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/im/v1/messages",
        "/open-apis/im/v1/messages/om-inbound/reply",
    ]
    assert requests[1].headers["authorization"] == "Bearer tenant-token"
    sent_body = json.loads(requests[1].content)
    assert sent_body["receive_id"] == "oc-chat"
    assert json.loads(sent_body["content"]) == {"text": "assistant reply"}
    reply_body = json.loads(requests[2].content)
    assert "receive_id" not in reply_body
    assert reply_body["msg_type"] == "text"
    assert json.loads(reply_body["content"]) == {"text": "exact reply"}


@pytest.mark.parametrize("bad_token", ["", "   "])
@pytest.mark.asyncio
async def test_client_rejects_empty_tenant_token(
    bot_config: FeishuBotConfig,
    bad_token: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "tenant_access_token": bad_token, "expire": 7200},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://open.feishu.test",
    ) as http_client:
        client = FeishuBotClient(bot_config, client=http_client)
        with pytest.raises(FeishuBotError, match="tenant token request"):
            await client.validate_credentials()


@pytest.mark.asyncio
async def test_explicit_reply_token_never_fetches_tenant_token(
    bot_config: FeishuBotConfig,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert not request.url.path.endswith("/tenant_access_token/internal")
        return httpx.Response(200, json={"code": 0})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://open.feishu.test",
    ) as http_client:
        client = FeishuBotClient(bot_config, client=http_client)
        with pytest.raises(FeishuBotError, match="tenant token is empty"):
            await client.reply_text("om-empty", "answer", access_token="")
        await client.reply_text("om-valid", "answer", access_token="explicit-token")

    assert paths == ["/open-apis/im/v1/messages/om-valid/reply"]


@pytest.mark.asyncio
async def test_external_chat_bridge_preserves_feishu_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    unsubscribed: list[tuple[str, object]] = []

    class FakeManager:
        async def subscribe(self, session_arg):
            assert session_arg is session
            return queue

        def unsubscribe(self, session_id, queue_arg) -> None:
            unsubscribed.append((session_id, queue_arg))

    dispatched: list[tuple[str, str, str, dict[str, object]]] = []

    async def fake_dispatch(
        tab_id: str, payload: stream_api.AgentStreamSendRequest, **kwargs: object
    ) -> str:
        dispatched.append((tab_id, payload.text, payload.client_turn_id, kwargs))
        await queue.put(
            AgentStreamEvent(
                stream_sequence=4,
                session_id=session.id,
                tab_id=tab_id,
                agent_type=AgentType.CLAUDE,
                type=AgentStreamEventType.TURN_COMPLETED,
                turn_id=payload.client_turn_id,
                payload={
                    "status": "completed",
                    "assistant_text": "existing session reply",
                },
                created_at=datetime.now(timezone.utc),
            )
        )
        return payload.client_turn_id

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    manager = FakeManager()
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", fake_dispatch)

    metadata = {
        "origin": "feishu",
        "provider_text_format": FEISHU_PROVIDER_TEXT_FORMAT_V1,
        "feishu": {
            "app_id": "cli-bot",
            "chat_id": "oc-chat",
            "message_id": "om-1",
            "sender_open_id": "ou-owner",
        },
    }
    provider_text = format_feishu_provider_text_v1(
        "question",
        app_id="cli-bot",
        chat_id="oc-chat",
        message_id="om-1",
        sender_open_id="ou-owner",
    )
    result = await stream_api.dispatch_tab_chat_and_wait(
        "tab-1",
        provider_text,
        "feishu-turn",
        visible_text="question",
        turn_metadata=metadata,
        timeout_seconds=1,
    )

    assert result == "existing session reply"
    assert dispatched == [
        (
            "tab-1",
            provider_text,
            "feishu-turn",
            {
                "visible_text": "question",
                "turn_metadata": metadata,
                "allow_question_answer": False,
            },
        )
    ]
    assert unsubscribed == [(session.id, queue)]


@pytest.mark.asyncio
async def test_external_goal_input_does_not_consume_pending_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = SimpleNamespace(
        status=SimpleNamespace(value="active"),
        dispatch_state=SimpleNamespace(value="idle"),
        current_turn_id="goal-turn",
    )
    manager = SimpleNamespace(
        answer_pending_question=AsyncMock(return_value=True),
        accepts_question_followup=AsyncMock(return_value=True),
    )
    send = AsyncMock()
    monkeypatch.setattr(goal_run, "get_goal_admission_lock", lambda _tab_id: asyncio.Lock())
    monkeypatch.setattr(
        goal_run,
        "get_goal_manager",
        lambda: SimpleNamespace(current=lambda _tab_id: current),
    )
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: object())
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_send_to_native", send)

    with pytest.raises(HTTPException) as raised:
        await stream_api._dispatch_tab_stream_input(
            "tab-1",
            stream_api.AgentStreamSendRequest(text="answer", client_turn_id="external-turn"),
            allow_question_answer=False,
        )

    assert raised.value.status_code == 409
    assert raised.value.headers == {stream_api.CHAT_ERROR_REASON_HEADER: "chat_busy"}
    manager.answer_pending_question.assert_not_awaited()
    manager.accepts_question_followup.assert_not_awaited()
    send.assert_not_awaited()


def test_inflight_send_error_has_stable_busy_reason() -> None:
    mapped = stream_api._map_send_exception(
        RuntimeError("a turn is already in flight; wait for completion")
    )
    assert mapped.status_code == 409
    assert mapped.detail == "a turn is already in flight; wait for completion"
    assert mapped.headers == {stream_api.CHAT_ERROR_REASON_HEADER: "chat_busy"}


@pytest.mark.asyncio
async def test_external_chat_bridge_classifies_structured_source_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")

    class FailedManager:
        async def subscribe(self, session_arg):
            assert session_arg is session
            raise stream_api.StructuredSourceUnavailable("native source unavailable")

        def unsubscribe(self, _session_id, _queue_arg) -> None:
            raise AssertionError("a failed subscription must not be unsubscribed")

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    manager = FailedManager()
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)

    with pytest.raises(HTTPException) as raised:
        await stream_api.dispatch_tab_chat_and_wait(
            "tab-1",
            "provider text",
            "feishu-turn",
            timeout_seconds=1,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "native source unavailable"
    assert raised.value.headers == {
        stream_api.CHAT_ERROR_REASON_HEADER: "structured_source_unavailable"
    }


@pytest.mark.asyncio
async def test_external_chat_bridge_preserves_unknown_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    unsubscribed: list[tuple[str, object]] = []

    class FakeManager:
        async def subscribe(self, _session_arg):
            return queue

        def unsubscribe(self, session_id, queue_arg) -> None:
            unsubscribed.append((session_id, queue_arg))

    async def conflict(_tab_id, _payload, **_kwargs) -> str:
        raise HTTPException(status_code=409, detail="unclassified Chat conflict")

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    manager = FakeManager()
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", conflict)

    with pytest.raises(HTTPException) as raised:
        await stream_api.dispatch_tab_chat_and_wait(
            "tab-1",
            "provider text",
            "feishu-turn",
            timeout_seconds=1,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "unclassified Chat conflict"
    assert not raised.value.headers
    assert unsubscribed == [(session.id, queue)]


@pytest.mark.asyncio
async def test_external_chat_bridge_cancellation_stops_the_matching_native_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    dispatched = asyncio.Event()
    cancelled: list[tuple[object, str | None]] = []
    unsubscribed: list[tuple[str, object]] = []

    class FakeManager:
        async def subscribe(self, session_arg):
            assert session_arg is session
            return queue

        async def retire_external_turn(self, session_arg, expected_turn_id=None):
            cancelled.append((session_arg, expected_turn_id))
            return ExternalTurnRetirement.CANCELLED_MATCHING

        def unsubscribe(self, session_id, queue_arg) -> None:
            unsubscribed.append((session_id, queue_arg))

    async def fake_dispatch(
        _tab_id: str, payload: stream_api.AgentStreamSendRequest, **_kwargs: object
    ) -> str:
        dispatched.set()
        return payload.client_turn_id

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    manager = FakeManager()
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", fake_dispatch)

    task = asyncio.create_task(
        stream_api.dispatch_tab_chat_and_wait(
            "tab-1", "provider text", "feishu-turn", timeout_seconds=60
        )
    )
    await dispatched.wait()
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(stream_api.ExternalDispatchRetired):
        await task
    assert cancelled == [(session, "feishu-turn")]
    assert unsubscribed == [(session.id, queue)]


@pytest.mark.asyncio
async def test_external_chat_bridge_cancellation_releases_when_turn_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    dispatched = asyncio.Event()

    class FakeManager:
        async def subscribe(self, _session_arg):
            return queue

        async def retire_external_turn(self, _session_arg, expected_turn_id=None):
            assert expected_turn_id == "feishu-turn"
            return ExternalTurnRetirement.NOT_STARTED

        def unsubscribe(self, _session_id, _queue_arg) -> None:
            pass

    async def fake_dispatch(
        _tab_id: str, payload: stream_api.AgentStreamSendRequest, **_kwargs: object
    ) -> str:
        dispatched.set()
        return payload.client_turn_id

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: FakeManager())
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", fake_dispatch)

    task = asyncio.create_task(
        stream_api.dispatch_tab_chat_and_wait(
            "tab-1", "provider text", "feishu-turn", timeout_seconds=60
        )
    )
    await dispatched.wait()
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(stream_api.ExternalDispatchRetired):
        await task


@pytest.mark.parametrize(
    "retirement",
    [
        ExternalTurnRetirement.ALREADY_TERMINAL,
        ExternalTurnRetirement.DIFFERENT_TURN,
        ExternalTurnRetirement.UNKNOWN,
    ],
)
@pytest.mark.asyncio
async def test_external_chat_bridge_cancellation_preserves_non_retryable_turns(
    monkeypatch: pytest.MonkeyPatch, retirement: ExternalTurnRetirement
) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    dispatched = asyncio.Event()

    class FakeManager:
        async def subscribe(self, _session_arg):
            return queue

        async def retire_external_turn(self, _session_arg, expected_turn_id=None):
            assert expected_turn_id == "feishu-turn"
            return retirement

        def unsubscribe(self, _session_id, _queue_arg) -> None:
            pass

    async def fake_dispatch(
        _tab_id: str, payload: stream_api.AgentStreamSendRequest, **_kwargs: object
    ) -> str:
        dispatched.set()
        return payload.client_turn_id

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: FakeManager())
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", fake_dispatch)

    task = asyncio.create_task(
        stream_api.dispatch_tab_chat_and_wait(
            "tab-1", "provider text", "feishu-turn", timeout_seconds=60
        )
    )
    await dispatched.wait()
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert not isinstance(raised.value, stream_api.ExternalDispatchRetired)


@pytest.mark.asyncio
async def test_external_chat_bridge_cancellation_times_out_a_stalled_native_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    dispatched = asyncio.Event()

    class FakeManager:
        async def subscribe(self, _session_arg):
            return queue

        async def retire_external_turn(self, _session_arg, expected_turn_id=None):
            assert expected_turn_id == "feishu-turn"
            await asyncio.Future()

        def unsubscribe(self, _session_id, _queue_arg) -> None:
            pass

    async def fake_dispatch(
        _tab_id: str, payload: stream_api.AgentStreamSendRequest, **_kwargs: object
    ) -> str:
        dispatched.set()
        return payload.client_turn_id

    monkeypatch.setattr(stream_api, "_EXTERNAL_CANCEL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: FakeManager())
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", fake_dispatch)

    task = asyncio.create_task(
        stream_api.dispatch_tab_chat_and_wait(
            "tab-1", "provider text", "feishu-turn", timeout_seconds=60
        )
    )
    await dispatched.wait()
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)


def test_activation_boundary_does_not_round_down_or_add_grace():
    assert feishu_message_time_is_valid(1125, activated_at=1.125, now=1.125)
    assert not feishu_message_time_is_valid(1125, activated_at=1.1259765625, now=2.0)
    assert feishu_message_time_is_valid(1126, activated_at=1.1259765625, now=2.0)
    assert not feishu_message_time_is_valid(1126, activated_at=1.0, now=1.125)
    assert not feishu_message_time_is_valid(1000, activated_at=2.0, now=1.0)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(True, id="bool"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(10**400, id="overflow"),
        pytest.param(-1.0, id="negative"),
    ],
)
def test_invalid_clock_bound_is_not_admissible(bad):
    assert not feishu_message_time_is_valid(1000, activated_at=bad, now=2.0)
    assert not feishu_message_time_is_valid(1000, activated_at=0.0, now=bad)
