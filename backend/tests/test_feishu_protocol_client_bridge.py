"""Feishu callback protocol, outbound client, and Chat source bridge tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import HTTPException

from claude_hub.api import agent_stream as stream_api
from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType
from claude_hub.services import goal_run
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V1,
    format_feishu_provider_text_v1,
)
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuBotError,
    FeishuEventPayloadError,
    FeishuEventVerificationError,
    FeishuMessageEvent,
    feishu_message_time_is_valid,
    parse_feishu_callback,
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
        verification_token="verify-token",
        encrypt_key="encrypt-key",
    )


def _sign(body: bytes, timestamp: int, nonce: str, encrypt_key: str) -> str:
    signed = str(timestamp).encode() + nonce.encode() + encrypt_key.encode() + body
    return hashlib.sha256(signed).hexdigest()


def _callback_headers(
    body: bytes,
    now: int,
    *,
    encrypt_key: str = "encrypt-key",
) -> dict[str, str]:
    nonce = "nonce-1"
    return {
        "content-type": "application/json",
        "x-lark-request-timestamp": str(now),
        "x-lark-request-nonce": nonce,
        "x-lark-signature": _sign(body, now, nonce, encrypt_key),
    }


def _encrypted_callback(
    payload: Mapping[str, object],
    request_time: int,
    *,
    encrypt_key: str = "encrypt-key",
    iv: bytes = b"0123456789abcdef",
) -> tuple[bytes, dict[str, str]]:
    plaintext = json.dumps(payload).encode()
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    key = hashlib.sha256(encrypt_key.encode()).digest()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = iv + encryptor.update(padded) + encryptor.finalize()
    body = json.dumps({"encrypt": base64.b64encode(ciphertext).decode()}).encode()
    return body, _callback_headers(body, request_time, encrypt_key=encrypt_key)


def _invalid_padding_callback(now: int) -> tuple[bytes, dict[str, str]]:
    iv = b"0123456789abcdef"
    key = hashlib.sha256(b"encrypt-key").digest()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = iv + encryptor.update(b"\x00" * 16) + encryptor.finalize()
    body = json.dumps({"encrypt": base64.b64encode(ciphertext).decode()}).encode()
    return body, _callback_headers(body, now)


def _message_payload(
    *,
    created_at: int,
    text: str,
    message_id: str = "om-1",
    sender_open_id: str = "ou-owner",
    chat_id: str = "oc-chat",
    app_id: str = "cli-bot",
) -> dict[str, object]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt-{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": str(created_at * 1000),
            "token": "verify-token",
            "app_id": app_id,
            "tenant_key": "tenant-1",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": sender_open_id},
                "sender_type": "user",
                "tenant_key": "tenant-1",
            },
            "message": {
                "message_id": message_id,
                "create_time": str(created_at * 1000),
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_encrypted_callback_signature_token_and_time_windows(
    bot_config: FeishuBotConfig,
) -> None:
    challenge = {
        "challenge": "challenge-value",
        "token": "verify-token",
        "type": "url_verification",
    }
    body, headers = _encrypted_callback(challenge, _NOW)
    assert parse_feishu_callback(body, headers, bot_config, now=_NOW) == (
        "challenge",
        "challenge-value",
    )
    with pytest.raises(FeishuEventPayloadError, match="Plaintext"):
        parse_feishu_callback(json.dumps(challenge).encode(), {}, bot_config, now=_NOW)

    bad_token_body, bad_token_headers = _encrypted_callback(
        {**challenge, "token": "wrong"},
        _NOW,
    )
    with pytest.raises(FeishuEventVerificationError, match="verification token"):
        parse_feishu_callback(bad_token_body, bad_token_headers, bot_config, now=_NOW)

    event_body, event_headers = _encrypted_callback(
        _message_payload(created_at=_NOW, text="hello"),
        _NOW,
    )
    kind, parsed = parse_feishu_callback(event_body, event_headers, bot_config, now=_NOW)
    assert kind == "message"
    assert isinstance(parsed, FeishuMessageEvent)
    assert parsed.text == "hello"
    assert parsed.message_id == "om-1"
    assert parsed.message_created_at_ms == _NOW * 1000

    with pytest.raises(FeishuEventVerificationError, match="signature"):
        parse_feishu_callback(
            event_body,
            {**event_headers, "x-lark-signature": "bad"},
            bot_config,
            now=_NOW,
        )

    stale_request_body, stale_request_headers = _encrypted_callback(
        _message_payload(created_at=_NOW, text="hello"),
        _NOW - 301,
    )
    with pytest.raises(FeishuEventVerificationError, match="replay window"):
        parse_feishu_callback(
            stale_request_body,
            stale_request_headers,
            bot_config,
            now=_NOW,
        )

    stale_event_body, stale_event_headers = _encrypted_callback(
        _message_payload(created_at=_NOW - 301, text="hello"),
        _NOW,
    )
    with pytest.raises(FeishuEventVerificationError, match="replay window"):
        parse_feishu_callback(stale_event_body, stale_event_headers, bot_config, now=_NOW)

    wrong_key = FeishuBotConfig(
        app_id=bot_config.app_id,
        app_secret=bot_config.app_secret,
        verification_token=bot_config.verification_token,
        encrypt_key="wrong-key",
    )
    with pytest.raises(FeishuEventVerificationError, match="signature"):
        parse_feishu_callback(event_body, event_headers, wrong_key, now=_NOW)

    tampered = json.loads(event_body)
    encoded = tampered["encrypt"]
    tampered["encrypt"] = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    with pytest.raises(FeishuEventVerificationError, match="signature"):
        parse_feishu_callback(json.dumps(tampered).encode(), event_headers, bot_config, now=_NOW)

    padding_body, padding_headers = _invalid_padding_callback(_NOW)
    with pytest.raises(FeishuEventVerificationError, match="padding"):
        parse_feishu_callback(padding_body, padding_headers, bot_config, now=_NOW)

    wrong_app_body, wrong_app_headers = _encrypted_callback(
        _message_payload(created_at=_NOW, text="hello", app_id="cli-other"),
        _NOW,
    )
    with pytest.raises(FeishuEventVerificationError, match="app_id"):
        parse_feishu_callback(wrong_app_body, wrong_app_headers, bot_config, now=_NOW)

    oversized_body, oversized_headers = _encrypted_callback(
        _message_payload(created_at=_NOW, text="x" * 4_001),
        _NOW,
    )
    with pytest.raises(FeishuEventPayloadError, match="character limit"):
        parse_feishu_callback(oversized_body, oversized_headers, bot_config, now=_NOW)


def test_plaintext_mode_checks_token_without_request_signature(
    bot_config: FeishuBotConfig,
) -> None:
    plaintext_config = FeishuBotConfig(
        app_id=bot_config.app_id,
        app_secret=bot_config.app_secret,
        verification_token=bot_config.verification_token,
        encrypt_key=None,
    )
    challenge = {
        "challenge": "plain-challenge",
        "token": "verify-token",
        "type": "url_verification",
    }
    # Plaintext mode has no request signature; only the verification token is checked.
    assert parse_feishu_callback(
        json.dumps(challenge).encode(), {}, plaintext_config, now=_NOW
    ) == ("challenge", "plain-challenge")
    with pytest.raises(FeishuEventVerificationError, match="verification token"):
        parse_feishu_callback(
            json.dumps({**challenge, "token": "wrong"}).encode(),
            {},
            plaintext_config,
            now=_NOW,
        )

    encrypted_body, encrypted_headers = _encrypted_callback(challenge, _NOW)
    with pytest.raises(FeishuEventPayloadError, match="without an Encrypt Key"):
        parse_feishu_callback(
            encrypted_body,
            encrypted_headers,
            plaintext_config,
            now=_NOW,
        )

    kind, parsed = parse_feishu_callback(
        json.dumps(_message_payload(created_at=_NOW, text="plain event")).encode(),
        {},
        plaintext_config,
        now=_NOW,
    )
    assert kind == "message"
    assert isinstance(parsed, FeishuMessageEvent)
    assert parsed.text == "plain event"


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


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("missing", id="missing"),
        pytest.param(None, id="null"),
        pytest.param(True, id="bool"),
        pytest.param(_NOW * 1000, id="json-number"),
        pytest.param("", id="empty"),
        pytest.param(" 2000000000000", id="whitespace"),
        pytest.param("+2000000000000", id="sign"),
        pytest.param("-1", id="negative"),
        pytest.param("2000000000000.0", id="fraction"),
        pytest.param("2e12", id="exponent"),
        pytest.param("２００００００００００００", id="non-ascii"),
        pytest.param("9" * 14, id="overlong"),
        pytest.param("4102444800001", id="out-of-range"),
    ],
)
def test_message_timestamp_must_be_a_strict_millisecond_string(bot_config, raw):
    payload = _message_payload(created_at=_NOW, text="hello")
    event = payload["event"]
    assert isinstance(event, dict)
    message = event["message"]
    assert isinstance(message, dict)
    if raw == "missing":
        del message["create_time"]
    else:
        message["create_time"] = raw
    body, headers = _encrypted_callback(payload, _NOW)
    with pytest.raises(FeishuEventPayloadError, match="message create_time"):
        parse_feishu_callback(body, headers, bot_config, now=_NOW)


def test_message_time_is_preserved_instead_of_the_header_time(bot_config):
    payload = _message_payload(created_at=_NOW, text="hello")
    event = payload["event"]
    assert isinstance(event, dict)
    message = event["message"]
    assert isinstance(message, dict)
    message["create_time"] = str((_NOW - 10) * 1000)
    body, headers = _encrypted_callback(payload, _NOW)
    kind, parsed = parse_feishu_callback(body, headers, bot_config, now=_NOW)
    assert kind == "message"
    assert parsed.message_created_at_ms == (_NOW - 10) * 1000


def test_seconds_looking_message_time_is_not_rescaled(bot_config):
    payload = _message_payload(created_at=_NOW, text="hello")
    event = payload["event"]
    assert isinstance(event, dict)
    message = event["message"]
    assert isinstance(message, dict)
    message["create_time"] = str(_NOW)
    body, headers = _encrypted_callback(payload, _NOW)
    _, parsed = parse_feishu_callback(body, headers, bot_config, now=_NOW)
    assert parsed.message_created_at_ms == _NOW
    assert not feishu_message_time_is_valid(
        parsed.message_created_at_ms, activated_at=_NOW - 1, now=_NOW
    )


def test_future_message_is_not_saved_by_a_current_header(bot_config):
    payload = _message_payload(created_at=_NOW, text="hello")
    event = payload["event"]
    assert isinstance(event, dict)
    message = event["message"]
    assert isinstance(message, dict)
    message["create_time"] = str((_NOW + 1) * 1000)
    body, headers = _encrypted_callback(payload, _NOW)
    with pytest.raises(FeishuEventVerificationError, match="message timestamp"):
        parse_feishu_callback(body, headers, bot_config, now=_NOW)


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
