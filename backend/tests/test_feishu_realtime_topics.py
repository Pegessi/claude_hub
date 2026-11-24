"""Topic participation can be restored without a seeded identity mapping."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claude_hub.services.feishu_realtime_topics import (
    _is_newer_reply,
    _OwnerReplyMark,
    _reply_mark_is_newer_or_equal,
    owner_participated,
)
from claude_hub.services.feishu_user_client import FeishuMessagePage, FeishuUserMessage
from claude_hub.services.feishu_web_session.rich import WebMessage


def test_equal_chat_positions_use_topic_order() -> None:
    assert _is_newer_reply(1000, 9, 3, 1000, 9, 2)
    assert not _is_newer_reply(1000, 9, 2, 1000, 9, 3)
    assert _is_newer_reply(1000, 9, 2, 1000, 9, 2, direct_anchor=True)
    old = _OwnerReplyMark(1000, 10, 5, "old")
    assert not _reply_mark_is_newer_or_equal(_OwnerReplyMark(1000, 0, 5, "new"), old)
    assert not _reply_mark_is_newer_or_equal(_OwnerReplyMark(1000, 12, 0, "new"), old)


@pytest.mark.asyncio
async def test_owner_before_question_in_same_second_uses_topic_position() -> None:
    owner = FeishuUserMessage(
        message_id="7400000000000000001",
        chat_id="7300000000000000001",
        sender_open_id="7100000000000000001",
        sender_type="user",
        msg_type="text",
        text="earlier reply",
        create_time_ms=1760000000000,
    )
    client = SimpleNamespace(
        list_messages_page=AsyncMock(return_value=FeishuMessagePage(items=(owner,)))
    )
    transport = SimpleNamespace(
        _msg_cache={owner.message_id: WebMessage(id=owner.message_id, topic_position=2)}
    )
    assert await owner_participated(
        client,
        "7500000000000000001",
        {owner.sender_open_id},
        owner.create_time_ms,
        web_transport=transport,
        topic_position=3,
    )
    with pytest.raises(ValueError, match="ambiguous"):
        await owner_participated(
            client,
            "7500000000000000001",
            {owner.sender_open_id},
            owner.create_time_ms,
            web_transport=transport,
            topic_position=None,
        )


@pytest.mark.asyncio
async def test_owner_topic_root_precedes_reply_even_in_same_second() -> None:
    topic_id = "7500000000000000001"
    owner = FeishuUserMessage(
        message_id=topic_id,
        chat_id="7300000000000000001",
        sender_open_id="7100000000000000001",
        sender_type="user",
        msg_type="text",
        text="root",
        create_time_ms=1760000000000,
    )
    client = SimpleNamespace(
        list_messages_page=AsyncMock(return_value=FeishuMessagePage(items=(owner,)))
    )
    assert await owner_participated(
        client,
        topic_id,
        {owner.sender_open_id},
        owner.create_time_ms,
        web_transport=SimpleNamespace(_msg_cache={}),
        topic_position=0,
    )
