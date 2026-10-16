"""Checks that every Feishu HTTP client survives a proxy environment HTTPX cannot parse.

The Bot API client, the WebSocket endpoint discovery and the OAuth sign-in flow
each build outbound clients. A deployment ``no_proxy`` list containing IPv6 CIDR
entries made all of them raise ``httpx.InvalidURL`` during construction, before
any request was attempted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import claude_hub.api.feishu_bot as bot_api
import claude_hub.auth.feishu as auth_feishu
import claude_hub.services.feishu_bot_websocket as websocket_runtime
from claude_hub.config import settings
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuBotConfig,
    FeishuBotConfigurationError,
    FeishuBotRemoteError,
    create_feishu_http_client,
)
from tests.test_proxy_rules import (
    PROXY_URL,
    apply_poisoned_proxy_environment,
    resolved_proxy,
)

_BASE_URL = "https://open.feishu.cn"


def test_poisoned_environment_breaks_the_previous_bot_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect this module guards: the old call shape never reached a request."""
    apply_poisoned_proxy_environment(monkeypatch)

    with pytest.raises(httpx.InvalidURL):
        httpx.AsyncClient(base_url=_BASE_URL, timeout=15.0)


def test_bot_client_is_constructible_and_proxied_under_the_poisoned_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_poisoned_proxy_environment(monkeypatch)

    client = create_feishu_http_client(_BASE_URL, timeout=15.0)

    assert str(client.base_url) == _BASE_URL
    assert resolved_proxy(client, f"{_BASE_URL}/open-apis/auth/v3") == PROXY_URL


def test_self_hosted_bot_endpoints_stay_direct_under_the_poisoned_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_poisoned_proxy_environment(monkeypatch)

    client = create_feishu_http_client("https://feishu.byted.org", timeout=15.0)

    assert resolved_proxy(client, "https://feishu.byted.org/open-apis") is None


def test_websocket_discovery_shares_the_bot_client_factory() -> None:
    assert websocket_runtime.create_feishu_http_client is create_feishu_http_client


def test_oauth_client_is_constructible_and_proxied_under_the_poisoned_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the exact expression the four OAuth call sites evaluate."""
    apply_poisoned_proxy_environment(monkeypatch)

    client = auth_feishu.create_async_client(
        auth_feishu.FEISHU_API_BASE_URL, timeout=auth_feishu._TIMEOUT_SECONDS
    )

    assert resolved_proxy(client, auth_feishu.FEISHU_TOKEN_URL) == PROXY_URL
    # The proxy fix must not change how long sign-in waits for Feishu.
    assert client.timeout == httpx.Timeout(5.0)


async def test_every_oauth_call_builds_its_client_from_the_shared_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    built: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.endswith("/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "app-token"})
        if request.url.path.endswith("/authen/v1/access_token"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"access_token": "user", "refresh_token": "refresh"}},
            )
        return httpx.Response(200, json={"code": 0, "data": {"open_id": "ou_1"}})

    def factory(api_base_url: str, *, timeout: float) -> httpx.AsyncClient:
        built.append((api_base_url, timeout))
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(settings, "feishu_app_id", "cli-oauth")
    monkeypatch.setattr(settings, "feishu_app_secret", "oauth-secret")
    monkeypatch.setattr(auth_feishu, "create_async_client", factory)

    assert await auth_feishu.get_app_access_token() == "app-token"
    assert await auth_feishu.get_user_access_token("code") == ("user", "refresh")
    assert await auth_feishu.refresh_user_access_token("refresh") == ("user", "refresh")
    assert await auth_feishu.get_user_info("user") == {"open_id": "ou_1"}

    assert built == [(auth_feishu.FEISHU_API_BASE_URL, 5.0)] * 6
    assert all(url.startswith(f"{auth_feishu.FEISHU_API_BASE_URL}/open-apis/") for url in requested)


def test_feishu_modules_never_construct_httpx_clients_directly() -> None:
    """Every outbound Feishu client must come from the shared factory."""
    package = Path(bot_api.__file__).resolve().parent.parent
    owners = (
        package / "services" / "feishu_bot.py",
        package / "services" / "feishu_bot_websocket.py",
        package / "auth" / "feishu.py",
    )

    for owner in owners:
        source = owner.read_text(encoding="utf-8")
        assert "httpx.AsyncClient(" not in source, owner.name
        assert "httpx.Client(" not in source, owner.name


def test_bot_base_url_must_be_an_absolute_http_url() -> None:
    for rejected in ("open.feishu.cn", "ftp://open.feishu.cn", ""):
        with pytest.raises(FeishuBotConfigurationError):
            create_feishu_http_client(rejected, timeout=1.0)


async def test_tenant_token_rejection_keeps_safe_upstream_diagnostics() -> None:
    secret = "must-not-appear"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 21001, "msg": "Access denied by access control policy"},
            headers={"X-Tt-Logid": "support-log-id"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=_BASE_URL,
    ) as transport:
        client = FeishuBotClient(FeishuBotConfig(app_id="cli-test", app_secret=secret), transport)
        with pytest.raises(FeishuBotRemoteError) as caught:
            await client.get_tenant_token()

    error = caught.value
    assert error.code == 21001
    assert error.remote_message == "Access denied by access control policy"
    assert error.log_id == "support-log-id"
    assert secret not in str(error)


async def test_tenant_token_http_failures_keep_their_status_contract() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=_BASE_URL,
    ) as transport:
        client = FeishuBotClient(FeishuBotConfig(app_id="cli-test", app_secret="s"), transport)
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_tenant_token()


async def test_tenant_token_rejection_truncates_an_oversized_remote_message() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": "99999", "msg": "x" * 900},
            headers={"X-Request-Id": " fallback-id "},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=_BASE_URL,
    ) as transport:
        client = FeishuBotClient(FeishuBotConfig(app_id="cli-test", app_secret="s"), transport)
        with pytest.raises(FeishuBotRemoteError) as caught:
            await client.get_tenant_token()

    assert caught.value.remote_message == "x" * 500
    assert caught.value.log_id == "fallback-id"


def test_access_control_rejection_maps_to_an_actionable_api_detail() -> None:
    error = bot_api._credentials_http_error(
        FeishuBotRemoteError(
            "the Bot tenant token request",
            code=21001,
            remote_message="Access denied by access control policy",
            log_id="support-log-id",
        )
    )

    assert error.status_code == 502
    assert error.detail == {
        "code": "feishu_access_control_denied",
        "upstream_code": "21001",
        "request_id": "support-log-id",
    }


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        FeishuBotRemoteError("op", code=10003, remote_message="bad secret", log_id=None),
    ],
)
def test_other_rejections_keep_the_generic_credentials_detail(exc: Exception) -> None:
    error = bot_api._credentials_http_error(exc)

    assert error.status_code == 502
    assert error.detail == "feishu_credentials_rejected"


def test_remote_error_never_names_a_missing_code() -> None:
    response = httpx.Response(400, json={}, request=httpx.Request("POST", _BASE_URL))
    payload: dict[str, Any] = {"code": True}

    error = FeishuBotClient._remote_error(response, payload, "the Bot tenant token request")

    assert error.code is None
    assert error.remote_message == ""
    assert error.log_id is None
    assert "unknown" in str(error)
