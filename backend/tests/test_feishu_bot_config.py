"""Instance Bot configuration, authorization, CAS and revocation tests."""

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
from claude_hub.services.feishu_bot import (
    FeishuBindingStore,
    FeishuBotClient,
    FeishuBotConfig,
)
from claude_hub.services.feishu_bot_config import (
    BOT_ADMIN_OPEN_IDS_ENV,
    FeishuBotConfigRevisionError,
    FeishuBotConfigStore,
    FeishuBotConfigStoreError,
)

_BOT_ENV = (
    "CLAUDE_HUB_FEISHU_BOT_APP_ID",
    "CLAUDE_HUB_FEISHU_BOT_APP_SECRET",
    "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN",
    "CLAUDE_HUB_FEISHU_BOT_ENCRYPT_KEY",
)


def _candidate(secret: str = "secret-1") -> FeishuBotConfig:
    return FeishuBotConfig(
        app_id="cli-test",
        app_secret=secret,
        verification_token="verify-token",
        encrypt_key="encrypt-key",
    )


def _cookie(open_id: str) -> dict[str, str]:
    login = session_store.create_session(
        User(open_id=open_id, name="Tester", email=f"{open_id}@example.test"),
        "access-token",
        "refresh-token",
    )
    return {settings.session_cookie_name: login.session_id}


def _payload(*, revision: int, secret: str = "secret-1") -> dict:
    return {
        "app_id": "cli-test",
        "app_secret": secret,
        "verification_token": "verify-token",
        "encrypt_key": "encrypt-key",
        "expected_revision": revision,
        "allow_app_id_change": False,
    }


@pytest.fixture
def manual_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(settings, "feishu_app_id", "cli-test")
    monkeypatch.setattr(settings, "feishu_app_secret", "oauth-secret")
    monkeypatch.setattr(settings, "auth_allowed_open_ids", None)
    monkeypatch.setattr(settings, "auth_allowed_emails", None)
    for key in _BOT_ENV:
        monkeypatch.delenv(key, raising=False)
    for key in ("CLAUDE_HUB_PUBLIC_BASE_URL", "CLAUDE_HUB_PROVIDER_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(BOT_ADMIN_OPEN_IDS_ENV, "ou-admin")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")
    config_store = FeishuBotConfigStore(tmp_path / "secrets" / "feishu_bot.json")
    binding_store = FeishuBindingStore(tmp_path / "feishu_bot_state.json")
    monkeypatch.setattr(bot_api, "_config_store", config_store)
    monkeypatch.setattr(bot_api, "_binding_store", binding_store)
    monkeypatch.setattr(bot_api, "_config_publish_lock", asyncio.Lock())
    return config_store, binding_store


class AcceptingClient:
    def __init__(self, config: FeishuBotConfig) -> None:
        self.config = config

    async def validate_credentials(self) -> None:
        return None


@pytest.mark.asyncio
async def test_store_permissions_revision_generation_and_tombstone(
    manual_config,
) -> None:
    store, _ = manual_config
    first = store.replace(_candidate(), expected_revision=0, allow_app_id_change=False)
    assert (first.revision, first.binding_generation) == (1, 1)
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    rotated = store.replace(_candidate("secret-2"), expected_revision=1, allow_app_id_change=False)
    assert (rotated.revision, rotated.binding_generation) == (2, 1)
    disabled = store.disable(expected_revision=2)
    assert (disabled.revision, disabled.binding_generation) == (3, 2)
    raw = store.path.read_text(encoding="utf-8")
    assert "secret-1" not in raw and "secret-2" not in raw
    assert json.loads(raw)["config"] is None
    enabled = store.replace(_candidate("secret-3"), expected_revision=3, allow_app_id_change=False)
    assert (enabled.revision, enabled.binding_generation) == (4, 3)
    with pytest.raises(FeishuBotConfigRevisionError):
        store.replace(_candidate("stale"), expected_revision=3, allow_app_id_change=False)


@pytest.mark.asyncio
async def test_environment_is_whole_read_only_and_corrupt_store_fails_closed(
    manual_config,
) -> None:
    store, _ = manual_config
    full = {
        "CLAUDE_HUB_FEISHU_BOT_APP_ID": "cli-test",
        "CLAUDE_HUB_FEISHU_BOT_APP_SECRET": "env-secret",
        "CLAUDE_HUB_FEISHU_BOT_VERIFICATION_TOKEN": "env-token",
    }
    state = store.state(full)
    assert state.source == "environment"
    assert state.configured and not state.editable and state.revision is None
    partial = store.state({"CLAUDE_HUB_FEISHU_BOT_APP_SECRET": "orphan-secret"})
    assert partial.source == "invalid_environment"
    assert not partial.configured and not partial.editable
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(FeishuBotConfigStoreError):
        store.state(full)


@pytest.mark.asyncio
async def test_status_permissions_and_admin_config_never_echo_secrets(
    manual_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_api, "FeishuBotClient", AcceptingClient)
    admin_cookie = _cookie("ou-admin")
    user_cookie = _cookie("ou-user")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        user_status = await client.get("/api/feishu/bot/config/status", cookies=user_cookie)
        admin_status = await client.get("/api/feishu/bot/config/status", cookies=admin_cookie)
        denied = await client.get("/api/feishu/bot/config", cookies=user_cookie)
        created = await client.put(
            "/api/feishu/bot/config", json=_payload(revision=0), cookies=admin_cookie
        )
        admin = await client.get("/api/feishu/bot/config", cookies=admin_cookie)
        local = await client.get("/api/feishu/bot/config")
    assert user_status.json()["revision"] == 0
    assert user_status.json()["can_manage"] is False
    assert user_status.json()["editable"] is False
    assert user_status.json()["event_url"] is None
    assert admin_status.json()["can_manage"] is True
    assert admin_status.json()["editable"] is True
    assert denied.status_code == 403
    assert local.status_code == 401
    assert created.status_code == 200
    assert admin.json()["app_id"] == "cli-test"
    assert admin.json()["app_secret_configured"] is True
    assert admin.json()["event_url"] is None
    combined = created.text + admin.text
    for secret in ("secret-1", "verify-token", "encrypt-key"):
        assert secret not in combined


@pytest.mark.asyncio
async def test_validation_failure_and_stale_validation_do_not_overwrite(
    manual_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = manual_config
    admin_cookie = _cookie("ou-admin")
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedClient(AcceptingClient):
        async def validate_credentials(self) -> None:
            started.set()
            await release.wait()

    monkeypatch.setattr(bot_api, "FeishuBotClient", DelayedClient)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        stale = asyncio.create_task(
            client.put(
                "/api/feishu/bot/config",
                json=_payload(revision=0, secret="candidate-secret"),
                cookies=admin_cookie,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        winner = store.replace(
            _candidate("winner-secret"), expected_revision=0, allow_app_id_change=False
        )
        release.set()
        response = await asyncio.wait_for(stale, timeout=2)
    assert winner.revision == 1
    assert response.status_code == 409
    assert response.json() == {"detail": "config_revision_conflict"}
    assert store.require().config.app_secret == "winner-secret"
    assert "candidate-secret" not in store.path.read_text(encoding="utf-8")

    class RejectingClient(AcceptingClient):
        async def validate_credentials(self) -> None:
            raise RuntimeError("upstream body containing candidate-secret")

    monkeypatch.setattr(bot_api, "FeishuBotClient", RejectingClient)
    before = store.path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rejected = await client.put(
            "/api/feishu/bot/config",
            json=_payload(revision=1, secret="rejected-secret"),
            cookies=admin_cookie,
        )
    assert rejected.status_code == 502
    assert rejected.json() == {"detail": "feishu_credentials_rejected"}
    assert "rejected-secret" not in rejected.text
    assert store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_same_app_rotation_keeps_binding_but_disable_reenable_cannot_revive_it(
    manual_config,
) -> None:
    store, bindings = manual_config
    first = store.replace(_candidate(), expected_revision=0, allow_app_id_change=False)
    code, _ = bindings.create_code(
        "ou-owner",
        "owner@example.test",
        "tab-1",
        None,
        app_id="cli-test",
        binding_generation=first.binding_generation,
    )
    binding = bindings.consume_code(
        code,
        sender_open_id="ou-owner",
        app_id="cli-test",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
        expected_app_id="cli-test",
        expected_binding_generation=first.binding_generation,
    )
    rotated = store.replace(_candidate("rotated"), expected_revision=1, allow_app_id_change=False)
    assert rotated.binding_generation == binding.binding_generation
    assert bindings.get_sender_binding("ou-owner", "cli-test", "oc-chat") == binding
    disabled = store.disable(expected_revision=2)
    assert disabled.binding_generation != binding.binding_generation
    enabled = store.replace(
        _candidate("enabled-again"), expected_revision=3, allow_app_id_change=False
    )
    assert enabled.binding_generation != binding.binding_generation
    assert not bot_api._binding_is_current(binding, store.require())


@pytest.mark.asyncio
async def test_invalid_explicit_public_url_returns_fixed_error(manual_config, monkeypatch) -> None:
    store, _ = manual_config
    store.replace(_candidate(), expected_revision=0, allow_app_id_change=False)
    monkeypatch.setenv(
        "CLAUDE_HUB_PUBLIC_BASE_URL", "https://secret.example.test/path?token=secret"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/feishu/bot/config/status", cookies=_cookie("ou-user"))
    assert response.status_code == 503
    assert response.json() == {"detail": "public_url_invalid"}
    assert "secret.example" not in response.text
    assert "token" not in response.text


@pytest.mark.asyncio
async def test_delete_waits_for_started_reply_and_blocks_later_old_snapshot(
    manual_config,
) -> None:
    store, _ = manual_config
    saved = store.replace(_candidate(), expected_revision=0, allow_app_id_change=False)
    snapshot = store.require()
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    class BlockingReplyClient:
        async def get_tenant_token(self) -> str:
            return "tenant-token"

        async def reply_text(self, message_id, text, *, access_token=None) -> None:
            assert access_token == "tenant-token"
            entered.set()
            await release.wait()
            sent.append(message_id)

    event = bot_api.FeishuMessageEvent(
        event_id="evt-1",
        message_id="om-old",
        app_id="cli-test",
        sender_open_id="ou-owner",
        chat_id="oc-chat",
        text="hello",
    )
    reply = asyncio.create_task(
        bot_api._reply_if_current(BlockingReplyClient(), event, snapshot, "answer")
    )
    await asyncio.wait_for(entered.wait(), timeout=2)

    async def disable() -> None:
        async with bot_api._config_gate():
            store.disable(expected_revision=saved.revision or 0)

    deleting = asyncio.create_task(disable())
    await asyncio.sleep(0)
    assert not deleting.done()
    release.set()
    assert await asyncio.wait_for(reply, timeout=2) is True
    await asyncio.wait_for(deleting, timeout=2)
    assert sent == ["om-old"]
    assert not await bot_api._reply_if_current(
        BlockingReplyClient(), event, snapshot, "late answer"
    )


@pytest.mark.parametrize(
    "raw_state",
    [
        json.dumps(
            {"version": 1, "revision": 0, "binding_generation": 0, "updated_at": None}
        ).encode(),
        b"\xff",
        *[
            json.dumps(
                {
                    "version": 1,
                    "revision": 0,
                    "binding_generation": 0,
                    "updated_at": None,
                    "config": None,
                    **changes,
                }
            ).encode()
            for changes in (
                {"updated_at": "yesterday"},
                {"updated_at": float("inf")},
                {"updated_at": float("nan")},
                {"updated_at": 10**400},
                {"updated_at": 1e300},
                {"updated_at": -1e300},
                {"updated_at": True},
                {"version": True},
                {"revision": True},
            )
        ],
    ],
)
@pytest.mark.asyncio
async def test_existing_invalid_store_fails_closed_without_overwrite(
    manual_config, monkeypatch, raw_state: bytes
) -> None:
    validation_calls = []

    async def forbid_validation(self):
        validation_calls.append(True)
        pytest.fail("Invalid persisted configuration must not validate credentials")

    monkeypatch.setattr(FeishuBotClient, "validate_credentials", forbid_validation)
    store, _ = manual_config
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(raw_state)
    before = store.path.read_bytes()
    admin_cookie = _cookie("ou-admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.get("/api/feishu/bot/config/status", cookies=admin_cookie),
            await client.put(
                "/api/feishu/bot/config",
                json=_payload(revision=0),
                cookies=admin_cookie,
            ),
            await client.request(
                "DELETE",
                "/api/feishu/bot/config",
                json={"expected_revision": 0},
                cookies=admin_cookie,
            ),
        ]
    assert [response.status_code for response in responses] == [503, 503, 503]
    assert all(response.json() == {"detail": "bot_config_unavailable"} for response in responses)
    assert store.path.read_bytes() == before
    assert validation_calls == []


@pytest.mark.asyncio
async def test_reply_wall_timeout_releases_gate_for_delete(manual_config, monkeypatch) -> None:
    store, _ = manual_config
    saved = store.replace(_candidate(), expected_revision=0, allow_app_id_change=False)
    snapshot = store.require()
    monkeypatch.setattr(bot_api, "_OUTBOUND_POST_TIMEOUT_SECONDS", 0.01)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    attempts: list[str] = []

    class BlockingClient:
        async def get_tenant_token(self) -> str:
            return "tenant-token"

        async def reply_text(self, message_id, text, *, access_token=None) -> None:
            attempts.append(message_id)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    event = bot_api.FeishuMessageEvent(
        event_id="evt-timeout",
        message_id="om-timeout",
        app_id="cli-test",
        sender_open_id="ou-admin",
        chat_id="oc-chat",
        text="hello",
    )
    reply_task = asyncio.create_task(
        bot_api._reply_if_current(BlockingClient(), event, snapshot, "answer")
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        delete_task = asyncio.create_task(
            client.request(
                "DELETE",
                "/api/feishu/bot/config",
                json={"expected_revision": saved.revision},
                cookies=_cookie("ou-admin"),
            )
        )
        assert await asyncio.wait_for(reply_task, timeout=2) is False
        response = await asyncio.wait_for(delete_task, timeout=2)
    assert response.status_code == 200
    assert cancelled.is_set()
    assert attempts == ["om-timeout"]
    assert store.state({}).configured is False


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_new_generation_and_old_binding_is_unusable(
    manual_config, monkeypatch
) -> None:
    store, bindings = manual_config
    code, _ = bindings.create_code(
        "ou-admin",
        "ou-admin@example.test",
        "tab-1",
        None,
        app_id="cli-test",
        binding_generation=0,
    )
    old_binding = bindings.consume_code(
        code,
        sender_open_id="ou-admin",
        app_id="cli-test",
        chat_id="oc-chat",
        owner_is_authorized=lambda *_: True,
        expected_app_id="cli-test",
        expected_binding_generation=0,
    )
    monkeypatch.setattr(bot_api, "FeishuBotClient", AcceptingClient)

    def fail_cleanup() -> bool:
        raise OSError("simulated routing cleanup failure")

    monkeypatch.setattr(bindings, "clear_routing_state", fail_cleanup)
    admin_cookie = _cookie("ou-admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/feishu/bot/config", json=_payload(revision=0), cookies=admin_cookie
        )
        assert response.status_code == 500
        assert response.json() == {"detail": "routing_cleanup_failed"}
        current = store.require()
        assert (current.revision, current.binding_generation) == (1, 1)
        assert not bot_api._binding_is_current(old_binding, current)
        assert bindings.get_owner_binding("ou-admin") == old_binding
        visible = await client.get("/api/feishu/bot/binding", cookies=admin_cookie)
    assert visible.json() == {"binding": None}
    assert bindings.get_owner_binding("ou-admin") is None


@pytest.mark.asyncio
async def test_empty_validation_token_does_not_replace_existing_config(
    manual_config, monkeypatch
) -> None:
    store, _ = manual_config
    old = store.replace(_candidate("old-secret"), expected_revision=0, allow_app_id_change=False)

    class EmptyTokenValidationClient:
        def __init__(self, config):
            self.config = config

        async def validate_credentials(self) -> None:
            transport = httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"code": 0, "tenant_access_token": "   ", "expire": 7200},
                )
            )
            async with httpx.AsyncClient(
                transport=transport, base_url="https://open.feishu.test"
            ) as http_client:
                await FeishuBotClient(self.config, client=http_client).validate_credentials()

    monkeypatch.setattr(bot_api, "FeishuBotClient", EmptyTokenValidationClient)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/feishu/bot/config",
            json=_payload(revision=old.revision or 0, secret="new-secret"),
            cookies=_cookie("ou-admin"),
        )
    assert response.status_code == 502
    assert response.json() == {"detail": "feishu_credentials_rejected"}
    current = store.require()
    assert current.revision == old.revision
    assert current.binding_generation == old.binding_generation
    assert current.config.app_secret == "old-secret"


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    """Keep failure paths offline; explicit ASGI and mock transports still work."""

    async def reject_async(*args, **kwargs):
        pytest.fail("Live HTTP is forbidden in Bot unit tests")

    def reject_sync(*args, **kwargs):
        pytest.fail("Live HTTP is forbidden in Bot unit tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_sync)
