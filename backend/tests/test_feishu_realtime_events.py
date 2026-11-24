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

from claude_hub.services.demand_patrol.models import PatrolConfig, PatrolState
from claude_hub.services.demand_patrol.service import feishu_due
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


def _encode_rich_message(
    nodes: list[Node],
    *,
    is_post: bool = False,
    title: str = "",
) -> tuple[int, bytes]:
    rich, _mentions = rich_text([nodes], is_post)
    if is_post:
        return TYPE_POST, encode(
            "lark_entities_PostContent",
            {"title": title, "richText": rich},
        )
    return TYPE_TEXT, encode(
        "lark_entities_TextContent",
        {"richText": rich, "text": rich.get("innerText", "")},
    )


def _build_pushed_via_wire(
    *,
    msg_id: str,
    from_id: str,
    chat_id: str,
    nodes: list[Node],
    create_time_s: int = 1_760_000_100,
    position: int = 10,
    root_id: str = "",
    parent_id: str = "",
    thread_id: str = "",
    topic_position: int | None = None,
    chat_type: int = CHAT_GROUP,
    at_me: bool = False,
    is_post: bool = False,
    title: str = "",
) -> PushedMessage:
    typ, content_bytes = _encode_rich_message(nodes, is_post=is_post, title=title)
    msg_entry: dict[str, Any] = {
        "id": msg_id,
        "type": typ,
        "fromId": from_id,
        "createTime": create_time_s,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "chatId": chat_id,
        "position": position,
        "chatType": chat_type,
    }
    if root_id:
        msg_entry["rootId"] = root_id
    if parent_id:
        msg_entry["parentId"] = parent_id
    if thread_id:
        msg_entry["threadId"] = thread_id
    if topic_position is not None:
        msg_entry["threadPosition"] = topic_position

    push_payload: dict[str, Any] = {"messages": {msg_id: msg_entry}}
    if at_me:
        push_payload["messagesAtMe"] = {msg_id: True}

    frame = build_frontier_frame(
        {
            "payloadType": 1,
            "cmd": CMD_PUSH_MESSAGES,
            "sid": f"sid-{msg_id}",
            "payload": base64.b64encode(encode("PushMessagesRequest", push_payload)).decode(
                "ascii"
            ),
        },
        now_ms=create_time_s * 1000,
    )
    _ack, _cmd, pushed_list = parse_frontier_frame(frame)
    assert len(pushed_list) == 1
    return pushed_list[0]


def _setup_poller(
    tmp_path: Path,
    *,
    realtime_config: FeishuRealtimeEventConfig | None = None,
    event_sink: Any = None,
    dispatched_prompts: list[str] | None = None,
) -> tuple[FeishuAiPoller, FeishuAiCommandService, FeishuUserClient]:
    token_store = FeishuUserTokenStore(
        path=tmp_path / "tokens.json",
        key_path=tmp_path / "tokens.key",
    )
    web_store = FeishuWebSessionStore(
        path=tmp_path / "web_session.json",
        key_path=tmp_path / "web_session.key",
    )
    web_store.save(
        FeishuWebSession(
            region="feishu",
            cookies={"session": "sess-token", "passport_web_did": "did-1"},
            saved_at=time.time(),
            device_id="did-1",
            csrf_token="csrf-1",
            web_app_id="161471",
            user_id=OWNER_WEB_UID,
            name="Owner User",
            open_id=OWNER_OPEN_ID,
        )
    )

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        pkt = decode("Packet", request.content) if request.content else {}
        cmd_id = int(pkt.get("cmd") or 0)
        if cmd_id == 25:  # PutReactionRequest
            resp_bytes = encode("reactions_PutReactionResponse", {"count": 1})
        elif cmd_id == 26:  # DeleteReactionRequest
            resp_bytes = encode("reactions_DeleteReactionResponse", {})
        elif cmd_id == 46:  # PullChattersByIdsRequest
            chatter_ids = decode(
                "lark_chatters_PullChattersByIdsRequest", base64.b64decode(pkt["payload"])
            ).get("chatterIds", [])
            resp_bytes = encode(
                "lark_chatters_PullChattersByIdsResponse",
                {
                    "chatters": {
                        uid: {"id": uid, "name": "Test user", "type": 1} for uid in chatter_ids
                    }
                },
            )
        elif cmd_id == 5:  # PutMessageRequest
            resp_bytes = encode(
                "lark_messages_PutMessageResponse",
                {
                    "message": {
                        "id": "7400000000000009999",
                        "type": TYPE_POST,
                        "fromId": OWNER_WEB_UID,
                        "createTime": 1_760_000_200,
                        "chatId": ORDINARY_CHAT_WEB_ID,
                        "position": 99,
                    }
                },
            )
        else:
            resp_bytes = b""
        out_pkt = encode(
            "Packet",
            {
                "cmd": cmd_id or 5,
                "payloadType": 1,
                "payload": base64.b64encode(resp_bytes).decode("ascii"),
            },
        )
        return httpx.Response(200, content=out_pkt)

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(gateway_handler))
    user_client = FeishuUserClient(
        token_store,
        open_id=OWNER_OPEN_ID,
        app_id="web_session",
        client=mock_http,
        web_session_store=web_store,
        send_mode="web_session",
    )

    # The current adapter requires chat mode to encode threaded replies safely.
    transport = user_client._get_web_transport()
    assert transport is not None
    transport._remember_chat(WebChat(id=ORDINARY_CHAT_WEB_ID, type=CHAT_GROUP))

    prompts = dispatched_prompts if dispatched_prompts is not None else []

    async def fake_turn_dispatcher(
        tab_id: str,
        prompt: str,
        turn_id: str,
        visible_text: str,
        metadata: Any,
    ) -> str:
        _ = tab_id, turn_id, visible_text, metadata
        prompts.append(prompt)
        return "处理完成"

    service = FeishuAiCommandService(
        state_path=tmp_path / "ai_command_state.json",
        token_store=token_store,
        web_session_store=web_store,
        dedup_store=FeishuMessageDedupStore(tmp_path / "dedup.json"),
        user_client_factory=lambda _cfg: user_client,
        tab_creator=lambda **kwargs: f"tab-{kwargs.get('name', 'ai')}",
        tab_getter=lambda tab_id: {"id": tab_id},
        turn_dispatcher=fake_turn_dispatcher,
    )
    service.update_config(
        {
            "enabled": True,
            "owner_open_id": OWNER_OPEN_ID,
            "send_mode": "web_session",
        }
    )
    router = FeishuRealtimeEventRouter(config=realtime_config, event_sink=event_sink)
    poller = FeishuAiPoller(service, realtime_router=router)
    return poller, service, user_client


@pytest.mark.parametrize(
    "text,expected",
    [
        ("收到", True),
        ("@Owner 收到，谢谢！", True),
        ("@Owner 好的，辛苦了 🙏", True),
        ('<p><at user_id="ou_1">@Owner</at> 好的谢谢 [抱拳]</p>', True),
        ("已经恢复了，不用看了，多谢！", True),
        ("ok, thanks!", True),
        ("+1", True),
        ("👍", True),
        ("[赞]", True),
        ("@Owner 服务还是 OOM 报错，帮忙看下", False),
        ("收到，但这个链接还是挂了 https://compute.example.com/job/42", False),
        ("好的 https://compute.example.com/job/42", False),
        ("@Owner 为什么会出现超时？", False),
        ("@Owner 可以帮我检查下吗", False),
    ],
)
def test_is_courtesy_or_ack_only_classification(text: str, expected: bool) -> None:
    assert is_courtesy_or_ack_only(text) is expected


def test_extract_mentions_and_urls_from_richtext() -> None:
    nodes = [
        Node(text="@Owner", at=OWNER_WEB_UID),
        T(" 请看故障面板 "),
        L("任务详情", "https://compute.example.com/job/1001"),
        T(" 以及 https://monitor.example.com/alert/2002。"),
    ]
    typ, content = _encode_rich_message(nodes, is_post=True, title="线上告警")
    assert extract_mentioned_user_ids(typ, content) == (OWNER_WEB_UID,)
    urls = extract_message_urls(typ, content)
    assert urls == (
        "https://compute.example.com/job/1001",
        "https://monitor.example.com/alert/2002",
    )


@pytest.mark.asyncio
async def test_realtime_at_me_triggers_immediately_and_deduplicates(
    tmp_path: Path,
) -> None:
    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, _svc, _uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(enabled=True),
        event_sink=sink,
    )

    # 1. Non-owner @current_user via richText TAG_AT in an ordinary group chat triggers immediately
    pushed_at_me = _build_pushed_via_wire(
        msg_id="7400000000000000101",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[
            Node(text="@Owner", at=OWNER_WEB_UID),
            T(" 推理服务启动报错 OOM，麻烦看下 https://compute.example.com/job/101"),
        ],
        at_me=False,
    )
    triggered = await poller.handle_pushed_web_message(pushed_at_me)
    assert triggered is True
    assert len(emitted) == 1
    assert emitted[0].source == "feishu"
    assert emitted[0].event_type == "im.mention"
    assert emitted[0].severity == "urgent"
    assert emitted[0].dedupe_key == "7400000000000000101"
    assert str(emitted[0].url) == "https://compute.example.com/job/101"
    assert emitted[0].payload["trigger_reason"] == "mention"

    # Duplicate push of the same message is suppressed
    dup = await poller.handle_pushed_web_message(pushed_at_me)
    assert dup is False
    assert len(emitted) == 1

    # 2. @all noise must NOT trigger @me
    pushed_at_all = _build_pushed_via_wire(
        msg_id="7400000000000000102",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[
            Node(text="@所有人", at="all"),
            T(" 今晚例行发布通知"),
        ],
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(pushed_at_all) is False
    assert len(emitted) == 1

    # 3. Self-sent @me must NOT trigger an alert
    pushed_self = _build_pushed_via_wire(
        msg_id="7400000000000000103",
        from_id=OWNER_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[
            Node(text="@Owner", at=OWNER_WEB_UID),
            T(" 备忘录测试"),
        ],
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(pushed_self) is False
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_watch_group_unmentioned_url_triggers_while_ordinary_group_has_zero_false_positives(
    tmp_path: Path,
) -> None:
    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    cfg = FeishuRealtimeEventConfig(
        enabled=True,
        watch_chat_ids=frozenset({PRIORITY_CHAT_OPEN_ID.casefold()}),
        url_patterns=(r"https?://(?:compute|eval)\.example\.com/(?:job|arena)/\w+",),
    )
    poller, _svc, uc = _setup_poller(tmp_path, realtime_config=cfg, event_sink=sink)
    wt = uc._get_web_transport()
    assert wt is not None
    # Seed chat mapping from numeric web chat ID to configured OpenAPI oc_ chat ID
    wt._remember_chat(
        WebChat(
            id=PRIORITY_CHAT_WEB_ID,
            type=CHAT_GROUP,
            name="Synthetic Priority Group",
            open_chat_id=PRIORITY_CHAT_OPEN_ID,
        )
    )

    # 1. Ordinary group chat with matching fault URL (no @me) -> ZERO false trigger
    ordinary_with_url = _build_pushed_via_wire(
        msg_id="7400000000000000201",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[
            T("分享一个任务链接 "),
            L("任务 9001", "https://compute.example.com/job/9001"),
        ],
        at_me=False,
    )
    assert await poller.handle_pushed_web_message(ordinary_with_url) is False
    assert len(emitted) == 0

    # 2. Priority watch group with non-matching URL (no @me) -> ZERO false trigger
    priority_unrelated_url = _build_pushed_via_wire(
        msg_id="7400000000000000202",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=PRIORITY_CHAT_WEB_ID,
        nodes=[
            T("普通文档参考 https://docs.example.org/guide/intro"),
        ],
        at_me=False,
    )
    assert await poller.handle_pushed_web_message(priority_unrelated_url) is False
    assert len(emitted) == 0

    # 3. Priority watch group with matching fault/task URL (even without @me) -> IMMEDIATE trigger
    priority_fault_url = _build_pushed_via_wire(
        msg_id="7400000000000000203",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=PRIORITY_CHAT_WEB_ID,
        nodes=[
            T("线上评测实例卡住了，链接："),
            L("Arena 7788", "https://eval.example.com/arena/7788"),
        ],
        at_me=False,
        is_post=True,
        title="重保问题上报",
    )
    assert await poller.handle_pushed_web_message(priority_fault_url) is True
    assert len(emitted) == 1
    assert emitted[0].event_type == "im.watch_chat_url"
    assert emitted[0].severity == "urgent"
    assert str(emitted[0].url) == "https://eval.example.com/arena/7788"
    assert emitted[0].payload["chat_id"] == PRIORITY_CHAT_OPEN_ID
    assert emitted[0].payload["trigger_reason"] == "watch_chat_url"
    assert poller.status_dict()["realtime_events_emitted"] == 1


@pytest.mark.asyncio
async def test_self_reply_and_courtesy_closing_suppression(tmp_path: Path) -> None:
    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    cfg = FeishuRealtimeEventConfig(
        enabled=True,
        watch_chat_ids=frozenset({PRIORITY_CHAT_WEB_ID}),
        url_patterns=(r"https?://compute\.example\.com/job/\d+",),
    )
    poller, _svc, uc = _setup_poller(tmp_path, realtime_config=cfg, event_sink=sink)

    # 1. Courtesy closing with @me ("@Owner 收到，谢谢！") is silently suppressed
    courtesy_push = _build_pushed_via_wire(
        msg_id="7400000000000000301",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 收到，谢谢！")],
        create_time_s=1_760_000_300,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(courtesy_push) is False
    assert len(emitted) == 0
    assert poller.realtime_router.suppressed_courtesy_count == 1

    # 2. Owner already replied in the same topic/thread -> earlier/concurrent trigger is suppressed
    topic_id = "7500000000000000001"
    owner_reply_push = _build_pushed_via_wire(
        msg_id="7400000000000000303",
        from_id=OWNER_WEB_UID,
        chat_id=PRIORITY_CHAT_WEB_ID,
        nodes=[T("我已经在排查这个任务了，稍等")],
        create_time_s=1_760_000_320,
        position=0,
        root_id=topic_id,
        parent_id="7400000000000000302",
        thread_id=topic_id,
    )
    assert await poller.handle_pushed_web_message(owner_reply_push) is False

    question_already_answered = _build_pushed_via_wire(
        msg_id="7400000000000000302",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=PRIORITY_CHAT_WEB_ID,
        nodes=[
            Node(text="@Owner", at=OWNER_WEB_UID),
            T(" 帮忙看下这个任务失败 https://compute.example.com/job/302"),
        ],
        create_time_s=1_760_000_310,
        position=0,
        root_id=topic_id,
        thread_id=topic_id,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(question_already_answered) is False
    assert len(emitted) == 0
    assert poller.realtime_router.suppressed_replied_count == 1

    # 3. A NEW follow-up question in the same thread AFTER the owner's reply IS triggered
    later_followup = _build_pushed_via_wire(
        msg_id="7400000000000000304",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=PRIORITY_CHAT_WEB_ID,
        nodes=[
            Node(text="@Owner", at=OWNER_WEB_UID),
            T(" 重试后新日志还是报错 https://compute.example.com/job/304"),
        ],
        create_time_s=1_760_000_350,
        position=0,
        root_id=topic_id,
        thread_id=topic_id,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(later_followup) is True
    assert len(emitted) == 1
    assert emitted[0].dedupe_key == "7400000000000000304"

    # 4. Latest-messages check via user_client.list_messages_page suppresses when owner replied on another device
    uc.list_messages_page = AsyncMock(  # type: ignore[method-assign]
        return_value=FeishuMessagePage(
            items=(
                FeishuUserMessage(
                    message_id="7400000000000000306",
                    chat_id=P2P_CHAT_WEB_ID,
                    sender_open_id=OWNER_OPEN_ID,
                    sender_type="user",
                    msg_type="text",
                    text="刚刚在手机上回你了，已经修好",
                    create_time_ms=1_760_000_410_000,
                ),
            ),
            has_more=False,
            page_token="",
        )
    )
    dm_question = _build_pushed_via_wire(
        msg_id="7400000000000000305",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 请问服务恢复了吗？")],
        create_time_s=1_760_000_400,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(dm_question) is False
    assert len(emitted) == 1
    assert poller.realtime_router.suppressed_replied_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("realtime_enabled", [True, False])
async def test_ai_command_unaffected_and_records_owner_activity(
    tmp_path: Path, realtime_enabled: bool
) -> None:
    emitted: list[ExternalEvent] = []
    dispatched_prompts: list[str] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, service, _uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(
            enabled=realtime_enabled,
            watch_chat_ids=frozenset({ORDINARY_CHAT_WEB_ID}),
            url_patterns=(r"https?://compute\.example\.com/job/\d+",),
        ),
        event_sink=sink,
        dispatched_prompts=dispatched_prompts,
    )

    ai_cmd_push = _build_pushed_via_wire(
        msg_id="7400000000000000501",
        from_id=OWNER_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("/ai 总结当前话题 https://compute.example.com/job/501")],
        create_time_s=1_760_000_500,
        position=50,
    )
    accepted = await poller.handle_pushed_web_message(ai_cmd_push)
    assert accepted is True
    assert poller.status_dict()["commands_detected"] == 1
    assert len(emitted) == 0
    await poller.wait_for_inflight(timeout=3.0)
    assert len(dispatched_prompts) == 1
    assert "总结当前话题" in dispatched_prompts[0]
    assert poller.status_dict()["commands_succeeded"] == 1
    await poller.quiesce()


@pytest.mark.asyncio
async def test_r2_frontier_intake_never_blocks_subsequent_ai_command_when_sink_stalls(
    tmp_path: Path,
) -> None:
    """R2 regression: a stalled event sink in a multi-push frame must not delay /ai intake."""
    sink_entered = asyncio.Event()
    release_sink = asyncio.Event()
    emitted: list[ExternalEvent] = []
    dispatched_prompts: list[str] = []

    async def slow_sink(event: ExternalEvent) -> bool:
        sink_entered.set()
        await release_sink.wait()
        emitted.append(event)
        return True

    poller, _service, uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(enabled=True),
        event_sink=slow_sink,
        dispatched_prompts=dispatched_prompts,
    )
    wt = uc._get_web_transport()
    assert wt is not None
    poller._active_web_transport = wt
    listener = wt.create_frontier_listener(
        poller._handle_frontier_pushed_web_message,
        on_state=poller._on_frontier_state,
    )

    p1 = _build_pushed_via_wire(
        msg_id="7400000000000000701",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 帮忙排查超时问题")],
        create_time_s=1_760_000_701,
        position=1,
        at_me=True,
    )
    p2 = _build_pushed_via_wire(
        msg_id="7400000000000000702",
        from_id=OWNER_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("/ai 立即执行命令")],
        create_time_s=1_760_000_702,
        position=2,
    )

    # Deliver a single frame with two pushes ([mention, /ai]) or two consecutive frames while slow_sink is blocked
    _, m1_bytes = _encode_rich_message(
        [Node(text="@Owner", at=OWNER_WEB_UID), T(" 帮忙排查超时问题")]
    )
    _, m2_bytes = _encode_rich_message([T("/ai 立即执行命令")])
    push_payload = {
        "messages": {
            p1.message.id: {
                "id": p1.message.id,
                "type": TYPE_TEXT,
                "fromId": COLLEAGUE_WEB_UID,
                "createTime": 1_760_000_701,
                "content": base64.b64encode(m1_bytes).decode("ascii"),
                "chatId": ORDINARY_CHAT_WEB_ID,
                "position": 1,
                "chatType": CHAT_GROUP,
            },
            p2.message.id: {
                "id": p2.message.id,
                "type": TYPE_TEXT,
                "fromId": OWNER_WEB_UID,
                "createTime": 1_760_000_702,
                "content": base64.b64encode(m2_bytes).decode("ascii"),
                "chatId": ORDINARY_CHAT_WEB_ID,
                "position": 2,
                "chatType": CHAT_GROUP,
            },
        },
        "messagesAtMe": {p1.message.id: True},
    }
    raw_frame = build_frontier_frame(
        {
            "payloadType": 1,
            "cmd": CMD_PUSH_MESSAGES,
            "sid": "sid-multi",
            "payload": base64.b64encode(encode("PushMessagesRequest", push_payload)).decode(
                "ascii"
            ),
        },
        now_ms=1_760_000_702_000,
    )

    await listener.handle_raw_frame(raw_frame, AsyncMock())
    # Even while slow_sink is still blocked, the second message (/ai command) is already detected!
    assert poller.status_dict()["commands_detected"] == 1
    await asyncio.wait_for(sink_entered.wait(), timeout=2.0)
    assert len(emitted) == 0
    release_sink.set()
    await poller.wait_for_inflight(timeout=3.0)
    assert len(emitted) == 1
    assert len(dispatched_prompts) == 1
    await poller.quiesce()


@pytest.mark.asyncio
async def test_r3_distinct_message_ids_with_identical_text_both_trigger(
    tmp_path: Path,
) -> None:
    """R3 regression: two distinct wire message IDs with identical text (24h apart or another topic) both trigger."""
    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, _svc, _uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(enabled=True),
        event_sink=sink,
    )
    first = _build_pushed_via_wire(
        msg_id="7400000000000000801",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[
            Node(text="@Owner", at=OWNER_WEB_UID),
            T(" 线上推理服务报错 OOM，麻烦看下"),
        ],
        create_time_s=1_760_000_000,
        position=10,
        at_me=True,
    )
    second_24h_later = _build_pushed_via_wire(
        msg_id="7400000000000000802",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[
            Node(text="@Owner", at=OWNER_WEB_UID),
            T(" 线上推理服务报错 OOM，麻烦看下"),
        ],
        create_time_s=1_760_086_400,
        position=25,
        at_me=True,
    )
    third_other_topic = _build_pushed_via_wire(
        msg_id="7400000000000000803",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[
            Node(text="@Owner", at=OWNER_WEB_UID),
            T(" 线上推理服务报错 OOM，麻烦看下"),
        ],
        create_time_s=1_760_000_005,
        position=11,
        root_id="7500000000000000800",
        thread_id="7500000000000000800",
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(first) is True
    assert await poller.handle_pushed_web_message(second_24h_later) is True
    assert await poller.handle_pushed_web_message(third_other_topic) is True
    assert [e.dedupe_key for e in emitted] == [
        "7400000000000000801",
        "7400000000000000802",
        "7400000000000000803",
    ]


@pytest.mark.asyncio
async def test_r4_same_second_owner_message_preceding_question_does_not_suppress(
    tmp_path: Path,
) -> None:
    """R4 regression: owner message at position 10 followed by a new question at position 11 in the same second must trigger."""
    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, _svc, _uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(enabled=True),
        event_sink=sink,
    )
    owner_msg = _build_pushed_via_wire(
        msg_id="7400000000000000901",
        from_id=OWNER_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("我先把上个问题关了")],
        create_time_s=1_760_000_900,
        position=10,
    )
    colleague_q_same_second = _build_pushed_via_wire(
        msg_id="7400000000000000902",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 新集群节点报错，帮忙看下")],
        create_time_s=1_760_000_900,
        position=11,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(owner_msg) is False
    assert await poller.handle_pushed_web_message(colleague_q_same_second) is True
    assert len(emitted) == 1
    assert poller.realtime_router.suppressed_replied_count == 0


def test_r5_regex_with_comma_quantifiers_parsed_intact_from_env_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 regression: regex quantifiers like {2,4} must not be split on commas in ENV or JSON config."""
    from claude_hub.services.feishu_realtime_events import matches_fault_or_task_url

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(tmp_path / "hub" / "workspaces"))
    (tmp_path / "hub").mkdir(parents=True, exist_ok=True)
    pattern = r"https://compute\.example\.com/job/\d{2,4}$"

    # 1. ENV form
    monkeypatch.setenv("CLAUDE_HUB_FEISHU_WATCH_URL_REGEX", pattern)
    cfg_env = FeishuRealtimeEventConfig.from_runtime()
    assert cfg_env.url_patterns == (pattern,)
    assert matches_fault_or_task_url("https://compute.example.com/job/123", cfg_env) is True
    assert matches_fault_or_task_url("https://compute.example.com/job/1", cfg_env) is False

    # 2. JSON-list form
    monkeypatch.delenv("CLAUDE_HUB_FEISHU_WATCH_URL_REGEX", raising=False)
    (tmp_path / "hub" / "local_runtime_settings.json").write_text(
        json.dumps({"url_patterns": [pattern]}),
        encoding="utf-8",
    )
    cfg_json = FeishuRealtimeEventConfig.from_runtime()
    assert cfg_json.url_patterns == (pattern,)
    assert matches_fault_or_task_url("https://compute.example.com/job/123", cfg_json) is True

    # 3. Literal top-level comma in ENV regex string is also preserved intact
    (tmp_path / "hub" / "local_runtime_settings.json").unlink()
    literal_comma_pattern = r"https://compute\.example\.com/task/foo,bar$"
    monkeypatch.setenv("CLAUDE_HUB_FEISHU_WATCH_URL_REGEX", literal_comma_pattern)
    cfg_comma = FeishuRealtimeEventConfig.from_runtime()
    assert cfg_comma.url_patterns == (literal_comma_pattern,)
    assert matches_fault_or_task_url("https://compute.example.com/task/foo,bar", cfg_comma) is True
    assert matches_fault_or_task_url("https://compute.example.com/task/foo", cfg_comma) is False


@pytest.mark.asyncio
async def test_r1_uncovered_p2p_and_delivery_failure_preserve_patrol_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 regression: unmentioned P2P questions and delivery failures keep feishu_due=True for fallback patrol."""
    import claude_hub.services.feishu_ai_poller as poller_mod
    from claude_hub.services.feishu_realtime_events import (
        is_feishu_realtime_stream_healthy,
    )

    should_fail = False

    async def flaky_sink(_event: ExternalEvent) -> bool:
        if should_fail:
            raise httpx.ConnectError("dot gateway temporarily unreachable")
        return True

    poller, _svc, _uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(
            enabled=True,
            suppress_patrol_when_healthy=True,
        ),
        event_sink=flaky_sink,
    )
    monkeypatch.setattr(poller_mod, "_DEFAULT_AI_POLLER", poller)
    monkeypatch.setattr(
        "claude_hub.services.demand_patrol.service.get_feishu_realtime_event_router",
        lambda: poller.realtime_router,
    )
    from claude_hub.services.feishu_web_session.rich import FeedPage

    wt = _uc._get_web_transport()
    assert wt is not None
    wt._web.feed_cards = AsyncMock(return_value=FeedPage())
    wt._web.chats = AsyncMock(return_value={PRIORITY_CHAT_WEB_ID: WebChat(id=PRIORITY_CHAT_WEB_ID)})
    poller._active_web_transport = wt
    poller._frontier_task = type("FakeTask", (), {"done": lambda self: False})()  # type: ignore[assignment]
    poller._on_frontier_state(None)
    assert poller._replay_task is not None
    await poller._replay_task

    now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
    state = PatrolState(config=PatrolConfig(enabled=True, feishu_enabled=True))
    assert is_feishu_realtime_stream_healthy() is True
    assert feishu_due(state, now) is False

    # Non-owner direct messages use the realtime route without a fallback.
    plain_dm = _build_pushed_via_wire(
        msg_id="7400000000000001001",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("服务启动报错 OOM 了，能帮忙看看吗？")],
        create_time_s=1_760_001_001,
    )
    assert await poller.handle_pushed_web_message(plain_dm) is True
    assert poller.realtime_router.needs_patrol_fallback is False

    should_fail = True
    # 2. Delivery failure on @me event also marks fallback needed so patrol can catch it
    mention_msg = _build_pushed_via_wire(
        msg_id="7400000000000001002",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 任务卡住了，麻烦看下")],
        create_time_s=1_760_001_002,
        at_me=True,
    )
    assert await poller.handle_pushed_web_message(mention_msg) is False
    assert poller.realtime_router.delivery_failed_count == 1
    assert poller.realtime_router.needs_patrol_fallback is True
    assert feishu_due(state, now) is False
    poller._frontier_task = None


@pytest.mark.asyncio
async def test_c2_r1_receipt_backed_patrol_suppression_retains_unhandled_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2-R1 regression: filter_unanswered must retain unhandled @me/watch-URL messages even when realtime_healthy=True."""
    from claude_hub.services.demand_patrol import feishu_triage as triage_mod
    from claude_hub.services.demand_patrol.feishu_triage import (
        _candidate,
        filter_unanswered,
    )
    from claude_hub.services.feishu_realtime_events import FeishuRealtimeEventRouter

    assert FeishuRealtimeEventConfig().suppress_patrol_when_healthy is True

    router = FeishuRealtimeEventRouter(
        config=FeishuRealtimeEventConfig(
            enabled=True,
            watch_chat_ids=frozenset({"oc_priority_group"}),
            url_patterns=(r"https?://compute\.example\.com/job/\d+",),
        )
    )
    monkeypatch.setattr(triage_mod, "get_feishu_realtime_event_router", lambda: router)

    unhandled_mention = _candidate(
        {
            "message_id": "om_unhandled_mention",
            "chat_id": "oc_group_1",
            "chat_name": "普通研发群",
            "chat_type": "group",
            "create_time": "1760002000000",
            "sender": {"id": "ou_colleague", "name": "Alice"},
            "content": "@Owner 启动报错 OOM，麻烦看下？",
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    # Without a handled receipt, filter_unanswered retains the candidate even when realtime_healthy=True
    cands_unhealthy, ans_unhealthy = filter_unanswered(
        [unhandled_mention], OWNER_OPEN_ID, [], realtime_healthy=False
    )
    cands_healthy, ans_healthy = filter_unanswered(
        [unhandled_mention], OWNER_OPEN_ID, [], realtime_healthy=True
    )
    assert [c.message_id for c in cands_unhealthy] == ["om_unhandled_mention"]
    assert ans_unhealthy == []
    assert [c.message_id for c in cands_healthy] == ["om_unhandled_mention"]
    assert ans_healthy == []

    # Once a real receipt is recorded, filter_unanswered suppresses the duplicate
    router._record_handled(
        "7400000000000002000",
        alias_id="om_unhandled_mention",
        chat_keys={"oc_group_1"},
        sender_ids={"ou_colleague"},
        text="@Owner 启动报错 OOM，麻烦看下？",
        created_at_ms=1_760_002_000_000,
    )
    cands_after, ans_after = filter_unanswered(
        [unhandled_mention], OWNER_OPEN_ID, [], realtime_healthy=True
    )
    assert cands_after == []
    assert ans_after == ["om_unhandled_mention"]


def test_c2_r3_cross_namespace_dedupe_rejects_conflicting_alias_different_topic_and_missing_chat() -> (
    None
):
    """C2-R3 regression: cross-namespace matching must reject conflicting aliases, different topics/times, and missing chat."""
    from claude_hub.services.feishu_realtime_events import FeishuRealtimeEventRouter

    router = FeishuRealtimeEventRouter()
    text = "@Owner 线上推理服务报错 OOM，麻烦看下"
    ts_ms = 1_760_003_000_000

    # 1. Record with a known verified OpenAPI alias om_known_A
    router._record_handled(
        "7400000000000003001",
        alias_id="om_known_A",
        chat_keys={"oc_chat_1"},
        thread_id="omt_topic_1",
        sender_ids={"ou_alice"},
        text=text,
        created_at_ms=ts_ms,
    )
    assert router.is_message_handled("om_known_A") is True
    # A different OpenAPI ID om_new_B must NEVER match a record already bound to om_known_A
    assert (
        router.is_message_handled(
            "om_new_B",
            chat_id="oc_chat_1",
            thread_id="omt_topic_1",
            sender_id="ou_alice",
            text=text,
            created_at_ms=ts_ms,
        )
        is False
    )

    # 2. Record an unaliased web message ID
    router._record_handled(
        "7400000000000003002",
        chat_keys={"oc_chat_1"},
        thread_id="omt_topic_1",
        sender_ids={"ou_alice"},
        text=text,
        created_at_ms=ts_ms,
    )
    # Different thread -> False
    assert (
        router.is_message_handled(
            "om_other_thread",
            chat_id="oc_chat_1",
            thread_id="omt_topic_2",
            sender_id="ou_alice",
            text=text,
            created_at_ms=ts_ms,
        )
        is False
    )
    # Timestamp +1s -> False
    assert (
        router.is_message_handled(
            "om_plus_1s",
            chat_id="oc_chat_1",
            thread_id="omt_topic_1",
            sender_id="ou_alice",
            text=text,
            created_at_ms=ts_ms + 1000,
        )
        is False
    )
    # Missing chat_id -> False
    assert (
        router.is_message_handled(
            "om_missing_chat",
            chat_id="",
            thread_id="omt_topic_1",
            sender_id="ou_alice",
            text=text,
            created_at_ms=ts_ms,
        )
        is False
    )
    # Different sender -> False
    assert (
        router.is_message_handled(
            "om_other_sender",
            chat_id="oc_chat_1",
            thread_id="omt_topic_1",
            sender_id="ou_bob",
            text=text,
            created_at_ms=ts_ms,
        )
        is False
    )
    # No cross-namespace content guess is accepted.
    assert (
        router.is_message_handled(
            "om_exact_1",
            chat_id="oc_chat_1",
            thread_id="omt_topic_1",
            sender_id="ou_alice",
            text=text,
            created_at_ms=ts_ms,
        )
        is False
    )
    assert (
        router.is_message_handled(
            "om_exact_2",
            chat_id="oc_chat_1",
            thread_id="omt_topic_1",
            sender_id="ou_alice",
            text=text,
            created_at_ms=ts_ms,
        )
        is False
    )


@pytest.mark.asyncio
async def test_c2_r4_equal_second_history_page_and_slow_triage_thread_quote_do_not_suppress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2-R4 regression: list_messages_page with ascending-position messages_at and _is_answered_by on equal-time unrelated thread quotes must not suppress."""
    from claude_hub.services.demand_patrol.feishu_triage import (
        _candidate,
        _is_answered_by,
    )
    from claude_hub.services.feishu_web_session.rich import WebChat

    emitted: list[ExternalEvent] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, _svc, uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(enabled=True),
        event_sink=sink,
    )
    wt = uc._get_web_transport()
    assert wt is not None

    # Mock web_client.chats and web_client.messages_at returning ascending positions [pos 10 (owner), pos 11 (question)] at the same second
    owner_pushed = _build_pushed_via_wire(
        msg_id="7400000000000004010",
        from_id=OWNER_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("我先看下上一个问题")],
        create_time_s=1_760_004_000,
        position=10,
    )
    question_pushed = _build_pushed_via_wire(
        msg_id="7400000000000004011",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" 这个新报错帮忙看下？")],
        create_time_s=1_760_004_000,
        position=11,
        at_me=True,
    )

    async def fake_chats(_chat_ids: list[str]) -> dict[str, WebChat]:
        return {
            ORDINARY_CHAT_WEB_ID: WebChat(
                id=ORDINARY_CHAT_WEB_ID,
                name="普通研发讨论群",
                last_position=11,
            )
        }

    async def fake_messages_at(_chat_id: str, _positions: list[int]) -> list[Any]:
        # Real messages_at sorts ascending by position: pos 10 before pos 11
        return [owner_pushed.message, question_pushed.message]

    monkeypatch.setattr(wt.web_client, "chats", fake_chats)
    monkeypatch.setattr(wt.web_client, "messages_at", fake_messages_at)

    # Deliver question_pushed directly (so only _verify_unanswered_via_client -> list_messages_page sees owner_pushed)
    assert await poller.handle_pushed_web_message(question_pushed) is True
    assert len(emitted) == 1
    assert poller.realtime_router.suppressed_replied_count == 0

    # Also verify slow-patrol _is_answered_by does not treat an equal-timestamp quote of an earlier message in the same thread as answering the new question
    q_cand = _candidate(
        {
            "message_id": "om_q_new",
            "chat_id": "oc_chat",
            "chat_type": "group",
            "thread_id": "omt_thread_1",
            "reply_to": "om_root",
            "create_time": "1760004000000",
            "sender": {"id": "ou_colleague", "name": "Alice"},
            "content": "@Owner 新问题报错了",
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    own_unrelated_quote_same_sec = _candidate(
        {
            "message_id": "om_own_quote_earlier",
            "chat_id": "oc_chat",
            "chat_type": "group",
            "thread_id": "omt_thread_1",
            "reply_to": "om_unrelated_old_msg",
            "create_time": "1760004000000",
            "sender": {"id": OWNER_OPEN_ID, "name": "Owner"},
            "content": "顺便回复一下上面那条旧消息",
            "mentions": [],
        }
    )
    assert _is_answered_by(q_cand, own_unrelated_quote_same_sec) is False


@pytest.mark.asyncio
async def test_sibling_29c6249b_gateway_coexistence_suppresses_courtesy_and_suppressed_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coexistence with 29c6249b: courtesy mentions or messages suppressed by realtime are rejected at submit_external_event."""
    from claude_hub.services.dot.events_gateway import submit_external_event

    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr(dot, "is_set_up", lambda: True)
    dot.wake_for_external_event = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)

    # 1. Legacy 90s bridge submits a courtesy @me mention ("@Owner 收到，谢谢！") -> suppressed at gateway
    courtesy_bridge_event = ExternalEvent(
        source="feishu",
        event_type="im.mention",
        dedupe_key="om_bridge_courtesy_1",
        severity="urgent",
        title="Feishu mention",
        summary=" Group · Alice · @Owner 收到，谢谢！",
        payload={
            "chat_id": "oc_group_1",
            "message_id": "om_bridge_courtesy_1",
            "text": "@Owner 收到，谢谢！",
        },
    )
    assert await submit_external_event(courtesy_bridge_event, dot) is False
    assert dot.store.list_inbox() == []
    dot.wake_for_external_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_dot_event_gateway_integration_and_20m_patrol_suppression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(tmp_path / "hub" / "workspaces"))
    (tmp_path / "hub").mkdir(parents=True, exist_ok=True)
    (tmp_path / "hub" / "local_runtime_settings.json").write_text(
        json.dumps(
            {
                "CLAUDE_HUB_FEISHU_WATCH_CHAT_IDS": [PRIORITY_CHAT_WEB_ID],
                "CLAUDE_HUB_FEISHU_WATCH_URL_REGEX": r"https?://compute\.example\.com/job/\d+",
                "CLAUDE_HUB_FEISHU_SUPPRESS_PATROL_WHEN_HEALTHY": True,
            }
        ),
        encoding="utf-8",
    )

    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr(dot, "is_set_up", lambda: True)
    dot.wake_for_external_event = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)

    poller, _svc, _uc = _setup_poller(tmp_path)
    pushed = _build_pushed_via_wire(
        msg_id="7400000000000000601",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=PRIORITY_CHAT_WEB_ID,
        nodes=[T("节点掉线告警 https://compute.example.com/job/601")],
        create_time_s=1_760_000_600,
    )
    assert await poller.handle_pushed_web_message(pushed) is True
    dot.wake_for_external_event.assert_awaited_once()
    inbox = dot.store.list_inbox()
    assert len(inbox) == 1
    assert inbox[0]["event_type"] == "im.watch_chat_url"
    assert inbox[0]["severity"] == "urgent"

    # Verify 20m slow patrol is suppressed when the WebSocket realtime stream is healthy and opt-in enabled
    now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
    state = PatrolState(config=PatrolConfig(enabled=True, feishu_enabled=True))
    assert feishu_due(state, now, realtime_healthy=False) is True
    assert feishu_due(state, now, realtime_healthy=True) is False

    # Also verify automatic health tracking on the singleton poller
    import claude_hub.services.feishu_ai_poller as poller_mod
    from claude_hub.services.feishu_realtime_events import (
        is_feishu_realtime_stream_healthy,
    )

    monkeypatch.setattr(poller_mod, "_DEFAULT_AI_POLLER", poller)
    monkeypatch.setattr(
        "claude_hub.services.demand_patrol.service.get_feishu_realtime_event_router",
        lambda: poller.realtime_router,
    )
    from claude_hub.services.feishu_web_session.rich import FeedPage

    wt = _uc._get_web_transport()
    assert wt is not None
    wt._web.feed_cards = AsyncMock(return_value=FeedPage())
    wt._web.chats = AsyncMock(return_value={PRIORITY_CHAT_WEB_ID: WebChat(id=PRIORITY_CHAT_WEB_ID)})
    poller._active_web_transport = wt
    poller._frontier_task = type("FakeTask", (), {"done": lambda self: False})()  # type: ignore[assignment]
    poller._on_frontier_state(None)
    assert poller._replay_task is not None
    await poller._replay_task
    assert is_feishu_realtime_stream_healthy() is True
    assert feishu_due(state, now) is False

    poller._on_frontier_state(RuntimeError("ws closed"))
    assert is_feishu_realtime_stream_healthy() is False
    assert feishu_due(state, now) is False
    poller._frontier_task = None


@pytest.mark.asyncio
async def test_c3_r1_probe_a_and_b_strict_fallback_identity_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3-R1 regression: failed watch delivery must not leak watch eligibility to ordinary groups or overwrite aliases across chats."""
    from claude_hub.services.demand_patrol.feishu_triage import _candidate

    async def fail_sink(_ev: ExternalEvent) -> bool:
        return False

    router = FeishuRealtimeEventRouter(
        config=FeishuRealtimeEventConfig(
            enabled=True,
            watch_chat_ids=frozenset({"oc_watch_A"}),
            url_patterns=(r"https://compute\.example\.com/job/\d+",),
        ),
        event_sink=fail_sink,
    )
    monkeypatch.setattr(
        "claude_hub.services.feishu_realtime_events._DEFAULT_REALTIME_ROUTER", router
    )
    pushed_a = _build_pushed_via_wire(
        msg_id="7400000000000000901",
        from_id=COLLEAGUE_WEB_UID,
        chat_id="oc_watch_A",
        nodes=[T("服务报错 https://compute.example.com/job/901")],
        create_time_s=1_760_000_901,
    )
    um_a = FeishuUserMessage(
        message_id="7400000000000000901",
        chat_id="oc_watch_A",
        sender_open_id="ou_alice",
        sender_type="user",
        msg_type="post",
        text="服务报错 https://compute.example.com/job/901",
        create_time_ms=1_760_000_901_000,
    )
    assert (
        await router.handle_pushed_message(pushed_a, um_a, owner_ids={OWNER_OPEN_ID, OWNER_WEB_UID})
        is False
    )
    assert router.pending_fallback_count == 1

    # Probe A: Same text/URL sent in an unconfigured ordinary group must NOT trigger watch-group alert
    emitted: list[ExternalEvent] = []

    async def ok_sink(ev: ExternalEvent) -> bool:
        emitted.append(ev)
        return True

    router._event_sink = ok_sink
    pushed_b = _build_pushed_via_wire(
        msg_id="7400000000000000902",
        from_id="7100000000000000009",
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[T("服务报错 https://compute.example.com/job/901")],
        create_time_s=1_760_000_902,
    )
    um_b = FeishuUserMessage(
        message_id="7400000000000000902",
        chat_id=ORDINARY_CHAT_WEB_ID,
        sender_open_id="ou_bob",
        sender_type="user",
        msg_type="post",
        text="服务报错 https://compute.example.com/job/901",
        create_time_ms=1_760_000_902_000,
    )
    assert (
        await router.handle_pushed_message(pushed_b, um_b, owner_ids={OWNER_OPEN_ID, OWNER_WEB_UID})
        is False
    )
    assert emitted == []

    # Probe B: Cross-chat / cross-sender / conflicting-alias candidate must NOT ack fallback or overwrite alias
    router2 = FeishuRealtimeEventRouter()
    router2._add_fallback_record(
        "om_known_A",
        "7400000000000000910",
        "failed_delivery",
        "Watch A",
        {"oc_watch_a"},
        "omt_t1",
        "ou_alice",
        "请看 https://compute.example.com/job/910",
        1_760_000_910_000,
        "om_known_A",
        "",
        ("https://compute.example.com/job/910",),
    )
    cand_b = _candidate(
        {
            "message_id": "om_other_B",
            "chat_id": "oc_other_B",
            "chat_type": "group",
            "thread_id": "omt_t1",
            "create_time": "1760000911000",
            "sender": {"id": "ou_bob", "name": "Bob"},
            "content": "请看 https://compute.example.com/job/910",
            "mentions": [{"id": OWNER_OPEN_ID}],
        }
    )
    router2.acknowledge_patrol_fallback(recovered_ids=["om_other_B"], recovered_candidates=[cand_b])
    assert router2.pending_fallback_count == 1
    assert router2.is_message_handled("om_other_B") is False


@pytest.mark.asyncio
async def test_c3_r3_probe_d_scoped_watched_chat_search_avoids_501_all_group_starvation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3-R3 regression: 501 unrelated group messages must not starve @me mentions or watched-group URL patrol."""
    from claude_hub.services.demand_patrol import feishu_triage as triage_mod
    from claude_hub.services.demand_patrol.feishu_triage import (
        TriageDiagnosis,
        TriageEvidenceLink,
        run_feishu_triage,
    )
    from tests.test_demand_patrol_triage import ME, FakeDot, FakeRunner, capabilities

    monkeypatch.setenv("INFOWALL_URL", "http://127.0.0.1:18899")
    monkeypatch.setenv("INFOWALL_API_KEY_FILE", "/home/tiger/.local/state/infowall/api-key")
    router = FeishuRealtimeEventRouter(
        config=FeishuRealtimeEventConfig(
            enabled=True,
            watch_chat_ids=frozenset({"oc_watch_priority"}),
            url_patterns=(r"https://compute\.example\.com/job/\d+",),
        )
    )
    monkeypatch.setattr(triage_mod, "get_feishu_realtime_event_router", lambda: router)
    monkeypatch.setattr(
        "claude_hub.services.feishu_realtime_events._DEFAULT_REALTIME_ROUTER", router
    )

    now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
    ts_ms = str((int(now.timestamp()) - 120) * 1000)
    rows: list[dict[str, Any]] = [
        {
            "message_id": f"om_noise_{i}",
            "chat_id": "oc_noisy_unwatched",
            "chat_type": "group",
            "create_time": ts_ms,
            "sender": {"id": "ou_other", "name": "Other"},
            "content": f"chatter {i}",
            "mentions": [],
        }
        for i in range(505)
    ] + [
        {
            "message_id": "om_valid_at_me",
            "chat_id": "oc_ordinary_group",
            "chat_type": "group",
            "create_time": ts_ms,
            "sender": {"id": "ou_alice", "name": "Alice"},
            "content": "@Owner 请帮忙看下这个报错？",
            "mentions": [{"id": ME}],
            "message_app_link": "https://applink.feishu.cn/client/message/om_valid_at_me",
        },
        {
            "message_id": "om_watch_url",
            "chat_id": "oc_watch_priority",
            "chat_type": "group",
            "create_time": ts_ms,
            "sender": {"id": "ou_bob", "name": "Bob"},
            "content": "重保群故障链接 https://compute.example.com/job/777",
            "mentions": [],
            "message_app_link": "https://applink.feishu.cn/client/message/om_watch_url",
        },
    ]

    async def fake_triage(_cand: Any) -> TriageDiagnosis:
        return TriageDiagnosis(
            headline="定位线上服务故障并核对日志",
            root_cause="日志显示节点连接超时，需进一步检查依赖服务状态。",
            evidence_links=[
                TriageEvidenceLink(title="故障链接", url="https://compute.example.com/job/777")
            ],
            reply_draft=(
                "已收到排障请求并初步查看故障现场，目前观察到连接超时异常。"
                "建议先核对对应实例状态与上下游依赖配置，确认后再决定下一步修复操作。"
            ),
            project_hint="服务稳定性",
            next_action="检查故障实例与日志",
            needs_code_fix=False,
        )

    runner = FakeRunner(rows)
    res = await run_feishu_triage(
        PatrolState(),
        now=now,
        dot=FakeDot(),
        runner=runner,
        triage=fake_triage,
        capabilities=capabilities,
    )
    assert res.errors == []
    assert res.candidates == 2
    assert res.notified == 2
    assert not any("--chat-type" in argv and "group" in argv for argv, _, _ in runner.calls)


@pytest.mark.asyncio
async def test_c4_r1_ordinary_messages_do_not_stall_fifo_queue_or_rebuild_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4-R1 regression: ordinary unmentioned messages must short-circuit before slow chat metadata lookup, and production poller reuses FeishuUserClient."""
    import asyncio
    import time

    from claude_hub.services.feishu_web_session.client import FeishuWebClient

    async def slow_chats(self: Any, chat_ids: list[str]) -> dict[str, Any]:
        await asyncio.sleep(3.0)
        return {}

    monkeypatch.setattr(FeishuWebClient, "chats", slow_chats)

    emitted: list[ExternalEvent] = []
    done = asyncio.Event()

    async def sink(ev: ExternalEvent) -> bool:
        emitted.append(ev)
        done.set()
        return True

    poller, svc, uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(
            enabled=True,
            watch_chat_ids=frozenset({PRIORITY_CHAT_OPEN_ID}),
            url_patterns=(r"https://compute\.example\.com/job/\d+",),
        ),
        event_sink=sink,
    )
    poller._active_web_transport = uc._get_web_transport()
    assert poller._active_web_transport is not None
    # Test production path where _user_client_factory is None
    svc._user_client_factory = None
    build_calls = 0

    def counting_build(_cfg: Any) -> Any:
        nonlocal build_calls
        build_calls += 1
        return uc

    monkeypatch.setattr(svc, "build_user_client", counting_build)

    ordinary_msgs = [
        _build_pushed_via_wire(
            msg_id=f"740000000000000910{i}",
            from_id=COLLEAGUE_WEB_UID,
            chat_id=ORDINARY_CHAT_WEB_ID,
            nodes=[T(f"ordinary discussion message {i}")],
            create_time_s=1_760_009_100 + i,
            at_me=False,
        )
        for i in range(5)
    ]
    urgent_at_me = _build_pushed_via_wire(
        msg_id="7400000000000009199",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=ORDINARY_CHAT_WEB_ID,
        nodes=[Node(text="@Owner", at=OWNER_WEB_UID), T(" urgent production alert")],
        create_time_s=1_760_009_110,
        at_me=True,
    )
    # Pre-warm chat metadata and verify that the 5 preceding ordinary messages do not call slow_chats
    t0 = time.perf_counter()
    for m in ordinary_msgs:
        await poller._handle_frontier_pushed_web_message(m)
    # Drain the 5 ordinary messages; because none of them are @me or contain matched URLs, slow_chats(3.0s) must NOT be called!
    assert poller._realtime_queue_worker is not None
    await asyncio.wait_for(poller._realtime_queue_worker, timeout=0.5)
    elapsed_ordinary = time.perf_counter() - t0
    assert elapsed_ordinary < 0.5
    assert build_calls == 1

    # Subsequent urgent @me reuses the same cached FeishuUserClient (build_calls stays 1)
    wt = poller._cached_user_client._get_web_transport()
    from claude_hub.services.feishu_web_session.rich import WebChat

    wt._remember_chat(
        WebChat(id=ORDINARY_CHAT_WEB_ID, open_chat_id="oc_ord_chat", name="研发讨论群")
    )
    monkeypatch.setattr(
        FeishuWebClient,
        "chats",
        lambda self, cids: asyncio.sleep(0, result=dict(wt._chat_cache)),
    )
    await poller._handle_frontier_pushed_web_message(urgent_at_me)
    await asyncio.wait_for(done.wait(), timeout=0.5)
    assert len(emitted) == 1
    assert build_calls == 1


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
async def test_configured_web_session_suppresses_cli_even_when_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import claude_hub.services.feishu_ai_poller as poller_mod
    from claude_hub.services.demand_patrol.feishu_triage import run_feishu_triage

    poller, _service, _client = _setup_poller(tmp_path)
    monkeypatch.setattr(poller_mod, "_DEFAULT_AI_POLLER", poller)
    state = PatrolState(config=PatrolConfig(enabled=True, feishu_enabled=True))
    now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
    runner = AsyncMock()
    assert not poller.realtime_stream_healthy
    assert not feishu_due(state, now)
    result = await run_feishu_triage(state, now=now, dot=AsyncMock(), runner=runner)
    runner.assert_not_awaited()
    assert "跳过" in result.markdown


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
    state = PatrolState(
        config=PatrolConfig(
            enabled=True,
            feishu_enabled=True,
            feishu_window_start="00:00",
            feishu_window_end="23:59",
        )
    )
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
    assert not feishu_due(state, now)

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
async def test_personal_ai_footer_suppresses_topic_reply(
    tmp_path: Path,
    mention_owner: bool,
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
    nodes.append(T("Here is the summary.\n🤖 AI 代答，仅供参考"))
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
                [T("I confirmed the issue")],
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
    transport._web.chatters.reset_mock()
    user_msg = transport.ingest_pushed_message(pushed)
    assert user_msg is not None
    assert not await poller.realtime_router.handle_pushed_message(
        pushed, replace(user_msg, sender_type="app"), web_transport=transport
    )
    transport._web.chatters.assert_not_awaited()
    sink.assert_not_awaited()
