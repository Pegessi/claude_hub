"""Feishu Bot binding, verification, deduplication, and Chat bridge tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_hub.api import agent_stream as stream_api
from claude_hub.api import feishu_bot as bot_api
from claude_hub.auth import session as session_store
from claude_hub.config import settings
from claude_hub.main import app
from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    SessionKind,
    User,
)
from claude_hub.services import ttyd_manager, workspace_manager
from claude_hub.services.feishu_bot import (
    BindingCodeError,
    FeishuBindingStore,
    FeishuBotConfig,
    FeishuEventPayloadError,
    FeishuEventVerificationError,
    parse_feishu_callback,
)


def _sign(body: bytes, timestamp: int, nonce: str, encrypt_key: str) -> str:
    signed = str(timestamp).encode() + nonce.encode() + encrypt_key.encode() + body
    return hashlib.sha256(signed).hexdigest()


def _callback_headers(body: bytes, now: int, *, encrypt_key: str = "encrypt-key") -> dict[str, str]:
    nonce = "nonce-1"
    return {
        "content-type": "application/json",
        "x-lark-request-timestamp": str(now),
        "x-lark-request-nonce": nonce,
        "x-lark-signature": _sign(body, now, nonce, encrypt_key),
    }


def _message_payload(
    *,
    now: int,
    text: str,
    message_id: str = "om-1",
    sender_open_id: str = "ou-owner",
    chat_id: str = "oc-chat",
    app_id: str = "cli-bot",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt-{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": str(now * 1000),
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
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


@pytest.fixture()
def bot_config() -> FeishuBotConfig:
    return FeishuBotConfig(
        app_id="cli-bot",
        app_secret="app-secret",
        verification_token="verify-token",
        encrypt_key="encrypt-key",
    )


@pytest.fixture()
def configured_bot(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "feishu_app_id", "cli-bot")
    monkeypatch.setattr(settings, "feishu_app_secret", "oauth-secret")
    monkeypatch.setattr(settings, "auth_allowed_open_ids", None)
    monkeypatch.setattr(settings, "auth_allowed_emails", None)
    monkeypatch.setenv("CLAUDE_HUB_FEISHU_BOT_APP_ID", "cli-bot")
    monkeypatch.setenv("CLAUDE_HUB_FEISHU_BOT_APP_SECRET", "app-secret")
    monkeypatch.setenv("CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN", "verify-token")
    monkeypatch.setenv("CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY", "encrypt-key")
    monkeypatch.setenv("CLAUDE_HUB_PUBLIC_BASE_URL", "https://hub.example.test/")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")
    store = FeishuBindingStore(tmp_path / "feishu_bot.json")
    monkeypatch.setattr(bot_api, "_binding_store", store)
    return store


def _login_cookie(open_id: str) -> dict[str, str]:
    login = session_store.create_session(
        User(open_id=open_id, name="Authorized User", email=f"{open_id}@example.test"),
        "access-token",
        "refresh-token",
    )
    return {settings.session_cookie_name: login.session_id}


def _install_chat_target(monkeypatch, *, workspace_id: str | None = "ws-1") -> None:
    tab = SimpleNamespace(
        id="tab-1",
        session_kind=SessionKind.CHAT,
        target=ExecutionTarget.LOCAL,
        workspace_role=None,
        workspace_id=workspace_id,
    )
    monkeypatch.setattr(
        ttyd_manager,
        "get_tab",
        lambda tab_id: tab if tab_id == "tab-1" else None,
    )
    monkeypatch.setitem(ttyd_manager.processes, "tab-1", SimpleNamespace(archived=False))
    if workspace_id is not None:
        monkeypatch.setitem(
            workspace_manager.workspaces, workspace_id, SimpleNamespace(id=workspace_id)
        )


def test_binding_start_rejects_local_auth_bypass(configured_bot) -> None:
    response = TestClient(app).post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1"},
    )

    assert response.status_code == 401


def test_binding_start_validates_workspace_target_and_owner(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    client = TestClient(app)
    owner_cookie = _login_cookie("ou-owner")
    other_cookie = _login_cookie("ou-other")

    mismatch = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "wrong-workspace"},
        cookies=owner_cookie,
    )
    assert mismatch.status_code == 403

    started = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=owner_cookie,
    )
    assert started.status_code == 201
    assert started.json()["event_url"] == "https://hub.example.test/api/feishu/bot/events"
    assert client.get("/api/feishu/bot/binding", cookies=other_cookie).json() == {"binding": None}


def test_binding_start_rejects_non_chat_target(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    monkeypatch.setattr(
        ttyd_manager,
        "get_tab",
        lambda tab_id: SimpleNamespace(
            session_kind=SessionKind.TERMINAL,
            target=ExecutionTarget.LOCAL,
            workspace_role=None,
            workspace_id="ws-1",
        ),
    )
    response = TestClient(app).post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=_login_cookie("ou-owner"),
    )
    assert response.status_code == 400


def test_binding_codes_expire_reject_other_sender_and_are_single_use(tmp_path) -> None:
    now = [100.0]
    store = FeishuBindingStore(tmp_path / "state.json", now=lambda: now[0])
    wrong_code, _ = store.create_code("ou-owner", "tab-1", None)
    with pytest.raises(BindingCodeError, match="different Feishu user"):
        store.consume_code(
            wrong_code,
            sender_open_id="ou-other",
            app_id="cli-bot",
            chat_id="oc-chat",
        )

    expired_code, _ = store.create_code("ou-owner", "tab-1", None, ttl_seconds=1)
    now[0] = 102.0
    with pytest.raises(BindingCodeError, match="expired"):
        store.consume_code(
            expired_code,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-chat",
        )

    now[0] = 200.0
    code, _ = store.create_code("ou-owner", "tab-1", None)
    store.consume_code(
        code,
        sender_open_id="ou-owner",
        app_id="cli-bot",
        chat_id="oc-chat",
    )
    with pytest.raises(BindingCodeError, match="already been used"):
        store.consume_code(
            code,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-chat",
        )
    assert store.get_sender_binding("ou-owner", "cli-bot", "oc-chat") is not None
    assert store.get_sender_binding("ou-owner", "cli-bot", "oc-other") is None


def test_challenge_and_event_verification(bot_config) -> None:
    now = int(time.time())
    challenge = {
        "challenge": "challenge-value",
        "token": "verify-token",
        "type": "url_verification",
    }
    body = json.dumps(challenge).encode()
    assert parse_feishu_callback(body, {}, bot_config, now=now) == (
        "challenge",
        "challenge-value",
    )

    bad_token = {**challenge, "token": "wrong"}
    bad_body = json.dumps(bad_token).encode()
    with pytest.raises(FeishuEventVerificationError, match="verification token"):
        parse_feishu_callback(bad_body, {}, bot_config, now=now)

    event_body = json.dumps(_message_payload(now=now, text="hello")).encode()
    with pytest.raises(FeishuEventVerificationError, match="signature"):
        parse_feishu_callback(
            event_body,
            {**_callback_headers(event_body, now), "x-lark-signature": "bad"},
            bot_config,
            now=now,
        )
    with pytest.raises(FeishuEventVerificationError, match="replay window"):
        parse_feishu_callback(
            event_body,
            _callback_headers(event_body, now - 301),
            bot_config,
            now=now,
        )
    with pytest.raises(FeishuEventVerificationError, match="app_id"):
        wrong_app_body = json.dumps(
            _message_payload(now=now, text="hello", app_id="cli-other")
        ).encode()
        parse_feishu_callback(
            wrong_app_body,
            _callback_headers(wrong_app_body, now),
            bot_config,
            now=now,
        )
    oversized_body = json.dumps(_message_payload(now=now, text="x" * 4_001)).encode()
    with pytest.raises(FeishuEventPayloadError, match="character limit"):
        parse_feishu_callback(
            oversized_body,
            _callback_headers(oversized_body, now),
            bot_config,
            now=now,
        )
    encrypted_body = json.dumps({"encrypt": "ciphertext"}).encode()
    with pytest.raises(FeishuEventPayloadError, match="Encrypted"):
        parse_feishu_callback(
            encrypted_body,
            _callback_headers(encrypted_body, now),
            bot_config,
            now=now,
        )


def test_challenge_endpoint_and_bad_verification(configured_bot) -> None:
    client = TestClient(app)
    valid = client.post(
        "/api/feishu/bot/events",
        json={
            "challenge": "challenge-value",
            "token": "verify-token",
            "type": "url_verification",
        },
    )
    rejected = client.post(
        "/api/feishu/bot/events",
        json={
            "challenge": "challenge-value",
            "token": "wrong",
            "type": "url_verification",
        },
    )

    assert valid.json() == {"challenge": "challenge-value"}
    assert rejected.status_code == 401


def test_full_bind_dispatch_reply_dedup_read_and_unbind(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[tuple[str, str]] = []
    dispatched: list[tuple[str, str, str]] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            assert config.app_id == "cli-bot"

        async def send_text(self, chat_id: str, text: str) -> None:
            sent.append((chat_id, text))

    async def fake_dispatch(tab_id: str, text: str, turn_id: str) -> str:
        dispatched.append((tab_id, text, turn_id))
        return "Hub assistant reply"

    monkeypatch.setattr(bot_api, "FeishuBotClient", FakeFeishuClient)
    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", fake_dispatch)
    client = TestClient(app)
    cookies = _login_cookie("ou-owner")
    started = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=cookies,
    )
    assert started.status_code == 201
    code = started.json()["code"]

    now = int(time.time())
    bind_body = json.dumps(_message_payload(now=now, text=code, message_id="om-bind")).encode()
    bound = client.post(
        "/api/feishu/bot/events",
        content=bind_body,
        headers=_callback_headers(bind_body, now),
    )
    assert bound.status_code == 200
    assert sent[-1] == ("oc-chat", "已连接 Claude Hub Chat：tab-1")
    binding = client.get("/api/feishu/bot/binding", cookies=cookies).json()["binding"]
    assert binding["owner_open_id"] == "ou-owner"
    assert binding["tab_id"] == "tab-1"
    assert binding["workspace_id"] == "ws-1"

    message_body = json.dumps(
        _message_payload(now=now, text="Continue the existing work", message_id="om-chat")
    ).encode()
    first = client.post(
        "/api/feishu/bot/events",
        content=message_body,
        headers=_callback_headers(message_body, now),
    )
    monkeypatch.setattr(
        bot_api,
        "_binding_store",
        FeishuBindingStore(configured_bot.path),
    )
    duplicate = client.post(
        "/api/feishu/bot/events",
        content=message_body,
        headers=_callback_headers(message_body, now),
    )
    assert first.status_code == 200
    assert duplicate.json() == {"ok": True, "duplicate": True}
    assert dispatched == [
        (
            "tab-1",
            "Continue the existing work",
            "feishu-" + hashlib.sha256(b"om-chat").hexdigest()[:32],
        )
    ]
    assert sent[-1] == ("oc-chat", "Hub assistant reply")

    assert client.delete("/api/feishu/bot/binding", cookies=cookies).status_code == 204
    assert client.get("/api/feishu/bot/binding", cookies=cookies).json() == {"binding": None}


def test_wrong_sender_cannot_consume_binding_code(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[str] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            pass

        async def send_text(self, chat_id: str, text: str) -> None:
            sent.append(text)

    monkeypatch.setattr(bot_api, "FeishuBotClient", FakeFeishuClient)
    client = TestClient(app)
    cookies = _login_cookie("ou-owner")
    code = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=cookies,
    ).json()["code"]
    now = int(time.time())
    body = json.dumps(
        _message_payload(
            now=now,
            text=code,
            message_id="om-other",
            sender_open_id="ou-other",
            chat_id="oc-other",
        )
    ).encode()
    response = client.post(
        "/api/feishu/bot/events",
        content=body,
        headers=_callback_headers(body, now),
    )

    assert response.status_code == 200
    assert "different Feishu user" in sent[-1]
    assert client.get("/api/feishu/bot/binding", cookies=cookies).json() == {"binding": None}


@pytest.mark.asyncio
async def test_feishu_client_uses_injected_http_transport(bot_config) -> None:
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
        client = bot_api.FeishuBotClient(bot_config, client=http_client)
        await client.send_text("oc-chat", "assistant reply")

    assert [request.url.path for request in requests] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/im/v1/messages",
    ]
    assert requests[1].headers["authorization"] == "Bearer tenant-token"
    sent_body = json.loads(requests[1].content)
    assert sent_body["receive_id"] == "oc-chat"
    assert json.loads(sent_body["content"]) == {"text": "assistant reply"}


@pytest.mark.asyncio
async def test_external_chat_adapter_reuses_existing_stream_manager(monkeypatch) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    unsubscribed: list[tuple[str, object]] = []

    class FakeManager:
        async def subscribe(self, session_arg):
            assert session_arg is session
            return queue

        def unsubscribe(self, session_id, queue_arg) -> None:
            unsubscribed.append((session_id, queue_arg))

    manager = FakeManager()
    dispatched: list[tuple[str, str, str]] = []

    async def fake_dispatch(tab_id, payload) -> str:
        dispatched.append((tab_id, payload.text, payload.client_turn_id))
        await queue.put(
            AgentStreamEvent(
                stream_sequence=4,
                session_id=session.id,
                tab_id=tab_id,
                agent_type=AgentType.CLAUDE,
                type=AgentStreamEventType.TURN_COMPLETED,
                turn_id=payload.client_turn_id,
                payload={"status": "completed", "assistant_text": "existing session reply"},
                created_at=datetime.now(),
            )
        )
        return payload.client_turn_id

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", fake_dispatch)

    result = await stream_api.dispatch_tab_chat_and_wait(
        "tab-1", "question", "feishu-turn", timeout_seconds=1
    )

    assert result == "existing session reply"
    assert dispatched == [("tab-1", "question", "feishu-turn")]
    assert unsubscribed == [(session.id, queue)]
