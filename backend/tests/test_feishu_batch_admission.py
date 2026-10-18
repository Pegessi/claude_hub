"""Every coalesced message must pass admission before any native handoff."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from claude_hub.api import feishu_bot as bot_api
from claude_hub.services.feishu_bot import FeishuMessageEvent
from claude_hub.services.feishu_bot_pool import dedup_key
from tests.test_feishu_bot_admission_guard import complete, env, install_stream, no_live_http
from tests.test_feishu_message_redelivery import intake


def _batch_payloads(
    intake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, offsets: list[int]
) -> tuple[list[FeishuMessageEvent], list[SimpleNamespace]]:
    events = [
        replace(
            intake.event,
            event_id=f"evt-{index}",
            message_id=f"om-{index}",
            text=f"batch message {index}",
            message_created_at_ms=intake.event.message_created_at_ms + offset,
        )
        for index, offset in enumerate(offsets, start=1)
    ]
    payloads = [
        SimpleNamespace(event=intake.sdk_event.event, message_event=event) for event in events
    ]
    monkeypatch.setattr(
        bot_api, "message_event_from_sdk", lambda payload, **kwargs: payload.message_event
    )
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.02)
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.08)
    return events, payloads


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_at", ["subscribe", "goal"])
@pytest.mark.parametrize(
    "boundary_index,rollback",
    [
        pytest.param(2, False, id="valid-future-follower"),
        pytest.param(0, True, id="invalid-leader"),
        pytest.param(1, True, id="invalid-middle"),
        pytest.param(2, True, id="invalid-last"),
    ],
)
async def test_every_batch_member_is_rechecked_before_handoff(
    intake: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    wait_at: str,
    boundary_index: int,
    rollback: bool,
) -> None:
    env = intake.env
    offsets = [0, 0, 0]
    offsets[boundary_index] = 4900
    events, payloads = _batch_payloads(intake, monkeypatch, offsets)
    binding = env.pool.snapshot().get(env.bot_id).binding
    assert all(bot_api._message_matches_binding_time(event, binding) for event in events)
    submitted: list[str] = []
    release = asyncio.Event()
    goal_lock = asyncio.Lock()
    held_goal = wait_at == "goal"
    if held_goal:
        await goal_lock.acquire()
        release.set()

    async def on_send(payload: Any, queue: Any) -> None:
        assert bot_api._bot_gates[env.bot_id].lock.locked()
        submitted.append(payload.text)
        complete(queue, payload.client_turn_id)

    native = install_stream(monkeypatch, on_send, subscribe_release=release, goal_lock=goal_lock)
    jobs = [
        asyncio.create_task(bot_api._handle_sdk_event(env.bot_id, payload)) for payload in payloads
    ]
    try:
        await asyncio.wait_for(native.subscribed.wait(), timeout=5)
        assert [
            item.event.message_id for item in bot_api._tab_dispatch_queues["tab-target"].active
        ] == [event.message_id for event in events]
        if rollback:
            env.clock[0] -= 0.2
            assert not bot_api._message_matches_binding_time(events[boundary_index], binding)
        if held_goal:
            goal_lock.release()
            held_goal = False
        release.set()
        await asyncio.wait_for(asyncio.gather(*jobs), timeout=5)
    finally:
        if held_goal:
            goal_lock.release()
        release.set()
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    records = json.loads(intake.path.read_text())["events"]
    if rollback:
        assert submitted == []
        assert len(env.replies) == 1
        assert "尚未执行" in env.replies[0][1]
        for event in events:
            record = records[dedup_key(env.bot_id, event.message_id)]
            assert record["status"] == "pre_dispatch_rejected"
            assert record["stage"] == "pre_dispatch"
            assert record["reason"] == "message_future_over_skew"
            assert record.get("attempts", 1) == 1
        env.clock[0] += 1
        await asyncio.wait_for(
            asyncio.gather(
                *(bot_api._handle_sdk_event(env.bot_id, payload) for payload in payloads)
            ),
            timeout=5,
        )
    assert len(submitted) == 1
    assert all(event.text in submitted[0] for event in events)
    assert len(env.replies) == (2 if rollback else 1)
    records = json.loads(intake.path.read_text())["events"]
    for event in events:
        record = records[dedup_key(env.bot_id, event.message_id)]
        assert record["status"] == "completed"
        assert record.get("attempts", 1) == (2 if rollback else 1)
    await asyncio.gather(*(bot_api._handle_sdk_event(env.bot_id, payload) for payload in payloads))
    assert len(submitted) == 1
    assert len(env.replies) == (2 if rollback else 1)
    assert bot_api._tab_dispatch_queues == {}


@pytest.mark.asyncio
async def test_cancelled_batch_rejection_notice_does_not_allow_redelivery(
    intake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = intake.env
    events, payloads = _batch_payloads(intake, monkeypatch, [0, 4900, 0])
    submitted: list[str] = []
    release = asyncio.Event()
    posting = asyncio.Event()
    finish_post = asyncio.Event()

    async def on_send(payload: Any, queue: Any) -> None:
        submitted.append(payload.text)
        complete(queue, payload.client_turn_id)

    async def blocked_token(client: Any) -> str:
        posting.set()
        await finish_post.wait()
        return "test-tenant-token"

    monkeypatch.setattr(bot_api.FeishuBotClient, "get_tenant_token", blocked_token)
    native = install_stream(monkeypatch, on_send, subscribe_release=release)
    jobs = [
        asyncio.create_task(bot_api._handle_sdk_event(env.bot_id, payload)) for payload in payloads
    ]
    try:
        await asyncio.wait_for(native.subscribed.wait(), timeout=5)
        env.clock[0] -= 0.2
        release.set()
        await asyncio.wait_for(posting.wait(), timeout=5)
        jobs[0].cancel()
        results = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), timeout=5)
        assert isinstance(results[0], asyncio.CancelledError)
    finally:
        release.set()
        finish_post.set()
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    records = json.loads(intake.path.read_text())["events"]
    assert [records[dedup_key(env.bot_id, event.message_id)]["status"] for event in events] == [
        "reply_failed",
        "reply_failed",
        "reply_failed",
    ]
    env.clock[0] += 1
    await asyncio.gather(*(bot_api._handle_sdk_event(env.bot_id, payload) for payload in payloads))
    assert submitted == []
    assert env.replies == []
    assert bot_api._tab_dispatch_queues == {}


@pytest.mark.asyncio
async def test_batch_unbound_before_admission_never_reaches_native(
    intake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = intake.env
    _, payloads = _batch_payloads(intake, monkeypatch, [0, 0, 0])
    submitted: list[str] = []
    release = asyncio.Event()

    async def on_send(payload: Any, queue: Any) -> None:
        submitted.append(payload.text)
        complete(queue, payload.client_turn_id)

    native = install_stream(monkeypatch, on_send, subscribe_release=release)
    jobs = [
        asyncio.create_task(bot_api._handle_sdk_event(env.bot_id, payload)) for payload in payloads
    ]
    try:
        await asyncio.wait_for(native.subscribed.wait(), timeout=5)
        env.pool.release_binding(
            env.bot_id, expected_revision=env.pool.snapshot().get(env.bot_id).revision
        )
        release.set()
        await asyncio.wait_for(asyncio.gather(*jobs), timeout=5)
    finally:
        release.set()
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
    assert submitted == []
    assert env.replies == []
    assert bot_api._tab_dispatch_queues == {}
