"""Unit tests for real-time Feishu WebSocket event routing, courtesy/self-reply suppression, and /ai isolation."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.dot.events_gateway import ExternalEvent
from claude_hub.services.feishu_ai_command import (
    FeishuAiCommandService,
    FeishuMessageDedupStore,
)
from claude_hub.services.feishu_ai_poller import FeishuAiPoller
from claude_hub.services.feishu_realtime_events import (
    FeishuRealtimeEventConfig,
    FeishuRealtimeEventRouter,
    extract_mentioned_user_ids,
    extract_message_urls,
    is_courtesy_or_ack_only,
)
from claude_hub.services.feishu_user_client import (
    FeishuMessagePage,
    FeishuUserClient,
    FeishuUserMessage,
)
from claude_hub.services.feishu_user_token_store import FeishuUserTokenStore
from claude_hub.services.feishu_web_session.client import (
    CMD_PUSH_MESSAGES,
    PushedMessage,
    build_frontier_frame,
    parse_frontier_frame,
)
from claude_hub.services.feishu_web_session.codec import decode, encode
from claude_hub.services.feishu_web_session.rich import (
    CHAT_GROUP,
    CHAT_P2P,
    TYPE_POST,
    TYPE_TEXT,
    L,
    Node,
    T,
    WebChat,
    WebChatter,
    WebMessage,
    rich_text,
)
from claude_hub.services.feishu_web_session.store import (
    FeishuWebSession,
    FeishuWebSessionStore,
)

OWNER_WEB_UID = "7100000000000000001"
OWNER_OPEN_ID = "ou_owner_synthetic_1"
COLLEAGUE_WEB_UID = "7100000000000000002"
COLLEAGUE_OPEN_ID = "ou_colleague_synthetic_2"
ORDINARY_CHAT_WEB_ID = "7300000000000000010"
PRIORITY_CHAT_WEB_ID = "7300000000000000020"
PRIORITY_CHAT_OPEN_ID = "oc_priority_watch_group"
P2P_CHAT_WEB_ID = "7300000000000000030"


from tests.feishu_realtime_support import (
    _build_pushed_via_wire,
    _encode_rich_message,
    _setup_poller,
)


@pytest.mark.asyncio
async def test_c4_r2_same_second_same_text_collision_and_thread_mismatch_not_merged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4-R2 regression: same-chat/same-second/same-text messages from Alice & Bob or different threads must not falsely deduplicate or clear fallback."""
    from claude_hub.services.demand_patrol import feishu_triage as triage_mod
    from claude_hub.services.demand_patrol.feishu_triage import _candidate, filter_unanswered
    from claude_hub.services.feishu_web_session.rich import WebChat

    call_n = 0

    async def sink_first_ok_second_fail(_ev: ExternalEvent) -> bool:
        nonlocal call_n
        call_n += 1
        return call_n == 1

    poller, _svc, uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(enabled=True),
        event_sink=sink_first_ok_second_fail,
    )
    wt = uc._get_web_transport()
    assert wt is not None
    wt._remember_chat(
        WebChat(id=ORDINARY_CHAT_WEB_ID, open_chat_id="oc_group_1", name="研发排障群")
    )
    monkeypatch.setattr(
        triage_mod, "get_feishu_realtime_event_router", lambda: poller.realtime_router
    )

    # 1. Alice (710...2) and Bob (710...9) send the exact same @Owner question in the same chat at the same second.
    # Alice's Web event succeeds; Bob's Web event fails.
    alice_pushed = _build_pushed_via_wire(
        msg_id="7400000000000009201",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 线上服务挂了帮忙看下？")],
        create_time_s=1_760_009_200,
        at_me=True,
    )
    bob_pushed = _build_pushed_via_wire(
        msg_id="7400000000000009202",
        from_id="7100000000000000009",
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 线上服务挂了帮忙看下？")],
        create_time_s=1_760_009_200,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(alice_pushed) is True
    assert await poller.handle_pushed_web_message(bob_pushed) is False
    assert poller.realtime_router.pending_fallback_count == 1

    # Patrol sees Bob's OpenAPI candidate: must NOT be suppressed by Alice's handled Web record!
    bob_cand = _candidate(
        {
            "message_id": "om_bob_question_9202",
            "chat_id": "oc_group_1",
            "chat_name": "研发排障群",
            "chat_type": "group",
            "thread_id": "",
            "create_time": "1760009200000",
            "sender": {"id": "ou_bob", "name": "Bob"},
            "content": f'<at user_id="{OWNER_OPEN_ID}">@Owner</at> 线上服务挂了帮忙看下？',
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    cands, answered = filter_unanswered([bob_cand], OWNER_OPEN_ID, [], realtime_healthy=True)
    assert [c.message_id for c in cands] == ["om_bob_question_9202"]
    assert answered == []

    # 2. Cross-namespace thread_id mismatch (numeric Web topic vs OpenAPI omt_...) must NOT match
    router2 = FeishuRealtimeEventRouter()
    router2._record_handled(
        "7400000000000009210",
        "7400000000000009210",
        {"oc_group_1"},
        "研发排障群",
        {"7399999999999999999"},
        {"7100000000000000002"},
        "@Owner 话题报错排查",
        "",
        1_760_009_210_000,
    )
    assert (
        router2.is_message_handled(
            "om_different_topic_msg",
            chat_id="oc_group_1",
            chat_name="研发排障群",
            thread_id="omt_other_topic",
            sender_id="ou_alice",
            text="@Owner 话题报错排查",
            created_at_ms=1_760_009_210_000,
        )
        is False
    )


@pytest.mark.asyncio
async def test_c4_r3_duplicate_durable_ingress_receipt_treated_as_handled_not_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4-R3 regression: when dedupe_key already exists in Dot ingress SQLite, realtime router records handled without creating failed_delivery fallback."""
    from claude_hub.services.demand_patrol import feishu_triage as triage_mod
    from claude_hub.services.demand_patrol.feishu_triage import _candidate, filter_unanswered
    from claude_hub.services.dot.events_gateway import submit_external_event
    from claude_hub.services.feishu_web_session.rich import WebChat

    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr(dot, "is_set_up", lambda: True)
    dot.wake_for_external_event = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)

    poller, _svc, uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(
            enabled=True,
            suppress_patrol_when_healthy=True,
        ),
        event_sink=None,
    )
    wt = uc._get_web_transport()
    assert wt is not None
    wt._remember_chat(
        WebChat(id=ORDINARY_CHAT_WEB_ID, open_chat_id="oc_group_1", name="研发排障群")
    )
    monkeypatch.setattr(
        triage_mod, "get_feishu_realtime_event_router", lambda: poller.realtime_router
    )

    # Pre-insert durable receipt in Dot ingress (e.g. from external bridge or prior process run before restart)
    pre_event = ExternalEvent(
        source="feishu",
        event_type="im.mention",
        dedupe_key="7400000000000009301",
        severity="urgent",
        title="Feishu mention",
        summary="研发排障群 · ou_alice · @Owner 线上报错请看下",
        payload={
            "chat_id": "oc_group_1",
            "message_id": "7400000000000009301",
            "text": "@Owner 线上报错请看下",
        },
    )
    assert await submit_external_event(pre_event, dot) is True
    assert dot.wake_for_external_event.await_count == 1

    # Now WebSocket push arrives for the same message (aliased to 7400000000000009301)
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000009301",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 线上报错请看下")],
        create_time_s=1_760_009_301,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(pushed) is True
    # No second wake, no failed_delivery fallback, and message is recorded as handled!
    assert dot.wake_for_external_event.await_count == 1
    assert poller.realtime_router.pending_fallback_count == 0
    assert poller.realtime_router.needs_patrol_fallback is False
    assert poller.realtime_router.is_message_handled("7400000000000009301") is True

    cand = _candidate(
        {
            "message_id": "7400000000000009301",
            "chat_id": "oc_group_1",
            "chat_name": "研发排障群",
            "chat_type": "group",
            "create_time": "1760009301000",
            "sender": {"id": "ou_alice", "name": "Alice"},
            "content": f'<at user_id="{OWNER_OPEN_ID}">@Owner</at> 线上报错请看下',
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    cands, answered = filter_unanswered([cand], OWNER_OPEN_ID, [], realtime_healthy=True)
    assert cands == []
    assert answered == ["7400000000000009301"]


@pytest.mark.asyncio
async def test_c5_r2_renamed_watch_group_refreshes_cached_chat_metadata_and_routes_url(
    tmp_path: Path,
) -> None:
    """C5-R2 regression: when a cached ordinary group is renamed to a configured watch_chat_names entry, subsequent URL messages refresh chat metadata and trigger im.watch_chat_url."""
    from claude_hub.services.feishu_web_session.rich import WebChat

    emitted: list[ExternalEvent] = []

    async def sink(ev: ExternalEvent) -> bool:
        emitted.append(ev)
        return True

    poller, _svc, uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(
            enabled=True,
            watch_chat_names=frozenset({"priority group"}),
            actionable_hosts=frozenset({" tickets.example.com ".strip()}),
        ),
        event_sink=sink,
    )
    wt = uc._get_web_transport()
    assert wt is not None

    current_name = "Ordinary group"
    lookup_names: list[str] = []

    async def fake_chats(chat_ids: list[str]) -> dict[str, WebChat]:
        lookup_names.append(current_name)
        return {
            cid: WebChat(id=cid, open_chat_id="oc_renamed_group", name=current_name)
            for cid in chat_ids
        }

    wt.web_client.chats = fake_chats  # type: ignore[method-assign]

    # 1. Initial URL message while group is named "Ordinary group" -> cached and ignored
    first_push = _build_pushed_via_wire(
        msg_id="7400000000000006951",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("https://tickets.example.com/issues/501")],
        create_time_s=1_760_009_551,
        at_me=False,
    )
    assert await poller.handle_pushed_web_message(first_push) is False
    assert len(emitted) == 0
    assert lookup_names == ["Ordinary group"]

    # 2. Group is renamed on Feishu to "Priority group"
    current_name = "Priority group"
    second_push = _build_pushed_via_wire(
        msg_id="7400000000000006952",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("https://tickets.example.com/issues/502")],
        create_time_s=1_760_009_552,
        at_me=False,
    )
    assert await poller.handle_pushed_web_message(second_push) is True
    assert len(emitted) == 1
    assert emitted[0].event_type == "im.watch_chat_url"
    assert "Priority group" in lookup_names
    assert poller.realtime_router.watched_openapi_chat_ids() == ("oc_renamed_group",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other_sender_id", "other_sender_name"),
    [
        ("ou_bob", "Bob"),
        ("ou_carol", "Carol"),
        ("ou_dave_synthetic_99", "Dave"),
        ("ou_xavier_unfamiliar", "Alice"),
    ],
)
async def test_c6_r1_and_c6_r2_alpha_renamed_senders_and_populated_cache_unrelated_topics_not_merged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    other_sender_id: str,
    other_sender_name: str,
) -> None:
    """C6-R1 & C6-R2 regression:
    - Any unfamiliar/renamed OpenAPI sender (Bob, Carol, Dave, or same display name with different ou_...) must NOT match a lone handled or failed Web record from another user.
    - Even with a verified sender alias and a populated real transport _msg_cache, an unrelated unaliased OpenAPI topic (omt_...) must NOT match a Web topic record.
    """
    from claude_hub.services.demand_patrol import feishu_triage as triage_mod
    from claude_hub.services.demand_patrol.feishu_triage import _candidate, filter_unanswered
    from claude_hub.services.feishu_web_session.rich import WebChat

    should_fail = False

    async def sink(_ev: ExternalEvent) -> bool:
        return not should_fail

    poller, _svc, uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(
            enabled=True,
            suppress_patrol_when_healthy=True,
        ),
        event_sink=sink,
    )
    wt = uc._get_web_transport()
    assert wt is not None
    wt._remember_chat(
        WebChat(id=ORDINARY_CHAT_WEB_ID, open_chat_id="oc_group_1", name="研发排障群")
    )
    monkeypatch.setattr(
        triage_mod, "get_feishu_realtime_event_router", lambda: poller.realtime_router
    )

    # 1. C6-R1(a): Handled Web message from Alice (COLLEAGUE_WEB_UID) must not suppress other_sender_id's same-second/same-text candidate when Alice's row is absent
    alice_ok = _build_pushed_via_wire(
        msg_id="7400000000000007101",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 同一秒不同人去重测试？")],
        create_time_s=1_760_009_601,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(alice_ok) is True

    other_cand = _candidate(
        {
            "message_id": f"om_other_{other_sender_id}",
            "chat_id": "oc_group_1",
            "chat_name": "研发排障群",
            "chat_type": "group",
            "create_time": "1760009601000",
            "sender": {"id": other_sender_id, "name": other_sender_name},
            "content": f'<at user_id="{OWNER_OPEN_ID}">@Owner</at> 同一秒不同人去重测试？',
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    cands_a, answered_a = filter_unanswered([other_cand], OWNER_OPEN_ID, [], realtime_healthy=True)
    assert [c.message_id for c in cands_a] == [f"om_other_{other_sender_id}"]
    assert answered_a == []

    # 2. C6-R1(b): Failed Web delivery from Alice must not be cleared when patrol recovers only other_sender_id's candidate
    should_fail = True
    alice_fail = _build_pushed_via_wire(
        msg_id="7400000000000007102",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 同一秒不同人恢复测试？")],
        create_time_s=1_760_009_602,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(alice_fail) is False
    assert poller.realtime_router.pending_fallback_count == 1

    other_recovered = _candidate(
        {
            "message_id": f"om_rec_{other_sender_id}",
            "chat_id": "oc_group_1",
            "chat_name": "研发排障群",
            "chat_type": "group",
            "create_time": "1760009602000",
            "sender": {"id": other_sender_id, "name": other_sender_name},
            "content": f'<at user_id="{OWNER_OPEN_ID}">@Owner</at> 同一秒不同人恢复测试？',
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    poller.realtime_router.acknowledge_patrol_fallback(
        recovered_ids=[f"om_rec_{other_sender_id}"],
        recovered_candidates=[other_recovered],
    )
    assert poller.realtime_router.pending_fallback_count == 1
    assert poller.realtime_router.is_message_handled("7400000000000007102") is False

    # 3. C6-R2: Verified sender (ou_colleague <-> COLLEAGUE_WEB_UID) and populated _msg_cache,
    #    but unrelated unaliased OpenAPI topic (omt_unrelated_topic) must NOT match Web topic 7400000000000007100
    should_fail = False
    topic_push = _build_pushed_via_wire(
        msg_id="7400000000000007103",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        root_id="7400000000000007100",
        parent_id="7400000000000007100",
        thread_id="7400000000000007100",
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 话题报错隔离测试？")],
        create_time_s=1_760_009_603,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(topic_push) is True
    assert len(wt._msg_cache) > 0

    unrelated_topic_cand = _candidate(
        {
            "message_id": "om_unrelated_topic_msg",
            "chat_id": "oc_group_1",
            "chat_name": "研发排障群",
            "chat_type": "group",
            "thread_id": "omt_unrelated_topic",
            "reply_to": "om_unrelated_root",
            "create_time": "1760009603000",
            "sender": {"id": "ou_colleague", "name": "Alice"},
            "content": f'<at user_id="{OWNER_OPEN_ID}">@Owner</at> 话题报错隔离测试？',
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    cands_topic, answered_topic = filter_unanswered(
        [unrelated_topic_cand], OWNER_OPEN_ID, [], realtime_healthy=True
    )
    assert [c.message_id for c in cands_topic] == ["om_unrelated_topic_msg"]
    assert answered_topic == []


@pytest.mark.asyncio
async def test_unseeded_web_push_and_position_replay_use_one_identity(tmp_path: Path) -> None:
    from claude_hub.services.feishu_web_session.rich import FeedCard, FeedPage

    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, _service, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    wt = client._get_web_transport()
    assert wt is not None
    poller._active_web_transport = wt
    poller._frontier_connected = True
    chat = WebChat(id=ORDINARY_CHAT_WEB_ID, type=CHAT_GROUP, last_position=0)
    wt._web.feed_cards = AsyncMock(
        return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
    )
    await poller._run_web_replay()
    first = _build_pushed_via_wire(
        msg_id="7400000000000007777",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=chat.id,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 服务故障请帮忙排查")],
        create_time_s=1_760_009_000,
        position=1,
        at_me=True,
    )
    missed = _build_pushed_via_wire(
        msg_id="7400000000000007778",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=chat.id,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 第二个服务故障请帮忙排查")],
        create_time_s=1_760_009_001,
        position=2,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(first)
    later = WebChat(id=chat.id, type=CHAT_GROUP, last_position=2)
    wt._web.feed_cards.return_value = FeedPage(
        cards=(FeedCard(id=chat.id),), chats={chat.id: later}
    )
    wt._web.messages_at = AsyncMock(return_value=[first.message, missed.message])
    wt._web.topics = AsyncMock(return_value={})
    await poller._run_web_replay()
    assert [event.dedupe_key for event in emitted] == [first.message.id, missed.message.id]
    assert poller._replay_ready


@pytest.mark.asyncio
async def test_replayed_message_uses_protocol_at_user_ids(tmp_path: Path) -> None:
    from claude_hub.services.feishu_web_session.rich import message_of

    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, _service, _client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000007779",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("服务故障请帮忙排查")],
        at_me=False,
    )
    parsed = message_of({"atUserIdList": [OWNER_WEB_UID]})
    pushed = replace(pushed, message=replace(pushed.message, at_user_ids=parsed.at_user_ids))
    assert await poller.handle_pushed_web_message(pushed)
    assert [event.event_type for event in emitted] == ["im.mention"]


@pytest.mark.asyncio
async def test_p2p_topic_does_not_require_participation_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, _service, _client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    lookup = AsyncMock(side_effect=RuntimeError("lookup unavailable"))
    monkeypatch.setattr("claude_hub.services.feishu_realtime_events.owner_participated", lookup)
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000007781",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("服务故障请帮忙排查")],
    )
    pushed = replace(
        pushed,
        message=replace(pushed.message, thread_id="7500000000000007781", topic_position=1),
    )
    assert await poller.handle_pushed_web_message(pushed)
    lookup.assert_not_awaited()
    assert emitted[0].event_type == "im.p2p"


@pytest.mark.asyncio
async def test_failed_topic_participation_retains_recovery_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poller, _service, _client = _setup_poller(tmp_path, realtime_config=FeishuRealtimeEventConfig())
    monkeypatch.setattr(
        "claude_hub.services.feishu_realtime_events.owner_participated",
        AsyncMock(side_effect=RuntimeError("lookup unavailable")),
    )
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000007782",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        chat_type=CHAT_GROUP,
        nodes=[T("服务故障请帮忙排查")],
    )
    pushed = replace(
        pushed,
        message=replace(pushed.message, thread_id="7500000000000007782", topic_position=1),
    )
    assert not await poller.handle_pushed_web_message(pushed)
    assert poller.realtime_router.needs_patrol_fallback


@pytest.mark.asyncio
async def test_web_frontier_does_not_enqueue_ai_in_openapi_mode(tmp_path: Path) -> None:
    poller, service, _client = _setup_poller(tmp_path, realtime_config=FeishuRealtimeEventConfig())
    service.update_config({"send_mode": "openapi"})
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000007783",
        from_id=OWNER_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("/ai 请处理")],
    )
    assert not await poller.handle_pushed_web_message(pushed)
    assert not service.list_pending_commands()


@pytest.mark.asyncio
async def test_openapi_ai_mode_keeps_configured_realtime_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poller, service, _client = _setup_poller(tmp_path, realtime_config=FeishuRealtimeEventConfig())
    service.update_config({"send_mode": "openapi"})
    monkeypatch.setattr(poller, "web_replay_configured", lambda: True)
    monkeypatch.setattr(poller, "start", AsyncMock())
    # The production call is synchronous; capture its argument without scheduling work.
    captured: list[bool] = []
    monkeypatch.setattr(
        poller, "_ensure_frontier_task", lambda cfg, **kw: captured.append(kw["web_configured"])
    )
    await poller.reconcile()
    assert captured == [True]


@pytest.mark.asyncio
async def test_disabled_frontier_clears_tasks_and_transport(tmp_path: Path) -> None:
    poller, service, client = _setup_poller(tmp_path)
    service.update_config({"enabled": False})
    poller._active_web_transport = client._get_web_transport()
    tasks = [asyncio.create_task(asyncio.sleep(60)) for _ in range(3)]
    poller._frontier_task, poller._replay_task, poller._realtime_queue_worker = tasks
    poller._ensure_frontier_task(service.get_config(), web_configured=False)
    assert poller._frontier_task is None
    assert poller._replay_task is None
    assert poller._realtime_queue_worker is None
    assert poller._active_web_transport is None
    await asyncio.gather(*tasks, return_exceptions=True)


def test_ai_reply_only_suppresses_its_direct_target() -> None:
    router = FeishuRealtimeEventRouter(config=FeishuRealtimeEventConfig())
    parent = "7400000000000007784"
    pushed = PushedMessage(
        WebMessage(
            id="7400000000000007785",
            chat_id=ORDINARY_CHAT_WEB_ID,
            parent_id=parent,
            create_time_s=2,
        )
    )
    own = FeishuUserMessage(
        message_id=pushed.message.id,
        chat_id=ORDINARY_CHAT_WEB_ID,
        sender_open_id=OWNER_WEB_UID,
        sender_type="user",
        msg_type="text",
        text="/ai please help",
        create_time_ms=2000,
        parent_id=parent,
    )
    router.record_owner_pushed_message(pushed, own)
    assert router.has_owner_replied(
        chat_id=ORDINARY_CHAT_WEB_ID, message_id=parent, created_at_ms=1000
    )
    assert not router.has_owner_replied(
        chat_id=ORDINARY_CHAT_WEB_ID,
        message_id="7400000000000007786",
        reply_to=parent,
        created_at_ms=1000,
    )


@pytest.mark.asyncio
async def test_frontier_keeps_journal_if_worker_already_popped(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled(_event: ExternalEvent) -> bool:
        entered.set()
        await release.wait()
        return True

    poller, _service, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=stalled
    )
    poller._active_web_transport = client._get_web_transport()
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000007780",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("服务故障请帮忙排查")],
    )
    original = poller.handle_pushed_web_message

    async def yield_to_worker(*args: Any, **kwargs: Any) -> bool:
        result = await original(*args, **kwargs)
        await asyncio.wait_for(entered.wait(), 2)
        return result

    poller.handle_pushed_web_message = yield_to_worker  # type: ignore[method-assign]
    await poller._handle_frontier_pushed_web_message(pushed)
    assert pushed.message.id not in poller._realtime_event_queue
    assert [item.message.id for item in poller._web_journal.pending()] == [pushed.message.id]
    release.set()
    assert poller._realtime_queue_worker is not None
    await poller._realtime_queue_worker
    assert not poller._web_journal.pending()


@pytest.mark.asyncio
async def test_replay_request_during_replay_is_not_lost(tmp_path: Path) -> None:
    poller, _service, client = _setup_poller(tmp_path)
    poller._active_web_transport = client._get_web_transport()
    poller._frontier_connected = True
    entered, release, reran = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def replay() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        else:
            reran.set()

    poller._run_web_replay = replay  # type: ignore[method-assign]
    poller._schedule_web_replay()
    await asyncio.wait_for(entered.wait(), 2)
    poller._schedule_web_replay()
    release.set()
    await asyncio.wait_for(reran.wait(), 2)
    assert calls == 2


@pytest.mark.asyncio
async def test_web_replay_delivery_failure_retains_position_and_retries(tmp_path: Path) -> None:
    from claude_hub.services.feishu_web_session.rich import FeedCard, FeedPage

    accept = False
    delivered: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        if not accept:
            return False
        delivered.append(event)
        return True

    poller, _service, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    wt = client._get_web_transport()
    assert wt is not None
    poller._active_web_transport = wt
    poller._frontier_connected = True
    chat = WebChat(id=P2P_CHAT_WEB_ID, type=CHAT_P2P, last_position=0)
    wt._web.feed_cards = AsyncMock(
        return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
    )
    await poller._run_web_replay()
    missed = _build_pushed_via_wire(
        msg_id="7400000000000008888",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=chat.id,
        chat_type=CHAT_P2P,
        nodes=[T("服务报错帮忙排查")],
        position=1,
    )
    later = WebChat(id=chat.id, type=CHAT_P2P, last_position=1)
    wt._web.feed_cards.return_value = FeedPage(
        cards=(FeedCard(id=chat.id),), chats={chat.id: later}
    )
    wt._web.messages_at = AsyncMock(return_value=[missed.message])
    await poller._run_web_replay()
    assert not poller._replay_ready
    assert poller._web_replay._load()["chats"][chat.id] == 0
    accept = True
    await poller._run_web_replay()
    assert poller._replay_ready
    assert [event.dedupe_key for event in delivered] == [missed.message.id]
    assert poller._web_replay._load()["chats"][chat.id] == 1


@pytest.mark.asyncio
async def test_web_intake_does_not_depend_on_outbound_auto_send_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, service, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    service.update_config({"send_mode": "auto"})
    monkeypatch.setattr(client, "should_use_web_session", lambda: False)
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000008890",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("服务报错帮忙排查")],
        position=1,
    )
    assert await poller.handle_pushed_web_message(pushed)
    assert [event.dedupe_key for event in emitted] == [pushed.message.id]


@pytest.mark.asyncio
async def test_frontier_ack_waits_for_durable_intake(monkeypatch: pytest.MonkeyPatch) -> None:
    import claude_hub.services.feishu_web_session.client as web_client

    pushed = _build_pushed_via_wire(
        msg_id="7400000000000008891",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("服务报错")],
        position=1,
    )
    monkeypatch.setattr(web_client, "parse_frontier_frame", lambda _raw: (b"ack", 6, [pushed]))
    calls: list[str] = []

    async def deliver(_pushed: Any) -> None:
        calls.append("durable")

    async def send(_ack: bytes) -> None:
        calls.append("ack")

    listener = web_client.FeishuWebFrontierListener(None, deliver)  # type: ignore[arg-type]
    await listener.handle_raw_frame(b"frame", send)
    assert calls == ["durable", "ack"]

    async def fail(_pushed: Any) -> None:
        raise OSError("journal unavailable")

    calls.clear()
    listener = web_client.FeishuWebFrontierListener(None, fail)  # type: ignore[arg-type]
    with pytest.raises(OSError, match="journal unavailable"):
        await listener.handle_raw_frame(b"frame", send)
    assert calls == []


@pytest.mark.asyncio
async def test_unfinished_web_push_survives_poller_restart(tmp_path: Path) -> None:
    from claude_hub.services.feishu_web_session.rich import FeedPage

    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled(_event: ExternalEvent) -> bool:
        entered.set()
        await release.wait()
        return True

    first, _service, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=stalled
    )
    wt = client._get_web_transport()
    assert wt is not None
    first._active_web_transport = wt
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000008892",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("服务报错需要排查")],
        position=1,
    )
    await first._handle_frontier_pushed_web_message(pushed)
    await asyncio.wait_for(entered.wait(), 2)
    assert len(first._web_journal.pending()) == 1
    await first.quiesce()

    received: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        received.append(event)
        return True

    resumed, _service, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    wt = client._get_web_transport()
    assert wt is not None
    wt._web.feed_cards = AsyncMock(return_value=FeedPage())
    resumed._active_web_transport = wt
    resumed._frontier_connected = True
    await resumed._run_web_replay()
    assert [event.dedupe_key for event in received] == [pushed.message.id]
    assert resumed._web_journal.pending() == []


@pytest.mark.asyncio
async def test_live_push_during_first_snapshot_is_not_lost(tmp_path: Path) -> None:
    from claude_hub.services.feishu_web_session.rich import FeedCard, FeedPage

    feed_entered, finish_feed = asyncio.Event(), asyncio.Event()
    delivered: list[ExternalEvent] = []
    accept = False

    async def sink(event: ExternalEvent) -> bool:
        if not accept:
            return False
        delivered.append(event)
        return True

    poller, _service, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    wt = client._get_web_transport()
    assert wt is not None
    poller._active_web_transport = wt
    poller._frontier_connected = True
    chat = WebChat(id=P2P_CHAT_WEB_ID, type=CHAT_P2P, last_position=1)

    async def snapshot(**_kwargs: Any) -> FeedPage:
        feed_entered.set()
        await finish_feed.wait()
        return FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})

    wt._web.feed_cards = AsyncMock(side_effect=snapshot)
    scan = asyncio.create_task(poller._run_web_replay())
    await asyncio.wait_for(feed_entered.wait(), 2)
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000008893",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=chat.id,
        chat_type=CHAT_P2P,
        nodes=[T("故障请帮忙排查")],
        position=1,
    )
    await poller._handle_frontier_pushed_web_message(pushed)
    await poller.wait_for_inflight(timeout=2)
    finish_feed.set()
    await scan
    assert not poller._replay_ready
    assert len(poller._web_journal.pending()) == 1
    accept = True
    await poller._run_web_replay()
    assert poller._replay_ready
    assert [event.dedupe_key for event in delivered] == [pushed.message.id]


@pytest.mark.asyncio
async def test_runtime_switch_suppresses_realtime_without_releasing_cli_patrol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services import feishu_ai_poller
    from claude_hub.services.feishu_realtime_settings import write_realtime_enabled

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_HUB_FEISHU_REALTIME_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv("enabled", raising=False)
    settings_path = tmp_path / "feishu_realtime_events.json"
    settings_path.write_text(
        json.dumps(
            {
                "watch_chat_ids": [PRIORITY_CHAT_WEB_ID],
                "url_patterns": [r"https://compute\.example\.com/job/\d+"],
            }
        )
    )
    sink = AsyncMock(return_value=True)
    poller, _, _ = _setup_poller(tmp_path, event_sink=sink)
    monkeypatch.setattr(feishu_ai_poller, "_DEFAULT_AI_POLLER", poller)

    now = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)

    write_realtime_enabled(False)
    pushes = []
    for idx, (chat_id, chat_type, nodes) in enumerate(
        [
            (P2P_CHAT_WEB_ID, CHAT_P2P, [T("Can you look into this?")]),
            (
                ORDINARY_CHAT_WEB_ID,
                CHAT_GROUP,
                [Node(text="@Owner", at=OWNER_WEB_UID), T(" Need help")],
            ),
            (
                PRIORITY_CHAT_WEB_ID,
                CHAT_GROUP,
                [T("Please investigate https://compute.example.com/job/123")],
            ),
        ]
    ):
        pushed = _build_pushed_via_wire(
            msg_id=f"74000000000000009{idx:02d}",
            from_id=COLLEAGUE_WEB_UID,
            chat_id=chat_id,
            chat_type=chat_type,
            nodes=nodes,
        )
        pushes.append(pushed)
        assert not await poller.handle_pushed_web_message(pushed, background_realtime=True)
        assert not await poller.handle_pushed_web_message(pushed)
    sink.assert_not_awaited()
    assert not poller.realtime_status()["effective_enabled"]
    assert poller.web_replay_configured()

    write_realtime_enabled(True)
    for pushed in pushes:
        assert await poller.handle_pushed_web_message(pushed)
    assert [call.args[0].event_type for call in sink.await_args_list] == [
        "im.p2p",
        "im.mention",
        "im.watch_chat_url",
    ]
    assert poller.realtime_status()["emitted"] == 3
    await poller.quiesce()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", [CHAT_P2P, CHAT_GROUP])
async def test_numeric_bot_entity_never_triggers_dot(tmp_path: Path, chat_type: int) -> None:
    sink = AsyncMock(return_value=True)
    poller, _, user_client = _setup_poller(tmp_path, event_sink=sink)
    transport = user_client._get_web_transport()
    assert transport is not None
    chat_id = P2P_CHAT_WEB_ID if chat_type == CHAT_P2P else ORDINARY_CHAT_WEB_ID
    sender = "7200000000000000099"
    transport._web.chatters = AsyncMock(return_value={sender: WebChatter(id=sender, type=2)})
    for index in range(2):
        pushed = _build_pushed_via_wire(
            msg_id=f"74000000000000990{index}",
            from_id=sender,
            chat_id=chat_id,
            chat_type=chat_type,
            nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" Check this")],
        )
        assert not await poller.handle_pushed_web_message(pushed)
    transport._web.chatters.assert_awaited_once_with([sender])
    sink.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("locally_bound", [True, False])
async def test_cold_p2p_bot_chat_suppresses_before_sender_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locally_bound: bool
) -> None:
    from claude_hub.services.feishu_bot_pool import (
        FeishuBotConfig,
        FeishuBotPoolStore,
        OwnerIdentity,
    )

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    sink = AsyncMock(return_value=True)
    poller, _, user_client = _setup_poller(tmp_path, event_sink=sink)
    transport = user_client._get_web_transport()
    assert transport is not None
    open_chat_id = "oc_synthetic_bot_dm"
    bot_open_id = "ou_synthetic_bot"
    if locally_bound:
        pool = FeishuBotPoolStore()
        bot_id = pool.create_bot(
            name="Synthetic Bot",
            config=FeishuBotConfig("cli_synthetic_bot", "test-secret"),
            feishu_open_id=bot_open_id,
        )
        pool.bind_p2p(
            bot_id,
            owner=OwnerIdentity("ou_test_owner", "", "oauth"),
            tab_id="test-tab",
            workspace_id=None,
            sender_open_id="ou_test_bot_user",
            chat_id=open_chat_id,
        )
    assert P2P_CHAT_WEB_ID not in transport._chat_cache
    transport._web.chats = AsyncMock(
        return_value={
            P2P_CHAT_WEB_ID: WebChat(
                id=P2P_CHAT_WEB_ID,
                type=CHAT_P2P,
                bot=not locally_bound,
                open_chat_id=open_chat_id if locally_bound else "",
            ),
        }
    )
    transport._web.chatters = AsyncMock(return_value={})
    pushed = _build_pushed_via_wire(
        msg_id="740000000000009911",
        from_id="7200000000000000099",
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("Here is my answer")],
    )
    assert not await poller.handle_pushed_web_message(pushed)
    transport._web.chats.assert_awaited_once_with([P2P_CHAT_WEB_ID])
    transport._web.chatters.assert_not_awaited()
    sink.assert_not_awaited()

    if locally_bound:
        # A known bot sender alias is enough to suppress a group mention too.
        transport.bind_sender_alias(bot_open_id, pushed.message.from_id)
        group = _build_pushed_via_wire(
            msg_id="740000000000009912",
            from_id=pushed.message.from_id,
            chat_id=ORDINARY_CHAT_WEB_ID,
            nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" Check this")],
        )
        assert not await poller.handle_pushed_web_message(group)
        transport._web.chatters.assert_not_awaited()
        sink.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mention_owner", [False, True])
@pytest.mark.parametrize("prefix", ["", "> "])
async def test_personal_ai_footer_suppresses_topic_reply(
    tmp_path: Path,
    mention_owner: bool,
    prefix: str,
) -> None:
    sink = AsyncMock(return_value=True)
    poller, _, _ = _setup_poller(tmp_path, event_sink=sink)
    await poller.handle_pushed_web_message(
        _build_pushed_via_wire(
            msg_id="740000000000009920",
            from_id=OWNER_WEB_UID,
            chat_id=ORDINARY_CHAT_WEB_ID,
            thread_id="740000000000009919",
            topic_position=1,
            nodes=[T("Investigating")],
        )
    )
    nodes = [Node(text="@Owner", at=OWNER_WEB_UID)] if mention_owner else []
    nodes.append(T(f"Here is the summary.\n{prefix}🤖 AI 代答，仅供参考\n"))
    pushed = _build_pushed_via_wire(
        msg_id="740000000000009921",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        thread_id="740000000000009919",
        topic_position=2,
        create_time_s=1_760_000_101,
        nodes=nodes,
    )
    assert not await poller.handle_pushed_web_message(pushed)
    sink.assert_not_awaited()
    human = replace(
        pushed,
        message=replace(
            pushed.message,
            content=_encode_rich_message(
                [T("The message says 「🤖 AI 代答，仅供参考」 but I confirmed the issue.")],
                is_post=False,
                title="",
            )[1],
        ),
    )
    assert await poller.handle_pushed_web_message(human)
    assert sink.await_args is not None
    assert sink.await_args.args[0].event_type == "im.topic_reply"


@pytest.mark.asyncio
async def test_unknown_numeric_dm_and_explicit_app_sender_are_quiet(tmp_path: Path) -> None:
    sink = AsyncMock(return_value=True)
    poller, _, user_client = _setup_poller(tmp_path, event_sink=sink)
    transport = user_client._get_web_transport()
    assert transport is not None
    transport._web.chatters = AsyncMock(return_value={})
    pushed = _build_pushed_via_wire(
        msg_id="740000000000009930",
        from_id="7200000000000000099",
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("Here is my reply")],
    )
    assert not await poller.handle_pushed_web_message(pushed)
    transport._web.chatters.assert_awaited_once_with([pushed.message.from_id])
    assert poller.realtime_router.needs_patrol_fallback
    generation = poller.realtime_router.fallback_generation
    transport._web.chatters.reset_mock()
    user_msg = transport.ingest_pushed_message(pushed)
    assert user_msg is not None
    assert not await poller.realtime_router.handle_pushed_message(
        pushed, replace(user_msg, sender_type="app"), web_transport=transport
    )
    transport._web.chatters.assert_not_awaited()
    sink.assert_not_awaited()

    assert poller.realtime_router.fallback_generation == generation


@pytest.mark.asyncio
async def test_frontier_sender_timeout_preserves_human_dm_until_replay(tmp_path: Path) -> None:
    from claude_hub.services.feishu_web_session.rich import FeedPage

    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(tmp_path, event_sink=sink)
    transport = client._get_web_transport()
    assert transport is not None
    poller._active_web_transport = transport
    poller._frontier_connected = True
    transport._web.feed_cards = AsyncMock(return_value=FeedPage())
    transport._web.chatters = AsyncMock(
        side_effect=[TimeoutError(), {}, {COLLEAGUE_WEB_UID: WebChatter(id=COLLEAGUE_WEB_UID)}],
    )
    pushed = _build_pushed_via_wire(
        msg_id="740000000000009931",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("Please check this failure")],
        position=1,
    )
    await poller._handle_frontier_pushed_web_message(pushed)
    assert poller._realtime_queue_worker is not None
    await poller._realtime_queue_worker
    assert poller._replay_task is not None
    await poller._replay_task
    assert [p.message.id for p in poller._web_journal.pending()] == [pushed.message.id]
    assert not poller.realtime_stream_healthy
    sink.assert_not_awaited()

    await poller._run_web_replay()
    assert poller._web_journal.pending() == []
    assert not poller.realtime_router.needs_patrol_fallback
    sink.assert_awaited_once()
    assert sink.await_args is not None
    assert sink.await_args.args[0].dedupe_key == pushed.message.id
    await poller.quiesce()


@pytest.mark.asyncio
async def test_unresolved_dm_does_not_starve_journal_or_cross_chat_catchup(tmp_path: Path) -> None:
    from claude_hub.services.feishu_web_session.rich import FeedCard, FeedPage

    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(tmp_path, event_sink=sink)
    transport = client._get_web_transport()
    assert transport is not None
    poller._active_web_transport = transport
    poller._frontier_connected = True
    missing_sender = "7200000000000000099"
    chats = {cid: WebChat(id=cid, type=CHAT_P2P) for cid in (P2P_CHAT_WEB_ID, ORDINARY_CHAT_WEB_ID)}
    cards = tuple(FeedCard(id=cid) for cid in chats)
    transport._web.feed_cards = AsyncMock(return_value=FeedPage(cards=cards, chats=chats))
    await poller._run_web_replay()

    def dm(suffix: int, sender: str, chat_id: str, position: int) -> PushedMessage:
        return _build_pushed_via_wire(
            msg_id=f"74000000000000994{suffix}",
            from_id=sender,
            chat_id=chat_id,
            chat_type=CHAT_P2P,
            nodes=[T(f"Please investigate failure {suffix}")],
            position=position,
        )

    unresolved_push = dm(0, missing_sender, P2P_CHAT_WEB_ID, 0)
    human_push = dm(1, COLLEAGUE_WEB_UID, ORDINARY_CHAT_WEB_ID, 0)
    unresolved_history = dm(2, missing_sender, P2P_CHAT_WEB_ID, 1)
    human_history = dm(3, COLLEAGUE_WEB_UID, ORDINARY_CHAT_WEB_ID, 1)
    poller._web_journal.add(unresolved_push)
    poller._web_journal.add(human_push)
    transport._web.feed_cards.return_value = FeedPage(
        cards=cards,
        chats={cid: replace(chat, last_position=1, has_last=True) for cid, chat in chats.items()},
    )
    transport._web.chats = AsyncMock(return_value=chats)
    transport._web.topics = AsyncMock(return_value={})
    history = {
        P2P_CHAT_WEB_ID: [unresolved_push.message, unresolved_history.message],
        ORDINARY_CHAT_WEB_ID: [human_push.message, human_history.message],
    }

    async def messages_at(chat_id: str, positions: list[int]) -> list[WebMessage]:
        return [history[chat_id][position] for position in positions]

    transport._web.messages_at = AsyncMock(side_effect=messages_at)
    people = {COLLEAGUE_WEB_UID: WebChatter(id=COLLEAGUE_WEB_UID)}

    async def chatters(ids: list[str]) -> dict[str, WebChatter]:
        return {uid: people[uid] for uid in ids if uid in people}

    transport._web.chatters = AsyncMock(side_effect=chatters)
    for _ in range(3):
        await asyncio.wait_for(poller._run_web_replay(), timeout=3)
    assert [call.args[0].dedupe_key for call in sink.await_args_list] == [
        human_push.message.id,
        human_history.message.id,
    ]
    assert [item.message.id for item in poller._web_journal.pending()] == [
        unresolved_push.message.id,
        unresolved_history.message.id,
    ]
    assert poller._web_replay._load()["chats"] == dict.fromkeys(chats, 1)
    assert not poller._replay_ready

    people[missing_sender] = WebChatter(id=missing_sender)
    await poller._run_web_replay()
    assert not poller._web_journal.pending()
    assert poller._replay_ready
    assert [call.args[0].dedupe_key for call in sink.await_args_list] == [
        human_push.message.id,
        human_history.message.id,
        unresolved_push.message.id,
        unresolved_history.message.id,
    ]
    await poller.quiesce()


@pytest.mark.asyncio
@pytest.mark.parametrize("realtime_enabled", [True, False], ids=["confirmed-bot", "disabled"])
async def test_live_retransmission_clears_replay_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, realtime_enabled: bool
) -> None:
    from claude_hub.services.feishu_realtime_settings import write_realtime_enabled
    from claude_hub.services.feishu_web_session.rich import FeedPage

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_HUB_FEISHU_REALTIME_EVENTS_ENABLED", raising=False)
    monkeypatch.delenv("enabled", raising=False)
    write_realtime_enabled(True)
    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(tmp_path, event_sink=sink)
    transport = client._get_web_transport()
    assert transport is not None
    poller._active_web_transport = transport
    poller._frontier_connected = True
    transport._web.feed_cards = AsyncMock(return_value=FeedPage())
    transport._web.chatters = AsyncMock(return_value={})
    sender = "7200000000000000099"
    pushed = _build_pushed_via_wire(
        msg_id="740000000000009950",
        from_id=sender,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("Here is the result")],
        position=1,
    )

    await poller._handle_frontier_pushed_web_message(pushed)
    assert poller._realtime_queue_worker is not None
    await poller._realtime_queue_worker
    assert poller._replay_task is not None
    await poller._replay_task
    assert poller._web_journal.pending() == [pushed]
    assert poller.realtime_router.needs_patrol_fallback
    assert not poller._replay_ready

    transport._web.chatters.return_value = {sender: WebChatter(id=sender, type=2)}
    write_realtime_enabled(realtime_enabled)
    accepted = await poller._handle_frontier_pushed_web_message(pushed)
    assert accepted is realtime_enabled
    if accepted:
        assert poller._realtime_queue_worker is not None
        await poller._realtime_queue_worker
    assert not poller._web_journal.pending()
    assert not poller.realtime_router.needs_patrol_fallback
    sink.assert_not_awaited()

    await poller._run_web_replay()
    assert poller._replay_ready
    await poller.quiesce()
