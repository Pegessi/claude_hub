"""Position recovery preserves numeric IDs and never checkpoints incomplete work."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claude_hub.services.feishu_web_replay import WebPositionReplay
from claude_hub.services.feishu_web_session.rich import (
    CHAT_P2P,
    FeedCard,
    FeedPage,
    WebChat,
    WebMessage,
)


@pytest.mark.asyncio
async def test_initial_snapshot_then_replay_without_aliases(tmp_path: Path) -> None:
    chat = WebChat(id="7300000000000000010", last_position=2)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
        ),
        messages_at=AsyncMock(),
        topics=AsyncMock(return_value={}),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    delivered = AsyncMock()
    await replay.catch_up(web, delivered)
    delivered.assert_not_awaited()
    later = WebChat(id=chat.id, last_position=4)
    web.feed_cards.return_value = FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: later})
    web.messages_at.return_value = [
        WebMessage(id="7400000000000000003", chat_id=chat.id, position=3),
        WebMessage(id="7400000000000000004", chat_id=chat.id, position=4),
    ]
    await replay.catch_up(web, delivered)
    assert [c.args[0].message.id for c in delivered.await_args_list] == [
        "7400000000000000003",
        "7400000000000000004",
    ]
    web.messages_at.reset_mock()
    await replay.catch_up(web, delivered)
    web.messages_at.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_page_does_not_advance_checkpoint(tmp_path: Path) -> None:
    chat = WebChat(id="7300000000000000010", last_position=1)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
        ),
        messages_at=AsyncMock(return_value=[]),
        topics=AsyncMock(return_value={}),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    await replay.catch_up(web, AsyncMock())
    later = WebChat(id=chat.id, last_position=2)
    web.feed_cards.return_value = FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: later})
    with pytest.raises(ValueError, match="unsettled"):
        await replay.catch_up(web, AsyncMock())
    assert replay._load()["chats"][chat.id] == 1


@pytest.mark.asyncio
async def test_sparse_positions_advance_only_after_visible_high_water(tmp_path: Path) -> None:
    chat = WebChat(id="7300000000000000010", last_position=0)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
        ),
        messages_at=AsyncMock(),
        topics=AsyncMock(return_value={}),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    await replay.catch_up(web, AsyncMock())
    later = WebChat(id=chat.id, last_position=3)
    web.feed_cards.return_value = FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: later})
    web.messages_at.return_value = [
        WebMessage(id="7400000000000000001", chat_id=chat.id, position=1),
        WebMessage(id="7400000000000000003", chat_id=chat.id, position=3),
    ]
    deliver = AsyncMock()
    await replay.catch_up(web, deliver)
    assert [call.args[0].message.position for call in deliver.await_args_list] == [1, 3]
    assert replay._load()["chats"][chat.id] == 3


@pytest.mark.asyncio
async def test_known_topic_replays_numeric_messages_from_saved_position(tmp_path: Path) -> None:
    replay = WebPositionReplay(tmp_path / "replay.json")
    replay.remember_topic("7500000000000000001", "7300000000000000001", 3)
    web = SimpleNamespace(
        topics=AsyncMock(return_value={"7500000000000000001": SimpleNamespace(last=4)}),
        topic_messages=AsyncMock(
            return_value=[
                WebMessage(
                    id="7400000000000000004",
                    chat_id="7300000000000000001",
                    thread_id="7500000000000000001",
                    topic_position=4,
                )
            ]
        ),
    )
    deliver = AsyncMock()
    await replay.catch_up_topics(web, deliver)
    assert deliver.await_args is not None
    assert deliver.await_args.args[0].message.id == "7400000000000000004"
    assert replay._load()["topics"]["7500000000000000001"]["position"] == 4


@pytest.mark.asyncio
async def test_topic_replay_preserves_p2p_chat_type(tmp_path: Path) -> None:
    replay = WebPositionReplay(tmp_path / "replay.json")
    topic_id, chat_id = "7500000000000000001", "7300000000000000001"
    replay.remember_topic(topic_id, chat_id, 0, CHAT_P2P)
    web = SimpleNamespace(
        topics=AsyncMock(return_value={topic_id: SimpleNamespace(last=1)}),
        topic_messages=AsyncMock(
            return_value=[WebMessage(id="7400000000000000001", chat_id=chat_id, topic_position=1)]
        ),
    )
    deliver = AsyncMock()
    await replay.catch_up_topics(web, deliver)
    assert deliver.await_args is not None
    assert deliver.await_args.args[0].chat_type == CHAT_P2P


@pytest.mark.asyncio
async def test_known_chat_missing_from_feeds_is_queried_by_id(tmp_path: Path) -> None:
    chat_id = "7300000000000000010"
    chat = WebChat(id=chat_id, last_position=0)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat_id),), chats={chat_id: chat})
        ),
        chats=AsyncMock(),
        messages_at=AsyncMock(),
        topics=AsyncMock(return_value={}),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    await replay.catch_up(web, AsyncMock())
    web.feed_cards.return_value = FeedPage()
    web.chats.return_value = {chat_id: WebChat(id=chat_id, last_position=1)}
    web.messages_at.return_value = [
        WebMessage(id="7400000000000000011", chat_id=chat_id, position=1)
    ]
    deliver = AsyncMock()
    await replay.catch_up(web, deliver)
    web.chats.assert_awaited_once_with([chat_id])
    assert deliver.await_args is not None
    assert deliver.await_args.args[0].message.id == "7400000000000000011"


@pytest.mark.asyncio
async def test_topic_replay_respects_server_start_position(tmp_path: Path) -> None:
    replay = WebPositionReplay(tmp_path / "replay.json")
    topic_id, chat_id = "7500000000000000001", "7300000000000000001"
    replay.remember_topic(topic_id, chat_id, -1)
    web = SimpleNamespace(
        topics=AsyncMock(return_value={topic_id: SimpleNamespace(start=4, last=4)}),
        topic_messages=AsyncMock(
            return_value=[WebMessage(id="7400000000000000004", chat_id=chat_id, topic_position=4)]
        ),
    )
    await replay.catch_up_topics(web, AsyncMock())
    web.topic_messages.assert_awaited_once_with(topic_id, chat_id, [4])


@pytest.mark.asyncio
async def test_new_empty_chat_and_empty_topic_do_not_block_replay(tmp_path: Path) -> None:
    replay = WebPositionReplay(tmp_path / "replay.json")
    chat = WebChat(id="7300000000000000070")
    web = SimpleNamespace(
        feed_cards=AsyncMock(return_value=FeedPage()),
        messages_at=AsyncMock(),
        topics=AsyncMock(),
        topic_messages=AsyncMock(),
    )
    await replay.catch_up(web, AsyncMock())
    web.feed_cards.return_value = FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
    await replay.catch_up(web, AsyncMock())
    web.messages_at.assert_not_awaited()
    topic_id = "7500000000000000070"
    replay.remember_topic(topic_id, chat.id, 0)
    web.topics.return_value = {topic_id: SimpleNamespace(last=-1, start=1, replies=0)}
    await replay.catch_up_topics(web, AsyncMock())
    web.topic_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_does_not_advance_past_failed_position(tmp_path: Path) -> None:
    chat = WebChat(id="7300000000000000020", last_position=1)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
        ),
        messages_at=AsyncMock(),
        topics=AsyncMock(return_value={}),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    await replay.catch_up(web, AsyncMock())
    later = WebChat(id=chat.id, last_position=3)
    web.feed_cards.return_value = FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: later})
    web.messages_at.return_value = [
        WebMessage(id="7400000000000000002", chat_id=chat.id, position=2),
        WebMessage(id="7400000000000000003", chat_id=chat.id, position=3),
    ]

    async def deliver(pushed: object) -> None:
        if pushed.message.position == 3:  # type: ignore[attr-defined]
            raise RuntimeError("delivery failed")

    with pytest.raises(RuntimeError, match="delivery failed"):
        await replay.catch_up(web, deliver)
    assert replay._load()["chats"][chat.id] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [False, True])
async def test_snapshot_discovers_old_topic_without_delivering_history(
    tmp_path: Path, thread: bool
) -> None:
    chat_id = "7300000000000000030"
    topic_id = "7500000000000000030"
    chat = WebChat(id=chat_id, thread=thread, has_last=True, last_position=1)
    root = WebMessage(id=topic_id, chat_id=chat_id, position=1)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat_id),), chats={chat_id: chat})
        ),
        messages_at=AsyncMock(
            return_value=[WebMessage(id="7400000000000000030", chat_id=chat_id, position=0), root]
        ),
        topics=AsyncMock(return_value={topic_id: SimpleNamespace(chat=chat_id, last=4, start=0)}),
        topic_messages=AsyncMock(),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    deliver = AsyncMock()
    await replay.catch_up(web, deliver)
    deliver.assert_not_awaited()
    assert replay._load()["topics"][topic_id]["position"] == 4
    web.topics.return_value = {topic_id: SimpleNamespace(chat=chat_id, last=5, start=0)}
    web.topic_messages.return_value = [
        WebMessage(id="7400000000000000031", chat_id=chat_id, topic_position=5)
    ]
    await replay.catch_up_topics(web, deliver)
    assert deliver.await_args is not None
    assert deliver.await_args.args[0].message.id == "7400000000000000031"


@pytest.mark.asyncio
async def test_snapshot_keeps_empty_topic_for_first_offline_reply(tmp_path: Path) -> None:
    chat_id = "7300000000000000035"
    topic_id = "7500000000000000035"
    chat = WebChat(id=chat_id, thread=True, has_last=True, last_position=0)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat_id),), chats={chat_id: chat})
        ),
        messages_at=AsyncMock(return_value=[WebMessage(id=topic_id, chat_id=chat_id, position=0)]),
        topics=AsyncMock(return_value={topic_id: SimpleNamespace(chat=chat_id, last=-1, start=0)}),
        topic_messages=AsyncMock(
            return_value=[WebMessage(id="7400000000000000035", chat_id=chat_id, topic_position=0)]
        ),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    await replay.catch_up(web, AsyncMock())
    assert replay._load()["topics"][topic_id]["position"] == -1
    web.topics.return_value = {topic_id: SimpleNamespace(chat=chat_id, last=0, start=0)}
    deliver = AsyncMock()
    await replay.catch_up_topics(web, deliver)
    assert deliver.await_args is not None
    assert deliver.await_args.args[0].message.id == "7400000000000000035"


@pytest.mark.asyncio
async def test_topic_registered_during_snapshot_uses_snapshot_baseline(tmp_path: Path) -> None:
    chat_id, topic_id = "7300000000000000036", "7500000000000000036"
    chat = WebChat(id=chat_id, thread=True, has_last=True, last_position=0)
    entered, release = asyncio.Event(), asyncio.Event()

    async def messages_at(_chat_id: str, _positions: list[int]) -> list[WebMessage]:
        entered.set()
        await release.wait()
        return [WebMessage(id=topic_id, chat_id=chat_id, position=0)]

    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat_id),), chats={chat_id: chat})
        ),
        messages_at=messages_at,
        topics=AsyncMock(return_value={topic_id: SimpleNamespace(chat=chat_id, last=4)}),
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    snapshot = asyncio.create_task(replay.catch_up(web, AsyncMock()))
    await asyncio.wait_for(entered.wait(), 2)
    assert replay.remember_topic(topic_id, chat_id, -1)
    release.set()
    await snapshot
    assert replay._load()["topics"][topic_id]["position"] == 4


@pytest.mark.asyncio
async def test_live_checkpoint_advances_only_contiguous_positions(tmp_path: Path) -> None:
    chat = WebChat(id="7300000000000000050", last_position=0)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
        )
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    await replay.catch_up(web, AsyncMock())
    second = WebMessage(id="7400000000000000052", chat_id=chat.id, position=1)
    first = WebMessage(id="7400000000000000051", chat_id=chat.id, position=0)
    replay.mark_handled(second)
    assert replay._load()["chats"][chat.id] == -1
    replay.mark_handled(first)
    assert replay._load()["chats"][chat.id] == 0
    replay.mark_handled(second)
    assert replay._load()["chats"][chat.id] == 1


@pytest.mark.asyncio
async def test_topic_reply_does_not_advance_main_chat_position(tmp_path: Path) -> None:
    chat = WebChat(id="7300000000000000050", last_position=0)
    web = SimpleNamespace(
        feed_cards=AsyncMock(
            return_value=FeedPage(cards=(FeedCard(id=chat.id),), chats={chat.id: chat})
        )
    )
    replay = WebPositionReplay(tmp_path / "replay.json")
    await replay.catch_up(web, AsyncMock())
    replay.mark_handled(
        WebMessage(id="7400000000000000051", chat_id=chat.id, position=1, topic_position=1)
    )
    assert replay._load()["chats"][chat.id] == -1
