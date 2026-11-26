"""P2P quiet-window, active-conversation and Source routing regressions."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claude_hub.services import feishu_ai_poller as poller_module
from claude_hub.services import feishu_realtime_delivery as delivery
from claude_hub.services import feishu_realtime_events as events
from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.feishu_ai_command_models import ensure_ai_footer
from claude_hub.services.feishu_realtime_matching import FeishuRealtimeEventConfig
from claude_hub.services.feishu_realtime_settings import write_realtime_settings
from claude_hub.services.feishu_user_client import FeishuReplyReceipt
from claude_hub.services.feishu_web_session.rich import CHAT_GROUP, CHAT_P2P, T, WebChatter
from tests.feishu_realtime_support import (
    COLLEAGUE_WEB_UID,
    ORDINARY_CHAT_WEB_ID,
    OWNER_WEB_UID,
    P2P_CHAT_WEB_ID,
    _build_pushed_via_wire,
    _setup_poller,
)

NOW = 1_800_000_000


def pushed(mid, text, *, chat=P2P_CHAT_WEB_ID, owner=False, seconds=NOW, group=False, position=1):
    return _build_pushed_via_wire(
        msg_id=mid,
        from_id=OWNER_WEB_UID if owner else COLLEAGUE_WEB_UID,
        chat_id=chat,
        chat_type=CHAT_GROUP if group else CHAT_P2P,
        at_me=group,
        nodes=[T(text)],
        create_time_s=seconds,
        position=position,
    )


@pytest.fixture
def clock(monkeypatch):
    current = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        poller_module, "time", SimpleNamespace(monotonic=lambda: current.now, time=time.time)
    )
    return current


async def wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 5)


async def drain(poller):
    assert poller._realtime_queue_worker is not None
    await asyncio.wait_for(poller._realtime_queue_worker, 2)


async def close(poller, client):
    await poller.quiesce()
    await client._client.aclose()


@pytest.mark.asyncio
async def test_debounce_coalesces_in_order_and_preserves_group_latency(tmp_path, clock):
    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    poller._active_web_transport = client._get_web_transport()
    first = pushed("7400000000000010003", "Please check", position=1)
    second = pushed("7400000000000010011", "this timeout", seconds=NOW + 1, position=2)
    try:
        await poller._handle_frontier_pushed_web_message(first)
        await asyncio.sleep(0.01)
        clock.now = 50
        await poller._handle_frontier_pushed_web_message(second)
        await poller._handle_frontier_pushed_web_message(
            first
        )  # retransmission doesn't reset quiet time
        mention = pushed("7400000000000010004", "Help?", chat=ORDINARY_CHAT_WEB_ID, group=True)
        await poller._handle_frontier_pushed_web_message(mention)
        await wait_until(lambda: sink.await_count == 1 and len(poller._web_journal.pending()) == 2)
        assert [call.args[0].dedupe_key for call in sink.await_args_list] == ["7400000000000010004"]
        clock.now = 124
        poller._realtime_queue_changed.set()
        await asyncio.sleep(0.01)
        assert sink.await_count == 1
        clock.now = 125
        poller._realtime_queue_changed.set()
        await drain(poller)
        combined = sink.call_args.args[0]
        assert combined.payload["text"] == "Please check\nthis timeout"
        assert combined.payload["coalesced_message_ids"] == [
            "7400000000000010003",
            "7400000000000010011",
        ]
        assert not poller._web_journal.pending()
        await poller.handle_pushed_web_message(first)
        await poller.handle_pushed_web_message(second)
        assert sink.await_count == 2
    finally:
        await close(poller, client)


@pytest.mark.asyncio
async def test_two_p2p_chats_have_independent_quiet_deadlines(tmp_path, clock):
    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    poller._active_web_transport = client._get_web_transport()
    try:
        await poller._handle_frontier_pushed_web_message(
            pushed("7400000000000010000", "Question A?")
        )
        clock.now = 30
        await poller._handle_frontier_pushed_web_message(
            pushed("7400000000000010002", "Question B?", chat=ORDINARY_CHAT_WEB_ID)
        )
        clock.now = 75
        poller._realtime_queue_changed.set()
        await wait_until(lambda: sink.await_count == 1)
        assert [call.args[0].dedupe_key for call in sink.await_args_list] == ["7400000000000010000"]
        clock.now = 105
        poller._realtime_queue_changed.set()
        await drain(poller)
        assert [call.args[0].dedupe_key for call in sink.await_args_list] == [
            "7400000000000010000",
            "7400000000000010002",
        ]
    finally:
        await close(poller, client)


@pytest.mark.asyncio
async def test_owner_reply_cancels_pending_batch_without_waiting(tmp_path, clock):
    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    poller._active_web_transport = client._get_web_transport()
    try:
        await poller._handle_frontier_pushed_web_message(pushed("7400000000000010007", "Question?"))
        await poller._handle_frontier_pushed_web_message(
            pushed("7400000000000010012", "More details", seconds=NOW + 1, position=2)
        )
        await poller._handle_frontier_pushed_web_message(
            pushed(
                "7400000000000010009",
                "Already handling it",
                owner=True,
                seconds=NOW + 2,
                position=3,
            )
        )
        await drain(poller)
        sink.assert_not_awaited()
        assert not poller._web_journal.pending()
    finally:
        await close(poller, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("age,suppressed", [(899, True), (900, False), (901, False)])
async def test_owner_active_window_boundary_and_group_exemption(
    tmp_path, monkeypatch, age, suppressed
):
    monkeypatch.setattr(events, "time", SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic))
    sink = AsyncMock(return_value=True)
    cfg = FeishuRealtimeEventConfig(p2p_debounce_seconds=0)
    poller, _, client = _setup_poller(tmp_path, realtime_config=cfg, event_sink=sink)
    try:
        await poller.handle_pushed_web_message(
            pushed("7400000000000010009", "My answer", owner=True, seconds=NOW - age)
        )
        result = await poller.handle_pushed_web_message(
            pushed("7400000000000010010", "Another question?")
        )
        assert result is not suppressed
        assert await poller.handle_pushed_web_message(
            pushed("7400000000000010004", "Help?", chat=ORDINARY_CHAT_WEB_ID, group=True)
        )
    finally:
        await close(poller, client)


@pytest.mark.asyncio
async def test_ai_does_not_start_active_silence_and_owed_followup_bypasses_window(
    tmp_path, monkeypatch, clock
):
    monkeypatch.setattr(events, "time", SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic))
    sink = AsyncMock(return_value=True)
    prompts = []
    poller, _, client = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(),
        event_sink=sink,
        dispatched_prompts=prompts,
    )
    poller._active_web_transport = client._get_web_transport()
    monkeypatch.setattr(client, "reply_as_user", AsyncMock())
    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    try:
        await poller._handle_frontier_pushed_web_message(
            pushed("7400000000000010001", "/ai investigate", owner=True)
        )
        assert not poller.realtime_router.owner_active_in_p2p(P2P_CHAT_WEB_ID, 900)
        await poller.wait_for_inflight()
        assert len(prompts) == 1 and "Command instruction: investigate" in prompts[0]
        await poller._handle_frontier_pushed_web_message(
            pushed("7400000000000010009", "I'm here", owner=True, seconds=NOW - 1)
        )
        delivery._read_delivery(dot.store, "7400000000000010008")
        delivery._save_delivery(
            dot.store,
            "7400000000000010008",
            delivery.RealtimeDelivery(
                decision=delivery.RealtimeDecision(decision="investigate", summary="Pending"),
                notified=True,
                followup_due=True,
                initial_summary="Pending",
                message_id="om_card",
            ),
        )
        await poller._handle_frontier_pushed_web_message(
            pushed("7400000000000010008", "Old question?", seconds=NOW - 2)
        )
        await drain(poller)
        assert sink.await_count == 1 and sink.call_args.args[0].dedupe_key == "7400000000000010008"
    finally:
        await close(poller, client)


@pytest.mark.asyncio
async def test_new_message_during_active_turn_does_not_cancel_it(tmp_path, clock):
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def sink(event):
        seen.append(event.dedupe_key)
        if event.dedupe_key == "7400000000000010003":
            entered.set()
            await release.wait()
        return True

    poller, _, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    poller._active_web_transport = client._get_web_transport()
    try:
        await poller._handle_frontier_pushed_web_message(pushed("7400000000000010003", "First?"))
        clock.now = 75
        poller._realtime_queue_changed.set()
        await asyncio.wait_for(entered.wait(), 2)
        await poller._handle_frontier_pushed_web_message(
            pushed("7400000000000010005", "Second?", seconds=NOW + 1)
        )
        assert seen == ["7400000000000010003"]
        clock.now = 150
        release.set()
        await drain(poller)
        assert seen == ["7400000000000010003", "7400000000000010005"]
    finally:
        release.set()
        await close(poller, client)


@pytest.mark.asyncio
async def test_display_name_lookup_is_locked_and_preserves_case(tmp_path):
    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(tmp_path, event_sink=sink)
    wt = client._get_web_transport()
    wt._remember_chatter(WebChatter(id=COLLEAGUE_WEB_UID, name="Peer Name"))
    router = poller.realtime_router

    class LockedNames(dict):
        def get(self, key, default=None):
            assert router._lock._is_owned()
            return super().get(key, default)

        def __getitem__(self, key):
            assert router._lock._is_owned()
            return super().__getitem__(key)

    router._known_sender_names = LockedNames()
    try:
        assert await poller.handle_pushed_web_message(
            pushed("7400000000000010006", "Can you help?")
        )
        assert sink.call_args.args[0].payload["sender_name"] == "Peer Name"
    finally:
        await close(poller, client)


def test_numeric_runtime_settings_defaults_overrides_and_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    assert FeishuRealtimeEventConfig.from_runtime().p2p_debounce_seconds == 75
    assert FeishuRealtimeEventConfig.from_runtime().p2p_active_seconds == 900
    write_realtime_settings({"p2p_debounce_seconds": 25, "p2p_active_seconds": 120})
    assert FeishuRealtimeEventConfig.from_runtime().p2p_debounce_seconds == 25
    assert FeishuRealtimeEventConfig.from_runtime().p2p_active_seconds == 120
    monkeypatch.setenv("CLAUDE_HUB_FEISHU_REALTIME_P2P_DEBOUNCE_SECONDS", "1")
    assert FeishuRealtimeEventConfig.from_runtime().p2p_debounce_seconds == 1
    for invalid in ("-1", "nan", "inf"):
        monkeypatch.setenv("CLAUDE_HUB_FEISHU_REALTIME_P2P_DEBOUNCE_SECONDS", invalid)
        assert FeishuRealtimeEventConfig.from_runtime().p2p_debounce_seconds == 25


@pytest.mark.asyncio
async def test_replayed_journal_coalesces_and_cancelled_window_stays_durable(tmp_path, clock):
    from claude_hub.services.feishu_web_session.rich import FeedPage

    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(
        tmp_path, realtime_config=FeishuRealtimeEventConfig(), event_sink=sink
    )
    wt = client._get_web_transport()
    poller._active_web_transport = wt
    wt._web.feed_cards = AsyncMock(return_value=FeedPage())
    wt._web.topics = AsyncMock(return_value={})
    items = [
        pushed("7400000000000010200", "Question?", position=1),
        pushed("7400000000000010201", "Extra context", seconds=NOW + 1, position=2),
    ]
    try:
        for item in items:
            await poller._handle_frontier_pushed_web_message(item)
        await asyncio.sleep(0.01)
        await poller.quiesce()
        assert len(poller._web_journal.pending()) == 2
        poller._stopping = False
        replay = asyncio.create_task(poller._run_web_replay())

        async def wait_queued():
            while len(poller._realtime_event_queue) < 2:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_queued(), 2)
        assert sink.await_count == 0 and len(poller._web_journal.pending()) == 2
        clock.now = 75
        poller._realtime_queue_changed.set()
        await asyncio.wait_for(replay, 2)
        assert sink.await_count == 1
        assert sink.call_args.args[0].payload["text"] == "Question?\nExtra context"
        assert not poller._web_journal.pending()
    finally:
        await close(poller, client)


@pytest.mark.asyncio
async def test_owner_cache_alias_and_watch_url_exemption(tmp_path, monkeypatch):
    from claude_hub.services.feishu_web_session.rich import WebChat

    monkeypatch.setattr(events, "time", SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic))
    sink = AsyncMock(return_value=True)
    cfg = FeishuRealtimeEventConfig(
        watch_chat_ids=frozenset({ORDINARY_CHAT_WEB_ID}),
        actionable_hosts=frozenset({"example.com"}),
        p2p_debounce_seconds=0,
    )
    poller, _, client = _setup_poller(tmp_path, realtime_config=cfg, event_sink=sink)
    wt = client._get_web_transport()
    owner = pushed("7400000000000010300", "My answer", owner=True, seconds=NOW - 2)
    wt._msg_cache[owner.message.id] = owner.message
    wt._remember_chat(WebChat(id=P2P_CHAT_WEB_ID, open_chat_id="oc_peer_alias", type=CHAT_P2P))
    try:
        assert poller.realtime_router.owner_active_in_p2p(
            "oc_peer_alias", 900, web_transport=wt, owner_ids={OWNER_WEB_UID}
        )
        assert not await poller.handle_pushed_web_message(
            pushed("7400000000000010301", "New question?")
        )
        # The same owner's recent group participation doesn't invoke p2p silence.
        await poller.handle_pushed_web_message(
            pushed(
                "7400000000000010302",
                "Discussing",
                chat=ORDINARY_CHAT_WEB_ID,
                owner=True,
                group=True,
                seconds=NOW - 2,
            )
        )
        watch = replace(
            pushed(
                "7400000000000010303",
                "Check https://example.com/job/1",
                chat=ORDINARY_CHAT_WEB_ID,
                group=True,
            ),
            at_me=False,
        )
        assert await poller.handle_pushed_web_message(watch)
        assert sink.call_args.args[0].payload["trigger_reason"] == "watch_chat_url"
    finally:
        await close(poller, client)


@pytest.mark.asyncio
async def test_cancelled_announced_batch_resumes_original_evidence_once(
    tmp_path, monkeypatch, clock
):
    from claude_hub.api import feishu_bot as bot_api
    from claude_hub.services.feishu_web_session.rich import FeedPage

    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    send = AsyncMock(return_value=(True, "om_shared", "synthetic-app"))
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", send)
    monkeypatch.setattr(delivery, "archive_realtime_event_sessions", AsyncMock())
    announced = asyncio.Event()
    calls = []

    async def diagnose(event, config, *, on_initial, resume_investigation, attachments):
        calls.append((event.dedupe_key, event.payload["text"], resume_investigation))
        if len(calls) == 1:
            assert await on_initial(
                delivery.RealtimeDecision(decision="investigate", summary="Checking")
            )
            announced.set()
            await asyncio.Event().wait()
        return delivery.RealtimeDecision(decision="notify", summary="Verified")

    monkeypatch.setattr(delivery, "diagnose_realtime_event", diagnose)
    poller, _, client = _setup_poller(tmp_path, realtime_config=FeishuRealtimeEventConfig())
    wt = client._get_web_transport()
    poller._active_web_transport = wt
    wt._web.feed_cards = AsyncMock(return_value=FeedPage())
    wt._web.topics = AsyncMock(return_value={})
    a = pushed("7400000000000010400", "Question A?", position=1)
    b = pushed("7400000000000010401", "Critical context B", seconds=NOW + 1, position=2)
    try:
        await poller._handle_frontier_pushed_web_message(a)
        await poller._handle_frontier_pushed_web_message(b)
        clock.now = 75
        poller._realtime_queue_changed.set()
        await asyncio.wait_for(announced.wait(), 2)
        await poller.quiesce()
        assert len(poller._web_journal.pending()) == 2
        poller._stopping = False
        replay = asyncio.create_task(poller._run_web_replay())
        # Owned batches bypass the quiet window; allow any incorrectly split member
        # to reach the diagnosis too, exposing duplicate cards in the old code.
        await asyncio.sleep(0.05)
        clock.now = 150
        poller._realtime_queue_changed.set()
        await asyncio.wait_for(replay, 2)
        assert calls == [
            (b.message.id, "Question A?\nCritical context B", False),
            (b.message.id, "Question A?\nCritical context B", True),
        ]
        assert send.await_count == 2
        assert not poller._web_journal.pending()
    finally:
        await close(poller, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_terminal_latest_retransmission_cannot_discard_unhandled_older_member(
    tmp_path, monkeypatch, clock, restart
):
    from claude_hub.api import feishu_bot as bot_api

    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", AsyncMock(return_value=(True, "", "")))
    monkeypatch.setattr(delivery, "archive_realtime_event_sessions", AsyncMock())
    diagnose = AsyncMock(
        return_value=delivery.RealtimeDecision(decision="notify", summary="Answer")
    )
    monkeypatch.setattr(delivery, "diagnose_realtime_event", diagnose)
    cfg = FeishuRealtimeEventConfig()
    poller, _, client = _setup_poller(tmp_path, realtime_config=cfg)
    poller._active_web_transport = client._get_web_transport()
    a = pushed("7400000000000010500", "Older unseen question?", position=1)
    b = pushed("7400000000000010501", "Already answered question?", seconds=NOW + 1, position=2)
    try:
        await poller._handle_frontier_pushed_web_message(b)
        clock.now = 75
        poller._realtime_queue_changed.set()
        await drain(poller)
        if restart:
            poller.realtime_router.reset_account_state()
        await poller._handle_frontier_pushed_web_message(a)
        await poller._handle_frontier_pushed_web_message(b)
        clock.now = 150
        poller._realtime_queue_changed.set()
        await drain(poller)
        assert [call.args[0].dedupe_key for call in diagnose.await_args_list] == [
            b.message.id,
            a.message.id,
        ]
        assert not poller._web_journal.pending()
    finally:
        await close(poller, client)


def pause_realtime_worker(poller, monkeypatch):
    release = asyncio.Event()
    original = poller._drain_realtime_event_queue

    async def paused():
        await release.wait()
        await original()

    monkeypatch.setattr(poller, "_drain_realtime_event_queue", paused)
    return release


@pytest.mark.asyncio
@pytest.mark.parametrize("ordering", ["timestamps", "positions", "topic-positions"])
async def test_answered_row_does_not_shorten_or_contaminate_unanswered_window(
    tmp_path, monkeypatch, clock, ordering
):
    sink = AsyncMock(return_value=True)
    poller, _, client = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(p2p_active_seconds=0),
        event_sink=sink,
    )
    poller._active_web_transport = client._get_web_transport()
    release = pause_realtime_worker(poller, monkeypatch)
    m1 = pushed("7400000000000010600", "Already answered question?", position=1)
    owner = pushed(
        "7400000000000010601",
        "My answer",
        owner=True,
        position=2,
        seconds=NOW + 1 if ordering == "timestamps" else NOW,
    )
    m2 = pushed(
        "7400000000000010602",
        "New unanswered question?",
        position=3,
        seconds=NOW + 2 if ordering == "timestamps" else NOW,
    )
    if ordering == "topic-positions":
        topic = "7400000000000010699"
        m1, owner, m2 = [
            replace(
                item,
                message=replace(
                    item.message, position=0, thread_id=topic, root_id=topic, topic_position=index
                ),
            )
            for index, item in enumerate((m1, owner, m2), 1)
        ]
    try:
        await poller._handle_frontier_pushed_web_message(m1)
        clock.now = 10
        await poller._handle_frontier_pushed_web_message(owner)
        clock.now = 20
        await poller._handle_frontier_pushed_web_message(m2)
        release.set()
        await wait_until(
            lambda: m1.message.id not in {p.message.id for p in poller._web_journal.pending()}
        )
        assert sink.await_count == 0
        assert [p.message.id for p in poller._web_journal.pending()] == [m2.message.id]
        clock.now = 94
        poller._realtime_queue_changed.set()
        await asyncio.sleep(0.01)
        assert sink.await_count == 0
        clock.now = 95
        poller._realtime_queue_changed.set()
        await drain(poller)
        assert sink.await_count == 1
        assert sink.call_args.args[0].payload["text"] == "New unanswered question?"
        assert sink.call_args.args[0].payload["coalesced_message_ids"] == [m2.message.id]
    finally:
        release.set()
        await close(poller, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("echo_route", ["none", "push-thread", "push-main", "cache", "history"])
async def test_owner_ai_reply_suppresses_only_target_not_other_pending_peer(
    tmp_path, monkeypatch, clock, echo_route
):
    monkeypatch.setattr(
        events, "time", SimpleNamespace(time=lambda: NOW + 2, monotonic=time.monotonic)
    )
    sink = AsyncMock(return_value=True)
    prompts = []
    poller, _, client = _setup_poller(
        tmp_path,
        realtime_config=FeishuRealtimeEventConfig(),
        event_sink=sink,
        dispatched_prompts=prompts,
    )
    wt = client._get_web_transport()
    poller._active_web_transport = wt
    monkeypatch.setattr(
        client,
        "reply_as_user",
        AsyncMock(return_value=FeishuReplyReceipt(message_id="om_ai_answer")),
    )
    release = pause_realtime_worker(poller, monkeypatch)
    m1 = pushed("7400000000000010700", "First unanswered question?", position=1)
    m2 = pushed("7400000000000010701", "Second question?", position=2)
    command = pushed("7400000000000010702", "/ai answer this", owner=True, position=3)
    command = replace(command, message=replace(command.message, parent_id=m2.message.id))
    try:
        await poller._handle_frontier_pushed_web_message(m1)
        clock.now = 20
        await poller._handle_frontier_pushed_web_message(m2)
        clock.now = 30
        await poller._handle_frontier_pushed_web_message(command)
        # Suppression must survive cache eviction and be recorded before command
        # dispatch returns; a targeted /ai command must not start chat-wide silence.
        wt._msg_cache.pop(command.message.id, None)
        assert m2.message.id in poller.realtime_router._owner_ai_parent_replies
        if echo_route != "none":
            echo = pushed(
                "7400000000000010703",
                ensure_ai_footer("Assistant answer"),
                owner=True,
                seconds=NOW + 1,
                position=4,
            )
            if echo_route == "push-thread":
                echo = replace(
                    echo,
                    message=replace(
                        echo.message,
                        parent_id=command.message.id,
                        root_id=command.message.id,
                        thread_id=command.message.id,
                        topic_position=1,
                    ),
                )
            if echo_route.startswith("push"):
                await poller._handle_frontier_pushed_web_message(echo)
            else:
                echo_user = wt.ingest_pushed_message(echo)
                if echo_route == "history":
                    wt._msg_cache.pop(echo.message.id, None)
                    monkeypatch.setattr(
                        client,
                        "list_messages_page",
                        AsyncMock(return_value=SimpleNamespace(items=[echo_user])),
                    )
        assert not poller.realtime_router.owner_active_in_p2p(
            P2P_CHAT_WEB_ID, 900, web_transport=wt, owner_ids={OWNER_WEB_UID}
        )
        release.set()
        await wait_until(
            lambda: m2.message.id not in {p.message.id for p in poller._web_journal.pending()}
        )
        assert sink.await_count == 0
        clock.now = 74
        poller._realtime_queue_changed.set()
        await asyncio.sleep(0.01)
        assert sink.await_count == 0
        clock.now = 75
        poller._realtime_queue_changed.set()
        await drain(poller)
        await poller.wait_for_inflight()
        assert len(prompts) == 1
        assert sink.await_count == 1
        assert sink.call_args.args[0].payload["text"] == "First unanswered question?"
        assert sink.call_args.args[0].payload["coalesced_message_ids"] == [m1.message.id]
        assert not poller._web_journal.pending()
    finally:
        release.set()
        await close(poller, client)
