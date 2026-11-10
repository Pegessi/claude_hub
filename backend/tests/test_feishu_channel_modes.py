"""Serving modes (``single`` vs ``per_channel``), multi-binding model, and auto-provisioning."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_hub.api import feishu_bot as bot_api
from claude_hub.main import app
from claude_hub.models import AgentType, SessionKind
from claude_hub.services.bot_pool.common import (
    _STATE_VERSION,
    BOT_MODE_PER_CHANNEL,
    BOT_MODE_SINGLE,
    MAX_CHANNELS_PER_BOT,
    OWNER_KIND_LOCAL,
    OWNER_KIND_OAUTH,
    FeishuBotModeSwitchBlocked,
    FeishuBotOccupied,
    FeishuChatOccupied,
    FeishuPoolFull,
    OwnerIdentity,
)
from claude_hub.services.feishu_auto_chat import (
    FeishuAutoChatCreator,
    format_dm_tab_name,
    format_group_tab_name,
)
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuMessageDedupStore,
)
from claude_hub.services.feishu_bot_pool import FeishuBotPoolStore

OWNER = OwnerIdentity(open_id="ou-owner", email="owner@example.test", kind=OWNER_KIND_OAUTH)
LOCAL = OwnerIdentity(open_id="local", email="local@localhost", kind=OWNER_KIND_LOCAL)


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("a test attempted a real outbound HTTP request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked, raising=True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked, raising=True)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def pool(tmp_path: Path, clock: Clock) -> FeishuBotPoolStore:
    return FeishuBotPoolStore(
        path=tmp_path / "secrets" / "feishu_bot_pool.json",
        legacy_path=tmp_path / "secrets" / "feishu_bot.json",
        now=clock,
    )


def _config(app_id: str = "app-1") -> FeishuBotConfig:
    return FeishuBotConfig(app_id=app_id, app_secret=f"secret-{app_id}")


class FakeTabManager:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.fail_create = False

    async def create_tab(
        self,
        *,
        name: str,
        session_kind: SessionKind = SessionKind.CHAT,
        agent_type: AgentType = AgentType.CLAUDE,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if self.fail_create:
            raise RuntimeError("tab limit reached")
        tab_id = f"auto-tab-{len(self.created) + 1}"
        record = {
            "id": tab_id,
            "name": name,
            "session_kind": session_kind,
            "agent_type": agent_type,
            "cwd": cwd,
            **kwargs,
        }
        self.created.append(record)
        return SimpleNamespace(id=tab_id, name=name)

    async def delete_tab(self, tab_id: str) -> bool:
        self.deleted.append(tab_id)
        return True


class _Wire:
    def __init__(self) -> None:
        self.replies: list[dict[str, Any]] = []
        self.pushes: list[tuple[str, dict[str, Any]]] = []
        self.chats: dict[str, dict[str, Any]] = {}
        self.sent_sequence = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if request.url.path == "/open-apis/bot/v3/info/":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "bot": {
                        "app_name": "Test Bot",
                        "avatar_url": "https://example.test/bot.png",
                        "open_id": "ou-current-bot",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/open-apis/im/v1/messages":
            body = json.loads(request.content)
            self.pushes.append((body["receive_id"], body))
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/reply"):
            self.replies.append(json.loads(request.content))
            self.sent_sequence += 1
            return httpx.Response(
                200, json={"code": 0, "data": {"message_id": f"om-sent-{self.sent_sequence}"}}
            )
        if request.method == "GET" and request.url.path.startswith("/open-apis/im/v1/chats/"):
            chat_id = request.url.path.split("/")[-1]
            info = self.chats.get(chat_id)
            if info is None:
                return httpx.Response(404, json={"code": 230002, "msg": "no chat"})
            return httpx.Response(200, json={"code": 0, "data": info})
        if request.method == "POST" and request.url.path.endswith("/reactions"):
            return httpx.Response(200, json={"code": 0, "data": {"reaction_id": "react-1"}})
        if request.method == "DELETE" and "/reactions/" in request.url.path:
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 1})

    def reply_texts(self) -> list[str]:
        texts: list[str] = []
        for item in self.replies:
            content = json.loads(item["content"])
            if item["msg_type"] == "text":
                texts.append(content["text"])
            else:
                texts.append(content["zh_cn"]["content"][0][0]["text"])
        return texts


@pytest.fixture
def env_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: Clock):
    wire = _Wire()

    def client_factory(config: FeishuBotConfig, client: httpx.AsyncClient | None = None):
        return FeishuBotClient(
            config,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(wire.handler),
                base_url=config.api_base_url,
            ),
        )

    store = FeishuBotPoolStore(
        path=tmp_path / "secrets" / "feishu_bot_pool.json",
        legacy_path=tmp_path / "secrets" / "feishu_bot.json",
        now=clock,
    )
    tabs = FakeTabManager()
    auto_chat = FeishuAutoChatCreator(manager=tabs, now=clock)

    monkeypatch.setattr(bot_api, "FeishuBotClient", client_factory)
    monkeypatch.setattr(bot_api, "_pool", store)
    monkeypatch.setattr(
        bot_api,
        "_dedup",
        FeishuMessageDedupStore(path=tmp_path / "feishu_bot_dedup.json", now=clock),
    )
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    monkeypatch.setattr(bot_api, "_tab_dispatch_queues", {})
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.01)
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.04)
    monkeypatch.setattr(bot_api, "_validate_bind_target", lambda tab_id, workspace_id: None)
    monkeypatch.setattr(bot_api, "_now", clock)
    monkeypatch.setattr(bot_api, "_create_gate", asyncio.Lock())
    monkeypatch.setattr(bot_api, "ttyd_manager", tabs)
    monkeypatch.setattr(bot_api, "_auto_chat", auto_chat)
    client = TestClient(app)
    try:
        yield SimpleNamespace(
            client=client,
            pool=store,
            wire=wire,
            tabs=tabs,
            auto_chat=auto_chat,
        )
    finally:
        client.close()


def _sdk_dm(
    app_id: str,
    text: str,
    now: float,
    *,
    message_id: str,
    chat_id: str,
    sender_open_id: str,
) -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(event_id=f"ev-{message_id}", app_id=app_id),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user", sender_id=SimpleNamespace(open_id=sender_open_id)
            ),
            message=SimpleNamespace(
                chat_type="p2p",
                message_type="text",
                message_id=message_id,
                create_time=str(int(now * 1000)),
                chat_id=chat_id,
                thread_id=None,
                parent_id=None,
                root_id=None,
                mentions=[],
                content=json.dumps({"text": text}),
            ),
        ),
    )


# --------------------------------------------------- 1. model & migration


def test_v3_single_binding_migrates_to_v4_bindings_list_and_tolerates_missing_keys(
    tmp_path: Path, clock: Clock
) -> None:
    assert _STATE_VERSION == 4
    path = tmp_path / "secrets" / "feishu_bot_pool.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "pool_revision": 2,
                "bots": {
                    "bot-v3": {
                        "bot_id": "bot-v3",
                        "name": "Legacy V3",
                        "app_id": "app-v3",
                        "credentials": {"app_secret": "sec-v3"},
                        "enabled": True,
                        "revision": 4,
                        "generation": 1,
                        "created_at": 100.0,
                        "updated_at": 100.0,
                        "pending": {},
                        "claims": {},
                        "binding": {
                            "pairing_id": "pair-v3",
                            "tab_id": "tab-v3",
                            "workspace_id": None,
                            "sender_open_id": "ou-v3",
                            "chat_id": "oc-v3",
                            "created_at": 100.0,
                            "generation": 1,
                            "owner_kind": "local",
                            "owner_open_id": "local",
                            "owner_email": "local@localhost",
                        },
                    },
                    "bot-bare": {
                        "bot_id": "bot-bare",
                        "name": "Bare",
                        "app_id": "app-bare",
                        "credentials": {"app_secret": "sec-bare"},
                        "enabled": True,
                        "revision": 1,
                        "generation": 0,
                        "created_at": 100.0,
                        "updated_at": 100.0,
                        "pending": {},
                        "claims": {},
                    },
                },
                "rate_limits": {},
            }
        ),
        encoding="utf-8",
    )
    store = FeishuBotPoolStore(
        path=path, legacy_path=tmp_path / "secrets" / "feishu_bot.json", now=clock
    )
    snap = store.snapshot({})
    v3_entry = snap.get("bot-v3")
    assert v3_entry is not None
    assert v3_entry.mode == BOT_MODE_SINGLE
    assert len(v3_entry.bindings) == 1
    assert v3_entry.binding == v3_entry.bindings[0]
    assert v3_entry.binding is not None
    assert v3_entry.binding.pairing_id == "pair-v3"

    bare_entry = snap.get("bot-bare")
    assert bare_entry is not None
    assert bare_entry.mode == BOT_MODE_SINGLE
    assert bare_entry.bindings == ()
    assert bare_entry.binding is None

    # Writing back commits v4 format with `bindings` and no `binding` key.
    store.update_bot("bot-bare", expected_revision=1, name="Bare Renamed", environ={})
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 4
    assert "binding" not in persisted["bots"]["bot-v3"]
    assert len(persisted["bots"]["bot-v3"]["bindings"]) == 1
    assert persisted["bots"]["bot-v3"]["mode"] == "single"
    assert persisted["bots"]["bot-bare"]["bindings"] == []
    assert persisted["bots"]["bot-bare"]["mode"] == "single"


def test_bindings_order_is_deterministic_oldest_first_and_binding_is_first_projection(
    pool: FeishuBotPoolStore, clock: Clock
) -> None:
    bot_id = pool.create_bot(name="Multi", config=_config("app-1"), environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.update_bot(bot_id, expected_revision=entry.revision, mode=BOT_MODE_PER_CHANNEL, environ={})

    first = pool.bind_p2p(
        bot_id,
        owner=LOCAL,
        tab_id="tab-1",
        workspace_id=None,
        sender_open_id="ou-1",
        chat_id="oc-1",
        environ={},
    )
    clock.advance(10)
    mid_entry = pool.snapshot({}).get(bot_id)
    assert mid_entry is not None
    second = pool.bind_group(
        bot_id,
        owner=LOCAL,
        tab_id="tab-2",
        workspace_id=None,
        chat_id="oc-group-2",
        chat_name="Group Two",
        expected_revision=mid_entry.revision,
        environ={},
    )
    clock.advance(10)
    third = pool.bind_p2p(
        bot_id,
        owner=LOCAL,
        tab_id="tab-3",
        workspace_id=None,
        sender_open_id="ou-3",
        chat_id="oc-3",
        environ={},
    )

    current = pool.snapshot({}).get(bot_id)
    assert current is not None
    assert [b.pairing_id for b in current.bindings] == [
        first.pairing_id,
        second.pairing_id,
        third.pairing_id,
    ]
    assert current.binding == first


# ------------------------------------------------ 2. caps, uniqueness, routing


def test_per_channel_caps_uniqueness_and_routing_over_list(
    pool: FeishuBotPoolStore, clock: Clock
) -> None:
    bot_a = pool.create_bot(name="Bot A", config=_config("app-a"), environ={})
    bot_b = pool.create_bot(name="Bot B", config=_config("app-b"), environ={})
    pool.update_bot(bot_a, expected_revision=0, mode=BOT_MODE_PER_CHANNEL, environ={})
    pool.update_bot(bot_b, expected_revision=0, mode=BOT_MODE_PER_CHANNEL, environ={})

    for idx in range(MAX_CHANNELS_PER_BOT - 1):
        pool.bind_p2p(
            bot_a,
            owner=LOCAL,
            tab_id=f"tab-p2p-{idx}",
            workspace_id=None,
            sender_open_id=f"ou-user-{idx}",
            chat_id=f"oc-p2p-{idx}",
            environ={},
        )
    bot_a_entry = pool.snapshot({}).get(bot_a)
    assert bot_a_entry is not None
    pool.bind_group(
        bot_a,
        owner=LOCAL,
        tab_id="tab-group-7",
        workspace_id=None,
        chat_id="oc-group-7",
        chat_name="Seven",
        expected_revision=bot_a_entry.revision,
        environ={},
    )
    bot_a_full = pool.snapshot({}).get(bot_a)
    assert bot_a_full is not None
    assert len(bot_a_full.bindings) == MAX_CHANNELS_PER_BOT

    # 9th channel on the same Bot is rejected with FeishuPoolFull.
    with pytest.raises(FeishuPoolFull):
        pool.bind_p2p(
            bot_a,
            owner=LOCAL,
            tab_id="tab-overflow",
            workspace_id=None,
            sender_open_id="ou-user-overflow",
            chat_id="oc-overflow",
            environ={},
        )

    # Reusing a tab_id or chat_id on another Bot is rejected with FeishuChatOccupied.
    with pytest.raises(FeishuChatOccupied):
        pool.bind_p2p(
            bot_b,
            owner=LOCAL,
            tab_id="tab-p2p-3",
            workspace_id=None,
            sender_open_id="ou-other",
            chat_id="oc-fresh",
            environ={},
        )
    with pytest.raises(FeishuChatOccupied):
        pool.bind_p2p(
            bot_b,
            owner=LOCAL,
            tab_id="tab-fresh",
            workspace_id=None,
            sender_open_id="ou-other",
            chat_id="oc-p2p-3",
            environ={},
        )

    # Routing resolves each p2p and group channel across the list.
    for idx in range(MAX_CHANNELS_PER_BOT - 1):
        matched_p2p = pool.routed_binding(
            bot_a, sender_open_id=f"ou-user-{idx}", chat_id=f"oc-p2p-{idx}", environ={}
        )
        assert matched_p2p is not None and matched_p2p.tab_id == f"tab-p2p-{idx}"
        by_sender = pool.routed_binding_for_sender(
            bot_a, sender_open_id=f"ou-user-{idx}", environ={}
        )
        assert by_sender is not None and by_sender.tab_id == f"tab-p2p-{idx}"
        assert pool.routed_binding_for_chat(bot_a, chat_id=f"oc-p2p-{idx}", environ={}) is None

    matched_group = pool.routed_binding_for_chat(bot_a, chat_id="oc-group-7", environ={})
    assert matched_group is not None and matched_group.tab_id == "tab-group-7"
    assert (
        pool.routed_binding(bot_a, sender_open_id="local", chat_id="oc-group-7", environ={}) is None
    )


def test_drop_and_release_binding_by_pairing_id_keep_sibling_channels(
    pool: FeishuBotPoolStore,
) -> None:
    bot_id = pool.create_bot(name="Multi", config=_config("app-1"), environ={})
    pool.update_bot(bot_id, expected_revision=0, mode=BOT_MODE_PER_CHANNEL, environ={})
    b1 = pool.bind_p2p(
        bot_id,
        owner=LOCAL,
        tab_id="tab-1",
        workspace_id=None,
        sender_open_id="ou-1",
        chat_id="oc-1",
        environ={},
    )
    b2 = pool.bind_p2p(
        bot_id,
        owner=LOCAL,
        tab_id="tab-2",
        workspace_id=None,
        sender_open_id="ou-2",
        chat_id="oc-2",
        environ={},
    )
    b3 = pool.bind_p2p(
        bot_id,
        owner=LOCAL,
        tab_id="tab-3",
        workspace_id=None,
        sender_open_id="ou-3",
        chat_id="oc-3",
        environ={},
    )

    assert pool.drop_binding(bot_id, b2.pairing_id) is True
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    assert [b.pairing_id for b in entry.bindings] == [b1.pairing_id, b3.pairing_id]

    released = pool.release_binding(
        bot_id,
        expected_revision=entry.revision,
        pairing_id=b1.pairing_id,
        environ={},
    )
    assert released is not None and released.pairing_id == b1.pairing_id
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    assert [b.pairing_id for b in entry.bindings] == [b3.pairing_id]
    assert pool.routed_binding(bot_id, sender_open_id="ou-3", chat_id="oc-3", environ={}) == b3


# ----------------------------------------- 3. admin API mode switch & group bind


def test_patch_bot_mode_switch_requires_no_active_bindings(env_api) -> None:
    bot_id = env_api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    entry = env_api.pool.snapshot({}).get(bot_id)
    assert entry is not None and entry.mode == BOT_MODE_SINGLE

    # Switch single -> per_channel when unbound succeeds.
    res = env_api.client.patch(
        f"/api/feishu/bot/bots/{bot_id}",
        json={"mode": "per_channel", "expected_revision": entry.revision},
    )
    assert res.status_code == 200
    bot_summary = res.json()["bots"][0]
    assert bot_summary["mode"] == "per_channel"
    assert bot_summary["bindings"] == []
    assert bot_summary["binding"] is None

    # Bind one channel, then attempt to switch back to single -> 409.
    env_api.pool.bind_p2p(
        bot_id,
        owner=LOCAL,
        tab_id="tab-1",
        workspace_id=None,
        sender_open_id="ou-1",
        chat_id="oc-1",
        environ={},
    )
    bound_entry = env_api.pool.snapshot({}).get(bot_id)
    assert bound_entry is not None
    blocked = env_api.client.patch(
        f"/api/feishu/bot/bots/{bot_id}",
        json={"mode": "single", "expected_revision": bound_entry.revision},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "bot_has_active_bindings"

    # Also blocked at the store layer directly.
    with pytest.raises(FeishuBotModeSwitchBlocked):
        env_api.pool.update_bot(
            bot_id,
            expected_revision=bound_entry.revision,
            mode=BOT_MODE_SINGLE,
            environ={},
        )

    # Invalid mode value -> 400.
    bad_mode = env_api.client.patch(
        f"/api/feishu/bot/bots/{bot_id}",
        json={"mode": "unknown_mode", "expected_revision": bound_entry.revision},
    )
    assert bad_mode.status_code == 400


def test_group_bind_in_per_channel_mode_auto_creates_tab_when_tab_id_omitted(env_api) -> None:
    env_api.wire.chats["oc-group-alpha"] = {
        "chat_id": "oc-group-alpha",
        "name": "Alpha Squad",
        "chat_mode": "group",
        "chat_type": "public",
    }
    bot_id = env_api.pool.create_bot(name="One", config=_config("app-1"), environ={})

    # In single mode, omitting tab_id is rejected with 400.
    entry = env_api.pool.snapshot({}).get(bot_id)
    assert entry is not None
    missing_tab = env_api.client.post(
        f"/api/feishu/bot/bots/{bot_id}/bindings/group",
        json={"chat_id": "oc-group-alpha", "expected_revision": entry.revision},
    )
    assert missing_tab.status_code == 400

    # Switch to per_channel mode and bind group without tab_id.
    env_api.pool.update_bot(
        bot_id, expected_revision=entry.revision, mode=BOT_MODE_PER_CHANNEL, environ={}
    )
    entry = env_api.pool.snapshot({}).get(bot_id)
    assert entry is not None
    res = env_api.client.post(
        f"/api/feishu/bot/bots/{bot_id}/bindings/group",
        json={"chat_id": "oc-group-alpha", "expected_revision": entry.revision},
    )
    assert res.status_code == 200
    assert len(env_api.tabs.created) == 1
    assert env_api.tabs.created[0]["name"] == "Feishu · Alpha Squad"
    assert env_api.tabs.created[0]["session_kind"] == SessionKind.CHAT

    bot_summary = res.json()["bots"][0]
    assert bot_summary["mode"] == "per_channel"
    assert len(bot_summary["bindings"]) == 1
    assert bot_summary["bindings"][0]["tab_id"] == "auto-tab-1"
    assert bot_summary["bindings"][0]["chat_type"] == "group"
    assert bot_summary["bindings"][0]["chat_name"] == "Alpha Squad"
    assert bot_summary["binding"] == bot_summary["bindings"][0]


# ------------------------------------ 4. per_channel DM auto-provisioning


def test_tab_naming_helpers() -> None:
    assert format_dm_tab_name("ou_1234567890abcd") == "Feishu · DM ou…abcd"
    assert format_group_tab_name("Core Platform", "oc_1") == "Feishu · Core Platform"


def test_per_channel_dm_auto_provisions_tab_and_enforces_cooldown_and_caps(
    env_api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        bot_id = env_api.pool.create_bot(
            name="One",
            config=_config("app-1"),
            feishu_name="Test Bot",
            feishu_open_id="ou-current-bot",
            environ={},
        )
        env_api.pool.update_bot(bot_id, expected_revision=0, mode=BOT_MODE_PER_CHANNEL, environ={})

        dispatched: list[tuple[str, str]] = []

        async def fake_dispatch(
            tab_id: str,
            _text: str,
            _turn_id: str,
            *,
            admission_guard: Any,
            visible_text: str,
            **_kwargs: Any,
        ) -> str:
            async with admission_guard():
                dispatched.append((tab_id, visible_text))
            return f"ack:{visible_text}"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", fake_dispatch)

        # 1. First DM from Alice auto-creates a Chat tab and dispatches the message.
        await bot_api._handle_sdk_event(
            bot_id,
            _sdk_dm(
                "app-1",
                "hello from alice",
                clock.value,
                message_id="om-alice-1",
                chat_id="oc-alice",
                sender_open_id="ou_11112222abcd",
            ),
        )
        assert len(env_api.tabs.created) == 1
        assert env_api.tabs.created[0]["name"] == "Feishu · DM ou…abcd"
        assert dispatched == [("auto-tab-1", "hello from alice")]
        assert env_api.wire.reply_texts()[-1] == "ack:hello from alice"

        # 2. Follow-up from Alice in the same DM reuses auto-tab-1 without cooldown rejection.
        clock.advance(1.0)
        await bot_api._handle_sdk_event(
            bot_id,
            _sdk_dm(
                "app-1",
                "second from alice",
                clock.value,
                message_id="om-alice-2",
                chat_id="oc-alice",
                sender_open_id="ou_11112222abcd",
            ),
        )
        assert len(env_api.tabs.created) == 1
        assert dispatched[-1] == ("auto-tab-1", "second from alice")

        # 3. Same sender Alice from a different chat_id is refused (1 channel per sender per Bot).
        clock.advance(10.0)
        await bot_api._handle_sdk_event(
            bot_id,
            _sdk_dm(
                "app-1",
                "alice second chat",
                clock.value,
                message_id="om-alice-alt",
                chat_id="oc-alice-alt",
                sender_open_id="ou_11112222abcd",
            ),
        )
        assert len(env_api.tabs.created) == 1
        assert "could not open a new session" in env_api.wire.reply_texts()[-1].lower()

        # 4. New sender Bob arrives, then Charlie arrives 1s later (< 5s cooldown).
        await bot_api._handle_sdk_event(
            bot_id,
            _sdk_dm(
                "app-1",
                "hello from bob",
                clock.value,
                message_id="om-bob-1",
                chat_id="oc-bob",
                sender_open_id="ou_33334444ef01",
            ),
        )
        assert len(env_api.tabs.created) == 2
        assert env_api.tabs.created[1]["name"] == "Feishu · DM ou…ef01"

        clock.advance(1.0)
        await bot_api._handle_sdk_event(
            bot_id,
            _sdk_dm(
                "app-1",
                "hello from charlie too fast",
                clock.value,
                message_id="om-charlie-fast",
                chat_id="oc-charlie",
                sender_open_id="ou_555566669999",
            ),
        )
        assert len(env_api.tabs.created) == 2
        assert "could not open a new session" in env_api.wire.reply_texts()[-1].lower()

        # 5. After 5s cooldown elapses, Charlie and remaining senders up to MAX_CHANNELS_PER_BOT succeed.
        for idx in range(2, MAX_CHANNELS_PER_BOT):
            clock.advance(6.0)
            await bot_api._handle_sdk_event(
                bot_id,
                _sdk_dm(
                    "app-1",
                    f"hello {idx}",
                    clock.value,
                    message_id=f"om-u-{idx}",
                    chat_id=f"oc-u-{idx}",
                    sender_open_id=f"ou_user_{idx:04d}",
                ),
            )
        assert len(env_api.tabs.created) == MAX_CHANNELS_PER_BOT
        assert len(env_api.pool.snapshot({}).get(bot_id).bindings) == MAX_CHANNELS_PER_BOT

        # 6. 9th sender exceeds MAX_CHANNELS_PER_BOT and gets the English rejection reply.
        clock.advance(6.0)
        await bot_api._handle_sdk_event(
            bot_id,
            _sdk_dm(
                "app-1",
                "hello 9th",
                clock.value,
                message_id="om-u-overflow",
                chat_id="oc-u-overflow",
                sender_open_id="ou_user_9999",
            ),
        )
        assert len(env_api.tabs.created) == MAX_CHANNELS_PER_BOT
        assert "could not open a new session" in env_api.wire.reply_texts()[-1].lower()

        # 7. push_to_tab reaches any bound channel tab in the list, not just the first.
        assert await bot_api.push_to_tab("auto-tab-2", "Notice for Bob")
        assert env_api.wire.pushes[-1][0] == "oc-bob"

    asyncio.run(scenario())
