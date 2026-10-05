"""Feishu Bot binding, verification, deduplication, and Chat bridge tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import HTTPException
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
from claude_hub.services import goal_run, ttyd_manager, workspace_manager
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V1,
    format_feishu_provider_text_v1,
)
from claude_hub.services.feishu_bot import (
    BindingCodeError,
    BindingRateLimitError,
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


def _encrypted_callback(
    payload: dict,
    now: int,
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
    return body, _callback_headers(body, now, encrypt_key=encrypt_key)


def _invalid_padding_callback(now: int) -> tuple[bytes, dict[str, str]]:
    iv = b"0123456789abcdef"
    key = hashlib.sha256(b"encrypt-key").digest()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = iv + encryptor.update(b"\x00" * 16) + encryptor.finalize()
    body = json.dumps({"encrypt": base64.b64encode(ciphertext).decode()}).encode()
    return body, _callback_headers(body, now)


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


def test_deleted_workspace_preserves_rebinding_reason(monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    monkeypatch.delitem(workspace_manager.workspaces, "ws-1")
    with pytest.raises(HTTPException) as raised:
        bot_api._validate_bind_target("tab-1", "ws-1")
    assert raised.value.status_code == 409
    assert raised.value.detail == "Chat tab workspace target no longer exists"
    assert raised.value.headers == {stream_api.CHAT_ERROR_REASON_HEADER: "binding_target_missing"}


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


def test_delete_endpoint_revokes_unconsumed_code(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[str] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            pass

        async def reply_text(self, message_id: str, text: str) -> None:
            sent.append(text)

    monkeypatch.setattr(bot_api, "FeishuBotClient", FakeFeishuClient)
    client = TestClient(app)
    cookies = _login_cookie("ou-owner")
    code = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=cookies,
    ).json()["code"]
    assert client.delete("/api/feishu/bot/binding", cookies=cookies).status_code == 204
    now = int(time.time())
    body, headers = _encrypted_callback(
        _message_payload(now=now, text=code, message_id="om-revoked-code"), now
    )

    response = client.post("/api/feishu/bot/events", content=body, headers=headers)

    assert response.status_code == 200
    assert "invalid" in sent[-1]
    assert configured_bot.get_owner_binding("ou-owner") is None


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
    wrong_code, _ = store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    with pytest.raises(BindingCodeError, match="different Feishu user"):
        store.consume_code(
            wrong_code,
            sender_open_id="ou-other",
            app_id="cli-bot",
            chat_id="oc-chat",
            owner_is_authorized=lambda *_: True,
        )

    expired_code, _ = store.create_code(
        "ou-owner", "owner@example.test", "tab-1", None, ttl_seconds=1
    )
    now[0] = 102.0
    with pytest.raises(BindingCodeError, match="expired"):
        store.consume_code(
            expired_code,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-chat",
            owner_is_authorized=lambda *_: True,
        )

    now[0] = 200.0
    code, _ = store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    store.consume_code(
        code,
        sender_open_id="ou-owner",
        app_id="cli-bot",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
    )
    with pytest.raises(BindingCodeError, match="already been used"):
        store.consume_code(
            code,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-chat",
            owner_is_authorized=lambda *_: True,
        )
    assert store.get_sender_binding("ou-owner", "cli-bot", "oc-chat") is not None
    assert store.get_sender_binding("ou-owner", "cli-bot", "oc-other") is None


def test_unbind_revokes_pending_codes_with_and_without_binding(tmp_path) -> None:
    store = FeishuBindingStore(tmp_path / "state.json")
    pending_only, _ = store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    assert store.delete_owner_binding("ou-owner")
    with pytest.raises(BindingCodeError, match="invalid"):
        store.consume_code(
            pending_only,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-chat",
            owner_is_authorized=lambda *_: True,
        )

    first, _ = store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    store.consume_code(
        first,
        sender_open_id="ou-owner",
        app_id="cli-bot",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
    )
    pending_after_binding, _ = store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    assert store.delete_owner_binding("ou-owner")
    assert store.get_owner_binding("ou-owner") is None
    with pytest.raises(BindingCodeError, match="invalid"):
        store.consume_code(
            pending_after_binding,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-chat",
            owner_is_authorized=lambda *_: True,
        )


def test_binding_code_rate_limit_is_bounded_and_expires(tmp_path) -> None:
    now = [100.0]
    path = tmp_path / "state.json"
    store = FeishuBindingStore(path, now=lambda: now[0])
    for _ in range(5):
        store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    restarted_store = FeishuBindingStore(path, now=lambda: now[0])
    with pytest.raises(BindingRateLimitError, match="per minute"):
        restarted_store.create_code("ou-owner", "owner@example.test", "tab-1", None)

    now[0] += 61
    restarted_store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    state = json.loads(path.read_text())
    assert len(state["rate_limits"]["ou-owner"]) == 1
    assert len(state["pending"]) == 1


def test_binding_code_rechecks_current_authorization(tmp_path) -> None:
    store = FeishuBindingStore(tmp_path / "state.json")
    code, _ = store.create_code("ou-owner", "owner@example.test", "tab-1", None)
    with pytest.raises(BindingCodeError, match="no longer authorized"):
        store.consume_code(
            code,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-chat",
            owner_is_authorized=lambda *_: False,
        )
    assert store.get_owner_binding("ou-owner") is None


def test_challenge_and_event_verification(bot_config) -> None:
    now = int(time.time())
    challenge = {
        "challenge": "challenge-value",
        "token": "verify-token",
        "type": "url_verification",
    }
    body, headers = _encrypted_callback(challenge, now)
    assert parse_feishu_callback(body, headers, bot_config, now=now) == (
        "challenge",
        "challenge-value",
    )
    with pytest.raises(FeishuEventPayloadError, match="Plaintext"):
        parse_feishu_callback(json.dumps(challenge).encode(), {}, bot_config, now=now)

    bad_token_body, bad_token_headers = _encrypted_callback({**challenge, "token": "wrong"}, now)
    with pytest.raises(FeishuEventVerificationError, match="verification token"):
        parse_feishu_callback(bad_token_body, bad_token_headers, bot_config, now=now)

    event_body, event_headers = _encrypted_callback(_message_payload(now=now, text="hello"), now)
    kind, parsed = parse_feishu_callback(event_body, event_headers, bot_config, now=now)
    assert kind == "message"
    assert parsed.text == "hello"

    with pytest.raises(FeishuEventVerificationError, match="signature"):
        parse_feishu_callback(
            event_body,
            {**event_headers, "x-lark-signature": "bad"},
            bot_config,
            now=now,
        )
    with pytest.raises(FeishuEventVerificationError, match="replay window"):
        stale_body, stale_headers = _encrypted_callback(
            _message_payload(now=now, text="hello"), now - 301
        )
        parse_feishu_callback(stale_body, stale_headers, bot_config, now=now)
    with pytest.raises(FeishuEventVerificationError, match="signature"):
        wrong_key = bot_config.__class__(
            app_id=bot_config.app_id,
            app_secret=bot_config.app_secret,
            verification_token=bot_config.verification_token,
            encrypt_key="wrong-key",
        )
        parse_feishu_callback(event_body, event_headers, wrong_key, now=now)

    tampered = json.loads(event_body)
    encoded = tampered["encrypt"]
    tampered["encrypt"] = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    tampered_body = json.dumps(tampered).encode()
    with pytest.raises(FeishuEventVerificationError, match="signature"):
        parse_feishu_callback(tampered_body, event_headers, bot_config, now=now)

    padding_body, padding_headers = _invalid_padding_callback(now)
    with pytest.raises(FeishuEventVerificationError, match="padding"):
        parse_feishu_callback(padding_body, padding_headers, bot_config, now=now)

    wrong_app_body, wrong_app_headers = _encrypted_callback(
        _message_payload(now=now, text="hello", app_id="cli-other"), now
    )
    with pytest.raises(FeishuEventVerificationError, match="app_id"):
        parse_feishu_callback(wrong_app_body, wrong_app_headers, bot_config, now=now)
    oversized_body, oversized_headers = _encrypted_callback(
        _message_payload(now=now, text="x" * 4_001), now
    )
    with pytest.raises(FeishuEventPayloadError, match="character limit"):
        parse_feishu_callback(oversized_body, oversized_headers, bot_config, now=now)


def test_plaintext_mode_is_explicitly_unsigned(bot_config) -> None:
    now = int(time.time())
    plaintext_config = bot_config.__class__(
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
    assert parse_feishu_callback(json.dumps(challenge).encode(), {}, plaintext_config, now=now) == (
        "challenge",
        "plain-challenge",
    )
    encrypted_body, encrypted_headers = _encrypted_callback(challenge, now)
    with pytest.raises(FeishuEventPayloadError, match="without an Encrypt Key"):
        parse_feishu_callback(
            encrypted_body,
            encrypted_headers,
            plaintext_config,
            now=now,
        )
    kind, parsed = parse_feishu_callback(
        json.dumps(_message_payload(now=now, text="plain event")).encode(),
        {},
        plaintext_config,
        now=now,
    )
    assert kind == "message"
    assert parsed.text == "plain event"


def test_encrypted_challenge_endpoint_and_bad_verification(configured_bot) -> None:
    client = TestClient(app)
    now = int(time.time())
    valid_body, valid_headers = _encrypted_callback(
        {
            "challenge": "challenge-value",
            "token": "verify-token",
            "type": "url_verification",
        },
        now,
    )
    bad_body, bad_headers = _encrypted_callback(
        {
            "challenge": "challenge-value",
            "token": "wrong",
            "type": "url_verification",
        },
        now,
    )
    valid = client.post("/api/feishu/bot/events", content=valid_body, headers=valid_headers)
    rejected = client.post("/api/feishu/bot/events", content=bad_body, headers=bad_headers)

    assert valid.json() == {"challenge": "challenge-value"}
    assert rejected.status_code == 401


def test_full_bind_dispatch_reply_dedup_read_and_unbind(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[tuple[str, str]] = []
    dispatched: list[tuple[str, str, str, dict]] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            assert config.app_id == "cli-bot"

        async def reply_text(self, message_id: str, text: str) -> None:
            sent.append((message_id, text))

    async def fake_dispatch(
        tab_id: str,
        text: str,
        turn_id: str,
        **kwargs,
    ) -> str:
        dispatched.append((tab_id, text, turn_id, kwargs))
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
    bind_body, bind_headers = _encrypted_callback(
        _message_payload(now=now, text=code, message_id="om-bind"), now
    )
    bound = client.post(
        "/api/feishu/bot/events",
        content=bind_body,
        headers=bind_headers,
    )
    assert bound.status_code == 200
    assert sent[-1] == ("om-bind", "已连接 Claude Hub Chat：tab-1")
    binding = client.get("/api/feishu/bot/binding", cookies=cookies).json()["binding"]
    assert binding["owner_open_id"] == "ou-owner"
    assert binding["tab_id"] == "tab-1"
    assert binding["workspace_id"] == "ws-1"

    message_body, message_headers = _encrypted_callback(
        _message_payload(now=now, text="Continue the existing work", message_id="om-chat"),
        now,
    )
    first = client.post(
        "/api/feishu/bot/events",
        content=message_body,
        headers=message_headers,
    )
    monkeypatch.setattr(
        bot_api,
        "_binding_store",
        FeishuBindingStore(configured_bot.path),
    )
    duplicate = client.post(
        "/api/feishu/bot/events",
        content=message_body,
        headers=message_headers,
    )
    assert first.status_code == 200
    assert duplicate.json() == {"ok": True, "duplicate": True}
    metadata = {
        "origin": "feishu",
        "provider_text_format": FEISHU_PROVIDER_TEXT_FORMAT_V1,
        "feishu": {
            "app_id": "cli-bot",
            "chat_id": "oc-chat",
            "message_id": "om-chat",
            "sender_open_id": "ou-owner",
        },
    }
    assert dispatched == [
        (
            "tab-1",
            format_feishu_provider_text_v1(
                "Continue the existing work",
                app_id="cli-bot",
                chat_id="oc-chat",
                message_id="om-chat",
                sender_open_id="ou-owner",
            ),
            "feishu-" + hashlib.sha256(b"om-chat").hexdigest()[:32],
            {"visible_text": "Continue the existing work", "turn_metadata": metadata},
        )
    ]
    assert sent[-1] == ("om-chat", "Hub assistant reply")

    assert client.delete("/api/feishu/bot/binding", cookies=cookies).status_code == 204
    assert client.get("/api/feishu/bot/binding", cookies=cookies).json() == {"binding": None}
    replay_after_unbind = client.post(
        "/api/feishu/bot/events",
        content=message_body,
        headers=message_headers,
    )
    assert replay_after_unbind.json() == {"ok": True, "duplicate": True}
    assert len(dispatched) == 1


def test_wrong_sender_cannot_consume_binding_code(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[str] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            pass

        async def reply_text(self, message_id: str, text: str) -> None:
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
    body, headers = _encrypted_callback(
        _message_payload(
            now=now,
            text=code,
            message_id="om-other",
            sender_open_id="ou-other",
            chat_id="oc-other",
        ),
        now,
    )
    response = client.post(
        "/api/feishu/bot/events",
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    assert "different Feishu user" in sent[-1]
    assert client.get("/api/feishu/bot/binding", cookies=cookies).json() == {"binding": None}


def test_revoked_owner_cannot_dispatch(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[str] = []
    dispatched: list[str] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            pass

        async def reply_text(self, message_id: str, text: str) -> None:
            sent.append(text)

    async def fail_if_dispatched(tab_id: str, text: str, turn_id: str, **kwargs) -> str:
        dispatched.append(text)
        return "must not run"

    monkeypatch.setattr(bot_api, "FeishuBotClient", FakeFeishuClient)
    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", fail_if_dispatched)
    client = TestClient(app)
    code = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=_login_cookie("ou-owner"),
    ).json()["code"]
    configured_bot.consume_code(
        code,
        sender_open_id="ou-owner",
        app_id="cli-bot",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
    )
    monkeypatch.setattr(settings, "auth_allowed_open_ids", "ou-other")
    now = int(time.time())
    body, headers = _encrypted_callback(
        _message_payload(now=now, text="must not execute", message_id="om-revoked"), now
    )

    response = client.post("/api/feishu/bot/events", content=body, headers=headers)

    assert response.status_code == 200
    assert dispatched == []
    assert sent == ["Claude Hub 授权已失效，请在网页重新绑定。"]
    assert configured_bot.get_owner_binding("ou-owner") is None


def test_unbind_during_turn_suppresses_old_target_reply(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[str] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            pass

        async def reply_text(self, message_id: str, text: str) -> None:
            sent.append(text)

    async def unbind_then_complete(
        tab_id: str,
        text: str,
        turn_id: str,
        **kwargs,
    ) -> str:
        configured_bot.delete_owner_binding("ou-owner")
        return "secret old target result"

    monkeypatch.setattr(bot_api, "FeishuBotClient", FakeFeishuClient)
    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", unbind_then_complete)
    client = TestClient(app)
    code = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=_login_cookie("ou-owner"),
    ).json()["code"]
    configured_bot.consume_code(
        code,
        sender_open_id="ou-owner",
        app_id="cli-bot",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
    )
    now = int(time.time())
    body, headers = _encrypted_callback(
        _message_payload(now=now, text="long task", message_id="om-in-flight"), now
    )

    response = client.post("/api/feishu/bot/events", content=body, headers=headers)

    assert response.status_code == 200
    assert sent == []


def test_rebind_during_turn_suppresses_reply_to_old_message(configured_bot, monkeypatch) -> None:
    _install_chat_target(monkeypatch)
    sent: list[tuple[str, str]] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            pass

        async def reply_text(self, message_id: str, text: str) -> None:
            sent.append((message_id, text))

    async def rebind_then_complete(
        tab_id: str,
        text: str,
        turn_id: str,
        **kwargs,
    ) -> str:
        code, _ = configured_bot.create_code("ou-owner", "ou-owner@example.test", "tab-1", "ws-1")
        configured_bot.consume_code(
            code,
            sender_open_id="ou-owner",
            app_id="cli-bot",
            chat_id="oc-new-chat",
            owner_is_authorized=lambda *_: True,
        )
        return "old target result"

    monkeypatch.setattr(bot_api, "FeishuBotClient", FakeFeishuClient)
    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", rebind_then_complete)
    client = TestClient(app)
    code = client.post(
        "/api/feishu/bot/bind/start",
        json={"tab_id": "tab-1", "workspace_id": "ws-1"},
        cookies=_login_cookie("ou-owner"),
    ).json()["code"]
    configured_bot.consume_code(
        code,
        sender_open_id="ou-owner",
        app_id="cli-bot",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
    )
    now = int(time.time())
    body, headers = _encrypted_callback(
        _message_payload(now=now, text="long task", message_id="om-old"), now
    )

    response = client.post("/api/feishu/bot/events", content=body, headers=headers)

    assert response.status_code == 200
    assert sent == []
    assert configured_bot.get_sender_binding("ou-owner", "cli-bot", "oc-new-chat")


@pytest.mark.parametrize(
    ("reason", "expected_message"),
    [
        (
            "chat_busy",
            "Claude Hub Chat 正在处理其他消息或等待网页回答，本条消息尚未执行。",
        ),
        (
            "structured_source_unavailable",
            "Claude Hub Chat 当前不可用，本条消息尚未执行。请在网页检查 Chat 状态后重试。",
        ),
        (
            "binding_target_missing",
            "Claude Hub 目标当前不可用，请在网页重新绑定。",
        ),
        (
            None,
            "Claude Hub Chat 当前不可用，本条消息尚未执行。请在网页检查 Chat 状态后重试。",
        ),
    ],
)
def test_chat_conflict_replies_to_same_message_without_claiming_binding_expired(
    configured_bot, monkeypatch, reason: str | None, expected_message: str
) -> None:
    _install_chat_target(monkeypatch)
    sent: list[tuple[str, str]] = []
    dispatched: list[str] = []

    class FakeFeishuClient:
        def __init__(self, config) -> None:
            pass

        async def reply_text(self, message_id: str, text: str) -> None:
            sent.append((message_id, text))

    async def busy(tab_id: str, text: str, turn_id: str, **kwargs) -> str:
        dispatched.append(text)
        headers = {stream_api.CHAT_ERROR_REASON_HEADER: reason} if reason is not None else None
        raise HTTPException(status_code=409, detail="test conflict", headers=headers)

    monkeypatch.setattr(bot_api, "FeishuBotClient", FakeFeishuClient)
    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", busy)
    code, _ = configured_bot.create_code("ou-owner", "ou-owner@example.test", "tab-1", "ws-1")
    configured_bot.consume_code(
        code,
        sender_open_id="ou-owner",
        app_id="cli-bot",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
    )
    answer = json.dumps(
        {
            "type": "ask_question_response",
            "answers": [{"questionId": "q1", "selected": ["red"]}],
        }
    )
    now = int(time.time())
    body, headers = _encrypted_callback(
        _message_payload(now=now, text=answer, message_id="om-busy"), now
    )

    response = TestClient(app).post("/api/feishu/bot/events", content=body, headers=headers)

    assert response.status_code == 200
    assert dispatched and dispatched[0] != answer
    assert dispatched[0].endswith(answer)
    assert sent == [("om-busy", expected_message)]
    assert configured_bot.get_sender_binding("ou-owner", "cli-bot", "oc-chat")


@pytest.mark.asyncio
async def test_event_body_limit_stops_chunked_request_without_content_length(
    configured_bot,
) -> None:
    yielded_late_chunk = False

    async def chunks():
        nonlocal yielded_late_chunk
        yield b"x" * ((256 * 1024) - 1)
        yield b"xx"
        yielded_late_chunk = True
        raise AssertionError("event reader continued after crossing its size limit")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/feishu/bot/events", content=chunks())

    assert response.status_code == 413
    assert not yielded_late_chunk


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
    dispatched: list[tuple[str, str, str, dict]] = []

    async def fake_dispatch(tab_id, payload, **kwargs) -> str:
        dispatched.append((tab_id, payload.text, payload.client_turn_id, kwargs))
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
    result = await stream_api.dispatch_tab_chat_and_wait(
        "tab-1",
        "provider question",
        "feishu-turn",
        visible_text="question",
        turn_metadata=metadata,
        timeout_seconds=1,
    )

    assert result == "existing session reply"
    assert dispatched == [
        (
            "tab-1",
            "provider question",
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
async def test_external_goal_input_is_rejected_without_consuming_question(monkeypatch) -> None:
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
    monkeypatch.setattr(goal_run, "get_goal_admission_lock", lambda tab_id: asyncio.Lock())
    monkeypatch.setattr(
        goal_run, "get_goal_manager", lambda: SimpleNamespace(current=lambda tab_id: current)
    )
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: object())
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


def test_inflight_send_error_has_stable_busy_reason_without_changing_body() -> None:
    mapped = stream_api._map_send_exception(
        RuntimeError("a turn is already in flight; wait for completion")
    )
    assert mapped.status_code == 409
    assert mapped.detail == "a turn is already in flight; wait for completion"
    assert mapped.headers == {stream_api.CHAT_ERROR_REASON_HEADER: "chat_busy"}


@pytest.mark.asyncio
async def test_external_chat_adapter_classifies_structured_source_failure(monkeypatch) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")

    class FailedManager:
        async def subscribe(self, session_arg):
            assert session_arg is session
            raise stream_api.StructuredSourceUnavailable("native source unavailable")

        def unsubscribe(self, session_id, queue_arg) -> None:
            raise AssertionError("a failed subscription must not be unsubscribed")

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: FailedManager())
    with pytest.raises(HTTPException) as raised:
        await stream_api.dispatch_tab_chat_and_wait(
            "tab-1", "provider text", "feishu-turn", timeout_seconds=1
        )
    assert raised.value.status_code == 409
    assert raised.value.detail == "native source unavailable"
    assert raised.value.headers == {
        stream_api.CHAT_ERROR_REASON_HEADER: "structured_source_unavailable"
    }


@pytest.mark.asyncio
async def test_external_chat_adapter_preserves_unknown_409(monkeypatch) -> None:
    session = SimpleNamespace(id="terminal-tab-tab-1")
    queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue()
    unsubscribed: list[tuple[str, object]] = []

    class FakeManager:
        async def subscribe(self, session_arg):
            return queue

        def unsubscribe(self, session_id, queue_arg) -> None:
            unsubscribed.append((session_id, queue_arg))

    async def conflict(tab_id, payload, **kwargs) -> str:
        raise HTTPException(status_code=409, detail="unclassified Chat conflict")

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: FakeManager())
    monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", conflict)
    with pytest.raises(HTTPException) as raised:
        await stream_api.dispatch_tab_chat_and_wait(
            "tab-1", "provider text", "feishu-turn", timeout_seconds=1
        )
    assert raised.value.status_code == 409
    assert raised.value.detail == "unclassified Chat conflict"
    assert not raised.value.headers
    assert unsubscribed == [(session.id, queue)]
