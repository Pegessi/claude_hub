"""Native question-card surfaces: protected bot settings and message identities."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_hub.api import feishu_bot as bot_api
from claude_hub.auth.dependencies import get_current_user_from_cookie
from claude_hub.main import app
from claude_hub.models import User
from claude_hub.services.feishu_bot import FeishuBotClient, FeishuBotConfig, FeishuBotError
from claude_hub.services.feishu_bot_pool import (
    FeishuBotPoolStore,
    FeishuBotRevisionConflict,
    OwnerIdentity,
)
from claude_hub.services.feishu_bot_websocket import message_event_from_sdk
from claude_hub.services.feishu_message_format import build_feishu_question_card_payload


def make_pool(path: Path) -> tuple[FeishuBotPoolStore, str]:
    pool = FeishuBotPoolStore(path / "pool.json", path / "legacy.json")
    bot = pool.create_bot(name="Bot", config=FeishuBotConfig("app", "secret"), environ={})
    return pool, bot


def test_callbacks_default_off_provision_rotation_and_restart(tmp_path: Path) -> None:
    pool, bot = make_pool(tmp_path)
    owner = OwnerIdentity(open_id="local", email="local@localhost", kind="local")
    entry = pool.effective(bot, {})
    code, _, _ = pool.issue_code(
        bot,
        owner=owner,
        tab_id="tab",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )
    claim, word = pool.claim_code(
        bot, code=code, sender_open_id="ou-owner", chat_id="oc-chat", environ={}
    )
    entry = pool.effective(bot, {})
    binding = pool.activate_claim(
        bot,
        pairing_id=claim.pairing_id,
        confirm_word=word,
        actor=owner,
        expected_revision=entry.revision,
        environ={},
    )
    old = pool.effective(bot, {})
    assert not old.callback_enabled
    with pytest.raises(ValueError):
        pool.configure_callbacks(
            bot, callback_enabled=True, expected_revision=old.revision, environ={}
        )
    pool.configure_callbacks(
        bot,
        callback_enabled=True,
        verification_token="verify-private",
        encrypt_key="encrypt-private",
        expected_tenant_key="tenant",
        expected_revision=old.revision,
        environ={},
    )
    current = pool.effective(bot, {})
    assert current.callback_enabled and current.expected_tenant_key == "tenant"
    assert current.verification_token == "verify-private"
    assert current.encrypt_key == "encrypt-private"
    assert "verify-private" not in repr(current) and "encrypt-private" not in repr(current)
    assert "secret" not in repr(current.config)
    assert not pool.is_current(old, {})
    assert pool.snapshot({}).get(bot).binding == binding  # type: ignore[union-attr]
    assert pool.path.stat().st_mode & 0o777 == 0o600
    restarted = FeishuBotPoolStore(pool.path, pool.legacy_path).effective(bot, {})
    assert restarted == current
    with pytest.raises(FeishuBotRevisionConflict):
        pool.configure_callbacks(
            bot, callback_enabled=False, expected_revision=old.revision, environ={}
        )
    pool.configure_callbacks(
        bot, callback_enabled=False, expected_revision=current.revision, environ={}
    )
    assert not pool.effective(bot, {}).callback_enabled
    assert not pool.is_current(current, {})


def test_callback_api_redacts_secrets_and_rejects_stale_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, bot = make_pool(tmp_path)
    monkeypatch.setattr(bot_api, "_pool", pool)
    app.dependency_overrides[get_current_user_from_cookie] = lambda: User(
        open_id="local", name="Local", email="local@localhost"
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        body = {
            "callback_enabled": True,
            "expected_revision": pool.effective(bot).revision,
            "verification_token": "verify-private",
            "encrypt_key": "encrypt-private",
            "expected_tenant_key": "tenant",
        }
        response = client.put(f"/api/feishu/bot/bots/{bot}/callbacks", json=body)
        assert response.status_code == 200, response.text
        assert "verify-private" not in response.text and "encrypt-private" not in response.text
        entry = response.json()["bots"][0]
        assert entry["callback_enabled"] and entry["callback_credentials_configured"]
        assert client.put(f"/api/feishu/bot/bots/{bot}/callbacks", json=body).status_code == 409
    finally:
        client.close()
        app.dependency_overrides.pop(get_current_user_from_cookie, None)


def test_native_card_actions_are_distinct_and_terminal_card_has_no_tokens() -> None:
    payload = build_feishu_question_card_payload(
        "Choose <at id=all>now</at>",
        [("Run", "run-token"), ("Skip", "skip-token")],
        question_ref="QTEST",
        other_token="other-token",
    )
    card = payload.content
    assert card["schema"] == "2.0" and card["config"] == {
        "update_multi": True,
        "enable_forward": False,
    }
    assert "<at" not in json.dumps(card)
    body = card["body"]
    assert isinstance(body, dict)
    buttons = [e for e in body["elements"] if e["tag"] == "button"]
    assert [e["behaviors"][0]["value"]["hub_token"] for e in buttons] == [
        "run-token",
        "skip-token",
        "other-token",
    ]
    closed = build_feishu_question_card_payload(
        "Choose",
        [("Run", "run-token")],
        question_ref="QTEST",
        other_token="other-token",
        state="resolved",
        answer="Run",
    )
    serialized = json.dumps(closed.content)
    assert "run-token" not in serialized and "other-token" not in serialized
    assert '"disabled": true' in serialized
    no_tokens = build_feishu_question_card_payload(
        "Choose", [("Run", "")], question_ref="QTEST", other_token="", state="expired"
    )
    assert "hub_token" not in json.dumps(no_tokens.content)


@pytest.mark.parametrize(
    "prompt,options,other",
    [
        ("Choose", [("Run", "same")], "same"),
        ("x" * 30_000, [], "other"),
        ("", [], "other"),
        ("Choose", [("", "run")], "other"),
    ],
    ids=["duplicate-token", "oversize", "empty-prompt", "empty-option"],
)
def test_question_cards_reject_lossy_or_ambiguous_payload(
    prompt: str, options: list[tuple[str, str]], other: str
) -> None:
    with pytest.raises(ValueError):
        build_feishu_question_card_payload(prompt, options, question_ref="QTEST", other_token=other)


@pytest.mark.asyncio
async def test_message_identity_uuid_and_patch_keep_exact_target() -> None:
    recorded: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if request.url.path.endswith("/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om-exact"}})

    async with httpx.AsyncClient(
        base_url="https://example.invalid", transport=httpx.MockTransport(handle)
    ) as http:
        client = FeishuBotClient(FeishuBotConfig("app", "secret"), http)
        payload = build_feishu_question_card_payload(
            "Choose", [], question_ref="QTEST", other_token="other"
        )
        assert (
            await client.send_message_with_identity(
                "oc-exact", "interactive", payload.content, uuid="stable-id"
            )
            == "om-exact"
        )
        await client.patch_message_card("om-exact", payload.content)
        await client.send_message("oc-exact", "text", {"text": "legacy"})
    assert json.loads(recorded[1].content)["uuid"] == "stable-id"
    assert recorded[2].method == "PATCH" and recorded[2].url.path.endswith("/om-exact")
    assert json.loads(recorded[2].content)["content"] == json.dumps(
        payload.content, ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_missing_send_identity_is_not_success_and_bad_uuid_never_sends() -> None:
    async with httpx.AsyncClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"code": 0})),
    ) as http:
        client = FeishuBotClient(FeishuBotConfig("app", "secret"), http)
        client._tenant_token = "token"
        client._token_expires_at = float("inf")
        with pytest.raises(FeishuBotError, match="delivery may have occurred"):
            await client.send_message_with_identity("chat", "text", {"text": "test"})
        with pytest.raises(FeishuBotError, match="UUID"):
            await client.send_message_with_identity("chat", "text", {"text": "test"}, uuid="x" * 51)
        with pytest.raises(FeishuBotError, match="shared"):
            await client.patch_message_card("om", {})


def test_reply_parent_and_root_are_retained_and_malformed_parent_rejected() -> None:
    message = SimpleNamespace(
        chat_type="p2p",
        message_type="text",
        content='{"text":"answer"}',
        create_time="1700000000000",
        message_id="om-reply",
        chat_id="oc-chat",
        parent_id="om-card",
        root_id="om-root",
    )
    data = SimpleNamespace(
        header=SimpleNamespace(app_id="app", event_id="event"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user", sender_id=SimpleNamespace(open_id="ou-owner")
            ),
            message=message,
        ),
    )
    event = message_event_from_sdk(data, expected_app_id="app")
    assert event.parent_id == "om-card" and event.root_id == "om-root"
    message.parent_id = 42
    with pytest.raises(ValueError, match="reply message identity"):
        message_event_from_sdk(data, expected_app_id="app")
