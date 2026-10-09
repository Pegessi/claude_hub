"""The environment Bot must never share an app_id with a stored Bot.

The environment is outside Hub's control, so an operator can point it at an app
a stored Bot already owns. Both entries would then open long connections for
the same identity, and the per-Bot deduplication namespace would not stop one
inbound message from reaching two Chats. The conflicting identity is refused
and stored as an empty app_id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_hub.services.feishu_bot import FeishuBotConfig
from claude_hub.services.feishu_bot_pool import (
    ENV_BOT_ID,
    OFFICIAL_FEISHU_API,
    OWNER_KIND_OAUTH,
    BotEntry,
    FeishuBotAppIdConflict,
    FeishuBotPoolStore,
    FeishuBotUnavailable,
    OwnerIdentity,
)

_T0 = 1_700_000_000.0
_OWNER = OwnerIdentity(open_id="ou-owner", email="owner@example.com", kind=OWNER_KIND_OAUTH)


class _Clock:
    """Injected time source; the store keeps ``now`` as an instance attribute."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _store(tmp_path: Path, clock: _Clock | None = None) -> FeishuBotPoolStore:
    # legacy_path must point somewhere empty, or a missing pool file would send
    # _load into _migrate_legacy against the real runtime home.
    return FeishuBotPoolStore(
        path=tmp_path / "secrets" / "feishu_bot_pool.json",
        legacy_path=tmp_path / "secrets" / "feishu_bot.json",
        now=clock or _Clock(_T0),
    )


def _config(app_id: str) -> FeishuBotConfig:
    return FeishuBotConfig(
        app_id=app_id,
        app_secret=f"{app_id}-secret",
        api_base_url=OFFICIAL_FEISHU_API,
    )


def _env(app_id: str) -> dict[str, str]:
    return {
        "CLAUDE_HUB_FEISHU_BOT_APP_ID": app_id,
        "CLAUDE_HUB_FEISHU_BOT_APP_SECRET": f"{app_id}-secret",
        "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN": f"{app_id}-token",
        "CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY": f"{app_id}-encrypt",
    }


def _raw(store: FeishuBotPoolStore) -> dict:
    return json.loads(store.path.read_text(encoding="utf-8"))


def _entry(store: FeishuBotPoolStore, bot_id: str, environ: dict[str, str]) -> BotEntry:
    found = store.snapshot(environ=environ).get(bot_id)
    assert found is not None
    return found


def _pair(
    store: FeishuBotPoolStore,
    bot_id: str,
    environ: dict[str, str],
    *,
    tab_id: str = "tab-1",
    chat_id: str = "oc-1",
):
    """Drive the real three-step pairing instead of forging a binding."""

    code, _expires_at, revision = store.issue_code(
        bot_id,
        owner=_OWNER,
        tab_id=tab_id,
        workspace_id=None,
        expected_revision=_entry(store, bot_id, environ).revision,
        environ=environ,
    )
    claim, word = store.claim_code(
        bot_id, code=code, sender_open_id="ou-sender", chat_id=chat_id, environ=environ
    )
    return store.activate_claim(
        bot_id,
        pairing_id=claim.pairing_id,
        confirm_word=word,
        actor=_OWNER,
        expected_revision=_entry(store, bot_id, environ).revision,
        environ=environ,
    )


def test_first_sync_refuses_a_conflicting_environment_app_id(tmp_path: Path) -> None:
    """Gap 1: the conflict check has to run before the entry is created."""

    store = _store(tmp_path)
    # environ={} here on purpose: naming cli_B would make _sync_env_entry adopt
    # it first and the creation would then collide with the entry it just made.
    store.create_bot(name="Stored", config=_config("cli_B"), environ={})
    assert ENV_BOT_ID not in _raw(store)["bots"]

    store.snapshot(environ=_env("cli_B"))

    env_raw = _raw(store)["bots"][ENV_BOT_ID]
    assert env_raw["app_id"] == ""
    assert env_raw["binding"] is None


def test_refused_environment_is_reported_as_invalid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_bot(name="Stored", config=_config("cli_B"), environ={})
    environ = _env("cli_B")

    entry = _entry(store, ENV_BOT_ID, environ)

    assert entry.configured is False
    assert entry.source == "invalid_environment"
    assert entry.app_id == ""
    assert entry.config is None


def test_refused_environment_credentials_are_unusable(tmp_path: Path) -> None:
    """Nothing may route through the refused entry, so no Chat can be reached."""

    store = _store(tmp_path)
    store.create_bot(name="Stored", config=_config("cli_B"), environ={})
    environ = _env("cli_B")

    with pytest.raises(FeishuBotUnavailable):
        store.effective(ENV_BOT_ID, environ=environ)


def test_the_stored_bot_keeps_working_through_a_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored_id = store.create_bot(name="Stored", config=_config("cli_B"), environ={})
    environ = _env("cli_B")

    usable = store.effective(stored_id, environ=environ)

    assert usable.app_id == "cli_B"
    assert usable.config.app_secret == "cli_B-secret"


def test_preexisting_duplicate_app_id_is_healed_on_first_sync(tmp_path: Path) -> None:
    """Gap 2: the equality short-circuit used to make a duplicate permanent.

    A pool written before the check existed can hold the duplicate directly, so
    the state is injected rather than produced through the API, which no longer
    allows it. The binding is a real one taken from an actual pairing.
    """

    store = _store(tmp_path)
    env_a = _env("cli_A")
    store.snapshot(environ=env_a)
    _pair(store, ENV_BOT_ID, env_a)
    stored_id = store.create_bot(name="Stored", config=_config("cli_B"), environ=env_a)

    state = _raw(store)
    assert state["bots"][ENV_BOT_ID]["binding"] is not None
    state["bots"][ENV_BOT_ID]["app_id"] = "cli_B"
    before = state["bots"][ENV_BOT_ID]["generation"]
    store.path.write_text(json.dumps(state), encoding="utf-8")

    store.snapshot(environ=_env("cli_B"))

    healed = _raw(store)["bots"][ENV_BOT_ID]
    assert healed["app_id"] == ""
    assert healed["binding"] is None
    assert healed["generation"] > before
    assert _raw(store)["bots"][stored_id]["app_id"] == "cli_B"


def test_conflicted_environment_is_idempotent_across_reads(tmp_path: Path) -> None:
    """Gap 3: an unadopted identity must not advance anything on every read.

    Comparing the stored empty app_id against the raw environment would differ
    forever, so each read would advance the revision, the generation and the
    pool revision, leaving pool_revision growing with no change behind it.
    """

    store = _store(tmp_path)
    store.create_bot(name="Stored", config=_config("cli_B"), environ={})
    environ = _env("cli_B")

    settled = store.snapshot(environ=environ)
    settled_entry = _raw(store)["bots"][ENV_BOT_ID]
    settled_bytes = store.path.read_bytes()

    for _ in range(3):
        again = store.snapshot(environ=environ)
        assert again.pool_revision == settled.pool_revision
        current = _raw(store)["bots"][ENV_BOT_ID]
        assert current["revision"] == settled_entry["revision"]
        assert current["generation"] == settled_entry["generation"]
        assert store.path.read_bytes() == settled_bytes


def test_environment_change_into_a_conflict_revokes_the_binding(tmp_path: Path) -> None:
    """The binding must be alive right up to the switch, or this proves nothing.

    Creating the stored Bot carries environ=env_a deliberately: create_bot runs
    _sync_env_entry itself, so an empty environment there would revoke the
    binding before the conflict ever happens.
    """

    store = _store(tmp_path)
    env_a = _env("cli_A")
    store.snapshot(environ=env_a)
    _pair(store, ENV_BOT_ID, env_a)

    stored_id = store.create_bot(name="Stored", config=_config("cli_B"), environ=env_a)
    alive = _entry(store, ENV_BOT_ID, env_a)
    assert alive.binding is not None
    assert alive.app_id == "cli_A"
    before = alive.generation

    store.snapshot(environ=_env("cli_B"))

    revoked = _raw(store)["bots"][ENV_BOT_ID]
    assert revoked["app_id"] == ""
    assert revoked["binding"] is None
    assert revoked["generation"] > before
    assert store.effective(stored_id, environ=_env("cli_B")).app_id == "cli_B"


def test_switching_back_does_not_resurrect_the_old_binding(tmp_path: Path) -> None:
    """The binding is cleared, not hidden, so no later identity can revive it."""

    store = _store(tmp_path)
    env_a = _env("cli_A")
    store.snapshot(environ=env_a)
    _pair(store, ENV_BOT_ID, env_a)
    store.create_bot(name="Stored", config=_config("cli_B"), environ=env_a)
    store.snapshot(environ=_env("cli_B"))
    refused = _raw(store)["bots"][ENV_BOT_ID]["generation"]

    store.snapshot(environ=env_a)

    entry = _entry(store, ENV_BOT_ID, env_a)
    assert entry.app_id == "cli_A"
    assert entry.binding is None
    assert entry.generation > refused
    # Re-pairing must be possible: a hidden binding would still report occupied.
    store.issue_code(
        ENV_BOT_ID,
        owner=_OWNER,
        tab_id="tab-2",
        workspace_id=None,
        expected_revision=entry.revision,
        environ=env_a,
    )


def test_resolving_the_conflict_restores_a_usable_environment_bot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stored_id = store.create_bot(name="Stored", config=_config("cli_B"), environ={})
    environ = _env("cli_B")
    store.snapshot(environ=environ)
    assert _raw(store)["bots"][ENV_BOT_ID]["app_id"] == ""

    store.delete_bot(stored_id, expected_revision=0, environ=environ)
    store.snapshot(environ=environ)

    entry = _entry(store, ENV_BOT_ID, environ)
    assert entry.app_id == "cli_B"
    assert entry.configured is True
    assert entry.binding is None


def test_a_revoked_stored_bot_does_not_block_the_environment(tmp_path: Path) -> None:
    """Tombstones are skipped here exactly as they are in _require_app_id_free.

    Otherwise deleting a Bot would lock its app_id out of the environment for
    the whole tombstone retention, with nothing on screen to explain why.
    """

    store = _store(tmp_path)
    stored_id = store.create_bot(name="Stored", config=_config("cli_B"), environ={})
    store.delete_bot(stored_id, expected_revision=0, environ={})
    assert _raw(store)["bots"][stored_id]["revoked_at"] is not None

    store.snapshot(environ=_env("cli_B"))

    assert _raw(store)["bots"][ENV_BOT_ID]["app_id"] == "cli_B"


def test_creating_a_bot_on_the_live_environment_app_is_still_refused(tmp_path: Path) -> None:
    """The opposite order stays guarded by _require_app_id_free."""

    store = _store(tmp_path)
    environ = _env("cli_A")
    store.snapshot(environ=environ)

    with pytest.raises(FeishuBotAppIdConflict):
        store.create_bot(name="Clash", config=_config("cli_A"), environ=environ)
