"""Pool administration security: body limits, secret handling, CAS, reply gate.

These port the guarantees the single-Bot configuration tests used to hold. The
surface changed shape - a pool of Bots instead of one record, a per-Bot gate
instead of one publish lock - but the properties are the same: an unverified
credential never reaches disk, a secret never appears in public responses, a stale view
never wins a write, and a reply already handed to Feishu is never sent twice.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest

from claude_hub.api import feishu_bot as bot_api
from claude_hub.auth import session as session_store
from claude_hub.config import settings
from claude_hub.main import app
from claude_hub.models import User
from claude_hub.services.feishu_bot import FeishuBotConfig, FeishuMessageDedupStore
from claude_hub.services.feishu_bot_pool import (
    BOT_ENV_KEYS,
    FeishuBotPoolStore,
    FeishuBotRevisionConflict,
)

_TEST_NOW = 1_700_000_000.0
_SECRET = "app-secret-do-not-echo"
_TOKEN = "verification-token-do-not-echo"
_ENCRYPT = "encrypt-key-do-not-echo"


def _config(app_id: str = "cli_pool", secret: str = _SECRET) -> FeishuBotConfig:
    return FeishuBotConfig(
        app_id=app_id,
        app_secret=secret,
    )


def _cookie(open_id: str = "ou-admin") -> dict[str, str]:
    login = session_store.create_session(
        User(open_id=open_id, name="Tester", email=f"{open_id}@example.test"),
        "access-token",
        "refresh-token",
    )
    return {settings.session_cookie_name: login.session_id}


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FeishuBotPoolStore:
    for key in BOT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")
    store = FeishuBotPoolStore(
        path=tmp_path / "secrets" / "feishu_bot_pool.json",
        legacy_path=tmp_path / "secrets" / "feishu_bot.json",
        now=lambda: _TEST_NOW,
    )
    monkeypatch.setattr(bot_api, "_pool", store)
    monkeypatch.setattr(bot_api, "_now", lambda: _TEST_NOW)
    monkeypatch.setattr(
        bot_api,
        "_dedup",
        FeishuMessageDedupStore(tmp_path / "feishu_bot_events.json", now=lambda: _TEST_NOW),
    )
    # A fresh lock per test: the module-level one belongs to another loop.
    monkeypatch.setattr(bot_api, "_create_gate", asyncio.Lock())
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    return store


class _Accepting:
    def __init__(self, config: FeishuBotConfig) -> None:
        self.config = config

    async def validate_credentials(self) -> None:
        return None


class _Rejecting:
    def __init__(self, config: FeishuBotConfig) -> None:
        self.config = config

    async def validate_credentials(self) -> None:
        raise RuntimeError("feishu says no")


@pytest.fixture
def accepting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_api, "FeishuBotClient", _Accepting)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


# ------------------------------------------------------- secrets at rest


def test_pool_file_permissions_and_tombstone_leaves_no_secret(pool) -> None:
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})

    assert stat.S_IMODE(pool.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(pool.path.stat().st_mode) == 0o600
    assert _SECRET in pool.path.read_text(encoding="utf-8")

    pool.delete_bot(bot_id, expected_revision=0, environ={})

    raw = pool.path.read_text(encoding="utf-8")
    assert _SECRET not in raw
    assert _TOKEN not in raw and _ENCRYPT not in raw
    assert json.loads(raw)["bots"][bot_id]["revoked_at"] is not None


def test_stale_expected_revision_is_a_conflict(pool) -> None:
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    pool.rotate_secrets(
        bot_id,
        app_secret="second",
        expected_revision=0,
        environ={},
    )

    with pytest.raises(FeishuBotRevisionConflict):
        pool.rotate_secrets(
            bot_id,
            app_secret="third",
            expected_revision=0,
            environ={},
        )
    assert "third" not in pool.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_pool_response_never_echoes_secrets(pool) -> None:
    pool.create_bot(name="Bot", config=_config(), environ={})

    async with _client() as client:
        response = await client.get("/api/feishu/bot/bots", cookies=_cookie())

    assert response.status_code == 200
    body = response.text
    assert _SECRET not in body and _TOKEN not in body and _ENCRYPT not in body
    summary = response.json()["bots"][0]
    assert summary["app_secret_configured"] is True
    assert summary["connection_status"] == "stopped"
    assert "app_secret" not in summary and "credentials" not in summary


@pytest.mark.asyncio
async def test_corrupt_pool_file_fails_closed(pool) -> None:
    pool.path.parent.mkdir(parents=True, exist_ok=True)
    pool.path.write_text("{broken", encoding="utf-8")

    async with _client() as client:
        response = await client.get("/api/feishu/bot/bots", cookies=_cookie())

    assert response.status_code == 503
    assert response.json()["detail"] == "bot_pool_unavailable"
    assert pool.path.read_text(encoding="utf-8") == "{broken"


# ------------------------------------------- unverified credentials at rest


@pytest.mark.asyncio
async def test_rejected_credentials_never_create_a_bot(pool, monkeypatch) -> None:
    monkeypatch.setattr(bot_api, "FeishuBotClient", _Rejecting)

    async with _client() as client:
        response = await client.post(
            "/api/feishu/bot/bots",
            json={
                "name": "Bot",
                "app_id": "cli_pool",
                "app_secret": _SECRET,
            },
            cookies=_cookie(),
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "feishu_credentials_rejected"
    assert not pool.path.exists()


@pytest.mark.asyncio
async def test_rejected_credentials_never_rotate_an_existing_bot(pool, monkeypatch) -> None:
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    before = pool.path.read_bytes()
    monkeypatch.setattr(bot_api, "FeishuBotClient", _Rejecting)

    async with _client() as client:
        response = await client.put(
            f"/api/feishu/bot/bots/{bot_id}/secrets",
            json={
                "app_secret": "rotated-secret",
                "expected_revision": 0,
            },
            cookies=_cookie(),
        )

    assert response.status_code == 502
    assert pool.path.read_bytes() == before
    assert "rotated-secret" not in pool.path.read_text(encoding="utf-8")


# ----------------------------------------------------------- request bodies


@pytest.mark.asyncio
async def test_admin_body_limit_stops_a_chunked_request(pool) -> None:
    """The reader must stop at the limit, not after buffering the whole body."""

    continued = False

    async def chunks():
        nonlocal continued
        yield b"x" * (16 * 1024)
        yield b"xx"
        continued = True
        raise AssertionError("admin reader continued past its size limit")

    async with _client() as client:
        response = await client.post("/api/feishu/bot/bots", content=chunks(), cookies=_cookie())

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_bot_request"
    assert not continued


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"{broken", id="invalid-json"),
        pytest.param(b"[]", id="json-array"),
        pytest.param(b'"text"', id="json-string"),
        pytest.param(b"", id="empty"),
    ],
)
@pytest.mark.asyncio
async def test_unreadable_admin_body_is_rejected(pool, body: bytes) -> None:
    async with _client() as client:
        response = await client.post("/api/feishu/bot/bots", content=body, cookies=_cookie())

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_bot_request"
    assert not pool.path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"name": "Bot", "app_id": "cli_pool"}, id="missing-keys"),
        pytest.param(
            {
                "name": "Bot",
                "app_id": "cli_pool",
                "app_secret": _SECRET,
                "extra": 1,
            },
            id="extra-key",
        ),
        pytest.param(
            {
                "name": "   ",
                "app_id": "cli_pool",
                "app_secret": _SECRET,
            },
            id="blank-name",
        ),
        pytest.param(
            {
                "name": "Bot",
                "app_id": "cli_pool",
                "app_secret": "",
            },
            id="blank-secret",
        ),
    ],
)
@pytest.mark.asyncio
async def test_admin_body_must_carry_exactly_the_expected_keys(pool, payload) -> None:
    async with _client() as client:
        response = await client.post("/api/feishu/bot/bots", json=payload, cookies=_cookie())

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_bot_request"
    assert not pool.path.exists()


# -------------------------------------------------------------------- CAS


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        pytest.param(
            "PUT",
            "/secrets",
            {
                "app_secret": "rotated",
                "expected_revision": 7,
            },
            id="rotate",
        ),
        pytest.param("PATCH", "", {"name": "Renamed", "expected_revision": 7}, id="update"),
        pytest.param("DELETE", "", {"expected_revision": 7}, id="delete"),
    ],
)
@pytest.mark.asyncio
async def test_stale_revision_is_rejected_by_mutating_routes(
    pool, accepting, method: str, suffix: str, payload: dict
) -> None:
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    before = pool.path.read_bytes()

    async with _client() as client:
        response = await client.request(
            method,
            f"/api/feishu/bot/bots/{bot_id}{suffix}",
            json=payload,
            cookies=_cookie(),
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "bot_revision_conflict"
    assert pool.path.read_bytes() == before


# ------------------------------------------------------------- reply gate


def _event(message_id: str) -> "bot_api.FeishuMessageEvent":
    return bot_api.FeishuMessageEvent(
        event_id=f"evt-{message_id}",
        message_id=message_id,
        app_id="cli_pool",
        sender_open_id="ou-sender",
        chat_id="oc-chat",
        text="hello",
        message_created_at_ms=int(_TEST_NOW * 1000),
    )


@pytest.mark.asyncio
async def test_delete_waits_for_a_started_reply_and_blocks_the_late_one(pool, monkeypatch) -> None:
    """A reply already in flight finishes; the snapshot behind it then dies."""

    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    effective = pool.effective(bot_id, environ={})
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    class Blocking:
        async def get_tenant_token(self) -> str:
            return "tenant-token"

        async def reply_text(self, message_id, text, *, access_token=None) -> None:
            assert access_token == "tenant-token"
            entered.set()
            await release.wait()
            sent.append(message_id)

    reply = asyncio.create_task(
        bot_api._reply_if_current(Blocking(), _event("om-old"), effective, "answer")
    )
    await asyncio.wait_for(entered.wait(), timeout=2)

    async def delete() -> None:
        async with bot_api._gate(bot_id):
            pool.delete_bot(bot_id, expected_revision=effective.revision, environ={})

    queued = asyncio.Event()
    original_enter = bot_api._enter_gate_registry

    def observe_enter(bot_id):
        entry = original_enter(bot_id)
        if entry.users >= 2:
            queued.set()
        return entry

    monkeypatch.setattr(bot_api, "_enter_gate_registry", observe_enter)
    deleting = asyncio.create_task(delete())
    await asyncio.wait_for(queued.wait(), timeout=2)
    assert not deleting.done()

    release.set()
    assert await asyncio.wait_for(reply, timeout=2) is True
    await asyncio.wait_for(deleting, timeout=2)
    assert sent == ["om-old"]

    # The same snapshot must not produce a second delivery afterwards.
    assert not await bot_api._reply_if_current(
        Blocking(), _event("om-old"), effective, "late answer"
    )
    assert sent == ["om-old"]


@pytest.mark.asyncio
async def test_reply_timeout_releases_the_gate_and_never_posts_twice(pool, monkeypatch) -> None:
    """An outbound POST cannot be recalled, so a timeout is never retried."""

    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    effective = pool.effective(bot_id, environ={})
    monkeypatch.setattr(bot_api, "_OUTBOUND_POST_TIMEOUT_SECONDS", 0.01)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    attempts: list[str] = []

    class Hanging:
        async def send_text(self, *args, **kwargs) -> None:
            pytest.fail("a reply must never fall back to an unsolicited chat send")

        async def get_tenant_token(self) -> str:
            return "tenant-token"

        async def reply_text(self, message_id, text, *, access_token=None) -> None:
            attempts.append(message_id)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    reply = asyncio.create_task(
        bot_api._reply_if_current(Hanging(), _event("om-timeout"), effective, "answer")
    )
    await asyncio.wait_for(entered.wait(), timeout=2)

    async with _client() as client:
        deleting = asyncio.create_task(
            client.request(
                "DELETE",
                f"/api/feishu/bot/bots/{bot_id}",
                json={"expected_revision": effective.revision},
                cookies=_cookie(),
            )
        )
        assert await asyncio.wait_for(reply, timeout=2) is False
        response = await asyncio.wait_for(deleting, timeout=2)

    assert response.status_code == 200
    assert cancelled.is_set()
    assert attempts == ["om-timeout"]


@pytest.mark.asyncio
async def test_rotation_invalidates_an_in_flight_reply_snapshot(pool) -> None:
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    effective = pool.effective(bot_id, environ={})
    attempts: list[str] = []

    class Counting:
        async def get_tenant_token(self) -> str:
            return "tenant-token"

        async def reply_text(self, message_id, text, *, access_token=None) -> None:
            attempts.append(message_id)

    pool.rotate_secrets(
        bot_id,
        app_secret="rotated-secret",
        expected_revision=effective.revision,
        environ={},
    )

    assert not await bot_api._reply_if_current(
        Counting(), _event("om-rotated"), effective, "answer"
    )
    assert attempts == []


@pytest.mark.asyncio
async def test_a_change_during_credential_validation_loses_the_cas(pool, monkeypatch) -> None:
    """Validation deliberately runs outside the gate, so the window is real.

    Holding the gate across a call to Feishu would let one slow network round
    trip block every other operation on that Bot. The price is that the
    revision read before validation can go stale, and the CAS inside the gate
    is what has to catch it.
    """

    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    entered = asyncio.Event()
    release = asyncio.Event()

    class Blocking:
        def __init__(self, config: FeishuBotConfig) -> None:
            self.config = config

        async def validate_credentials(self) -> None:
            entered.set()
            await release.wait()

    monkeypatch.setattr(bot_api, "FeishuBotClient", Blocking)

    async with _client() as client:
        rotating = asyncio.create_task(
            client.put(
                f"/api/feishu/bot/bots/{bot_id}/secrets",
                json={
                    "app_secret": "rotated-secret",
                    "expected_revision": 0,
                },
                cookies=_cookie(),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)

        # The rotation is parked outside the gate, so this one gets through.
        renamed = await client.patch(
            f"/api/feishu/bot/bots/{bot_id}",
            json={"name": "Renamed", "expected_revision": 0},
            cookies=_cookie(),
        )
        assert renamed.status_code == 200

        release.set()
        response = await asyncio.wait_for(rotating, timeout=2)

    assert response.status_code == 409
    assert response.json()["detail"] == "bot_revision_conflict"
    assert "rotated-secret" not in pool.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_failing_dedup_finish_does_not_send_a_second_reply(pool, monkeypatch) -> None:
    """The bookkeeping write happens after delivery and cannot undo it.

    The unpaired branch is used because it replies exactly once and returns
    straight into the finally, without needing a tab or a workspace.
    """

    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    effective = pool.effective(bot_id, environ={})
    posts: list[str] = []

    class Counting:
        def __init__(self, config: FeishuBotConfig) -> None:
            self.config = config

        async def get_tenant_token(self) -> str:
            return "tenant-token"

        async def reply_text(self, message_id, text, *, access_token=None) -> None:
            posts.append(message_id)

        async def send_text(self, *args, **kwargs) -> None:
            pytest.fail("a reply must never fall back to an unsolicited chat send")

    class FailingDedup:
        def finish(self, key: str, status: str) -> None:
            raise RuntimeError("dedup bookkeeping failed")

    monkeypatch.setattr(bot_api, "FeishuBotClient", Counting)
    monkeypatch.setattr(bot_api, "_dedup", FailingDedup())

    with pytest.raises(RuntimeError):
        await bot_api._handle_message_event(bot_id, _event("om-finish"), effective)

    assert posts == ["om-finish"]


@pytest.fixture(autouse=True)
def block_real_network_and_host_auth(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("real outbound HTTP is forbidden")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(settings, "feishu_app_id", None)
    monkeypatch.setattr(settings, "feishu_app_secret", None)
    monkeypatch.setattr(settings, "auth_allowed_open_ids", "")
    monkeypatch.setattr(settings, "auth_allowed_emails", "")
