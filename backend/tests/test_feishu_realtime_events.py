"""Unit tests for real-time Feishu WebSocket event routing, courtesy/self-reply suppression, and /ai isolation."""

from __future__ import annotations

import asyncio
import base64
import json
import time
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
async def test_ai_command_unaffected_and_records_owner_activity(tmp_path: Path) -> None:
    emitted: list[ExternalEvent] = []
    dispatched_prompts: list[str] = []

    async def sink(event: ExternalEvent) -> bool:
        emitted.append(event)
        return True

    poller, service, _uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(
            enabled=True,
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

    should_fail = True

    async def flaky_sink(_event: ExternalEvent) -> bool:
        if should_fail:
            raise httpx.ConnectError("dot gateway temporarily unreachable")
        return True

    poller, _svc, _uc = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(enabled=True),
        event_sink=flaky_sink,
    )
    monkeypatch.setattr(poller_mod, "_DEFAULT_AI_POLLER", poller)
    monkeypatch.setattr(
        "claude_hub.services.demand_patrol.service.get_feishu_realtime_event_router",
        lambda: poller.realtime_router,
    )
    poller._frontier_task = type("FakeTask", (), {"done": lambda self: False})()  # type: ignore[assignment]
    poller._on_frontier_state(None)

    now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
    state = PatrolState(config=PatrolConfig(enabled=True, feishu_enabled=True))
    assert is_feishu_realtime_stream_healthy() is True
    assert feishu_due(state, now) is False

    # 1. Unmentioned actionable P2P question is not covered by @me/watch-URL realtime rules -> marks fallback needed
    plain_dm = _build_pushed_via_wire(
        msg_id="7400000000000001001",
        from_id=COLLEAGUE_WEB_UID,
        chat_id=P2P_CHAT_WEB_ID,
        chat_type=CHAT_P2P,
        nodes=[T("服务启动报错 OOM 了，能帮忙看看吗？")],
        create_time_s=1_760_001_001,
        at_me=False,
    )
    assert await poller.handle_pushed_web_message(plain_dm) is False
    assert poller.realtime_router.needs_patrol_fallback is True
    assert is_feishu_realtime_stream_healthy() is False
    assert feishu_due(state, now, realtime_healthy=True) is True

    poller.realtime_router.acknowledge_patrol_fallback()
    assert is_feishu_realtime_stream_healthy() is True
    assert feishu_due(state, now) is False

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
    assert feishu_due(state, now) is True
    poller._frontier_task = None


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

    # Verify 20m slow patrol is suppressed when the WebSocket realtime stream is healthy
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
    poller._frontier_task = type("FakeTask", (), {"done": lambda self: False})()  # type: ignore[assignment]
    poller._on_frontier_state(None)
    assert is_feishu_realtime_stream_healthy() is True
    assert feishu_due(state, now) is False

    poller._on_frontier_state(RuntimeError("ws closed"))
    assert is_feishu_realtime_stream_healthy() is False
    assert feishu_due(state, now) is True
    poller._frontier_task = None
