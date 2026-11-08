"""Group bindings: persistence, routing separation and state validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_hub.services.feishu_bot import FeishuBotConfig, FeishuChatInfo
from claude_hub.services.feishu_bot_pool import (
    OWNER_KIND_LOCAL,
    FeishuBotOccupied,
    FeishuBotPoolStateError,
    FeishuBotPoolStore,
    FeishuBotRevisionConflict,
    FeishuChatOccupied,
    OwnerIdentity,
)

OWNER = OwnerIdentity(open_id="local", email="local@localhost", kind=OWNER_KIND_LOCAL)
OTHER = OwnerIdentity(open_id="ou-other", email="other@example.test", kind=OWNER_KIND_LOCAL)


def _config(app_id: str = "app-1") -> FeishuBotConfig:
    return FeishuBotConfig(app_id=app_id, app_secret=f"secret-{app_id}")


@pytest.fixture
def pool(tmp_path: Path) -> FeishuBotPoolStore:
    return FeishuBotPoolStore(
        path=tmp_path / "secrets" / "feishu_bot_pool.json",
        legacy_path=tmp_path / "secrets" / "feishu_bot.json",
    )


def _bind_group(pool: FeishuBotPoolStore, bot_id: str, chat_id: str = "oc-group"):
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    return pool.bind_group(
        bot_id,
        owner=OWNER,
        tab_id="tab-1",
        workspace_id=None,
        chat_id=chat_id,
        chat_name="Team",
        expected_revision=entry.revision,
        environ={},
    )


def test_group_binding_is_persisted_as_a_group(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    binding = _bind_group(pool, bot_id)
    assert binding.chat_type == "group"
    assert binding.is_group is True
    assert binding.chat_name == "Team"
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    assert entry.binding is not None
    assert entry.binding.pairing_id == binding.pairing_id
    # An in-memory snapshot must survive its own serialization.
    raw = json.loads(pool.path.read_text())
    stored = raw["bots"][bot_id]["binding"]
    assert stored["chat_type"] == "group"
    assert stored["chat_name"] == "Team"


def test_group_binding_routes_only_by_chat_id(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    _bind_group(pool, bot_id, chat_id="oc-group")
    routed = pool.routed_binding_for_chat(bot_id, chat_id="oc-group", environ={})
    assert routed is not None and routed.is_group
    assert pool.routed_binding_for_chat(bot_id, chat_id="oc-other", environ={}) is None
    # A group binding is never a per-sender or per-private-chat route.
    assert pool.routed_binding_for_sender(bot_id, sender_open_id="ou-any", environ={}) is None
    assert (
        pool.routed_binding(bot_id, sender_open_id="ou-any", chat_id="oc-group", environ={}) is None
    )


def test_private_binding_never_resolves_as_a_group(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    code, _expires, _revision = pool.issue_code(
        bot_id,
        owner=OWNER,
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )
    claim, word = pool.claim_code(
        bot_id, code=code, sender_open_id="ou-sender", chat_id="oc-1", environ={}
    )
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.activate_claim(
        bot_id,
        pairing_id=claim.pairing_id,
        confirm_word=word,
        actor=OWNER,
        expected_revision=entry.revision,
        environ={},
    )
    legacy = pool.routed_binding(bot_id, sender_open_id="ou-sender", chat_id="oc-1", environ={})
    assert legacy is not None
    assert legacy.chat_type == "p2p"
    assert legacy.is_group is False
    assert pool.routed_binding_for_chat(bot_id, chat_id="oc-1", environ={}) is None


def test_bound_bot_refuses_a_second_group_binding(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    _bind_group(pool, bot_id)
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    with pytest.raises(FeishuBotOccupied):
        pool.bind_group(
            bot_id,
            owner=OTHER,
            tab_id="tab-2",
            workspace_id=None,
            chat_id="oc-group-2",
            chat_name="Other",
            expected_revision=entry.revision,
            environ={},
        )


def test_group_binding_respects_tab_occupancy_and_revision(
    pool: FeishuBotPoolStore,
) -> None:
    first = pool.create_bot(name="One", config=_config("app-1"), environ={})
    second = pool.create_bot(name="Two", config=_config("app-2"), environ={})
    _bind_group(pool, first)
    entry = pool.snapshot({}).get(second)
    assert entry is not None
    with pytest.raises(FeishuChatOccupied):
        pool.bind_group(
            second,
            owner=OWNER,
            tab_id="tab-1",
            workspace_id=None,
            chat_id="oc-group-2",
            chat_name="Other",
            expected_revision=entry.revision,
            environ={},
        )
    with pytest.raises(FeishuBotRevisionConflict):
        pool.bind_group(
            second,
            owner=OWNER,
            tab_id="tab-3",
            workspace_id=None,
            chat_id="oc-group-2",
            chat_name="Other",
            expected_revision=entry.revision + 5,
            environ={},
        )


def test_unknown_chat_type_in_state_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "feishu_bot_pool.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "pool_revision": 1,
                "bots": {
                    "bot-1": {
                        "bot_id": "bot-1",
                        "name": "One",
                        "app_id": "app-1",
                        "credentials": {"app_secret": "secret"},
                        "enabled": True,
                        "revision": 1,
                        "generation": 1,
                        "created_at": 1.0,
                        "updated_at": 1.0,
                        "claims": {},
                        "pending": {},
                        "binding": {
                            "pairing_id": "pid",
                            "tab_id": "tab-1",
                            "workspace_id": None,
                            "sender_open_id": "ou-1",
                            "chat_id": "oc-1",
                            "chat_type": "channel",
                            "created_at": 1.0,
                            "generation": 1,
                            "owner_kind": "local",
                            "owner_open_id": "local",
                            "owner_email": "local@localhost",
                        },
                    }
                },
                "rate_limits": {"local": []},
            }
        )
    )
    store = FeishuBotPoolStore(path=path, legacy_path=tmp_path / "secrets" / "feishu_bot.json")
    with pytest.raises(FeishuBotPoolStateError):
        store.snapshot({})


def test_chat_info_group_and_topic_semantics() -> None:
    """The chat-info API reports visibility in chat_type, so topic groups count."""

    assert FeishuChatInfo(chat_id="oc_1", chat_mode="topic", chat_type="private").is_group
    assert FeishuChatInfo(chat_id="oc_2", chat_mode="group", chat_type="public").is_group
    assert not FeishuChatInfo(chat_id="oc_3", chat_mode=None, chat_type="p2p").is_group
    assert not FeishuChatInfo(chat_id="oc_4").is_group
    assert FeishuChatInfo(chat_id="oc_5", chat_mode="topic").is_topic_mode
    assert FeishuChatInfo(
        chat_id="oc_6", chat_mode="group", group_message_type="thread"
    ).is_topic_mode
    assert not FeishuChatInfo(chat_id="oc_7", chat_mode="group").is_topic_mode


def test_legacy_binding_without_chat_type_stays_private(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "feishu_bot_pool.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "pool_revision": 1,
                "bots": {
                    "bot-1": {
                        "bot_id": "bot-1",
                        "name": "One",
                        "app_id": "app-1",
                        "credentials": {"app_secret": "secret"},
                        "enabled": True,
                        "revision": 1,
                        "generation": 1,
                        "created_at": 1.0,
                        "updated_at": 1.0,
                        "claims": {},
                        "pending": {},
                        "binding": {
                            "pairing_id": "pid",
                            "tab_id": "tab-1",
                            "workspace_id": None,
                            "sender_open_id": "ou-1",
                            "chat_id": "oc-1",
                            "created_at": 1.0,
                            "generation": 1,
                            "owner_kind": "local",
                            "owner_open_id": "local",
                            "owner_email": "local@localhost",
                        },
                    }
                },
                "rate_limits": {"local": []},
            }
        )
    )
    store = FeishuBotPoolStore(path=path, legacy_path=tmp_path / "secrets" / "feishu_bot.json")
    binding = store.snapshot({}).get("bot-1").binding
    assert binding is not None
    assert binding.chat_type == "p2p"
    assert binding.is_group is False
    assert store.routed_binding("bot-1", sender_open_id="ou-1", chat_id="oc-1") is not None
    assert store.routed_binding_for_chat("bot-1", chat_id="oc-1") is None


def test_an_over_long_chat_name_cannot_corrupt_the_pool(pool: FeishuBotPoolStore) -> None:
    """A rejected value must never be written: the next read would fail forever."""

    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    with pytest.raises(FeishuBotPoolStateError):
        pool.bind_group(
            bot_id,
            owner=OWNER,
            tab_id="tab-1",
            workspace_id=None,
            chat_id="oc-group",
            chat_name="x" * 5000,
            expected_revision=entry.revision,
            environ={},
        )
    # The pool still reads and the Bot is still unbound.
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    assert entry.binding is None
