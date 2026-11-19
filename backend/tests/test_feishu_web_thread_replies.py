"""Regression coverage for web-session thread replies."""

from typing import Any

import pytest

from claude_hub.services.feishu_web_session import (
    FeishuWebClient,
    FeishuWebSessionStore,
    FeishuWebUserTransport,
    WebMessage,
)


@pytest.mark.asyncio
async def test_thread_followup_keeps_original_root(tmp_path: Any, monkeypatch: Any) -> None:
    chat_id = "7400000000000000001"
    root_id = "7400000000000000002"
    requests: list[dict[str, Any]] = []
    client = FeishuWebClient(FeishuWebSessionStore(tmp_path / "session.json"))

    async def call(command: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
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
