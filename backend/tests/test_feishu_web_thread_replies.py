"""Regression coverage for web-session thread replies."""

import threading
from typing import Any

import pytest

from claude_hub.services.feishu_user_client import FeishuUserMessage
from claude_hub.services.feishu_web_session import (
    FeishuWebClient,
    FeishuWebSessionStore,
    FeishuWebUserTransport,
    WebMessage,
    normalize_and_claim_ai_command_message,
)


@pytest.mark.asyncio
async def test_thread_followup_keeps_original_root(tmp_path: Any, monkeypatch: Any) -> None:
    chat_id = "7400000000000000001"
    root_id = "7400000000000000002"
    requests: list[dict[str, Any]] = []
    client = FeishuWebClient(FeishuWebSessionStore(tmp_path / "session.json"))

    async def call(command: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if command == "PullChatsByIdsRequest":
            return {"chats": {chat_id: {"id": chat_id, "chatMode": 1}}}
        assert command == "PutMessageRequest"
        requests.append(body)
        return {
            "message": {
                "id": str(7400000000000000002 + len(requests)),
                "chatId": chat_id,
                "rootId": body["rootId"],
                "parentId": body["parentId"],
            }
        }

    monkeypatch.setattr(client, "call", call)
    transport = FeishuWebUserTransport(client)
    transport._remember_msg(WebMessage(id=root_id, chat_id=chat_id))
    first = await transport.reply_message(root_id, "text", {"text": "ack"}, reply_in_thread=True)
    assert first.thread_id == root_id
    assert first.message_id is not None
    await transport.reply_message(first.message_id, "text", {"text": "follow-up"})
    assert [(r["parentId"], r["rootId"], r["isReplyInThread"]) for r in requests] == [
        (root_id, root_id, True),
        (first.message_id, root_id, True),
    ]


@pytest.mark.asyncio
async def test_incoming_thread_metadata_survives_fresh_transport(
    tmp_path: Any, monkeypatch: Any
) -> None:
    chat_id, root_id, message_id = (
        "7400000000000000001",
        "7400000000000000002",
        "7400000000000000003",
    )
    requests: list[dict[str, Any]] = []
    client = FeishuWebClient(FeishuWebSessionStore(tmp_path / "session.json"))

    async def call(command: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if command == "PullChatsByIdsRequest":
            return {"chats": {chat_id: {"id": chat_id, "chatMode": 1}}}
        assert command == "PutMessageRequest"  # PullMessagesByIdsRequest is unavailable.
        requests.append(body)
        return {"message": {"id": "7400000000000000004", "chatId": chat_id}}

    monkeypatch.setattr(client, "call", call)
    transport = FeishuWebUserTransport(client)
    user_client = type("UserClient", (), {"_get_web_transport": lambda self: transport})()
    message = FeishuUserMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_open_id="owner",
        sender_type="user",
        msg_type="text",
        text="/ai follow up",
        create_time_ms=0,
        root_id=root_id,
        thread_id=root_id,
        parent_id=root_id,
    )
    await normalize_and_claim_ai_command_message(
        message=message,
        parsed_instruction="follow up",
        owner_open_id="owner",
        user_client=user_client,
        dedup_store=None,
        lock=threading.RLock(),
        dry_run=True,
    )
    await transport.reply_message(message_id, "text", {"text": "reply"})
    assert requests[0]["parentId"] == message_id
    assert requests[0]["rootId"] == root_id
    assert requests[0]["isReplyInThread"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [1, 2, 3])
@pytest.mark.parametrize("shape", ["quote", "topic"])
@pytest.mark.parametrize("lookup", ["found", "empty", "fail"])
async def test_reply_chat_mode_matrix(mode: int, shape: str, lookup: str) -> None:
    from claude_hub.services.feishu_web_session.client import FeishuWebSessionError
    from claude_hub.services.feishu_web_session.rich import chat_of

    chat_id, message_id, root_id = (
        "7400000000000000001",
        "7400000000000000002",
        "7400000000000000003",
    )

    class Gateway:
        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def chats(self, ids: Any) -> dict[str, Any]:
            if lookup == "fail":
                raise TimeoutError("lookup failed before sending")
            return (
                {chat_id: chat_of({"id": chat_id, "chatMode": mode})} if lookup == "found" else {}
            )

        async def send(self, out: Any) -> WebMessage:
            self.sent.append(out)
            return WebMessage(id="7400000000000000004", chat_id=chat_id)

    gateway = Gateway()
    transport = FeishuWebUserTransport(gateway)  # type: ignore[arg-type]
    transport._remember_msg(
        WebMessage(
            id=message_id,
            chat_id=chat_id,
            root_id=root_id,
            thread_id=root_id,
            position=4,
            topic_position=2 if shape == "topic" else None,
        )
    )
    if shape == "topic" and lookup != "found":
        with pytest.raises(FeishuWebSessionError):
            await transport.reply_message(message_id, "text", {"text": "reply"}, chat_id=chat_id)
        assert not gateway.sent
        return
    await transport.reply_message(message_id, "text", {"text": "reply"}, chat_id=chat_id)
    out = gateway.sent[0]
    if shape == "topic" and mode in (2, 3):
        assert (out.reply_to.id, out.reply_to.root_id, out.in_thread) == (root_id, root_id, False)
    else:
        assert (out.reply_to.id, out.reply_to.root_id, out.in_thread) == (
            message_id,
            root_id,
            shape == "topic",
        )
