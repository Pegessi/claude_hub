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

import logging
import secrets
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.api import auth as auth_api
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
    monkeypatch.setattr(settings, "feishu_redirect_uri", "http://test/api/auth/callback")
    monkeypatch.setattr(settings, "public_url", None)
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


async def _begin_login(client: AsyncClient) -> str:
    """Perform /login and return the state echoed in the Feishu redirect."""
    resp = await client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 307
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    return query["state"][0]


def _set_cookie_headers(resp, name: str) -> list[str]:
    return [value for value in resp.headers.get_list("set-cookie") if value.startswith(f"{name}=")]


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
    # No /login handshake and no state cookie: the flow must still work with
    # auth disabled.
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

    state = await _begin_login(client)
    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 307
    assert settings.session_cookie_name in resp.cookies


@pytest.mark.asyncio
async def test_callback_denies_when_open_id_not_in_whitelist(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch, open_ids="ou_allowed")
    _stub_feishu(monkeypatch, open_id="ou_intruder", email="a@example.com")

    state = await _begin_login(client)
    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_callback_grants_via_email_whitelist_case_insensitive(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch, emails="Allowed@Example.com")
    _stub_feishu(monkeypatch, open_id="ou_x", email="allowed@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    state = await _begin_login(client)
    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 307
    assert settings.session_cookie_name in resp.cookies


@pytest.mark.asyncio
async def test_callback_denies_when_email_not_in_whitelist(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch, emails="allowed@example.com")
    _stub_feishu(monkeypatch, open_id="ou_x", email="stranger@example.com")

    state = await _begin_login(client)
    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_callback_grants_when_no_whitelist_configured(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch)  # no whitelist => allow any authenticated user
    _stub_feishu(monkeypatch, open_id="ou_anyone", email="anyone@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    state = await _begin_login(client)
    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 307
    assert settings.session_cookie_name in resp.cookies


# ── OAuth login state (login CSRF) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_issues_random_state_in_url_and_cookie(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)

    resp = await client.get("/api/auth/login", follow_redirects=False)

    assert resp.status_code == 307
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    state = query["state"][0]
    assert len(state) >= 32

    cookie_header = _set_cookie_headers(resp, auth_api.OAUTH_STATE_COOKIE_NAME)
    assert len(cookie_header) == 1
    lowered = cookie_header[0].lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert f"path={auth_api.OAUTH_COOKIE_PATH}" in lowered
    assert "max-age=600" in lowered
    # Plain-HTTP test client: no Secure attribute on the state cookie.
    assert "secure" not in lowered


@pytest.mark.asyncio
async def test_login_states_differ_between_requests(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)

    first = await _begin_login(client)
    second = await _begin_login(client)

    assert first != second


@pytest.mark.asyncio
async def test_callback_rejects_without_state_cookie(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    _stub_feishu(monkeypatch, open_id="ou_x", email="a@example.com")

    # No /login handshake: the client carries no state cookie.
    resp = await client.get(
        "/api/auth/callback",
        params={"code": "x", "state": "forged"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_callback_rejects_mismatched_state(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    _stub_feishu(monkeypatch, open_id="ou_x", email="a@example.com")

    await _begin_login(client)  # sets the state cookie in the client jar
    resp = await client.get(
        "/api/auth/callback",
        params={"code": "x", "state": "tampered-state-value"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    # The token must never be exchanged when the state check fails.
    # (fake_token would return successfully, so the 400 proves early rejection.)


@pytest.mark.asyncio
async def test_callback_rejects_missing_state_parameter(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    _enable_auth(monkeypatch)
    _stub_feishu(monkeypatch, open_id="ou_x", email="a@example.com")

    await _begin_login(client)
    resp = await client.get("/api/auth/callback", params={"code": "x"}, follow_redirects=False)

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_successful_callback_clears_state_cookie(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch)
    _stub_feishu(monkeypatch, open_id="ou_ok", email="ok@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    state = await _begin_login(client)
    assert client.cookies.get(auth_api.OAUTH_STATE_COOKIE_NAME) == state

    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 307
    # The response expires the single-use state cookie...
    cleared = _set_cookie_headers(resp, auth_api.OAUTH_STATE_COOKIE_NAME)
    assert len(cleared) == 1
    assert "max-age=0" in cleared[0].lower()
    # ...and the client jar drops it.
    assert client.cookies.get(auth_api.OAUTH_STATE_COOKIE_NAME) is None


# ── Secure cookie attribute ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_cookie_not_secure_over_plain_http(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch)
    _stub_feishu(monkeypatch, open_id="ou_anyone", email="anyone@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    state = await _begin_login(client)
    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 307
    session_cookie = _set_cookie_headers(resp, settings.session_cookie_name)[0].lower()
    assert "secure" not in session_cookie
    assert "samesite=lax" in session_cookie
    assert "httponly" in session_cookie


@pytest.mark.asyncio
async def test_session_cookie_secure_over_https_transport(
    monkeypatch: MonkeyPatch, tmp_path
) -> None:
    # End-to-end over an https-scoped ASGI transport: the Secure state cookie
    # must round-trip (the cookie jar refuses Secure cookies over http), and
    # the issued session cookie must carry Secure.
    from httpx import ASGITransport, AsyncClient

    from claude_hub.main import app

    _enable_auth(monkeypatch)
    _stub_feishu(monkeypatch, open_id="ou_anyone", email="anyone@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        login_resp = await client.get("/api/auth/login", follow_redirects=False)
        state_cookie = _set_cookie_headers(login_resp, auth_api.OAUTH_STATE_COOKIE_NAME)[0].lower()
        assert "secure" in state_cookie

        state = parse_qs(urlsplit(login_resp.headers["location"]).query)["state"][0]
        resp = await client.get(
            "/api/auth/callback",
            params={"code": "x", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code == 307
    session_cookie = _set_cookie_headers(resp, settings.session_cookie_name)[0].lower()
    assert "secure" in session_cookie


def _fake_request(*, scheme: str = "http", headers: dict[str, str] | None = None):
    from starlette.requests import Request

    raw_headers = [
        (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": scheme,
        "server": ("test", 80),
        "path": "/api/auth/login",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_secure_cookie_detection(monkeypatch: MonkeyPatch) -> None:
    _enable_auth(monkeypatch)

    assert auth_api._use_secure_cookie(_fake_request()) is False
    assert auth_api._use_secure_cookie(_fake_request(scheme="https")) is True
    assert (
        auth_api._use_secure_cookie(_fake_request(headers={"X-Forwarded-Proto": "https"})) is True
    )
    # The first hop in the chain determines the client-facing scheme.
    assert (
        auth_api._use_secure_cookie(_fake_request(headers={"X-Forwarded-Proto": "http, https"}))
        is False
    )
    assert (
        auth_api._use_secure_cookie(_fake_request(headers={"X-Forwarded-Proto": "https, http"}))
        is True
    )

    # A configured HTTPS public URL implies Secure even if the immediate
    # request looks like HTTP (TLS terminated upstream without a proto header).
    monkeypatch.setattr(settings, "public_url", "https://hub.example.com")
    assert auth_api._use_secure_cookie(_fake_request()) is True

    monkeypatch.setattr(settings, "public_url", "http://localhost:8173")
    assert auth_api._use_secure_cookie(_fake_request()) is False


@pytest.mark.asyncio
async def test_session_cookie_not_secure_for_localhost_http_url(
    client: AsyncClient, monkeypatch: MonkeyPatch, tmp_path
) -> None:
    _enable_auth(monkeypatch)
    monkeypatch.setattr(settings, "public_url", "http://localhost:8173")
    _stub_feishu(monkeypatch, open_id="ou_anyone", email="anyone@example.com")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", tmp_path / "sessions.json")

    state = await _begin_login(client)
    resp = await client.get(
        "/api/auth/callback", params={"code": "x", "state": state}, follow_redirects=False
    )

    assert resp.status_code == 307
    session_cookie = _set_cookie_headers(resp, settings.session_cookie_name)[0].lower()
    assert "secure" not in session_cookie


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
    client.cookies.set(settings.session_cookie_name, created.session_id)

    resp = await client.get("/api/auth/check")

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
    client.cookies.set(settings.session_cookie_name, created.session_id)

    resp = await client.post("/api/auth/logout")

    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    # Session was removed from the store.
    assert session_store.get_session(created.session_id) is None


# ── session id logging is masked ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_websocket_auth_logs_only_masked_session_id(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Full bearer session ids must never reach the logs."""
    from claude_hub.auth import dependencies

    _enable_auth(monkeypatch)
    monkeypatch.setattr(
        "claude_hub.auth.dependencies.is_local_network_request_ws",
        lambda websocket: False,
    )
    monkeypatch.setattr(dependencies, "get_session", lambda session_id: None)

    full_session_id = secrets.token_urlsafe(32)
    fake_websocket: Any = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.7"))

    with caplog.at_level(logging.INFO, logger="claude_hub.auth.dependencies"):
        result = await dependencies.get_current_user_ws(fake_websocket, full_session_id)

    assert result is None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert full_session_id not in logged
    # The 6-char prefix is present, proving the attempt was logged.
    assert full_session_id[:6] in logged
    assert "…" in logged


@pytest.mark.asyncio
async def test_websocket_auth_logs_sentinel_when_no_session_id(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from claude_hub.auth import dependencies

    _enable_auth(monkeypatch)
    monkeypatch.setattr(
        "claude_hub.auth.dependencies.is_local_network_request_ws",
        lambda websocket: False,
    )

    fake_websocket: Any = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.7"))

    with caplog.at_level(logging.INFO, logger="claude_hub.auth.dependencies"):
        result = await dependencies.get_current_user_ws(fake_websocket, None)

    assert result is None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "<none>" in logged
