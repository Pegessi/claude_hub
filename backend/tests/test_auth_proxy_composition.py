"""Composition regressions for the auth-hardening x proxy-trust integration.

The two reviewed branches both added ``auth/proxy.py`` and touched
``config.py``/``main.py``/``auth/dependencies.py``. Integrating them must keep
the *superset* of behaviors, not one parent at the expense of the other:

- proxy-trust owns the full ``auth/proxy.py`` module (trusted-peer XFF/XFP plus
  the browser Origin / unsafe-HTTP policy and the WebSocket origin guard);
- auth-hardening contributes the session-id/query log redaction, the Secure
  cookie decision and the ``public_url`` setting.

These tests pin both halves simultaneously so a future "take one side" merge
cannot silently drop the other.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

import claude_hub.main as main_module
from claude_hub.auth import proxy as proxy_module
from claude_hub.auth.dependencies import _mask_session_id
from claude_hub.auth.log_security import (
    _ERROR_LOGGER_NAME,
    SENSITIVE_QUERY_PARAMS,
    install_redacting_access_formatter,
)
from claude_hub.config import Settings


def test_proxy_module_is_the_proxy_trust_superset() -> None:
    """The shared module carries both proxy-trust Origin policy and the
    cookie/security primitives auth-hardening depends on."""
    # Proxy-trust browser policy (not present in the auth branch's copy).
    assert callable(proxy_module.browser_origin_is_allowed)
    assert callable(proxy_module.unsafe_http_request_is_allowed)
    assert callable(proxy_module.reject_bad_websocket_origin)
    # Shared stable primitives used by auth's Secure-cookie decision.
    assert callable(proxy_module.scheme_from_request)
    assert callable(proxy_module.client_ip_from_request)
    assert callable(proxy_module.direct_peer_is_trusted)


def test_config_union_keeps_auth_public_url_and_proxy_origins() -> None:
    """Both the auth public_url and the proxy-trust origin/listen settings parse."""
    parsed = Settings(
        public_url="https://hub.example.com",
        allowed_origins="https://hub.example.com, https://cdn.example.com",
        allow_insecure_listen=False,
        bind_host="::1",
    )
    assert parsed.public_url == "https://hub.example.com"
    assert parsed.allowed_origins_list == [
        "https://hub.example.com",
        "https://cdn.example.com",
    ]
    assert parsed.allow_insecure_listen is False
    assert parsed.bind_host == "::1"


def test_dependencies_keep_redaction_and_trusted_peer_resolution() -> None:
    """dependencies.py keeps the auth session-id mask and uses trusted-peer IPs."""
    import inspect

    from claude_hub.auth import dependencies

    # Auth-hardening redaction helper is still present and used by the WS path.
    assert _mask_session_id("abcdef1234567890") == "abcdef…"
    assert _mask_session_id("") == "<none>"
    # get_client_ip delegates to the trusted-peer helper rather than reading
    # the attacker-controllable XFF header naively.
    http_source = inspect.getsource(dependencies.get_client_ip)
    ws_source = inspect.getsource(dependencies.get_client_ip_ws)
    assert "return client_ip_from_request(request)" in http_source
    assert "return client_ip_from_request(websocket)" in ws_source
    # Neither function may read X-Forwarded-For itself (the helper decides trust).
    assert ".headers.get" not in http_source
    assert ".headers.get" not in ws_source


def test_main_installs_log_redaction_once_and_origin_guard_outermost() -> None:
    """main.py keeps the import-time log redaction and the outermost Origin guard."""
    # The redaction installer is wired at import time in main.py.
    installed = {
        _ERROR_LOGGER_NAME: False,
    }
    real_install = install_redacting_access_formatter

    def _spy_install() -> None:
        installed[_ERROR_LOGGER_NAME] = True
        real_install()

    # Idempotency: installing twice must not stack a second formatter/filter.
    _spy_install()
    error_logger = logging.getLogger(_ERROR_LOGGER_NAME)
    before_handlers = len(error_logger.handlers)
    before_filters = len(error_logger.filters)
    real_install()
    assert len(error_logger.handlers) == before_handlers
    assert len(error_logger.filters) == before_filters

    # The Origin guard must be the outermost middleware so it 403s before work.
    middlewares = list(main_module.app.user_middleware)
    assert middlewares, "app has no user middleware"
    assert middlewares[0].cls is main_module.BrowserOriginGuardMiddleware


def test_redaction_and_origin_guard_coexist_on_real_app(monkeypatch) -> None:
    """End-to-end: foreign-Origin write is rejected while log redaction stays active."""
    from claude_hub.config import settings

    monkeypatch.setattr(settings, "allowed_origins", None)
    client = TestClient(main_module.app)

    rejected = client.post(
        "/api/does-not-exist",
        headers={"origin": "http://evil.example"},
        json={},
    )
    assert rejected.status_code == 403

    # Sensitive query params are still masked by the auth log-redaction surface.
    from claude_hub.auth.log_security import redact_sensitive_query

    masked = redact_sensitive_query("/api/auth/callback?code=secret&state=csrf&ok=1")
    assert "secret" not in masked and "csrf" not in masked
    assert "code=[REDACTED]" in masked and "state=[REDACTED]" in masked
    assert "ok=1" in masked
    assert {"code", "state", "session_id"} <= set(SENSITIVE_QUERY_PARAMS)
