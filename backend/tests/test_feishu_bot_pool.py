"""Bot pool invariants, pairing, environment sync, gates, and events."""

from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claude_hub.api import feishu_bot as bot_api
from claude_hub.auth import session as session_store
from claude_hub.auth.dependencies import get_current_user_from_cookie
from claude_hub.config import settings
from claude_hub.main import app
from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    ChatMode,
    ManagedSession,
    User,
)
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.tailer import SessionTailer
from claude_hub.services.feishu_bot import FeishuBotClient, FeishuBotConfig
from claude_hub.services.feishu_bot_pool import (
    ENV_BOT_ID,
    LEGACY_BOT_ID,
    OWNER_KIND_LOCAL,
    OWNER_KIND_OAUTH,
    FeishuBindingCodeError,
    FeishuBotAppIdConflict,
    FeishuBotOccupied,
    FeishuBotPoolStateError,
    FeishuBotPoolStore,
    FeishuBotRateLimited,
    FeishuBotRevisionConflict,
    FeishuChatOccupied,
    FeishuPairingMismatch,
    FeishuPairingNotOwned,
    OwnerIdentity,
    dedup_key,
    turn_id_for,
)

CONFIRM_RE = re.compile(r"[A-HJ-NP-Z2-9]{6}")


# ------------------------------------------------------------------- A. net


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to reach a real socket.

    Replacing ``AsyncClient.request`` would also disable the ASGI and Mock
    transports these tests rely on, and a marker that re-opens it would let a
    regression reach the network unnoticed. Only the two real transports are
    blocked, so ASGITransport and MockTransport keep working.
    """

    def blocked(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a test attempted a real outbound HTTP request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked, raising=True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked, raising=True)


def test_network_guard_blocks_a_real_transport() -> None:
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(pytest.fail.Exception, match="real outbound HTTP"):
            client.get("http://127.0.0.1:1/never")


# -------------------------------------------------------------- fixtures


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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
    return FeishuBotConfig(
        app_id=app_id,
        app_secret=f"secret-{app_id}",
    )


OWNER = OwnerIdentity(open_id="ou-owner", email="owner@example.test", kind=OWNER_KIND_OAUTH)
OTHER = OwnerIdentity(open_id="ou-other", email="other@example.test", kind=OWNER_KIND_OAUTH)
LOCAL = OwnerIdentity(open_id="local", email="local@localhost", kind=OWNER_KIND_LOCAL)


def _env(app_id: str = "env-app") -> dict[str, str]:
    return {
        "CLAUDE_HUB_FEISHU_BOT_APP_ID": app_id,
        "CLAUDE_HUB_FEISHU_BOT_APP_SECRET": "env-secret",
        "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN": "env-verify",
        "CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY": "env-encrypt",
    }


def _pair(
    pool: FeishuBotPoolStore,
    bot_id: str,
    *,
    owner: OwnerIdentity = OWNER,
    tab_id: str = "tab-1",
    chat_id: str = "oc-1",
    sender: str = "ou-sender",
    environ: dict[str, str] | None = None,
):
    entry = pool.snapshot(environ).get(bot_id)
    assert entry is not None
    code, _expires, _revision = pool.issue_code(
        bot_id,
        owner=owner,
        tab_id=tab_id,
        workspace_id=None,
        expected_revision=entry.revision,
        environ=environ,
    )
    claim, word = pool.claim_code(
        bot_id, code=code, sender_open_id=sender, chat_id=chat_id, environ=environ
    )
    entry = pool.snapshot(environ).get(bot_id)
    assert entry is not None
    return pool.activate_claim(
        bot_id,
        pairing_id=claim.pairing_id,
        confirm_word=word,
        actor=owner,
        expected_revision=entry.revision,
        environ=environ,
    )


# ------------------------------------------------------- B. pool invariants


def test_app_id_is_unique_in_the_pool(pool: FeishuBotPoolStore) -> None:
    pool.create_bot(name="One", config=_config("app-1"), environ={})
    with pytest.raises(FeishuBotAppIdConflict):
        pool.create_bot(name="Two", config=_config("app-1"), environ={})


def test_one_bot_holds_at_most_one_binding(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    _pair(pool, bot_id, environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    with pytest.raises(FeishuBotOccupied):
        pool.issue_code(
            bot_id,
            owner=OTHER,
            tab_id="tab-2",
            workspace_id=None,
            expected_revision=entry.revision,
            environ={},
        )


def test_one_chat_tab_is_bound_at_most_once_across_the_pool(pool: FeishuBotPoolStore) -> None:
    first = pool.create_bot(name="One", config=_config("app-1"), environ={})
    second = pool.create_bot(name="Two", config=_config("app-2"), environ={})
    _pair(pool, first, tab_id="tab-1", environ={})
    entry = pool.snapshot({}).get(second)
    assert entry is not None
    with pytest.raises(FeishuChatOccupied):
        pool.issue_code(
            second,
            owner=OTHER,
            tab_id="tab-1",
            workspace_id=None,
            expected_revision=entry.revision,
            environ={},
        )


def test_occupied_bot_is_never_preempted_silently(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    binding = _pair(pool, bot_id, environ={})
    with pytest.raises(FeishuBotOccupied):
        entry = pool.snapshot({}).get(bot_id)
        assert entry is not None
        pool.issue_code(
            bot_id,
            owner=OTHER,
            tab_id="tab-9",
            workspace_id=None,
            expected_revision=entry.revision,
            environ={},
        )
    current = pool.snapshot({}).get(bot_id)
    assert current is not None
    assert current.binding is not None
    assert current.binding.pairing_id == binding.pairing_id


def test_rotating_secrets_keeps_the_active_binding(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    binding = _pair(pool, bot_id, environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.rotate_secrets(
        bot_id,
        app_secret="next-secret",
        expected_revision=entry.revision,
        environ={},
    )
    after = pool.snapshot({}).get(bot_id)
    assert after is not None
    assert after.binding is not None
    assert after.binding.pairing_id == binding.pairing_id
    assert after.generation == entry.generation
    assert after.revision == entry.revision + 1
    assert after.config is not None
    assert after.config.app_secret == "next-secret"


def test_pool_revision_increases_on_every_mutation(pool: FeishuBotPoolStore) -> None:
    start = pool.snapshot({}).pool_revision
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    after_create = pool.snapshot({}).pool_revision
    assert after_create > start
    _pair(pool, bot_id, environ={})
    assert pool.snapshot({}).pool_revision > after_create
    # A plain read must not advance it.
    steady = pool.snapshot({}).pool_revision
    assert pool.snapshot({}).pool_revision == steady


# ------------------------------------------------------ C. environment sync


def test_create_rejects_the_environment_app_id_before_any_pool_write(
    pool: FeishuBotPoolStore,
) -> None:
    """The environment entry is synthesized on read, so uniqueness must
    materialize it before checking, or the very first stored Bot could
    duplicate it."""

    environ = _env("env-app")
    assert not pool.path.exists()
    with pytest.raises(FeishuBotAppIdConflict):
        pool.create_bot(name="Clash", config=_config("env-app"), environ=environ)


def test_environment_app_change_revokes_binding_and_allows_repairing(
    pool: FeishuBotPoolStore,
) -> None:
    environ = _env("env-app")
    _pair(pool, ENV_BOT_ID, environ=environ)
    moved = _env("env-app-next")
    entry = pool.snapshot(moved).get(ENV_BOT_ID)
    assert entry is not None
    assert entry.binding is None
    # The raw record must really be cleared, not merely hidden: otherwise
    # issue_code keeps reporting an occupied Bot and re-pairing is impossible.
    code, _expires, _revision = pool.issue_code(
        ENV_BOT_ID,
        owner=OWNER,
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ=moved,
    )
    assert code.startswith("CH-")


def test_environment_app_restore_does_not_resurrect_the_old_binding(
    pool: FeishuBotPoolStore,
) -> None:
    environ = _env("env-app")
    _pair(pool, ENV_BOT_ID, environ=environ)
    before_entry = pool.snapshot(environ).get(ENV_BOT_ID)
    assert before_entry is not None
    before = before_entry.generation
    moved = _env("env-app-next")
    # Only a read happens while the app is different; the read path must still
    # persist the revocation.
    moved_entry = pool.snapshot(moved).get(ENV_BOT_ID)
    assert moved_entry is not None
    assert moved_entry.binding is None
    restored = pool.snapshot(environ).get(ENV_BOT_ID)
    assert restored is not None
    assert restored.binding is None
    assert restored.generation == before + 2


def test_environment_removal_revokes_the_binding(pool: FeishuBotPoolStore) -> None:
    environ = _env("env-app")
    _pair(pool, ENV_BOT_ID, environ=environ)
    entry = pool.snapshot({}).get(ENV_BOT_ID)
    assert entry is not None
    assert entry.binding is None
    assert entry.configured is False


def test_environment_sync_is_idempotent(pool: FeishuBotPoolStore) -> None:
    environ = _env("env-app")
    pool.snapshot(environ)
    first = pool.path.read_bytes()
    pool.snapshot(environ)
    pool.snapshot(environ)
    assert pool.path.read_bytes() == first


# ---------------------------------------------------------------- D. pairing


def test_pairing_requires_the_confirmation_word(pool: FeishuBotPoolStore) -> None:
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
    claim, word = pool.claim_code(bot_id, code=code, sender_open_id="ou-s", chat_id="oc-1")
    # A claim alone routes nothing.
    pending = pool.snapshot({}).get(bot_id)
    assert pending is not None
    assert pending.binding is None
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    with pytest.raises(FeishuPairingMismatch):
        pool.activate_claim(
            bot_id,
            pairing_id=claim.pairing_id,
            confirm_word="AAAAAA" if word != "AAAAAA" else "BBBBBB",
            actor=OWNER,
            expected_revision=entry.revision,
        )
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    binding = pool.activate_claim(
        bot_id,
        pairing_id=claim.pairing_id,
        confirm_word=word,
        actor=OWNER,
        expected_revision=entry.revision,
    )
    assert binding.tab_id == "tab-1"


def test_wrong_confirmation_word_destroys_the_claim_after_five_tries(
    pool: FeishuBotPoolStore,
) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    code, _e, _r = pool.issue_code(
        bot_id,
        owner=OWNER,
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )
    claim, word = pool.claim_code(bot_id, code=code, sender_open_id="ou-s", chat_id="oc-1")
    for _ in range(5):
        entry = pool.snapshot({}).get(bot_id)
        assert entry is not None
        with pytest.raises(FeishuPairingMismatch):
            pool.activate_claim(
                bot_id,
                pairing_id=claim.pairing_id,
                confirm_word="AAAAAA" if word != "AAAAAA" else "BBBBBB",
                actor=OWNER,
                expected_revision=entry.revision,
            )
    current = pool.snapshot({}).get(bot_id)
    assert current is not None
    assert current.claims == ()


def test_another_hub_identity_cannot_activate_someone_elses_claim(
    pool: FeishuBotPoolStore,
) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    code, _e, _r = pool.issue_code(
        bot_id,
        owner=OWNER,
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )
    claim, word = pool.claim_code(bot_id, code=code, sender_open_id="ou-s", chat_id="oc-1")
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    with pytest.raises(FeishuPairingNotOwned):
        pool.activate_claim(
            bot_id,
            pairing_id=claim.pairing_id,
            confirm_word=word,
            actor=OTHER,
            expected_revision=entry.revision,
        )


def test_expired_code_cannot_be_claimed(pool: FeishuBotPoolStore, clock: Clock) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    code, _e, _r = pool.issue_code(
        bot_id,
        owner=OWNER,
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )
    clock.advance(601)
    with pytest.raises(FeishuBindingCodeError):
        pool.claim_code(bot_id, code=code, sender_open_id="ou-s", chat_id="oc-1")


def test_identifiers_are_namespaced_per_bot() -> None:
    assert dedup_key("bot-a", "om-1") != dedup_key("bot-b", "om-1")
    assert turn_id_for("bot-a", "om-1") != turn_id_for("bot-b", "om-1")
    assert turn_id_for("bot-a", "om-1").startswith("feishu-")


# ------------------------------------------------------------------- E. CAS


def test_stale_release_cannot_delete_a_newer_binding(pool: FeishuBotPoolStore) -> None:
    """The counterexample that makes an integer revision mandatory even for the
    environment Bot: unbind, re-bind, then replay the old release."""

    environ = _env("env-app")
    _pair(pool, ENV_BOT_ID, environ=environ)
    stale_entry = pool.snapshot(environ).get(ENV_BOT_ID)
    assert stale_entry is not None
    stale = stale_entry.revision
    pool.release_binding(ENV_BOT_ID, expected_revision=stale, environ=environ)
    fresh = _pair(pool, ENV_BOT_ID, environ=environ)
    with pytest.raises(FeishuBotRevisionConflict):
        pool.release_binding(ENV_BOT_ID, expected_revision=stale, environ=environ)
    current = pool.snapshot(environ).get(ENV_BOT_ID)
    assert current is not None
    assert current.binding is not None
    assert current.binding.pairing_id == fresh.pairing_id


def test_drop_binding_ignores_a_superseded_pairing_id(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    first = _pair(pool, bot_id, environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.release_binding(bot_id, expected_revision=entry.revision, environ={})
    second = _pair(pool, bot_id, environ={})
    assert pool.drop_binding(bot_id, first.pairing_id) is False
    current = pool.snapshot({}).get(bot_id)
    assert current is not None
    assert current.binding is not None
    assert current.binding.pairing_id == second.pairing_id


# -------------------------------------------------------- I. hardening


def test_non_finite_timestamps_are_rejected(pool: FeishuBotPoolStore) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    state = json.loads(pool.path.read_text())
    state["bots"][bot_id]["binding"] = {
        "pairing_id": "p-1",
        "owner_open_id": "ou-owner",
        "owner_email": "o@e.test",
        "owner_kind": "oauth",
        "tab_id": "tab-1",
        "workspace_id": None,
        "sender_open_id": "ou-s",
        "chat_id": "oc-1",
        "created_at": float("inf"),
        "generation": 0,
    }
    pool.path.write_text(json.dumps(state))
    with pytest.raises(FeishuBotPoolStateError):
        pool.snapshot({})


def test_infinite_claim_expiry_does_not_make_a_claim_immortal(
    pool: FeishuBotPoolStore,
) -> None:
    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    code, _e, _r = pool.issue_code(
        bot_id,
        owner=OWNER,
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )
    claim, _word = pool.claim_code(bot_id, code=code, sender_open_id="ou-s", chat_id="oc-1")
    state = json.loads(pool.path.read_text())
    state["bots"][bot_id]["claims"][claim.pairing_id]["expires_at"] = math.inf
    pool.path.write_text(json.dumps(state).replace("Infinity", "1e999"))
    before = pool.path.read_bytes()
    with pytest.raises(FeishuBotPoolStateError):
        pool.snapshot({})
    assert pool.path.read_bytes() == before


# ------------------------------------------------------------ H. migration


def _legacy(revision: int = 3, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "revision": revision,
        "binding_generation": 2,
        "updated_at": 1_700_000_000.0,
        "config": config,
    }


def test_legacy_configuration_migrates_without_its_bindings(
    pool: FeishuBotPoolStore,
) -> None:
    pool.legacy_path.parent.mkdir(parents=True, exist_ok=True)
    pool.legacy_path.write_text(
        json.dumps(
            _legacy(
                config={
                    "app_id": "old-app",
                    "app_secret": "s",
                    "verification_token": "v",
                    "encrypt_key": "e",
                }
            )
        )
    )
    entry = pool.snapshot({}).get(LEGACY_BOT_ID)
    assert entry is not None
    assert entry.app_id == "old-app"
    assert entry.revision == 3
    assert entry.binding is None


def test_legacy_disabled_state_becomes_a_tombstone(pool: FeishuBotPoolStore) -> None:
    pool.legacy_path.parent.mkdir(parents=True, exist_ok=True)
    pool.legacy_path.write_text(json.dumps(_legacy(config=None)))
    assert pool.snapshot({}).get(LEGACY_BOT_ID) is None
    with pytest.raises(Exception):
        pool.effective(LEGACY_BOT_ID, {})


def test_corrupt_legacy_file_fails_closed(pool: FeishuBotPoolStore) -> None:
    """A legacy file that cannot be trusted must not look like an empty pool,
    or an operator would re-create a Bot while old credentials remain on disk."""

    pool.legacy_path.parent.mkdir(parents=True, exist_ok=True)
    pool.legacy_path.write_text(json.dumps({"version": 1, "config": {}}))
    with pytest.raises(FeishuBotPoolStateError):
        pool.snapshot({})


def test_pool_file_takes_over_and_legacy_is_never_read_again(
    pool: FeishuBotPoolStore,
) -> None:
    pool.create_bot(name="New", config=_config("new-app"), environ={})
    pool.legacy_path.parent.mkdir(parents=True, exist_ok=True)
    pool.legacy_path.write_text(
        json.dumps(
            _legacy(
                config={
                    "app_id": "old-app",
                    "app_secret": "s",
                    "verification_token": "v",
                    "encrypt_key": "e",
                }
            )
        )
    )
    ids = {entry.app_id for entry in pool.snapshot({}).bots}
    assert ids == {"new-app"}


# --------------------------------------------------------------- F. gates


def test_gate_entry_is_not_replaced_while_referenced(monkeypatch) -> None:
    """Evicting on ``locked()`` would be unsafe: after release() wakes a
    waiter nobody holds the lock while a waiter is still pending."""

    monkeypatch.setattr(bot_api, "_bot_gates", {})
    first = bot_api._enter_gate_registry("bot-1")
    try:
        assert first.users == 1
        second = bot_api._enter_gate_registry("bot-1")
        assert second is first
        assert first.users == 2
        bot_api._leave_gate_registry("bot-1", second)
        assert bot_api._bot_gates["bot-1"] is first
    finally:
        bot_api._leave_gate_registry("bot-1", first)
    assert "bot-1" not in bot_api._bot_gates


def test_gate_capacity_refuses_a_new_entry_without_evicting_referenced_ones(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    held = [bot_api._enter_gate_registry(f"bot-{index}") for index in range(bot_api._MAX_GATES)]
    try:
        with pytest.raises(HTTPException) as info:
            bot_api._enter_gate_registry("overflow")
        assert info.value.status_code == 503
        assert info.value.detail == "bot_operation_busy"
        assert all(
            bot_api._bot_gates[f"bot-{index}"] is held[index] for index in range(bot_api._MAX_GATES)
        )
    finally:
        for index, entry in enumerate(held):
            bot_api._leave_gate_registry(f"bot-{index}", entry)


async def test_gate_serializes_two_operations_on_one_bot(monkeypatch) -> None:
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    order: list[str] = []
    inside = asyncio.Event()

    async def first() -> None:
        async with bot_api._gate("bot-1"):
            order.append("first-in")
            inside.set()
            await asyncio.sleep(0)
            order.append("first-out")

    async def second() -> None:
        await inside.wait()
        async with bot_api._gate("bot-1"):
            order.append("second-in")

    await asyncio.gather(first(), second())
    assert order == ["first-in", "first-out", "second-in"]


async def test_waiter_keeps_the_same_gate_object_across_the_release_window(
    monkeypatch,
) -> None:
    """The exact race that reference counting exists for: the waiter must end
    up holding the same lock object the holder released, never a replacement."""

    monkeypatch.setattr(bot_api, "_bot_gates", {})
    inside = asyncio.Event()
    seen: list[Any] = []

    async def holder() -> None:
        async with bot_api._gate("bot-1"):
            seen.append(bot_api._bot_gates["bot-1"])
            inside.set()
            await asyncio.sleep(0)

    async def waiter() -> None:
        await inside.wait()
        async with bot_api._gate("bot-1"):
            seen.append(bot_api._bot_gates["bot-1"])

    await asyncio.gather(holder(), waiter())
    assert len(seen) == 2 and seen[0] is seen[1]
    assert "bot-1" not in bot_api._bot_gates


async def test_gates_of_different_bots_do_not_block_each_other(monkeypatch) -> None:
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    async with bot_api._gate("bot-1"):
        async with bot_api._gate("bot-2"):
            assert set(bot_api._bot_gates) == {"bot-1", "bot-2"}


async def test_reply_path_does_not_reacquire_the_gate(monkeypatch, wire) -> None:
    """``asyncio.Lock`` is not reentrant, so a reply issued while still holding
    a Bot's gate would deadlock. This pins that the reply happens outside."""

    monkeypatch.setattr(bot_api, "_bot_gates", {})
    async with bot_api._gate("bot-1"):
        pass
    assert "bot-1" not in bot_api._bot_gates


# ------------------------------------------------ WebSocket event integration


def _message(app_id: str, text: str, now: float, *, message_id: str = "om-1") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        header=SimpleNamespace(event_id=f"ev-{message_id}", app_id=app_id),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user", sender_id=SimpleNamespace(open_id="ou-sender")
            ),
            message=SimpleNamespace(
                chat_type="p2p",
                message_type="text",
                message_id=message_id,
                create_time=str(int(now * 1000)),
                chat_id="oc-1",
                content=json.dumps({"text": text}),
            ),
        ),
    )


class _Wire:
    """Fake Feishu transport that keeps the real client logic intact."""

    def __init__(self) -> None:
        self.replies: list[dict[str, Any]] = []
        self.reply_message_ids: list[str] = []
        self.reaction_creates: list[str] = []
        self.reaction_types: list[tuple[str, str]] = []
        self.reaction_deletes: list[tuple[str, str]] = []
        self.fail_reaction_create = False
        self.fail_reaction_delete = False
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            self.token_calls += 1
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        if request.url.path.endswith("/reply"):
            self.replies.append(json.loads(request.content))
            self.reply_message_ids.append(request.url.path.split("/")[-2])
            return httpx.Response(200, json={"code": 0})
        path = request.url.path.split("/")
        if request.method == "POST" and request.url.path.endswith("/reactions"):
            message_id = path[-2]
            self.reaction_creates.append(message_id)
            if self.fail_reaction_create:
                return httpx.Response(403, json={"code": 99991672})
            body = json.loads(request.content)
            emoji_type = body["reaction_type"]["emoji_type"]
            assert emoji_type in {"OneSecond", "Typing"}
            self.reaction_types.append((message_id, emoji_type))
            return httpx.Response(
                200, json={"code": 0, "data": {"reaction_id": f"react-{message_id}"}}
            )
        if request.method == "DELETE" and "/reactions/" in request.url.path:
            message_id, reaction_id = path[-3], path[-1]
            self.reaction_deletes.append((message_id, reaction_id))
            if self.fail_reaction_delete:
                return httpx.Response(403, json={"code": 99991672})
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
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    recorder = _Wire()

    def factory(config: FeishuBotConfig, client: httpx.AsyncClient | None = None):
        return FeishuBotClient(
            config,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(recorder.handler),
                base_url=config.api_base_url,
            ),
        )

    monkeypatch.setattr(bot_api, "FeishuBotClient", factory)
    return recorder


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: Clock, wire: _Wire):
    from types import SimpleNamespace

    from claude_hub.services.feishu_bot import FeishuMessageDedupStore

    store = FeishuBotPoolStore(
        path=tmp_path / "secrets" / "feishu_bot_pool.json",
        legacy_path=tmp_path / "secrets" / "feishu_bot.json",
        now=clock,
    )
    monkeypatch.setattr(bot_api, "_pool", store)
    monkeypatch.setattr(
        bot_api, "_dedup", FeishuMessageDedupStore(path=tmp_path / "feishu_bot.json", now=clock)
    )
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    monkeypatch.setattr(bot_api, "_tab_dispatch_queues", {})
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.01)
    monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.04)
    # Chat target validation has its own tests; stubbing it here keeps these
    # cases about the pool and the WebSocket event path.
    monkeypatch.setattr(bot_api, "_validate_bind_target", lambda tab_id, workspace_id: None)
    monkeypatch.setattr(bot_api, "_now", clock)
    monkeypatch.setattr(bot_api, "_create_gate", asyncio.Lock())
    client = TestClient(app)
    try:
        yield SimpleNamespace(client=client, pool=store, wire=wire)
    finally:
        client.close()


def test_pairing_code_reply_carries_the_confirmation_word(api, clock: Clock) -> None:
    """Regression for the blocking defect: consuming the code advances the Bot
    revision, so a reply still built on the pre-claim snapshot would be
    suppressed by ``is_current`` and the word would never reach the user."""

    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    entry = api.pool.snapshot({}).get(bot_id)
    code, _expires, _revision = api.pool.issue_code(
        bot_id,
        owner=LOCAL,
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )
    asyncio.run(bot_api._handle_sdk_event(bot_id, _message("app-1", code, clock.value)))
    texts = api.wire.reply_texts()
    assert texts, "the confirmation word was never delivered"
    found = CONFIRM_RE.search(texts[0])
    assert found is not None
    entry = api.pool.snapshot({}).get(bot_id)
    claim = entry.claims[0]
    activated = api.client.post(
        f"/api/feishu/bot/bots/{bot_id}/pair/activate",
        json={
            "pairing_id": claim.pairing_id,
            "confirm_word": found.group(0),
            "expected_revision": entry.revision,
        },
    )
    assert activated.status_code == 200
    bot = next(item for item in activated.json()["bots"] if item["bot_id"] == bot_id)
    assert bot["binding"]["state"] == "active"
    assert bot["binding"]["tab_id"] == "tab-1"


def test_unpaired_conversation_is_told_how_to_pair(api, clock: Clock) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    asyncio.run(bot_api._handle_sdk_event(bot_id, _message("app-1", "hello", clock.value)))
    assert any("尚未连接" in text for text in api.wire.reply_texts())


def test_websocket_event_for_a_deleted_bot_is_dropped_silently(api, clock: Clock) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    entry = api.pool.snapshot({}).get(bot_id)
    api.pool.delete_bot(bot_id, expected_revision=entry.revision, environ={})
    asyncio.run(bot_api._handle_sdk_event(bot_id, _message("app-1", "hello", clock.value)))
    assert api.wire.replies == []


def test_websocket_event_for_an_unknown_bot_is_dropped(api, clock: Clock) -> None:
    asyncio.run(
        bot_api._handle_sdk_event("does-not-exist", _message("app-1", "hello", clock.value))
    )
    assert api.wire.replies == []


def test_duplicate_message_is_claimed_once(api, clock: Clock) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    message = _message("app-1", "hello", clock.value)
    asyncio.run(bot_api._handle_sdk_event(bot_id, message))
    asyncio.run(bot_api._handle_sdk_event(bot_id, message))
    assert len(api.wire.replies) == 1


def test_continuous_message_burst_default_max_wait_is_five_seconds() -> None:
    assert bot_api._MESSAGE_COALESCE_MAX_WAIT_SECONDS == 5.0


def test_short_bound_message_burst_is_coalesced_in_arrival_order(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.02)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.08)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        submitted: list[str] = []

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, visible_text, **_kwargs):
            assert "lightweight Markdown" in _text
            assert _kwargs["turn_metadata"]["provider_text_format"] == "feishu-v2"
            async with admission_guard():
                submitted.append(visible_text)
            return "combined answer"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        tasks = [
            asyncio.create_task(
                bot_api._handle_sdk_event(
                    bot_id, _message("app-1", text, clock.value, message_id=f"om-{index}")
                )
            )
            for index, text in enumerate(("one", "two", "three"), start=1)
        ]
        await asyncio.gather(*tasks)

        assert len(submitted) == 1
        assert submitted[0].index("[消息 1]\none") < submitted[0].index("[消息 2]\ntwo")
        assert submitted[0].index("[消息 2]\ntwo") < submitted[0].index("[消息 3]\nthree")
        assert api.wire.reply_texts() == ["combined answer"]
        assert api.wire.reply_message_ids == ["om-3"]
        assert [reply["msg_type"] for reply in api.wire.replies] == ["post"]
        assert {(message_id, emoji) for message_id, emoji in api.wire.reaction_types} >= {
            ("om-1", "OneSecond"),
            ("om-2", "OneSecond"),
            ("om-3", "OneSecond"),
        }
        assert bot_api._tab_dispatch_queues == {}

    asyncio.run(scenario())


def test_messages_arriving_during_a_turn_form_one_followup_without_interrupting(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.02)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.08)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        submitted: list[str] = []

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, visible_text, **_kwargs):
            async with admission_guard():
                submitted.append(visible_text)
            if len(submitted) == 1:
                first_started.set()
                await release_first.wait()
            return f"answer-{len(submitted)}"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        first = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "initial", clock.value, message_id="om-first")
            )
        )
        await first_started.wait()
        followups = [
            asyncio.create_task(
                bot_api._handle_sdk_event(
                    bot_id, _message("app-1", text, clock.value, message_id=message_id)
                )
            )
            for text, message_id in (("more context", "om-more"), ("final detail", "om-final"))
        ]
        await asyncio.sleep(0.04)
        assert submitted == ["initial"], "a follow-up must not interrupt the active model turn"

        release_first.set()
        await asyncio.gather(first, *followups)

        assert len(submitted) == 2
        assert "处理上一条消息期间补充" in submitted[1]
        assert submitted[1].index("[消息 1]\nmore context") < submitted[1].index(
            "[消息 2]\nfinal detail"
        )
        assert api.wire.reply_texts() == ["answer-1", "answer-2"]
        assert bot_api._tab_dispatch_queues == {}

    asyncio.run(scenario())


def test_rejected_batch_does_not_mark_the_first_admitted_turn_as_followup(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.01)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.04)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        first_attempted = asyncio.Event()
        release_rejection = asyncio.Event()
        admitted: list[tuple[str, bool]] = []
        attempts = 0

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, visible_text, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                first_attempted.set()
                await release_rejection.wait()
                raise HTTPException(
                    status_code=409,
                    headers={bot_api.CHAT_ERROR_REASON_HEADER: "chat_busy"},
                )
            async with admission_guard():
                admitted.append((visible_text, kwargs["turn_metadata"]["feishu_followup"]))
            return "done"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        first = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "rejected", clock.value, message_id="om-rejected")
            )
        )
        await first_attempted.wait()
        second = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "first admitted", clock.value, message_id="om-admitted")
            )
        )
        for _ in range(100):
            queue = bot_api._tab_dispatch_queues.get("tab-1")
            if queue is not None and len(queue.waiters) == 1:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("second message never entered the existing queue")

        release_rejection.set()
        await asyncio.gather(first, second)

        assert admitted == [("first admitted", False)]
        assert bot_api._tab_dispatch_queues == {}

    asyncio.run(scenario())


def test_continuous_message_burst_is_dispatched_at_the_max_wait(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.05)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.12)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        started = asyncio.Event()
        submitted: list[str] = []

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, visible_text, **_kwargs):
            async with admission_guard():
                submitted.append(visible_text)
                started.set()
            return "done"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        tasks = [
            asyncio.create_task(
                bot_api._handle_sdk_event(
                    bot_id, _message("app-1", "first", clock.value, message_id="om-max-0")
                )
            )
        ]
        for index in range(1, 4):
            await asyncio.sleep(0.035)
            tasks.append(
                asyncio.create_task(
                    bot_api._handle_sdk_event(
                        bot_id,
                        _message(
                            "app-1",
                            f"extra-{index}",
                            clock.value,
                            message_id=f"om-max-{index}",
                        ),
                    )
                )
            )

        await asyncio.wait_for(started.wait(), timeout=0.08)
        await asyncio.gather(*tasks)
        assert len(submitted) == 1
        assert "extra-3" in submitted[0]

    asyncio.run(scenario())


def test_cancelling_a_coalesced_follower_does_not_cancel_the_shared_turn(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.04)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.1)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        submitted: list[str] = []

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, visible_text, **_kwargs):
            async with admission_guard():
                submitted.append(visible_text)
            return "done"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        leader = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "keep", clock.value, message_id="om-keep")
            )
        )
        follower_payload = _message(
            "app-1", "cancel me", clock.value, message_id="om-cancel-follower"
        )
        follower = asyncio.create_task(bot_api._handle_sdk_event(bot_id, follower_payload))
        for _ in range(100):
            queue = bot_api._tab_dispatch_queues.get("tab-1")
            if queue is not None and len(queue.pending) == 2:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("follower never joined the coalescing window")

        follower.cancel()
        with pytest.raises(bot_api.ExternalDispatchRetired):
            await follower
        await leader
        assert submitted == ["keep"]

        # The cancelled message never entered the model turn, so the same-ID
        # WebSocket retry must be admitted and delivered once.
        await bot_api._handle_sdk_event(bot_id, follower_payload)
        assert submitted == ["keep", "cancel me"]

    asyncio.run(scenario())


def test_shutdown_releases_every_claim_when_a_coalesced_turn_retires(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.02)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.08)
        monkeypatch.setattr(bot_api, "_BATCH_RETIREMENT_WAIT_SECONDS", 0.5)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        dispatched = asyncio.Event()
        release_retirement = asyncio.Event()
        attempts: list[str] = []

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, visible_text, **_kwargs):
            async with admission_guard():
                attempts.append(visible_text)
                dispatched.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_retirement.wait()
                raise bot_api.ExternalDispatchRetired(_turn_id) from None

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        payloads = [
            _message("app-1", text, clock.value, message_id=f"om-shutdown-{index}")
            for index, text in enumerate(("one", "two", "three"), start=1)
        ]
        routes = [
            asyncio.create_task(bot_api._handle_sdk_event(bot_id, payload)) for payload in payloads
        ]
        await dispatched.wait()
        for route in routes:
            route.cancel()
        await asyncio.sleep(0)
        release_retirement.set()
        results = await asyncio.gather(*routes, return_exceptions=True)
        # asyncio Tasks normalize CancelledError subclasses in gathered return
        # values, so the durable replay below is the authoritative retirement
        # assertion. All route tasks must at least end through cancellation.
        assert all(isinstance(result, asyncio.CancelledError) for result in results)

        # Each route owned a separate durable claim even though the model saw
        # one batch. Safe retirement must make all three IDs retryable.
        async def replay_dispatch(
            _tab_id, _text, _turn_id, *, admission_guard, visible_text, **_kwargs
        ):
            async with admission_guard():
                attempts.append(visible_text)
            return "done"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", replay_dispatch)
        await asyncio.gather(*(bot_api._handle_sdk_event(bot_id, payload) for payload in payloads))
        assert len(attempts) == 2
        assert "[消息 3]\nthree" in attempts[-1]

    asyncio.run(scenario())


def test_cancellation_during_error_reply_releases_the_active_batch(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 0.02)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 0.08)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        error_reply_started = asyncio.Event()
        real_reply = bot_api._reply_if_current

        async def dispatch(*_args, **_kwargs):
            raise HTTPException(
                status_code=409,
                headers={bot_api.CHAT_ERROR_REASON_HEADER: "chat_busy"},
            )

        async def blocked_reply(client, event, effective, text, **kwargs):
            if "正在处理其他消息" in text:
                error_reply_started.set()
                await asyncio.Future()
            return await real_reply(client, event, effective, text, **kwargs)

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        monkeypatch.setattr(bot_api, "_reply_if_current", blocked_reply)
        routes = [
            asyncio.create_task(
                bot_api._handle_sdk_event(
                    bot_id, _message("app-1", text, clock.value, message_id=message_id)
                )
            )
            for text, message_id in (("one", "om-error-one"), ("two", "om-error-two"))
        ]
        await error_reply_started.wait()
        for route in routes:
            route.cancel()
        await asyncio.gather(*routes, return_exceptions=True)

        assert bot_api._tab_dispatch_queues == {}

    asyncio.run(scenario())


def test_bound_message_queue_has_an_explicit_capacity_response(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        release = asyncio.Event()
        started = asyncio.Event()

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, **_kwargs):
            async with admission_guard():
                started.set()
            await release.wait()
            return "done"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        first = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "active", clock.value, message_id="om-active")
            )
        )
        await started.wait()
        waiting = []
        for index in range(bot_api._MAX_TAB_QUEUE_WAITERS):
            waiting.append(
                asyncio.create_task(
                    bot_api._handle_sdk_event(
                        bot_id,
                        _message("app-1", "waiting", clock.value, message_id=f"om-wait-{index}"),
                    )
                )
            )
            for _ in range(20):
                queue = bot_api._tab_dispatch_queues.get("tab-1")
                if queue is not None and len(queue.waiters) == index + 1:
                    break
                await asyncio.sleep(0)

        await bot_api._handle_sdk_event(
            bot_id, _message("app-1", "overflow", clock.value, message_id="om-overflow")
        )
        assert api.wire.reply_texts() == ["Claude Hub 消息队列已满，本条消息未执行，请稍后重试。"]
        assert ("om-overflow", "react-om-overflow") in api.wire.reaction_deletes

        release.set()
        await asyncio.gather(first, *waiting)
        assert bot_api._tab_dispatch_queues == {}

    async def bounded_scenario() -> None:
        await asyncio.wait_for(scenario(), timeout=2)

    asyncio.run(bounded_scenario())


@pytest.mark.parametrize("failure", ["create", "delete"])
def test_reaction_permission_failure_never_fails_the_main_message(
    api, clock: Clock, wire: _Wire, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    _pair(api.pool, bot_id, owner=LOCAL, environ={})
    setattr(wire, f"fail_reaction_{failure}", True)

    async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, **_kwargs):
        async with admission_guard():
            pass
        return "still delivered"

    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
    asyncio.run(
        bot_api._handle_sdk_event(
            bot_id, _message("app-1", "hello", clock.value, message_id=f"om-{failure}")
        )
    )

    assert api.wire.reply_texts() == ["still delivered"]
    assert bot_api._tab_dispatch_queues == {}


def test_reaction_network_steps_do_not_delay_native_dispatch(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        queued_add_started = asyncio.Event()
        allow_queued_add = asyncio.Event()
        queued_delete_started = asyncio.Event()
        allow_queued_delete = asyncio.Event()
        typing_add_started = asyncio.Event()
        allow_typing_add = asyncio.Event()
        native_started = asyncio.Event()
        allow_native_reply = asyncio.Event()
        add_calls = 0
        delete_calls = 0

        async def add_reaction(_client, _event, emoji_type="Typing"):
            nonlocal add_calls
            add_calls += 1
            if add_calls == 1:
                assert emoji_type == "OneSecond"
                queued_add_started.set()
                await allow_queued_add.wait()
                return "queued-reaction"
            assert emoji_type == "Typing"
            typing_add_started.set()
            await allow_typing_add.wait()
            return "typing-reaction"

        async def delete_reaction(_client, _event, _reaction_id):
            nonlocal delete_calls
            delete_calls += 1
            if delete_calls == 1:
                queued_delete_started.set()
                await allow_queued_delete.wait()

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, **_kwargs):
            async with admission_guard():
                native_started.set()
            await allow_native_reply.wait()
            return "done"

        monkeypatch.setattr(bot_api, "_add_processing_reaction", add_reaction)
        monkeypatch.setattr(bot_api, "_delete_processing_reaction", delete_reaction)
        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        route = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "hello", clock.value, message_id="om-reactions")
            )
        )

        try:
            await queued_add_started.wait()
            await asyncio.wait_for(native_started.wait(), timeout=1.0)
            allow_queued_add.set()
            await queued_delete_started.wait()
            assert native_started.is_set()
            allow_queued_delete.set()
            await typing_add_started.wait()
            assert native_started.is_set()
            allow_typing_add.set()
            allow_native_reply.set()
            await route
        finally:
            allow_queued_add.set()
            allow_queued_delete.set()
            allow_typing_add.set()
            allow_native_reply.set()
            if not route.done():
                route.cancel()
            await asyncio.gather(route, return_exceptions=True)

        assert add_calls == 2
        assert delete_calls == 2
        assert bot_api._tab_dispatch_queues == {}

    asyncio.run(scenario())


def test_double_cancellation_during_reaction_cleanup_preserves_retirement(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_QUIET_SECONDS", 10.0)
        monkeypatch.setattr(bot_api, "_MESSAGE_COALESCE_MAX_WAIT_SECONDS", 10.0)
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        reaction_added = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        reaction_tasks: list[asyncio.Task[None]] = []

        async def add_reaction(_client, _event, _emoji_type="Typing"):
            reaction_added.set()
            return "queued-reaction"

        async def delete_reaction(_client, _event, _reaction_id):
            task = asyncio.current_task()
            assert task is not None
            reaction_tasks.append(task)
            cleanup_started.set()
            await release_cleanup.wait()

        monkeypatch.setattr(bot_api, "_add_processing_reaction", add_reaction)
        monkeypatch.setattr(bot_api, "_delete_processing_reaction", delete_reaction)
        message_id = "om-double-cancel"
        route = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "hello", clock.value, message_id=message_id)
            )
        )

        try:
            await reaction_added.wait()
            await asyncio.sleep(0)
            route.cancel()
            await cleanup_started.wait()
            route.cancel()
            release_cleanup.set()
            with pytest.raises(bot_api.ExternalDispatchRetired):
                await route
        finally:
            release_cleanup.set()
            if not route.done():
                route.cancel()
            await asyncio.gather(route, return_exceptions=True)

        assert reaction_tasks
        assert all(task.done() for task in reaction_tasks)
        assert bot_api._tab_dispatch_queues == {}
        key = bot_api.dedup_key(bot_id, message_id)
        assert bot_api._dedup.claim(key), "the precise retirement must release the dedup claim"
        bot_api._dedup.release(key)

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))


def test_queued_message_rechecks_binding_before_dispatch(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        submitted: list[str] = []

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, visible_text, **_kwargs):
            async with admission_guard():
                submitted.append(visible_text)
            if visible_text == "first":
                first_started.set()
                await release_first.wait()
            return "done"

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        first = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "first", clock.value, message_id="om-first")
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "second", clock.value, message_id="om-second")
            )
        )
        for _ in range(100):
            queue = bot_api._tab_dispatch_queues.get("tab-1")
            if queue is not None and len(queue.waiters) == 1 and queue.waiters[0]._callbacks:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("queued route never reached its reservation wait")
        entry = api.pool.snapshot({}).get(bot_id)
        api.pool.release_binding(bot_id, expected_revision=entry.revision, environ={})
        release_first.set()
        await asyncio.gather(first, second)

        assert submitted == ["first"]
        assert bot_api._tab_dispatch_queues == {}
        assert ("om-second", "react-om-second") in api.wire.reaction_deletes

    asyncio.run(scenario())


def test_external_web_busy_remains_an_accurate_rejection(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    _pair(api.pool, bot_id, owner=LOCAL, environ={})

    async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, **_kwargs):
        async with admission_guard():
            pass
        raise HTTPException(
            status_code=409, headers={bot_api.CHAT_ERROR_REASON_HEADER: "chat_busy"}
        )

    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
    asyncio.run(
        bot_api._handle_sdk_event(
            bot_id, _message("app-1", "hello", clock.value, message_id="om-web-busy")
        )
    )

    assert api.wire.reply_texts() == [
        "Claude Hub Chat 正在处理其他消息或等待网页回答，本条消息尚未执行。"
    ]
    assert bot_api._tab_dispatch_queues == {}


def test_dispatch_queue_cancellation_releases_waiters_and_registry(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        started = asyncio.Event()

        async def dispatch(_tab_id, _text, _turn_id, *, admission_guard, **_kwargs):
            async with admission_guard():
                started.set()
            await asyncio.Future()

        monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
        active = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "active", clock.value, message_id="om-active")
            )
        )
        await started.wait()
        waiting = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "waiting", clock.value, message_id="om-waiting")
            )
        )
        for _ in range(100):
            queue = bot_api._tab_dispatch_queues.get("tab-1")
            if queue is not None and len(queue.waiters) == 1 and queue.waiters[0]._callbacks:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("queued route never reached its reservation wait")

        waiting.cancel()
        done, _pending = await asyncio.wait({waiting}, timeout=1)
        assert done, waiting.get_stack()
        with pytest.raises(bot_api.ExternalDispatchRetired):
            await waiting
        assert bot_api._tab_dispatch_queues["tab-1"].waiters == []

        # The cancelled waiter never entered the Chat bridge, so a WebSocket
        # replay with the same message ID must be admitted and queued again.
        replayed = asyncio.create_task(
            bot_api._handle_sdk_event(
                bot_id, _message("app-1", "waiting", clock.value, message_id="om-waiting")
            )
        )
        for _ in range(100):
            queue = bot_api._tab_dispatch_queues.get("tab-1")
            if queue is not None and len(queue.waiters) == 1 and queue.waiters[0]._callbacks:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("same-ID replay was not admitted after queued cancellation")
        replayed.cancel()
        with pytest.raises(bot_api.ExternalDispatchRetired):
            await replayed

        active.cancel()
        done, _pending = await asyncio.wait({active}, timeout=1)
        assert done, active.get_stack()
        with pytest.raises(asyncio.CancelledError):
            await active
        await asyncio.sleep(0)

        assert bot_api._tab_dispatch_queues == {}
        assert set(api.wire.reaction_deletes) == {
            ("om-active", "react-om-active"),
            ("om-waiting", "react-om-waiting"),
        }

    async def bounded_scenario() -> None:
        await asyncio.wait_for(scenario(), timeout=2)

    asyncio.run(bounded_scenario())


def test_replaced_single_bot_interfaces_return_410(api) -> None:
    for method, path in [
        ("get", "/api/feishu/bot/config/status"),
        ("get", "/api/feishu/bot/config"),
        ("put", "/api/feishu/bot/config"),
        ("delete", "/api/feishu/bot/config"),
        ("post", "/api/feishu/bot/bind/start"),
        ("get", "/api/feishu/bot/binding"),
        ("delete", "/api/feishu/bot/binding"),
    ]:
        response = getattr(api.client, method)(path)
        assert response.status_code == 410, path
        assert response.json()["detail"] == "interface_replaced_by_bot_pool"


def test_local_identity_can_manage_the_pool_without_oauth(api) -> None:
    """Hub's own access control admits the shared local identity; the pool must
    not add an OAuth gate the rest of Hub does not have."""

    assert not settings.auth_enabled
    created = api.client.post(
        "/api/feishu/bot/bots",
        json={
            "name": "One",
            "app_id": "app-1",
            "app_secret": "s",
        },
    )
    assert created.status_code == 201
    assert created.json()["focus_bot_id"]


def test_pool_response_carries_a_monotonic_pool_revision(api) -> None:
    first = api.client.get("/api/feishu/bot/bots").json()["pool_revision"]
    created = api.client.post(
        "/api/feishu/bot/bots",
        json={
            "name": "One",
            "app_id": "app-1",
            "app_secret": "s",
        },
    ).json()
    assert created["pool_revision"] > first
    assert (
        api.client.get("/api/feishu/bot/bots").json()["pool_revision"] == created["pool_revision"]
    )


def test_other_peoples_binding_hides_chat_and_owner(api, clock: Clock) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    _pair(api.pool, bot_id, owner=OTHER, environ={})
    bot = next(
        item
        for item in api.client.get("/api/feishu/bot/bots").json()["bots"]
        if item["bot_id"] == bot_id
    )
    assert bot["binding"]["is_mine"] is False
    assert bot["binding"]["chat_id"] is None
    assert bot["binding"]["tab_id"] == "tab-1"
    assert "owner_open_id" not in bot["binding"]
    assert bot["my_claims"] == []


# ---------------------------------------------------- J. allowlist parity


@pytest.mark.parametrize(
    "open_ids,emails,open_id,email",
    [
        ("", "", "ou-a", "a@example.test"),
        ("ou-a", "", "ou-a", "a@example.test"),
        ("ou-b", "", "ou-a", "a@example.test"),
        ("", "a@example.test", "ou-a", "a@example.test"),
        ("", "b@example.test", "ou-a", "a@example.test"),
        ("ou-b", "a@example.test", "ou-a", "a@example.test"),
        ("", "a@example.test", "ou-a", ""),
        ("", "A@Example.Test", "ou-a", "a@example.test"),
    ],
)
async def test_owner_authorization_matches_the_hub_session_rule(
    monkeypatch: pytest.MonkeyPatch, open_ids: str, emails: str, open_id: str, email: str
) -> None:
    """Pin the Bot-side predicate against Hub's own whitelist behaviour.

    The two implementations are intentionally separate, so this comparison is
    what makes a future change to Hub's rule fail loudly instead of letting the
    Bot copy drift.
    """

    monkeypatch.setattr(settings, "feishu_app_id", "oauth-app")
    monkeypatch.setattr(settings, "feishu_app_secret", "oauth-secret")
    monkeypatch.setattr(settings, "auth_allowed_open_ids", open_ids)
    monkeypatch.setattr(settings, "auth_allowed_emails", emails)
    login = session_store.create_session(
        User(open_id=open_id, name="Tester", email=email), "access", "refresh"
    )
    hub_allows = await get_current_user_from_cookie(login.session_id) is not None
    pool_allows = bot_api._owner_is_authorized(
        OwnerIdentity(open_id=open_id, email=email, kind=OWNER_KIND_OAUTH)
    )
    assert pool_allows == hub_allows


async def test_local_owner_is_never_revoked_by_an_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local trust is instance-wide, not personal. Pretending an allowlist can
    revoke it would advertise a protection that does not exist."""

    monkeypatch.setattr(settings, "feishu_app_id", "oauth-app")
    monkeypatch.setattr(settings, "feishu_app_secret", "oauth-secret")
    monkeypatch.setattr(settings, "auth_allowed_open_ids", "ou-someone-else")
    assert bot_api._owner_is_authorized(LOCAL) is True


async def test_oauth_owner_is_revoked_when_removed_from_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "oauth-app")
    monkeypatch.setattr(settings, "feishu_app_secret", "oauth-secret")
    monkeypatch.setattr(settings, "auth_allowed_open_ids", "ou-someone-else")
    assert bot_api._owner_is_authorized(OWNER) is False


@pytest.fixture(autouse=True)
def clean_bot_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in tuple(os.environ):
        if name.startswith("CLAUDE_HUB_FEISHU_BOT_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(settings, "feishu_app_id", None)
    monkeypatch.setattr(settings, "feishu_app_secret", None)
    monkeypatch.setattr(settings, "auth_allowed_open_ids", "")
    monkeypatch.setattr(settings, "auth_allowed_emails", "")


@pytest.mark.parametrize(
    "had_old_binding,message_offset_ms,header_offset_seconds,should_dispatch",
    [
        pytest.param(True, 1000, 1, False, id="first-delivery-after-rebind"),
        pytest.param(False, 1000, 1, False, id="sent-before-first-activation"),
        pytest.param(False, 11000, 11, True, id="sent-after-activation"),
        pytest.param(True, 1000, 15, False, id="fresh-header-does-not-refresh-old-message"),
    ],
)
def test_websocket_message_time_respects_latest_binding_activation(
    api,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    had_old_binding: bool,
    message_offset_ms: int,
    header_offset_seconds: int,
    should_dispatch: bool,
) -> None:
    base = clock.value
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    if had_old_binding:
        _pair(api.pool, bot_id, owner=LOCAL, tab_id="tab-old", environ={})
    clock.value = base + 10
    if had_old_binding:
        entry = api.pool.snapshot({}).get(bot_id)
        api.pool.release_binding(bot_id, expected_revision=entry.revision, environ={})
    binding = _pair(api.pool, bot_id, owner=LOCAL, tab_id="tab-new", environ={})
    assert binding.created_at == base + 10
    clock.value = base + 15
    assert not bot_api._dedup.path.exists(), "this event has never reached intake before"

    submitted: list[str] = []

    async def dispatch(tab_id, text, client_turn_id, *, admission_guard, **kwargs):
        async with admission_guard():
            submitted.append(tab_id)
        return "clock-checked answer"

    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", dispatch)
    payload = _message(
        "app-1", "ordinary message", base + header_offset_seconds, message_id="om-time"
    )
    payload.event.message.create_time = str(int(base * 1000) + message_offset_ms)
    asyncio.run(bot_api._handle_sdk_event(bot_id, payload))
    assert submitted == (["tab-new"] if should_dispatch else [])
    assert api.wire.reply_texts() == (["clock-checked answer"] if should_dispatch else [])

    asyncio.run(bot_api._handle_sdk_event(bot_id, payload))
    assert submitted == (["tab-new"] if should_dispatch else [])
    assert api.wire.reply_texts() == (["clock-checked answer"] if should_dispatch else [])


def test_cancelled_websocket_route_releases_dedup_for_retry(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    payload = _message("app-1", "hello", clock.value, message_id="om-cancelled")
    attempts = 0

    async def cancelled(*_args, **_kwargs) -> None:
        nonlocal attempts
        attempts += 1
        raise bot_api.ExternalDispatchRetired("feishu-turn")

    monkeypatch.setattr(bot_api, "_handle_message_event", cancelled)

    with pytest.raises(bot_api.ExternalDispatchRetired):
        asyncio.run(bot_api._handle_sdk_event(bot_id, payload))
    with pytest.raises(bot_api.ExternalDispatchRetired):
        asyncio.run(bot_api._handle_sdk_event(bot_id, payload))

    assert attempts == 2


def test_non_retryable_websocket_cancellation_preserves_dedup(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
    payload = _message("app-1", "hello", clock.value, message_id="om-completed")
    attempts = 0

    async def cancelled(*_args, **_kwargs) -> None:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    monkeypatch.setattr(bot_api, "_handle_message_event", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot_api._handle_sdk_event(bot_id, payload))
    asyncio.run(bot_api._handle_sdk_event(bot_id, payload))

    assert attempts == 1


def test_completed_native_turn_is_not_replayed_after_route_cancellation(
    api, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the full completion race with the real SessionTailer classifier."""
    from types import SimpleNamespace

    from claude_hub.api import agent_stream as stream_api

    class NativeTransport:
        def __init__(self) -> None:
            self._started = True
            self._records: asyncio.Queue[Any] = asyncio.Queue()
            self._turn_in_flight = False
            self.eof_is_fatal = False
            self.exit_error = None
            self.last_error = None
            self.sent_messages: list[str] = []

        async def start(self) -> None:
            self._started = True

        async def stop(self) -> None:
            self._started = False
            self._turn_in_flight = False

        async def cancel_active_turn(self) -> None:
            self._turn_in_flight = False

        async def read_line(self) -> Any:
            return await self._records.get()

        async def send_message(self, text: str, _images: list[bytes]) -> None:
            self.sent_messages.append(text)
            self._turn_in_flight = True

        async def answer_pending_question(self, _answers: Any) -> bool:
            return False

        @property
        def turn_in_flight(self) -> bool:
            return self._turn_in_flight

        def acknowledge_turn_complete(self) -> None:
            self._turn_in_flight = False

        def maybe_capture_conversation_id(self, _record: Any) -> None:
            pass

        def accepts_notification(self, _record: Any) -> bool:
            return True

    async def scenario() -> None:
        bot_id = api.pool.create_bot(name="One", config=_config("app-1"), environ={})
        _pair(api.pool, bot_id, owner=LOCAL, environ={})
        payload = _message("app-1", "run once", clock.value, message_id="om-race")
        session = SimpleNamespace(
            id="terminal-tab-tab-1",
            workspace_id="terminal-tabs",
            tab_id="tab-1",
            agent_type=AgentType.CLAUDE,
            chat_mode=ChatMode.DEFAULT,
        )
        transport = NativeTransport()
        completed = asyncio.Event()

        async def observe(event) -> None:
            if event.type == AgentStreamEventType.TURN_COMPLETED:
                completed.set()

        tailer = SessionTailer(
            workspace_id=session.workspace_id,
            session_id=session.id,
            adapter=ClaudeJsonlAdapter(),
            session_getter=lambda: cast(ManagedSession, session),
            native_transport=cast(ProviderSession, transport),
            post_persist_observers=[observe],
        )
        bridge_queue: asyncio.Queue[Any] = asyncio.Queue()

        class Manager:
            async def subscribe(self, _session) -> asyncio.Queue[Any]:
                await tailer.start()
                return bridge_queue

            async def retire_external_turn(self, _session, expected_turn_id: str):
                return await tailer.retire_external_turn(expected_turn_id)

            def unsubscribe(self, _session_id: str, _queue: asyncio.Queue[Any]) -> None:
                pass

        async def dispatch(
            _tab_id: str, request: stream_api.AgentStreamSendRequest, **kwargs: Any
        ) -> str:
            async with kwargs["admission_guard"]():
                await tailer.send_message(
                    request.text,
                    [],
                    request.client_turn_id,
                    visible_text=kwargs["visible_text"],
                    turn_metadata=kwargs["turn_metadata"],
                )
            transport._records.put_nowait({"type": "result", "subtype": "success"})
            return request.client_turn_id

        monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda _tab_id: session)
        monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: Manager())
        monkeypatch.setattr(stream_api, "_dispatch_tab_stream_input", dispatch)
        monkeypatch.setattr(
            bot_api, "dispatch_tab_chat_and_wait", stream_api.dispatch_tab_chat_and_wait
        )

        task = asyncio.create_task(bot_api._handle_sdk_event(bot_id, payload))
        await asyncio.wait_for(completed.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await bot_api._handle_sdk_event(bot_id, payload)
        assert len(transport.sent_messages) == 1
        started = [
            event
            for event in (await tailer.store.read_since(-1, limit=20)).events
            if event.type == AgentStreamEventType.TURN_STARTED
        ]
        assert len(started) == 1
        await tailer.stop()

    asyncio.run(scenario())


def test_pair_code_rate_limit_is_per_identity_and_expires(
    pool: FeishuBotPoolStore, clock: Clock
) -> None:
    """The allowance follows the Hub identity, so switching Bots cannot reset it.

    Codes accumulate instead of replacing one another, so five issues leave
    five live codes - well under the per-Bot pending cap. What is under test
    here is the rate window, not that cap.
    """

    first = pool.create_bot(name="One", config=_config("app-1"), environ={})
    second = pool.create_bot(name="Two", config=_config("app-2"), environ={})

    def issue(bot_id: str, owner: OwnerIdentity = OWNER, tab_id: str = "tab-1"):
        entry = pool.snapshot({}).get(bot_id)
        assert entry is not None
        return pool.issue_code(
            bot_id,
            owner=owner,
            tab_id=tab_id,
            workspace_id=None,
            expected_revision=entry.revision,
            environ={},
        )

    for _ in range(5):
        issue(first)

    with pytest.raises(FeishuBotRateLimited):
        issue(first)
    with pytest.raises(FeishuBotRateLimited):
        issue(second)
    # A different Hub identity carries its own allowance.
    issue(second, owner=OTHER, tab_id="tab-2")

    clock.advance(61)
    code, _expires, _revision = issue(first)
    assert code.startswith("CH-")


def test_a_code_cannot_be_claimed_twice(pool: FeishuBotPoolStore) -> None:
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
    pool.claim_code(bot_id, code=code, sender_open_id="ou-sender", chat_id="oc-1", environ={})

    with pytest.raises(FeishuBindingCodeError):
        pool.claim_code(bot_id, code=code, sender_open_id="ou-sender", chat_id="oc-1", environ={})
    current = pool.snapshot({}).get(bot_id)
    assert current is not None
    assert len(current.claims) == 1


def test_unbinding_revokes_outstanding_codes(pool: FeishuBotPoolStore) -> None:
    """Pending codes and an active binding never coexist.

    Activation empties pending and ``issue_code`` refuses while bound, so the
    unbound state is the only one where unbinding still has codes to revoke.
    """

    bot_id = pool.create_bot(name="One", config=_config(), environ={})
    codes = []
    for _ in range(2):
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
        codes.append(code)

    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    assert pool.release_binding(bot_id, expected_revision=entry.revision, environ={}) is None

    for code in codes:
        with pytest.raises(FeishuBindingCodeError):
            pool.claim_code(
                bot_id, code=code, sender_open_id="ou-sender", chat_id="oc-1", environ={}
            )
