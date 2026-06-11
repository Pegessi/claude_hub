"""Tests for the authentication API and its whitelist / local-bypass logic.

This environment carries real Feishu credentials, so ``settings.auth_enabled``
is True by default. Tests that need the disabled path null out the credentials
via ``_disable_auth``; tests that need the enabled whitelist path use
``_enable_auth`` and stub ``is_local_network_request`` so the Feishu callback
whitelist, ``/check``, ``/me``, and ``/logout`` branches can be exercised
directly. Endpoints that go through ``get_current_user`` still honor the loopback
local-bypass, which the ASGI transport triggers (client host resolves to
127.0.0.1).
"""

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.auth import session as session_store
from claude_hub.config import settings


def _disable_auth(monkeypatch: MonkeyPatch) -> None:
    """Null out Feishu credentials so settings.auth_enabled is False."""
    monkeypatch.setattr(settings, "feishu_app_id", None)
    monkeypatch.setattr(settings, "feishu_app_secret", None)


def _enable_auth(
    monkeypatch: MonkeyPatch,
    *,
    open_ids: str | None = None,
    emails: str | None = None,
    local: bool = False,
) -> None:
    """Flip settings into the auth-enabled state for a single test."""
    monkeypatch.setattr(settings, "feishu_app_id", "test-app-id")
    monkeypatch.setattr(settings, "feishu_app_secret", "test-secret")
    monkeypatch.setattr(settings, "auth_allowed_open_ids", open_ids)
    monkeypatch.setattr(settings, "auth_allowed_emails", emails)
    monkeypatch.setattr(
        "claude_hub.auth.dependencies.is_local_network_request",
        lambda request: local,
    )


def _stub_feishu(monkeypatch: MonkeyPatch, *, open_id: str, email: str) -> None:
    """Stub the Feishu token+userinfo calls used by the OAuth callback."""

    async def fake_token(code: str) -> tuple[str, str]:
        return "access-token", "refresh-token"

    async def fake_user_info(access_token: str) -> dict:
        return {"open_id": open_id, "name": "Tester", "email": email}

    monkeypatch.setattr("claude_hub.api.auth.get_user_access_token", fake_token)
    monkeypatch.setattr("claude_hub.api.auth.get_user_info", fake_user_info)


# ── auth disabled (default local-dev behavior) ──────────────────────────────


@pytest.mark.asyncio
async def test_login_redirects_to_frontend_when_auth_disabled(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    resp = await client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == settings.frontend_url


@pytest.mark.asyncio
async def test_me_returns_local_user_when_auth_disabled(client: AsyncClient) -> None:
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["open_id"] == "local"


@pytest.mark.asyncio
async def test_check_reports_not_required_for_local_request(client: AsyncClient) -> None:
    resp = await client.get("/api/auth/check")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False, "auth_required": False, "user": None}


# ── OAuth callback whitelist ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_callback_redirects_to_frontend_when_auth_disabled(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    resp = await client.get("/api/auth/callback", params={"code": "x"}, follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == settings.frontend_url


@pytest.mark.asyncio
async def test_callback_grants_when_open_id_in_whitelist(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch, open_ids="ou_allowed,ou_other")
    _stub_feishu(monkeypatch, open_id="ou_allowed", email="a@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    resp = await client.get("/api/auth/callback", params={"code": "x"}, follow_redirects=False)

    assert resp.status_code == 307
    assert settings.session_cookie_name in resp.cookies


@pytest.mark.asyncio
async def test_callback_denies_when_open_id_not_in_whitelist(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch, open_ids="ou_allowed")
    _stub_feishu(monkeypatch, open_id="ou_intruder", email="a@example.com")

    resp = await client.get("/api/auth/callback", params={"code": "x"}, follow_redirects=False)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_callback_grants_via_email_whitelist_case_insensitive(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch, emails="Allowed@Example.com")
    _stub_feishu(monkeypatch, open_id="ou_x", email="allowed@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    resp = await client.get("/api/auth/callback", params={"code": "x"}, follow_redirects=False)

    assert resp.status_code == 307
    assert settings.session_cookie_name in resp.cookies


@pytest.mark.asyncio
async def test_callback_denies_when_email_not_in_whitelist(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch, emails="allowed@example.com")
    _stub_feishu(monkeypatch, open_id="ou_x", email="stranger@example.com")

    resp = await client.get("/api/auth/callback", params={"code": "x"}, follow_redirects=False)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_callback_grants_when_no_whitelist_configured(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch)  # no whitelist => allow any authenticated user
    _stub_feishu(monkeypatch, open_id="ou_anyone", email="anyone@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    resp = await client.get("/api/auth/callback", params={"code": "x"}, follow_redirects=False)

    assert resp.status_code == 307
    assert settings.session_cookie_name in resp.cookies


# ── authenticated session round-trip via /check and /me ─────────────────────


@pytest.mark.asyncio
async def test_check_authenticated_with_valid_session_cookie(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch)
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")
    from claude_hub.models.schemas import User

    user = User(open_id="ou_session", name="Session User", email="s@example.com")
    created = session_store.create_session(user, "at", "rt")

    resp = await client.get(
        "/api/auth/check",
        cookies={settings.session_cookie_name: created.session_id},
    )

    body = resp.json()
    assert resp.status_code == 200
    assert body["authenticated"] is True
    assert body["auth_required"] is True
    assert body["user"]["open_id"] == "ou_session"


@pytest.mark.asyncio
async def test_check_unauthenticated_when_no_cookie_and_auth_required(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    resp = await client.get("/api/auth/check")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False, "auth_required": True, "user": None}


@pytest.mark.asyncio
async def test_me_requires_auth_when_no_session(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


# ── logout ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_logout_clears_session_and_cookie(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch)
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")
    from claude_hub.models.schemas import User

    user = User(open_id="ou_session", name="Session User", email="s@example.com")
    created = session_store.create_session(user, "at", "rt")

    resp = await client.post(
        "/api/auth/logout",
        cookies={settings.session_cookie_name: created.session_id},
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    # Session was removed from the store.
    assert session_store.get_session(created.session_id) is None
