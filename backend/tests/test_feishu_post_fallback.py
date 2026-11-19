"""Definite rejection fallback without retrying uncertain Feishu Bot deliveries."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from claude_hub.api import feishu_bot as bot_api
from claude_hub.api.routes.feishu_bot.message_dispatch import FeishuMessageDispatcher
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuBotError,
    FeishuBotRemoteError,
    FeishuDeliveryUncertainError,
    FeishuMessageEvent,
)
from claude_hub.services.feishu_message_format import build_feishu_message_payload


def rejected() -> httpx.Response:
    return httpx.Response(
        400,
        json={"code": 230099, "msg": "invalid post content"},
        headers={"x-tt-logid": "test-log-id"},
    )


class DeliveryHarness:
    def __init__(self) -> None:
        self.inside = False
        self.current = True
        self.valid = True
        self.checks = 0
        self.records: list[tuple[str, str | None, str | None]] = []
        self.requests: list[dict[str, Any]] = []
        self.effective: Any = SimpleNamespace(
            bot_id="bot", config=FeishuBotConfig("app", "private-test-secret")
        )
        self.binding: Any = SimpleNamespace(chat_id="chat", tab_id="tab")
        self.entry: Any = SimpleNamespace(
            bot_id="bot", binding=self.binding, bindings=(self.binding,)
        )

    @asynccontextmanager
    async def gate(self, bot_id):
        assert not self.inside
        self.inside = True
        try:
            yield
        finally:
            self.inside = False

    def is_current(self, effective):
        assert self.inside
        self.checks += 1
        return self.current

    def still_valid(self, *args):
        assert self.inside
        return self.valid

    def record(self, bot, message_id, *, thread_id=None):
        assert self.inside
        self.records.append((bot, message_id, thread_id))

    def dispatcher(self) -> FeishuMessageDispatcher:
        bindings: Any = SimpleNamespace(
            gate=self.gate,
            pool=lambda: SimpleNamespace(is_current_for_delivery=self.is_current),
            record_sent_message=self.record,
            outbound_post_timeout_seconds=lambda: 1,
        )
        return FeishuMessageDispatcher(bindings)

    def install_push(self, monkeypatch, client):
        monkeypatch.setattr(bot_api, "_bot_bound_to_tab", lambda tab: self.entry)
        monkeypatch.setattr(
            bot_api,
            "_pool",
            SimpleNamespace(
                effective=lambda bot: self.effective, is_current_for_delivery=self.is_current
            ),
        )
        monkeypatch.setattr(bot_api, "_gate", self.gate)
        monkeypatch.setattr(bot_api, "_binding_still_routes", self.still_valid)
        monkeypatch.setattr(bot_api, "FeishuBotClient", lambda config: client)

    def request(self, request):
        if "tenant_access_token" in request.url.path:
            assert not self.inside
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-token"})
        assert self.inside
        assert request.headers["authorization"] == "Bearer test-token"
        body = json.loads(request.content)
        body["decoded_content"] = json.loads(body["content"])
        self.requests.append(body)
        return body


def event(thread: str | None = None) -> FeishuMessageEvent:
    return FeishuMessageEvent(
        "event", "source", "app", "owner", "chat", "question", 1000, thread_id=thread
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [None, "existing-thread"])
@pytest.mark.parametrize("landed", [None, "landed"])
@pytest.mark.parametrize("invalidated", [None, "current", "binding"])
async def test_post_reply_rejection_falls_back_with_current_gate_and_receipt(
    thread, landed, invalidated, caplog
):
    h = DeliveryHarness()

    def handler(request):
        body = h.request(request)
        if isinstance(body, httpx.Response):
            return body
        if body["msg_type"] == "post":
            if invalidated == "current":
                h.current = False
            if invalidated == "binding":
                h.valid = False
            return rejected()
        return httpx.Response(
            200, json={"code": 0, "data": {"message_id": "sent-text", "thread_id": landed}}
        )

    text = "**Report**\n\n- Author: `Example <dev@example.org>`"
    async with httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = FeishuBotClient(h.effective.config, client=http)
        result = await h.dispatcher()._reply_if_current(
            client, event(thread), h.effective, text, formatted=True, still_valid=h.still_valid
        )
    assert result == (invalidated is None)
    assert [r["msg_type"] for r in h.requests] == (
        ["post", "text"] if invalidated is None else ["post"]
    )
    assert all(r.get("reply_in_thread", False) == bool(thread) for r in h.requests)
    assert h.checks == 2
    assert h.records == ([("bot", "sent-text", landed or thread)] if result else [])
    if result:
        assert h.requests[-1]["decoded_content"] == {"text": text}
    assert "code 230099" in caplog.text
    assert "invalid post content" in caplog.text
    assert "test-log-id" in caplog.text
    assert "private-test-secret" not in caplog.text
    assert "dev@example.org" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("private-failure"),
        FeishuDeliveryUncertainError("private-failure"),
        FeishuBotError("private-failure"),
        RuntimeError("private-failure"),
    ],
)
@pytest.mark.parametrize("push", [False, True])
async def test_uncertain_or_generic_failures_do_not_retry(failure, push, monkeypatch, caplog):
    h = DeliveryHarness()

    def handler(request):
        body = h.request(request)
        if isinstance(body, httpx.Response):
            return body
        raise failure

    async with httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = FeishuBotClient(h.effective.config, client=http)
        if push:
            h.install_push(monkeypatch, client)
            result = await bot_api.push_to_tab("tab", "body", title="title")
        else:
            result = await h.dispatcher()._reply_if_current(
                client, event(), h.effective, "body", formatted=True
            )
    assert result is False
    assert len(h.requests) == 1
    assert h.records == []
    assert type(failure).__name__ in caplog.text
    assert "private-failure" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [None, "Notice"])
@pytest.mark.parametrize("invalidated", [None, "current", "binding"])
async def test_proactive_post_fallback_rechecks_binding(title, invalidated, monkeypatch, caplog):
    h = DeliveryHarness()

    def handler(request):
        body = h.request(request)
        if isinstance(body, httpx.Response):
            return body
        if body["msg_type"] != "text":
            if body["msg_type"] == "post":
                if invalidated == "current":
                    h.current = False
                if invalidated == "binding":
                    h.valid = False
            return rejected()
        return httpx.Response(200, json={"code": 0})

    async with httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = FeishuBotClient(h.effective.config, client=http)
        h.install_push(monkeypatch, client)
        result = await bot_api.push_to_tab("tab", "body", title=title)
    expected = (["interactive"] if title else []) + ["post"]
    if invalidated is None:
        expected += ["text"]
    assert result == (invalidated is None)
    assert [r["msg_type"] for r in h.requests] == expected
    assert "code 230099" in caplog.text and "test-log-id" in caplog.text
    assert "invalid post content" in caplog.text
    assert "private-test-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("push", [False, True])
async def test_already_plain_payload_rejection_is_not_retried(push, monkeypatch, caplog):
    h = DeliveryHarness()

    def handler(request):
        body = h.request(request)
        return body if isinstance(body, httpx.Response) else rejected()

    async with httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = FeishuBotClient(h.effective.config, client=http)
        if push:
            h.install_push(monkeypatch, client)
            result = await bot_api.push_to_tab("tab", "unclosed\n```")
        else:
            result = await h.dispatcher()._reply_if_current(
                client, event(), h.effective, "unclosed\n```", formatted=True
            )
    assert result is False
    assert [r["msg_type"] for r in h.requests] == ["text"]
    assert "code 230099" in caplog.text and "test-log-id" in caplog.text


def test_incident_like_markdown_remains_well_formed_and_bounded():
    # Synthetic structure only: the historical response body is unavailable,
    # so this checks local representation, not Feishu's undocumented validator.
    text = (
        "**Report ready**\n\n- **History checked**:\n"
        "  - Identity: `Example <dev@example.org>`\n"
        "  - Command: `git fsck`; status **0 matches**.\n"
        "1. **Next**: review `main`.\n2. Or keep the current revision."
    )
    payload = build_feishu_message_payload(text)
    assert payload.msg_type == "post"
    encoded = json.dumps(payload.content, ensure_ascii=False)
    assert len(encoded.encode()) < 3000
    assert json.loads(encoded)["zh_cn"]["content"][0][0] == {"tag": "md", "text": text}


@pytest.mark.asyncio
@pytest.mark.parametrize("push", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("private-failure"),
        FeishuDeliveryUncertainError("private-failure"),
        FeishuBotRemoteError(
            "text", code=230001, remote_message="text rejected", log_id="text-log"
        ),
    ],
)
async def test_plain_fallback_failure_is_reported_without_further_retry(
    push, failure, monkeypatch, caplog
):
    h = DeliveryHarness()

    def handler(request):
        body = h.request(request)
        if isinstance(body, httpx.Response):
            return body
        if body["msg_type"] == "post":
            return rejected()
        raise failure

    async with httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    ) as http:
        client = FeishuBotClient(h.effective.config, client=http)
        if push:
            h.install_push(monkeypatch, client)
            result = await bot_api.push_to_tab("tab", "body")
        else:
            result = await h.dispatcher()._reply_if_current(
                client, event("thread"), h.effective, "body", formatted=True
            )
    assert result is False
    assert [r["msg_type"] for r in h.requests] == ["post", "text"]
    assert not h.records
    if isinstance(failure, FeishuBotRemoteError):
        assert "code 230001" in caplog.text
        assert "text rejected" in caplog.text and "text-log" in caplog.text
    else:
        assert type(failure).__name__ in caplog.text
        assert "private-failure" not in caplog.text
