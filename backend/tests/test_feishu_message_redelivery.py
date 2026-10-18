"""Exercise real Feishu intake, admission and deduplication with mocked boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from claude_hub.api import feishu_bot as bot_api
from claude_hub.services.feishu_bot import FeishuMessageDedupStore
from claude_hub.services.feishu_bot_pool import dedup_key
from tests.test_feishu_bot_admission_guard import (
    activate,
    complete,
    env,
    install_stream,
    message,
    no_live_http,
)


@pytest.fixture()
def intake(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> SimpleNamespace:
    activate(env)
    env.clock[0] += 10
    event = replace(message(), message_created_at_ms=int(env.clock[0] * 1000))
    path = tmp_path / "events.json"
    store = FeishuMessageDedupStore(path=path, now=lambda: env.clock[0])
    monkeypatch.setattr(bot_api, "_dedup", store)
    monkeypatch.setattr(bot_api, "_tab_dispatch_queues", {})
    monkeypatch.setattr(bot_api.settings, "feishu_message_future_skew_seconds", 5.0)
    monkeypatch.setattr(bot_api, "message_event_from_sdk", lambda *args, **kwargs: event)
    monkeypatch.setattr(bot_api, "_validate_saved_target", lambda binding: None)
    monkeypatch.setattr(bot_api, "_maintain_processing_reaction", AsyncMock(return_value=None))

    async def profile(effective: Any, client: Any, **kwargs: Any) -> Any:
        return effective

    async def reply_message(
        client: Any, message_id: str, msg_type: str, content: Any, **kwargs: Any
    ) -> None:
        env.replies.append((message_id, "assistant response"))

    monkeypatch.setattr(bot_api, "_refresh_missing_profile", profile)
    monkeypatch.setattr(bot_api.FeishuBotClient, "reply_message", reply_message, raising=False)
    sdk_event = SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(chat_type="p2p")))
    return SimpleNamespace(env=env, event=event, sdk_event=sdk_event, path=path)


@pytest.mark.asyncio
async def test_time_rejection_at_native_admission_is_visible_and_can_redeliver_once(
    intake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = intake.env
    submitted: list[str] = []
    release = asyncio.Event()

    async def on_send(payload: Any, queue: Any) -> None:
        submitted.append(payload.client_turn_id)
        complete(queue, payload.client_turn_id)

    native = install_stream(monkeypatch, on_send, subscribe_release=release)
    task = asyncio.create_task(bot_api._handle_sdk_event(env.bot_id, intake.sdk_event))
    try:
        await asyncio.wait_for(native.subscribed.wait(), timeout=5)
        env.clock[0] -= 6
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert submitted == []
    assert len(env.replies) == 1
    assert "尚未执行" in env.replies[0][1]
    record = json.loads(intake.path.read_text())["events"][
        dedup_key(env.bot_id, intake.event.message_id)
    ]
    assert record["status"] == "pre_dispatch_rejected"
    assert record["stage"] == "pre_dispatch"
    assert record["reason"] == "message_future_over_skew"

    env.clock[0] += 7
    await asyncio.wait_for(bot_api._handle_sdk_event(env.bot_id, intake.sdk_event), timeout=5)
    assert len(submitted) == 1
    assert env.replies[-1][1] == "assistant response"
    await asyncio.wait_for(bot_api._handle_sdk_event(env.bot_id, intake.sdk_event), timeout=5)
    assert len(submitted) == 1
    assert len(env.replies) == 2


@pytest.mark.asyncio
async def test_provider_error_with_matching_headers_does_not_authorize_redelivery(
    intake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []

    async def on_send(payload: Any, queue: Any) -> None:
        submitted.append(payload.client_turn_id)
        raise HTTPException(
            status_code=409,
            detail="message_outside_binding_period",
            headers={
                bot_api.CHAT_ERROR_REASON_HEADER: "message_outside_binding_period",
                "X-Claude-Hub-Feishu-Reject-Reason": "message_future_over_skew",
            },
        )

    install_stream(monkeypatch, on_send)
    await bot_api._handle_sdk_event(intake.env.bot_id, intake.sdk_event)
    await bot_api._handle_sdk_event(intake.env.bot_id, intake.sdk_event)
    assert len(submitted) == 1
    record = json.loads(intake.path.read_text())["events"][
        dedup_key(intake.env.bot_id, intake.event.message_id)
    ]
    assert record["status"] == "failed"
    assert record["stage"] == "native_dispatch"
