from __future__ import annotations

import json

import pytest

from claude_hub.services.feishu_bot import FeishuBotConfig
from claude_hub.services.feishu_bot_pool import (
    ENV_BOT_ID,
    OWNER_KIND_OAUTH,
    FeishuBotPoolStateError,
    FeishuBotPoolStore,
    OwnerIdentity,
)

_T0 = 1_700_000_000.0
_OWNER = OwnerIdentity(open_id="ou-hub", email="hub@example.test", kind=OWNER_KIND_OAUTH)
_CONFIG = FeishuBotConfig(
    app_id="cli-stored",
    app_secret="test-secret",
    verification_token="test-token",
    encrypt_key="test-encrypt",
)
_ENV = {
    "CLAUDE_HUB_FEISHU_BOT_APP_ID": "cli-env",
    "CLAUDE_HUB_FEISHU_BOT_APP_SECRET": "env-secret",
    "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN": "env-token",
    "CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY": "env-encrypt",
}


def _store(tmp_path):
    return FeishuBotPoolStore(
        path=tmp_path / "pool.json", legacy_path=tmp_path / "legacy.json", now=lambda: _T0
    )


def _revision(store, bot_id, environ):
    return store.snapshot(environ=environ).get(bot_id).revision


def _claim(store, bot_id, environ):
    code, _, _ = store.issue_code(
        bot_id,
        owner=_OWNER,
        tab_id="tab-a",
        workspace_id=None,
        expected_revision=_revision(store, bot_id, environ),
        environ=environ,
    )
    return store.claim_code(
        bot_id, code=code, sender_open_id="ou-bot", chat_id="oc-bot", environ=environ
    )


def _activate(store, bot_id, pending, word, environ):
    return store.activate_claim(
        bot_id,
        pairing_id=pending.pairing_id,
        confirm_word=word,
        actor=_OWNER,
        expected_revision=_revision(store, bot_id, environ),
        environ=environ,
    )


@pytest.mark.parametrize(
    "key",
    [
        "CLAUDE_HUB_FEISHU_BOT_APP_SECRET",
        "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN",
        "CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY",
        "CLAUDE_HUB_FEISHU_API_BASE_URL",
    ],
)
def test_env_config_change_invalidates_snapshot_without_revoking_binding(tmp_path, key):
    store = _store(tmp_path)
    store.snapshot(environ=_ENV)
    pending, word = _claim(store, ENV_BOT_ID, _ENV)
    binding = _activate(store, ENV_BOT_ID, pending, word, _ENV)
    old = store.effective(ENV_BOT_ID, environ=_ENV)
    before = store.path.read_bytes()
    assert store.is_current(old, environ=dict(_ENV))

    changed = dict(_ENV)
    changed[key] = (
        "https://alternate.example.test" if key.endswith("API_BASE_URL") else "rotated-value"
    )
    assert not store.is_current(old, environ=changed)
    current = store.effective(ENV_BOT_ID, environ=changed)
    assert current.revision == old.revision
    assert current.generation == old.generation
    assert store.snapshot(environ=changed).get(ENV_BOT_ID).binding == binding
    assert store.is_current(current, environ=changed)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("version", [True, False, 2.0, "2", None])
def test_invalid_pool_version_is_not_rewritten(tmp_path, version):
    store = _store(tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "version": version,
                "pool_revision": 0,
                "bots": {},
                "rate_limits": {},
            }
        ),
        encoding="utf-8",
    )
    before = store.path.read_bytes()
    with pytest.raises(FeishuBotPoolStateError):
        store.create_bot(name="new", config=_CONFIG, environ={})
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None])
def test_invalid_legacy_version_cannot_create_a_pool(tmp_path, version):
    store = _store(tmp_path)
    store.legacy_path.write_text(
        json.dumps(
            {
                "version": version,
                "revision": 1,
                "binding_generation": 1,
                "updated_at": _T0,
                "config": None,
            }
        ),
        encoding="utf-8",
    )
    before = store.legacy_path.read_bytes()
    with pytest.raises(FeishuBotPoolStateError):
        store.create_bot(name="new", config=_CONFIG, environ={})
    assert not store.path.exists()
    assert store.legacy_path.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(True, id="bool"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(10**400, id="overflow"),
    ],
)
@pytest.mark.parametrize(
    "field",
    [
        "created_at",
        "updated_at",
        "revoked_at",
        "pending.issued_at",
        "pending.expires_at",
        "claims.created_at",
        "claims.expires_at",
        "binding.created_at",
        "rate_limits",
    ],
)
def test_invalid_persisted_time_fails_before_read_or_mutation(tmp_path, bad, field):
    store = _store(tmp_path)
    bot_id = store.create_bot(name="test", config=_CONFIG, environ={})
    pending, word = _claim(store, bot_id, {})
    if field.startswith("binding."):
        _activate(store, bot_id, pending, word, {})
    else:
        store.issue_code(
            bot_id,
            owner=_OWNER,
            tab_id="tab-b",
            workspace_id=None,
            expected_revision=_revision(store, bot_id, {}),
            environ={},
        )
    state = json.loads(store.path.read_text(encoding="utf-8"))
    row = state["bots"][bot_id]
    if field == "rate_limits":
        state["rate_limits"][_OWNER.open_id] = [bad]
    elif "." not in field:
        row[field] = bad
    else:
        collection, name = field.split(".")
        record = (
            row[collection] if collection == "binding" else next(iter(row[collection].values()))
        )
        record[name] = bad
    store.path.write_text(json.dumps(state), encoding="utf-8")
    before = store.path.read_bytes()

    with pytest.raises(FeishuBotPoolStateError):
        store.snapshot(environ={})
    assert store.path.read_bytes() == before
    with pytest.raises(FeishuBotPoolStateError):
        store.create_bot(
            name="unrelated",
            config=FeishuBotConfig(
                app_id="cli-other",
                app_secret="other",
                verification_token="other",
                encrypt_key="other",
            ),
            environ={},
        )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("location", ["pool", "legacy"])
def test_invalid_utf8_is_controlled_and_preserved(tmp_path, location):
    store = _store(tmp_path)
    path = store.path if location == "pool" else store.legacy_path
    path.write_bytes(b"\xff")
    with pytest.raises(FeishuBotPoolStateError):
        store.create_bot(name="test", config=_CONFIG, environ={})
    assert path.read_bytes() == b"\xff"
    if location == "legacy":
        assert not store.path.exists()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(float("inf"), id="infinity"),
        pytest.param(10**400, id="overflow"),
    ],
)
def test_invalid_legacy_timestamp_is_controlled_and_preserved(tmp_path, bad):
    store = _store(tmp_path)
    store.legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 1,
                "binding_generation": 1,
                "updated_at": bad,
                "config": None,
            }
        ),
        encoding="utf-8",
    )
    before = store.legacy_path.read_bytes()
    with pytest.raises(FeishuBotPoolStateError):
        store.snapshot(environ={})
    assert not store.path.exists()
    assert store.legacy_path.read_bytes() == before
