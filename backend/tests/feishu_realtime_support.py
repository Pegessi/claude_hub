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
    router = FeishuRealtimeEventRouter(
        config=realtime_config,
        event_sink=event_sink,
    )
    poller = FeishuAiPoller(service, realtime_router=router)
    return poller, service, user_client
