"""Channel/sender dispatch contracts, without live Feishu or native provider I/O."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from claude_hub.api import agent_stream as stream_api
from claude_hub.api import feishu_bot as bot_api
from claude_hub.api.agent_stream import ExternalDispatchRetired
from claude_hub.api.routes.agent_stream import messages as messages_api
from claude_hub.api.routes.feishu_bot.message_dispatch import FeishuMessageTimeRejected
from claude_hub.api.routes.feishu_bot.queueing import TabDispatchItem, TabDispatchQueue
from claude_hub.services import dot
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V3,
    format_feishu_provider_text_v1,
    format_feishu_provider_text_v2,
    format_feishu_provider_text_v3,
    is_feishu_provider_text,
    provider_text_for_stored_turn,
)
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuMessageEvent,
    FeishuMessageTimeStatus,
)
from claude_hub.services.feishu_bot_pool import BotBinding, EffectiveBot, OwnerIdentity, turn_id_for


class DispatchHarness:
    def __init__(self) -> None:
        self.effective = EffectiveBot(
            bot_id="bot",
            app_id="app",
            config=FeishuBotConfig(app_id="app", app_secret="test"),
            revision=3,
            generation=2,
            feishu_name="Test Bot",
            feishu_avatar_url=None,
            feishu_open_id="bot-open-id",
        )
        self.binding = BotBinding(
            pairing_id="pair-a",
            bot_id="bot",
            owner=OwnerIdentity("owner", "", "local"),
            tab_id="tab",
            workspace_id=None,
            sender_open_id="pairing-owner",
            chat_id="room-a",
            created_at=1,
            generation=2,
            chat_type="group",
            chat_name="研发群",
        )
        self.client = Mock(spec=FeishuBotClient)
        self.submitted: list[dict[str, Any]] = []
        self.reply = AsyncMock(return_value=True)
        self.blocked: set[str] = set()
        self.completions: dict[str, asyncio.Future[str]] = {}
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.stop = AsyncMock(side_effect=self.cancel_turn)

    def reserve(
        self,
        message: str,
        *,
        sender: str = "alice",
        binding: BotBinding | None = None,
        thread: str | None = "topic",
        effective: EffectiveBot | None = None,
        event_chat_id: str | None = None,
        event_chat_type: str | None = None,
    ) -> TabDispatchItem:
        binding = binding or self.binding
        effective = effective or self.effective
        return bot_api._reserve_tab_dispatch(
            "tab",
            bot_id=effective.bot_id,
            effective=effective,
            binding=binding,
            client=self.client,
            event=FeishuMessageEvent(
                event_id=f"ev-{message}",
                message_id=message,
                app_id=effective.app_id,
                sender_open_id=sender,
                chat_id=event_chat_id or binding.chat_id,
                text=message,
                message_created_at_ms=2000,
                chat_type=event_chat_type or binding.chat_type,
                thread_id=thread,
            ),
        )

    async def dispatch(
        self,
        tab_id: str,
        text: str,
        turn_id: str,
        *,
        admission_guard: Any,
        visible_text: str,
        turn_metadata: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        async with admission_guard():
            self.submitted.append(
                {"text": text, "visible": visible_text, "metadata": turn_metadata}
            )
        message = turn_metadata["feishu"]["message_id"]
        if message in self.blocked:
            self.completions[turn_id] = asyncio.get_running_loop().create_future()
        self.started.put_nowait(message)
        if message in self.blocked:
            return await self.completions[turn_id]
        return f"answer-{message}"

    async def cancel_turn(self, session: Any, *, expected_turn_id: str) -> bool:
        future = self.completions[expected_turn_id]
        if future.done():
            return False
        future.set_exception(RuntimeError("provider turn stopped"))
        return True

    async def run(self, item: TabDispatchItem, queue: TabDispatchQueue) -> str:
        assert queue.processor is item, "batch dispatch must respect FIFO promotion"
        return await asyncio.wait_for(bot_api._dispatch_tab_batch("tab", queue, item), timeout=2)

    async def wait_started(self, message: str) -> None:
        assert await asyncio.wait_for(self.started.get(), timeout=2) == message


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> DispatchHarness:
    harness = DispatchHarness()
    monkeypatch.setattr(bot_api, "_tab_dispatch_queues", {})
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.001)
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.004)
    monkeypatch.setattr(
        bot_api._message_dispatcher, "_validate_binding_dispatch", lambda *args: None
    )
    monkeypatch.setattr(bot_api, "_event_attachments", AsyncMock(return_value=([], 0, [])))
    monkeypatch.setattr(bot_api, "_reply_if_current", harness.reply)
    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", harness.dispatch)
    monkeypatch.setattr(messages_api, "get_inbox", lambda: SimpleNamespace(questions={}))
    monkeypatch.setattr(
        dot, "get_dot_service", lambda: SimpleNamespace(is_dot_tab=lambda tab: True)
    )
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab: object())
    monkeypatch.setattr(
        stream_api, "_get_tab_tailer_manager", lambda: SimpleNamespace(cancel_turn=harness.stop)
    )
    return harness


async def test_different_senders_in_one_topic_never_merge_or_share_followup(dispatch):
    first = dispatch.reserve("a1")
    second = dispatch.reserve("b1", sender="bob")
    third = dispatch.reserve("a2")
    queue = bot_api._tab_dispatch_queues["tab"]
    for item in (first, second, third):
        assert await dispatch.run(item, queue) == "completed"
    assert [call["metadata"]["feishu_message_ids"] for call in dispatch.submitted] == [
        ["a1"],
        ["b1"],
        ["a2"],
    ]
    assert [call["metadata"]["feishu_followup"] for call in dispatch.submitted] == [
        False,
        False,
        True,
    ]
    assert [call.args[1].message_id for call in dispatch.reply.await_args_list] == [
        "a1",
        "b1",
        "a2",
    ]
    dispatch.stop.assert_not_awaited()


async def test_same_sender_same_channel_coalesces_and_replies_once(dispatch):
    first = dispatch.reserve("a1")
    second = dispatch.reserve("a2")
    queue = bot_api._tab_dispatch_queues["tab"]
    assert await dispatch.run(first, queue) == "completed"
    assert second.outcome.result() == "completed"
    assert dispatch.submitted[0]["visible"] == (
        "用户在短时间内连续发送了以下消息，请作为一次请求统一处理：\n\n[消息 1]\na1\n\n[消息 2]\na2"
    )
    assert dispatch.submitted[0]["metadata"]["feishu_message_ids"] == ["a1", "a2"]
    dispatch.reply.assert_awaited_once()
    assert dispatch.reply.await_args.args[1].message_id == "a2"


@pytest.mark.parametrize("boundary", ["room", "pairing", "type", "topic", "bot", "generation"])
async def test_channel_and_version_boundaries_neither_coalesce_nor_interrupt(dispatch, boundary):
    binding = dispatch.binding
    effective = dispatch.effective
    thread = "topic"
    if boundary == "room":
        binding = replace(binding, chat_id="room-b", pairing_id="pair-b")
    elif boundary == "pairing":
        binding = replace(binding, pairing_id="rebound")
    elif boundary == "type":
        binding = replace(binding, chat_type="p2p")
    elif boundary == "topic":
        thread = "another-topic"
    elif boundary == "bot":
        effective = replace(effective, bot_id="another-bot")
        binding = replace(binding, bot_id=effective.bot_id)
    else:
        effective = replace(effective, generation=3)
    dispatch.blocked.add("first")
    first = dispatch.reserve("first")
    queue = bot_api._tab_dispatch_queues["tab"]
    running = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("first")
    second = dispatch.reserve("second", binding=binding, effective=effective, thread=thread)
    assert not bot_api._message_dispatcher._batch_matches(first, second)
    assert not first.superseded
    assert first.stop_task is None
    assert queue.pending == [second]
    assert not second.start.done()
    dispatch.stop.assert_not_called()
    dispatch.completions[turn_id_for("bot", "first")].set_result("answer-first")
    assert await running == "completed"
    assert await dispatch.run(second, queue) == "completed"
    assert [call["metadata"]["feishu_message_ids"] for call in dispatch.submitted] == [
        ["first"],
        ["second"],
    ]
    assert "[上一条]" not in dispatch.submitted[1]["visible"]
    assert dispatch.reply.await_count == 2


async def test_cross_channel_bursts_do_not_merge_or_jump_ahead_in_fifo(dispatch):
    first = dispatch.reserve("a1")
    other = dispatch.reserve(
        "b1", binding=replace(dispatch.binding, chat_id="room-b", pairing_id="pair-b")
    )
    latest = dispatch.reserve("a2")
    queue = bot_api._tab_dispatch_queues["tab"]
    for item in (first, other, latest):
        assert await dispatch.run(item, queue) == "completed"
    assert [call["metadata"]["feishu_message_ids"] for call in dispatch.submitted] == [
        ["a1"],
        ["b1"],
        ["a2"],
    ]
    assert dispatch.submitted[1]["visible"] == "b1"
    assert [call.args[1].chat_id for call in dispatch.reply.await_args_list] == [
        "room-a",
        "room-b",
        "room-a",
    ]


async def test_late_stop_of_completed_answer_does_not_discard_other_channel_text(dispatch):
    dispatch.blocked.add("a1")
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    async def late_stop(session, *, expected_turn_id):
        stop_started.set()
        await release_stop.wait()
        return True

    dispatch.stop.side_effect = late_stop
    first = dispatch.reserve("a1")
    queue = bot_api._tab_dispatch_queues["tab"]
    running = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("a1")
    other = dispatch.reserve(
        "b2", binding=replace(dispatch.binding, chat_id="room-b", pairing_id="pair-b")
    )
    queue.interrupted_text[other.channel_key] = "b1"
    latest = dispatch.reserve("a2")
    await asyncio.wait_for(stop_started.wait(), timeout=2)
    dispatch.completions[turn_id_for("bot", "a1")].set_result("answer-a1")
    # The native answer won the race; the stop result arrives afterwards.
    await asyncio.sleep(0)
    release_stop.set()
    assert await running == "completed"
    assert dict(queue.interrupted_text) == {other.channel_key: "b1"}
    assert await dispatch.run(other, queue) == "completed"
    assert "[上一条]\nb1" in dispatch.submitted[1]["visible"]
    assert await dispatch.run(latest, queue) == "completed"
    assert "[上一条]" not in dispatch.submitted[2]["visible"]
    assert dispatch.reply.await_count == 3
    assert not queue.interrupted_text


@pytest.mark.parametrize("sender", ["alice", "bob"])
async def test_same_channel_supersedes_even_when_sender_changes(dispatch, sender):
    dispatch.blocked.add("a1")
    first = dispatch.reserve("a1")
    queue = bot_api._tab_dispatch_queues["tab"]
    running = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("a1")
    second = dispatch.reserve("a2", sender=sender)
    assert await running == "superseded"
    assert queue.interrupted_text[first.channel_key] == "a1"
    assert await dispatch.run(second, queue) == "completed"
    assert dispatch.submitted[1]["visible"] == (
        "你回复用户上一条消息时，用户又发来了新消息，上一次回复已停止、未发出。"
        "请结合两者给出一个回复：\n\n[上一条]\na1\n\n[新消息 1]\na2"
    )
    dispatch.stop.assert_awaited_once()
    dispatch.reply.assert_awaited_once()
    assert dispatch.reply.await_args.args[1].message_id == "a2"
    assert not queue.interrupted_text


@pytest.mark.parametrize("stop_succeeds", [True, False])
async def test_a1_b1_a2_keeps_fifo_and_channel_scoped_carryover(dispatch, stop_succeeds):
    dispatch.blocked.add("a1")
    if not stop_succeeds:
        dispatch.stop.side_effect = None
        dispatch.stop.return_value = False
    first = dispatch.reserve("a1")
    queue = bot_api._tab_dispatch_queues["tab"]
    running = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("a1")
    other = dispatch.reserve(
        "b1", binding=replace(dispatch.binding, chat_id="room-b", pairing_id="pair-b")
    )
    dispatch.stop.assert_not_called()
    latest = dispatch.reserve("a2")
    assert queue.pending == [other, latest]
    assert first.stop_task is not None
    assert await first.stop_task is stop_succeeds
    if not stop_succeeds:
        dispatch.completions[turn_id_for("bot", "a1")].set_result("answer-a1")
    assert await running == ("superseded" if stop_succeeds else "completed")
    assert await dispatch.run(other, queue) == "completed"
    assert dispatch.submitted[1]["visible"] == "b1"
    assert (first.channel_key in queue.interrupted_text) is stop_succeeds
    assert await dispatch.run(latest, queue) == "completed"
    assert ("[上一条]\na1" in dispatch.submitted[2]["visible"]) is stop_succeeds
    assert [call["metadata"]["feishu"]["message_id"] for call in dispatch.submitted] == [
        "a1",
        "b1",
        "a2",
    ]
    assert dispatch.reply.await_count == (2 if stop_succeeds else 3)
    assert not queue.interrupted_text


async def test_two_channels_can_each_retain_stopped_text(dispatch):
    dispatch.blocked.update({"a1", "b1"})
    first = dispatch.reserve("a1")
    queue = bot_api._tab_dispatch_queues["tab"]
    running_a = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("a1")
    binding_b = replace(dispatch.binding, chat_id="room-b", pairing_id="pair-b")
    other = dispatch.reserve("b1", binding=binding_b)
    latest_a = dispatch.reserve("a2")
    assert await running_a == "superseded"
    running_b = asyncio.create_task(dispatch.run(other, queue))
    await dispatch.wait_started("b1")
    latest_b = dispatch.reserve("b2", binding=binding_b)
    assert await running_b == "superseded"
    assert dict(queue.interrupted_text) == {first.channel_key: "a1", other.channel_key: "b1"}
    assert await dispatch.run(latest_a, queue) == "completed"
    assert "[上一条]\na1" in dispatch.submitted[2]["visible"]
    assert dict(queue.interrupted_text) == {other.channel_key: "b1"}
    assert await dispatch.run(latest_b, queue) == "completed"
    assert "[上一条]\nb1" in dispatch.submitted[3]["visible"]
    assert "a1" not in dispatch.submitted[3]["visible"]
    assert not queue.interrupted_text


@pytest.mark.parametrize(
    "failure,expected_status",
    [
        ("admission_retired", "retired"),
        ("time_rejected", "pre_dispatch_rejected"),
        ("http_rejected", "failed"),
        ("cancelled_before_handoff", "retired"),
    ],
)
async def test_carryover_survives_unsubmitted_batch_and_reaches_later_arrival(
    dispatch: DispatchHarness,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_status: str,
) -> None:
    """When A2 fails or retires before native handoff with no A3 queued yet,
    the stopped words from A1 must survive both the failed admission and the
    idle queue cleanup so an A3 arriving later still answers A1 + A3."""

    dispatch.blocked.add("a1")
    first = dispatch.reserve("a1")
    queue = bot_api._tab_dispatch_queues["tab"]
    running_a1 = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("a1")
    second = dispatch.reserve("a2")
    assert await running_a1 == "superseded"
    assert dict(queue.interrupted_text) == {first.channel_key: "a1"}

    if failure == "cancelled_before_handoff":
        monkeypatch.setattr(
            bot_api._message_dispatcher.bindings,
            "event_attachments",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
    else:

        def fail_admission(*_args: Any) -> None:
            if failure == "admission_retired":
                raise ExternalDispatchRetired(turn_id_for("bot", "a2"))
            if failure == "time_rejected":
                raise FeishuMessageTimeRejected(FeishuMessageTimeStatus.MESSAGE_BEFORE_BINDING)
            raise HTTPException(
                status_code=409,
                detail="busy",
                headers={"X-Claude-Hub-Error-Reason": "chat_busy"},
            )

        monkeypatch.setattr(
            bot_api._message_dispatcher, "_validate_binding_dispatch", fail_admission
        )

    assert await dispatch.run(second, queue) == expected_status
    # Only a1 was ever submitted to the native bridge; a2 never reached it.
    assert [call["metadata"]["feishu_message_ids"] for call in dispatch.submitted] == [["a1"]]
    assert queue.pending == []
    assert queue.active == []
    assert dict(queue.interrupted_text) == {first.channel_key: "a1"}
    assert bot_api._tab_dispatch_queues.get("tab") is queue

    # Restore normal admission; A3 arrives AFTER A2 has already retired/failed.
    monkeypatch.setattr(
        bot_api._message_dispatcher, "_validate_binding_dispatch", lambda *_args: None
    )
    monkeypatch.setattr(
        bot_api._message_dispatcher.bindings,
        "event_attachments",
        AsyncMock(return_value=([], 0, [])),
    )
    third = dispatch.reserve("a3")
    assert await dispatch.run(third, queue) == "completed"
    assert [call["metadata"]["feishu_message_ids"] for call in dispatch.submitted] == [
        ["a1"],
        ["a3"],
    ]
    assert "[上一条]\na1" in dispatch.submitted[1]["visible"]
    assert "[新消息 1]\na3" in dispatch.submitted[1]["visible"]
    assert not queue.interrupted_text
    assert bot_api._tab_dispatch_queues == {}


async def test_cancelled_leader_empty_batch_retires_cleanly_and_preserves_carryover(
    dispatch: DispatchHarness,
) -> None:
    """When a batch leader is cancelled before collection so _collect_tab_dispatch_batch
    returns no candidates, _dispatch_tab_batch must return 'retired' without
    UnboundLocalError and leave any existing carryover intact."""

    dispatch.blocked.add("a1")
    first = dispatch.reserve("a1")
    queue = bot_api._tab_dispatch_queues["tab"]
    running_a1 = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("a1")
    second = dispatch.reserve("a2")
    assert await running_a1 == "superseded"
    assert dict(queue.interrupted_text) == {first.channel_key: "a1"}

    # Cancel the promoted leader before _collect_tab_dispatch_batch claims it.
    second.cancelled = True
    queue.changed.set()
    assert await dispatch.run(second, queue) == "retired"
    assert [call["metadata"]["feishu_message_ids"] for call in dispatch.submitted] == [["a1"]]
    assert dict(queue.interrupted_text) == {first.channel_key: "a1"}
    assert bot_api._tab_dispatch_queues.get("tab") is queue

    third = dispatch.reserve("a3")
    assert await dispatch.run(third, queue) == "completed"
    assert "[上一条]\na1" in dispatch.submitted[1]["visible"]
    assert "[新消息 1]\na3" in dispatch.submitted[1]["visible"]
    assert not queue.interrupted_text
    assert bot_api._tab_dispatch_queues == {}


async def test_sibling_rename_or_revision_bump_preserves_interruption_and_carryover(
    dispatch: DispatchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Administrative CAS revision bumps (Bot rename, sibling channel bind/release)
    between two messages of the same channel must not change channel_key: A2 still
    interrupts A1, and if A2 retires before handoff, A3 at yet another revision
    still inherits A1's stopped carryover text."""

    dispatch.blocked.add("a1")
    first = dispatch.reserve("a1", effective=replace(dispatch.effective, revision=3))
    queue = bot_api._tab_dispatch_queues["tab"]
    running_a1 = asyncio.create_task(dispatch.run(first, queue))
    await dispatch.wait_started("a1")

    # Sibling rename / bind advances revision from 3 -> 4 before A2 arrives.
    second = dispatch.reserve("a2", effective=replace(dispatch.effective, revision=4))
    assert second.channel_key == first.channel_key
    assert first.superseded
    assert await running_a1 == "superseded"
    assert dict(queue.interrupted_text) == {first.channel_key: "a1"}

    # Make A2 retire before handoff, then advance revision again (4 -> 5) before A3.
    monkeypatch.setattr(
        bot_api._message_dispatcher,
        "_validate_binding_dispatch",
        lambda *_args: (_ for _ in ()).throw(ExternalDispatchRetired(turn_id_for("bot", "a2"))),
    )
    assert await dispatch.run(second, queue) == "retired"
    assert dict(queue.interrupted_text) == {first.channel_key: "a1"}

    monkeypatch.setattr(
        bot_api._message_dispatcher, "_validate_binding_dispatch", lambda *_args: None
    )
    third = dispatch.reserve("a3", effective=replace(dispatch.effective, revision=5))
    assert third.channel_key == first.channel_key
    assert await dispatch.run(third, queue) == "completed"
    assert "[上一条]\na1" in dispatch.submitted[1]["visible"]
    assert "[新消息 1]\na3" in dispatch.submitted[1]["visible"]
    assert not queue.interrupted_text
    assert bot_api._tab_dispatch_queues == {}


@pytest.mark.parametrize("running", [False, True], ids=["coalescing", "active"])
async def test_legacy_dm_binding_keeps_two_root_group_events_separate(dispatch, running):
    legacy = replace(dispatch.binding, chat_type="p2p", chat_id="dm", chat_name="Private DM")
    if running:
        dispatch.blocked.add("g1")
    first = dispatch.reserve(
        "g1", binding=legacy, event_chat_id="group-a", event_chat_type="group", thread=None
    )
    queue = bot_api._tab_dispatch_queues["tab"]
    task = None
    if running:
        task = asyncio.create_task(dispatch.run(first, queue))
        await dispatch.wait_started("g1")
    other = dispatch.reserve(
        "g2", binding=legacy, event_chat_id="group-b", event_chat_type="group", thread=None
    )
    assert first.channel_key != other.channel_key
    assert not bot_api._message_dispatcher._batch_matches(first, other)
    dispatch.stop.assert_not_called()
    assert first.stop_task is None
    if task is not None:
        dispatch.completions[turn_id_for("bot", "g1")].set_result("answer-g1")
        assert await task == "completed"
    else:
        assert await dispatch.run(first, queue) == "completed"
    assert await dispatch.run(other, queue) == "completed"
    assert [call["metadata"]["feishu_message_ids"] for call in dispatch.submitted] == [
        ["g1"],
        ["g2"],
    ]
    for call, chat_id in zip(dispatch.submitted, ("group-a", "group-b")):
        context = json.loads(call["text"].splitlines()[0].split(": ", 1)[1])
        assert context["chat_type"] == "group"
        assert context["chat_id"] == chat_id
        assert context["thread_id"] is None
        assert "chat_name" not in context
        assert "Private DM" not in call["text"]
        assert provider_text_for_stored_turn(call["visible"], call["metadata"]) == call["text"]


async def test_v3_dispatch_metadata_reconstructs_exact_provider_text(dispatch):
    item = dispatch.reserve("hello", sender="bob")
    queue = bot_api._tab_dispatch_queues["tab"]
    assert await dispatch.run(item, queue) == "completed"
    call = dispatch.submitted[0]
    assert call["metadata"]["provider_text_format"] == FEISHU_PROVIDER_TEXT_FORMAT_V3
    assert provider_text_for_stored_turn(call["visible"], call["metadata"]) == call["text"]
    assert json.loads(call["text"].splitlines()[0].split(": ", 1)[1]) == {
        "app_id": "app",
        "chat_id": "room-a",
        "chat_name": "研发群",
        "chat_type": "group",
        "message_id": "hello",
        "sender_open_id": "bob",
        "thread_id": "topic",
    }
    assert "pairing-owner" not in call["text"]


_SOURCE = dict(
    app_id="cli-bot", chat_id="oc-chat", message_id="om-message", sender_open_id="ou-person"
)
_V2_TEXT = (
    'Claude Hub verified message source (feishu-v2): {"app_id":"cli-bot","chat_id":"oc-chat",'
    '"message_id":"om-message","sender_open_id":"ou-person"}\n'
    "Your final response will be delivered to Feishu. Write it as lightweight Markdown that "
    "can be converted reliably: use headings, lists, links, blockquotes, inline code, and "
    "fenced code blocks as needed. Avoid raw HTML, Markdown tables, or interactive cards. "
    "Do not write mentions or raw Feishu tags such as <at user_id=...>; those require a "
    "separate controlled capability.\n\n你好\nworld"
)


def test_feishu_v2_output_is_byte_identical_and_still_accepted():
    assert format_feishu_provider_text_v2("你好\nworld", **_SOURCE).encode() == _V2_TEXT.encode()
    metadata = {"origin": "feishu", "provider_text_format": "feishu-v2", "feishu": dict(_SOURCE)}
    assert provider_text_for_stored_turn("你好\nworld", metadata) == _V2_TEXT
    assert is_feishu_provider_text(_V2_TEXT)
    v1 = format_feishu_provider_text_v1("你好\nworld", **_SOURCE)
    assert is_feishu_provider_text(v1)
    assert (
        provider_text_for_stored_turn(
            "你好\nworld", {**metadata, "provider_text_format": "feishu-v1"}
        )
        == v1
    )


@pytest.mark.parametrize(
    "chat_type,chat_name,thread_id",
    [("p2p", None, None), ("group", '研发群\n"room"', "topic")],
    ids=["private", "named-topic"],
)
def test_feishu_v3_contains_room_person_and_escaped_context(chat_type, chat_name, thread_id):
    result = format_feishu_provider_text_v3(
        "你好\nworld", **_SOURCE, chat_type=chat_type, chat_name=chat_name, thread_id=thread_id
    )
    assert result.startswith("Claude Hub verified message source (feishu-v3): ")
    context = json.loads(result.splitlines()[0].split(": ", 1)[1])
    expected = {**_SOURCE, "chat_type": chat_type, "thread_id": thread_id}
    if chat_name is not None:
        expected["chat_name"] = chat_name
    assert context == expected
    assert is_feishu_provider_text(result)
    assert result.split("\n", 1)[1] == _V2_TEXT.split("\n", 1)[1]
    metadata = {"origin": "feishu", "provider_text_format": "feishu-v3", "feishu": context}
    assert provider_text_for_stored_turn("你好\nworld", metadata) == result
    assert provider_text_for_stored_turn("edited", metadata).endswith("\n\nedited")


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"chat_type": "group"},
        {"chat_type": "unknown", "thread_id": None},
        {"chat_type": "group", "thread_id": []},
        {"chat_type": "p2p", "thread_id": None, "chat_name": 3},
    ],
    ids=["missing-room", "missing-topic", "invalid-type", "invalid-topic", "invalid-name"],
)
def test_feishu_v3_rejects_incomplete_stored_context(invalid):
    with pytest.raises(ValueError, match="channel metadata is incomplete"):
        provider_text_for_stored_turn(
            "hello",
            {
                "origin": "feishu",
                "provider_text_format": "feishu-v3",
                "feishu": {**_SOURCE, **invalid},
            },
        )
