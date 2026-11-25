"""Quoted main-chat messages must not inherit their citation as a topic root."""

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from claude_hub.services.feishu_web_session.adapter import FeishuWebUserTransport
from claude_hub.services.feishu_web_session.client import FeishuWebClient
from claude_hub.services.feishu_web_session.codec import decode, encode
from claude_hub.services.feishu_web_session.rich import WebChat, WebMessage, chat_of
from claude_hub.services.feishu_web_session.store import FeishuWebSession, FeishuWebSessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("topic_group", [0, 2, 3])
@pytest.mark.parametrize(
    "position,thread,explicit,seed,refresh_fails,alias,expected_topic",
    [
        (666, "10000", True, False, False, False, False),
        (666, "10000", True, True, False, False, False),
        (666, "10000", True, True, True, False, False),
        (666, "10000", True, True, False, True, False),
        (666, "10000", False, False, False, False, False),
        (-3, "10000", False, False, False, False, True),
        (-3, "10000", False, True, True, False, True),
        (-3, "", False, False, False, False, True),
    ],
)
async def test_quote_and_topic_reply_wire_roots(
    tmp_path: Path,
    position: int,
    thread: str,
    explicit: bool,
    seed: bool,
    refresh_fails: bool,
    alias: bool,
    expected_topic: bool,
    topic_group: int,
) -> None:
    store = FeishuWebSessionStore(path=tmp_path / "web.json", key_path=tmp_path / "web.key")
    store.save(FeishuWebSession(cookies={"session": "synthetic-session"}, user_id="30000"))
    original = WebMessage(
        id="10001",
        chat_id="20000",
        root_id="10000",
        parent_id="10000",
        thread_id=thread,
        position=position,
    )
    requests: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        packet = decode("Packet", request.content)
        command = packet["cmd"]
        if command == 8:
            if refresh_fails:
                return httpx.Response(503)
            data: dict[str, Any] = {
                "messages": {
                    "10001": {
                        "id": original.id,
                        "chatId": original.chat_id,
                        "rootId": original.root_id,
                        "parentId": original.parent_id,
                        "threadId": original.thread_id,
                        "position": original.position,
                    }
                }
            }
            query = decode(
                "lark_messages_PullMessagesByIdsRequest", base64.b64decode(packet["payload"])
            )
            if original.id not in query["messageIds"]:
                data = {"messages": {}}
            response_type = "lark_messages_PullMessagesByIdsResponse"
        else:
            assert command == 5, "Offline test allows only target reads and captured sends"
            body = decode("lark_messages_PutMessageRequest", base64.b64decode(packet["payload"]))
            requests.append(body)
            data = {
                "message": {
                    "id": str(11000 + len(requests)),
                    "chatId": "20000",
                    "rootId": body["rootId"],
                    "parentId": body["parentId"],
                    # Gateway receipts can omit threadId; topic identity must still survive.
                    "position": 0 if seed else (-3 if body["isReplyInThread"] else 667),
                }
            }
            response_type = "lark_messages_PutMessageResponse"
        return httpx.Response(
            200,
            content=encode(
                "Packet",
                {
                    "cmd": command,
                    "status": 0,
                    "payload": base64.b64encode(encode(response_type, data)).decode(),
                },
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http:
        transport = FeishuWebUserTransport(FeishuWebClient(store, http_client=http))
        transport._remember_chat(WebChat(id="20000"))
        normalized = transport.to_user_message(original, session=store.load())
        assert normalized is not None
        assert bool(normalized.thread_id) is expected_topic
        if seed:
            transport = FeishuWebUserTransport(FeishuWebClient(store, http_client=http))
            transport.remember_user_message(normalized)
        transport._remember_chat(chat_of({"id": "20000", "chatMode": topic_group}))
        if alias:
            transport.bind_message_alias("om_synthetic", original.id)
        receipt = await transport.reply_message(
            "om_synthetic" if alias else original.id,
            "text",
            {"text": "synthetic reply"},
            chat_id=original.chat_id,
            reply_in_thread=explicit,
        )
        expected_root = original.root_id if expected_topic or not explicit else original.id
        assert requests[0]["parentId"] == (
            expected_root if topic_group and (explicit or expected_topic) else original.id
        )
        assert requests[0]["rootId"] == expected_root
        assert requests[0]["isReplyInThread"] is ((explicit or expected_topic) and not topic_group)
        if explicit or expected_topic:
            assert receipt.thread_id == expected_root
            assert receipt.message_id is not None
            await transport.reply_message(receipt.message_id, "text", {"text": "followup"})
            assert requests[1]["rootId"] == expected_root
            assert requests[1]["isReplyInThread"] is (not topic_group)
        else:
            assert receipt.thread_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_position", [0, 17])
async def test_topic_pagination_keeps_explicit_topic_provenance(
    tmp_path: Path, chat_position: int
) -> None:
    store = FeishuWebSessionStore(path=tmp_path / "web.json", key_path=tmp_path / "web.key")
    store.save(FeishuWebSession(cookies={"session": "synthetic-session"}, user_id="30000"))
    sent: list[dict[str, Any]] = []
    commands: list[int] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        packet = decode("Packet", request.content)
        command = packet["cmd"]
        commands.append(command)
        if command == 8109:
            data: dict[str, Any] = {
                "messages": {
                    "2": {
                        "id": "10001",
                        "chatId": "20000",
                        "rootId": "10000",
                        "threadId": "10000",
                        "position": chat_position,
                        "threadPosition": 2,
                    }
                }
            }
            response_type = "thread_PullThreadMessagesByPositionsResponse"
        else:
            assert command == 5
            body = decode("lark_messages_PutMessageRequest", base64.b64decode(packet["payload"]))
            sent.append(body)
            data = {"message": {"id": "11001", "chatId": "20000", "rootId": body["rootId"]}}
            response_type = "lark_messages_PutMessageResponse"
        return httpx.Response(
            200,
            content=encode(
                "Packet",
                {
                    "cmd": command,
                    "status": 0,
                    "payload": base64.b64encode(encode(response_type, data)).decode(),
                },
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http:
        client = FeishuWebClient(store, http_client=http)
        messages = await client.topic_messages("10000", "20000", [2])
        assert (
            messages[0].position == chat_position
        )  # Chat position is not overwritten with topic position.
        assert messages[0].topic_position == 2
        transport = FeishuWebUserTransport(client)
        transport._remember_chat(WebChat(id="20000"))
        msg = transport.to_user_message(messages[0], session=store.load())
        assert msg is not None and msg.thread_id == "10000"
        await transport.reply_message(msg.message_id, "text", {"text": "synthetic followup"})
        assert sent[0]["rootId"] == "10000"
        assert sent[0]["isReplyInThread"] is True
        assert commands == [8109, 5]


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_position", [None, -1, 0, 1, 2])
async def test_wire_thread_coordinate_preserves_topic_in_positive_chat_positions(
    tmp_path: Path, thread_position: int | None
) -> None:
    from claude_hub.services.feishu_web_session.rich import message_of

    # THREAD groups expose positive chat positions plus an independent thread
    # coordinate. Normal P2P citations omit that field despite carrying root IDs.
    raw: dict[str, Any] = {
        "id": "10001",
        "chatId": "20000",
        "rootId": "10000",
        "parentId": "10000",
        "threadId": "10000",
        "position": 17,
    }
    if thread_position is not None:
        raw["threadPosition"] = thread_position
    if thread_position == -1:  # The existing topic's root message itself.
        raw.update(rootId="", parentId="", threadId="10001")
    msg = message_of(decode("lark_entities_Message", encode("lark_entities_Message", raw)))
    assert msg.topic_position == thread_position
    store = FeishuWebSessionStore(path=tmp_path / "web.json", key_path=tmp_path / "web.key")
    store.save(FeishuWebSession(cookies={"session": "synthetic"}, user_id="30000"))
    captured: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        packet = decode("Packet", request.content)
        assert packet["cmd"] == 5
        body = decode("lark_messages_PutMessageRequest", base64.b64decode(packet["payload"]))
        captured.append(body)
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http:
        adapter = FeishuWebUserTransport(FeishuWebClient(store, http_client=http))
        adapter._remember_chat(WebChat(id="20000"))
        normalized = adapter.to_user_message(msg, session=store.load())
        assert normalized is not None
        assert normalized.thread_id == (raw["threadId"] if thread_position is not None else None)
        from claude_hub.services.feishu_web_session.client import FeishuWebGatewayError

        with pytest.raises(FeishuWebGatewayError):
            await adapter.reply_message(
                "10001", "text", {"text": "Working"}, chat_id="20000", reply_in_thread=True
            )
    # Assertions stay outside the handler so production's read fallback cannot hide failures.
    assert len(captured) == 1
    assert captured[0]["parentId"] == "10001"
    assert captured[0]["rootId"] == (
        "10000" if thread_position is not None and thread_position >= 0 else "10001"
    )
    assert captured[0]["isReplyInThread"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("position,thread_position", [(17, 0), (18, 1), (19, 2)])
async def test_thread_interim_and_error_http400_never_start_agent(
    tmp_path: Path, position: int, thread_position: int
) -> None:
    from unittest.mock import AsyncMock

    from claude_hub.services.feishu_ai_command import FeishuAiCommandService
    from claude_hub.services.feishu_bot import FeishuMessageDedupStore
    from claude_hub.services.feishu_user_client import FeishuUserClient
    from claude_hub.services.feishu_user_token_store import FeishuUserTokenStore
    from claude_hub.services.feishu_web_session.rich import message_of

    raw = {
        "id": "10001",
        "chatId": "20000",
        "rootId": "10000",
        "parentId": "10000",
        "threadId": "10000",
        "fromId": "30000",
        "position": position,
        "threadPosition": thread_position,
    }
    message = message_of(decode("lark_entities_Message", encode("lark_entities_Message", raw)))
    store = FeishuWebSessionStore(path=tmp_path / "web.json", key_path=tmp_path / "web.key")
    store.save(FeishuWebSession(cookies={"session": "synthetic"}, user_id="30000"))
    captured: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        packet = decode("Packet", request.content)
        assert packet["cmd"] == 5, "Offline fixture rejects every request except captured replies"
        captured.append(
            decode("lark_messages_PutMessageRequest", base64.b64decode(packet["payload"]))
        )
        return httpx.Response(
            400,
            content=encode(
                "Packet",
                {
                    "status": 4001,
                    "payload": base64.b64encode(
                        encode("Status", {"code": 1234, "msg": "invalid rootId"})
                    ).decode(),
                },
            ),
            headers={"x-tt-logid": "20261124ABCDEF123456"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http:
        adapter = FeishuWebUserTransport(
            FeishuWebClient(store, http_client=http), owner_open_id="30000"
        )
        adapter._remember_chat(WebChat(id="20000"))
        normalized = adapter.to_user_message(message, session=store.load())
        assert normalized is not None
        from dataclasses import replace

        normalized = replace(normalized, text="/ai inspect this report")
        client = FeishuUserClient(
            token_store=FeishuUserTokenStore(
                path=tmp_path / "user.json",
                key_path=tmp_path / "user.key",
                lark_cli_dir=tmp_path / "no-cli",
                lark_cli_data_dir=tmp_path / "no-cli-data",
            ),
            open_id="30000",
            web_session_store=store,
            client=http,
            send_mode="web_session",
        )
        client._web_transport = adapter
        create_tab, dispatch = AsyncMock(), AsyncMock()
        service = FeishuAiCommandService(
            state_path=tmp_path / "ai.json",
            dedup_store=FeishuMessageDedupStore(path=tmp_path / "dedup.json"),
            user_client_factory=lambda _: client,
            tab_creator=create_tab,
            turn_dispatcher=dispatch,
        )
        service.update_config(
            {
                "enabled": True,
                "owner_open_id": "30000",
                "ack_emoji": "",
                "send_interim_reply": True,
            }
        )
        parent = replace(normalized, message_id="10000", parent_id=None, text="original message")
        result = await service.handle_message(normalized, seeded_context=[parent, normalized])
    assert result.status == "failed" and result.tab_id is None
    assert "HTTP 400" in result.reason
    assert '"packet_status":4001' in result.reason
    assert '"error_code":1234' in result.reason
    assert '"x_tt_logid":"20261124ABCDEF123456"' in result.reason
    assert service.get_recent_runs()[0]["reason"] == result.reason
    assert len(captured) == 2  # Interim first, then the existing error fallback.
    assert all(
        body["parentId"] == "10001"
        and body["rootId"] == "10000"
        and body["isReplyInThread"] is True
        for body in captured
    )
    assert all(body["type"] == 2 and "richText" in body["content"] for body in captured)
    create_tab.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("thread_position", [-1, 0, 1, 2])
def test_frontier_preserves_the_same_protocol_thread_coordinate(thread_position: int) -> None:
    from claude_hub.services.feishu_web_session.client import (
        build_frontier_frame,
        parse_frontier_frame,
    )

    raw = {
        "id": "10001",
        "chatId": "20000",
        "rootId": "10000",
        "parentId": "10000",
        "threadId": "10000",
        "position": 17,
        "threadPosition": thread_position,
    }
    frame = build_frontier_frame(
        {
            "cmd": 6,
            "payload": base64.b64encode(
                encode("PushMessagesRequest", {"messages": {"10001": raw}})
            ).decode(),
        }
    )
    _, cmd, pushed = parse_frontier_frame(frame)
    assert cmd == 6 and len(pushed) == 1
    assert pushed[0].message.topic_position == thread_position
    from claude_hub.services.feishu_web_session.adapter import is_in_topic

    assert is_in_topic(pushed[0].message)
