"""Tests for trusted-proxy header handling and WebSocket Origin policy.

Covers ``claude_hub.auth.proxy``:

- ``X-Forwarded-For`` is parsed only when the *direct* peer is trusted, and
  the client address is selected by walking the chain right-to-left.
- ``X-Forwarded-Proto`` is honored only from a trusted direct peer.
- Browser WebSocket ``Origin`` must match ``Host`` or an allowed entry;
  handshakes without ``Origin`` (ttyd/websocat/curl) keep working.
- Settings parse/validate the trusted-proxy allowlist.
"""

import ipaddress
from collections.abc import MutableMapping
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect, WebSocketState

from claude_hub.auth.dependencies import (
    get_client_ip,
    get_client_ip_ws,
    is_local_network_request,
    is_local_network_request_ws,
)
from claude_hub.auth.proxy import (
    browser_origin_is_allowed,
    client_ip_from_request,
    direct_peer_is_trusted,
    reject_bad_websocket_origin,
    scheme_from_request,
    unsafe_http_request_is_allowed,
)
from claude_hub.config import Settings, settings

LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))
LOOPBACK_AND_PRIVATE = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
)


def _headers(raw: dict[str, str]) -> list[tuple[bytes, bytes]]:
    return [(key.lower().encode(), value.encode()) for key, value in raw.items()]


def _connection(
    *,
    client_host: str | None = "203.0.113.5",
    headers: dict[str, str] | None = None,
    scheme: str = "http",
    scope_type: str = "http",
) -> Request:
    scope = {
        "type": scope_type,
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "method": "GET",
        "scheme": scheme,
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": _headers(headers or {}),
        "client": (client_host, 54321) if client_host is not None else None,
        "server": ("testserver", 80),
        "root_path": "",
    }
    return Request(scope)


def _websocket(
    *,
    client_host: str = "203.0.113.5",
    headers: dict[str, str] | None = None,
):
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/api/terminal/ws/tab",
        "raw_path": b"/api/terminal/ws/tab",
        "query_string": b"",
        "headers": _headers({"host": "hub.local:8173", **(headers or {})}),
        "client": (client_host, 54321),
        "server": ("hub.local", 8173),
        "root_path": "",
        "subprotocols": [],
    }
    return WebSocket(scope, receive, send), sent


# ── X-Forwarded-For: untrusted direct peer ──────────────────────────────────


def test_untrusted_peer_xff_is_ignored() -> None:
    request = _connection(
        client_host="203.0.113.5",
        headers={"x-forwarded-for": "127.0.0.1"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "203.0.113.5"


def test_untrusted_peer_spoofs_private_ip_xff() -> None:
    request = _connection(
        client_host="203.0.113.5",
        headers={"x-forwarded-for": "10.0.0.7, 192.168.1.1, ::1"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "203.0.113.5"


def test_untrusted_peer_without_xff_returns_direct_host() -> None:
    request = _connection(client_host="198.51.100.2")
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "198.51.100.2"


def test_missing_client_falls_back_to_loopback() -> None:
    request = _connection(client_host=None, headers={"x-forwarded-for": "203.0.113.5"})
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "127.0.0.1"


def test_default_settings_loopback_peer_is_trusted() -> None:
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert direct_peer_is_trusted(request) is True
    assert client_ip_from_request(request) == "203.0.113.9"


def test_spoofed_xff_cannot_gain_local_exemption(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original bypass: remote peer + XFF: 127.0.0.1 must not look local."""
    monkeypatch.setattr(settings, "feishu_app_id", "test-app-id")
    monkeypatch.setattr(settings, "feishu_app_secret", "test-secret")
    # Use a genuinely public address: ipaddress marks the 203.0.113.0/24
    # documentation range as is_private.
    request = _connection(
        client_host="8.8.8.8",
        headers={"x-forwarded-for": "127.0.0.1"},
    )
    assert is_local_network_request(request) is False

    ws, _ = _websocket(
        client_host="8.8.8.8",
        headers={"x-forwarded-for": "192.168.1.5"},
    )
    assert is_local_network_request_ws(ws) is False


def test_trusted_loopback_proxy_keeps_local_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Behavior preserved for the default loopback deployment: a trusted local
    # proxy reporting a LAN client keeps the existing local-network trust.
    monkeypatch.setattr(settings, "feishu_app_id", "test-app-id")
    monkeypatch.setattr(settings, "feishu_app_secret", "test-secret")
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "192.168.1.5"},
    )
    assert is_local_network_request(request) is True


# ── X-Forwarded-For: trusted proxy chain parsing ────────────────────────────


def test_trusted_proxy_single_hop() -> None:
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "203.0.113.9"


def test_trusted_proxy_multi_hop_walks_right_to_left() -> None:
    # Client -> 10.0.0.2 (edge) -> 10.0.0.1 (sidecar) -> 127.0.0.1 nginx.
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.2, 10.0.0.1"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK_AND_PRIVATE) == "203.0.113.9"


def test_intermediate_proxy_must_itself_be_trusted() -> None:
    # Only loopback is trusted: 10.0.0.2 is the first untrusted hop walking
    # from the right, so it is reported as the client, not the spoofed v4.
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.2"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "10.0.0.2"


def test_spoofed_leftmost_loopback_cannot_bypass_auth() -> None:
    # Attacker prepends a loopback address; the real untrusted hop wins.
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "127.0.0.1, 203.0.113.9"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "203.0.113.9"


def test_garbage_entries_are_ignored() -> None:
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "not-an-ip, 203.0.113.9, ,, 999.999.999.999"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "203.0.113.9"


def test_all_garbage_entries_falls_back_to_direct_peer() -> None:
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "garbage, , 256.1.1.1"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "127.0.0.1"


def test_all_trusted_hops_use_leftmost() -> None:
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "10.0.0.2, 10.0.0.3"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK_AND_PRIVATE) == "10.0.0.2"


def test_ipv6_client_and_proxy() -> None:
    v6_loopback = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    )
    request = _connection(
        client_host="::1",
        headers={"x-forwarded-for": "2001:db8::dead:beef"},
    )
    assert client_ip_from_request(request, trusted_networks=v6_loopback) == ("2001:db8::dead:beef")


def test_ipv4_mapped_ipv6_peer_is_recognized_as_loopback() -> None:
    request = _connection(
        client_host="::ffff:127.0.0.1",
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "203.0.113.9"


def test_trusted_peer_without_xff_returns_direct_peer() -> None:
    request = _connection(client_host="127.0.0.1")
    assert client_ip_from_request(request, trusted_networks=LOOPBACK) == "127.0.0.1"


def test_get_client_ip_ws_shares_trust_rule() -> None:
    # An untrusted direct peer cannot claim loopback over XFF on either stack.
    http_request = _connection(
        client_host="203.0.113.5",
        headers={"x-forwarded-for": "127.0.0.1"},
    )
    ws, _ = _websocket(
        client_host="203.0.113.5",
        headers={"x-forwarded-for": "127.0.0.1"},
    )
    assert get_client_ip(http_request) == "203.0.113.5"
    assert get_client_ip_ws(ws) == "203.0.113.5"

    trusted_http = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    trusted_ws, _ = _websocket(
        client_host="127.0.0.1",
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert get_client_ip(trusted_http) == "203.0.113.9"
    assert get_client_ip_ws(trusted_ws) == "203.0.113.9"


def test_configured_trusted_proxies_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    trusted = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("10.10.0.0/24"),
    )
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.0/8,::1,10.10.0.0/24")
    request = _connection(
        client_host="10.10.0.7",
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert client_ip_from_request(request, trusted_networks=trusted) == "203.0.113.9"
    assert client_ip_from_request(request) == "203.0.113.9"
    outside = _connection(
        client_host="10.10.1.7",
        headers={"x-forwarded-for": "127.0.0.1"},
    )
    assert client_ip_from_request(outside) == "10.10.1.7"


# ── X-Forwarded-Proto ───────────────────────────────────────────────────────


def test_xff_proto_honored_only_from_trusted_peer() -> None:
    untrusted = _connection(
        client_host="203.0.113.5",
        headers={"x-forwarded-proto": "https"},
        scheme="http",
    )
    assert scheme_from_request(untrusted, trusted_networks=LOOPBACK) == "http"

    trusted = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-proto": "https"},
        scheme="http",
    )
    assert scheme_from_request(trusted, trusted_networks=LOOPBACK) == "https"


def test_xff_proto_takes_first_token_only() -> None:
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-proto": "https, http"},
        scheme="http",
    )
    assert scheme_from_request(request, trusted_networks=LOOPBACK) == "https"


def test_xff_proto_garbage_falls_back_to_url_scheme() -> None:
    request = _connection(
        client_host="127.0.0.1",
        headers={"x-forwarded-proto": "gopher"},
        scheme="http",
    )
    assert scheme_from_request(request, trusted_networks=LOOPBACK) == "http"


def test_xff_proto_absent_uses_url_scheme() -> None:
    request = _connection(client_host="127.0.0.1", scheme="https")
    assert scheme_from_request(request, trusted_networks=LOOPBACK) == "https"


# ── Settings parsing/validation ─────────────────────────────────────────────


def test_default_trusted_proxies_are_loopback() -> None:
    parsed = Settings().trusted_proxy_networks
    assert ipaddress.ip_address("127.0.0.1") in parsed[0]
    assert any(net == ipaddress.ip_network("::1/128") for net in parsed)


def test_trusted_proxies_accepts_mixed_ips_and_cidrs() -> None:
    parsed = Settings(trusted_proxies="127.0.0.1, 10.10.0.0/24 ,::1").trusted_proxy_networks
    assert len(parsed) == 3
    assert str(parsed[1]) == "10.10.0.0/24"


def test_empty_trusted_proxies_trusts_nobody() -> None:
    assert Settings(trusted_proxies="").trusted_proxy_networks == ()


def test_invalid_trusted_proxies_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(trusted_proxies="127.0.0.0/8,not-an-ip")
    with pytest.raises(ValidationError):
        Settings(trusted_proxies="10.0.0.0/33")


def test_allowed_origins_list_parsing() -> None:
    assert Settings().allowed_origins_list == []
    parsed = Settings(
        allowed_origins="https://a.example.com, b.example.com:8443 , "
    ).allowed_origins_list
    assert parsed == ["https://a.example.com", "b.example.com:8443"]


# ── Unsafe HTTP write Origin policy ─────────────────────────────────────────


def _http_headers(method: str, raw: dict[str, str]) -> list[tuple[bytes, bytes]]:
    return _headers({"host": "hub.local:8173", **raw})


def test_safe_methods_pass_regardless_of_origin() -> None:
    for method in ("GET", "HEAD", "OPTIONS"):
        headers = _http_headers(method, {"origin": "http://evil.example"})
        assert unsafe_http_request_is_allowed(method=method, headers=headers, allowed_origins=())


def test_unsafe_method_without_origin_passes() -> None:
    # curl, the Python CLI and server-to-server webhooks send no Origin.
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        headers = _http_headers(method, {})
        assert unsafe_http_request_is_allowed(method=method, headers=headers, allowed_origins=())


def test_unsafe_method_same_origin_passes() -> None:
    headers = _http_headers("POST", {"origin": "http://hub.local:8173"})
    assert unsafe_http_request_is_allowed(method="POST", headers=headers, allowed_origins=())


def test_unsafe_method_cross_origin_denied() -> None:
    # A "simple" cross-site form POST is delivered despite CORS; it must be
    # stopped here, including the classic spoofing header pair.
    headers = _http_headers("POST", {"origin": "http://evil.example"})
    assert not unsafe_http_request_is_allowed(method="POST", headers=headers, allowed_origins=())
    headers = _http_headers(
        "DELETE",
        {"origin": "http://evil.example", "x-forwarded-for": "127.0.0.1"},
    )
    assert not unsafe_http_request_is_allowed(method="DELETE", headers=headers, allowed_origins=())


def test_unsafe_method_allowed_origin_list_passes() -> None:
    headers = _http_headers("PUT", {"origin": "https://docs.example.com"})
    allowed = ("https://docs.example.com",)
    assert unsafe_http_request_is_allowed(method="PUT", headers=headers, allowed_origins=allowed)
    assert not unsafe_http_request_is_allowed(method="PUT", headers=headers, allowed_origins=())


def test_unsafe_method_malformed_origin_denied() -> None:
    headers = _http_headers("POST", {"origin": "null"})
    assert not unsafe_http_request_is_allowed(method="POST", headers=headers, allowed_origins=())


@pytest.mark.asyncio
async def test_origin_guard_middleware_rejects_cross_site_post() -> None:
    """End-to-end ASGI check: cross-site POST 403s before the route runs."""
    from claude_hub.main import BrowserOriginGuardMiddleware

    seen: list[str] = []

    async def inner_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        seen.append(scope["method"])

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    base_scope = {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "scheme": "http",
        "path": "/api/tabs/x/restart",
        "raw_path": b"/api/tabs/x/restart",
        "query_string": b"",
        "server": ("hub.local", 8173),
        "root_path": "",
    }
    guarded = BrowserOriginGuardMiddleware(inner_app)

    cross_scope = {
        **base_scope,
        "client": ("203.0.113.9", 4000),
        "headers": _http_headers("POST", {"origin": "http://evil.example"}),
    }
    sent.clear()
    await guarded(cross_scope, receive, send)
    assert seen == []  # inner app never ran
    assert sent[0]["status"] == 403

    seen.clear()
    same_scope = {
        **base_scope,
        "client": ("127.0.0.1", 4000),
        "headers": _http_headers("POST", {"origin": "http://hub.local:8173"}),
    }
    await guarded(same_scope, receive, send)
    assert seen == ["POST"]

    seen.clear()
    native_scope = {
        **base_scope,
        "client": ("127.0.0.1", 4000),
        "headers": _http_headers("POST", {}),
    }
    await guarded(native_scope, receive, send)
    assert seen == ["POST"]


# ── WebSocket Origin policy ─────────────────────────────────────────────────


def test_origin_missing_is_allowed() -> None:
    assert browser_origin_is_allowed(origin=None, host="hub.local:8173") is True
    assert browser_origin_is_allowed(origin="  ", host="hub.local:8173") is True


def test_origin_matching_same_host_is_allowed() -> None:
    assert browser_origin_is_allowed(origin="http://hub.local:8173", host="hub.local:8173")
    assert browser_origin_is_allowed(origin="https://hub.local:8173", host="hub.local:8173")


def test_origin_mismatch_is_denied() -> None:
    assert not browser_origin_is_allowed(origin="http://evil.example:8173", host="hub.local:8173")
    assert not browser_origin_is_allowed(origin="http://hub.local:8443", host="hub.local:8173")


def test_origin_port_normalization() -> None:
    # Browsers omit default ports; both sides must normalize 80/443.
    assert browser_origin_is_allowed(origin="http://hub.local", host="hub.local")
    assert browser_origin_is_allowed(origin="https://hub.local", host="hub.local")
    assert browser_origin_is_allowed(origin="http://hub.local:80", host="hub.local")
    assert browser_origin_is_allowed(origin="https://hub.local:443", host="hub.local")
    assert browser_origin_is_allowed(origin="http://hub.local", host="hub.local:80")
    assert not browser_origin_is_allowed(origin="http://hub.local:8080", host="hub.local")
    # Hostname comparison is case-insensitive.
    assert browser_origin_is_allowed(origin="http://HUB.local:8173", host="hub.local:8173")


def test_origin_ipv6_host() -> None:
    assert browser_origin_is_allowed(origin="http://[::1]:8173", host="[::1]:8173")
    assert not browser_origin_is_allowed(origin="http://[::1]:8173", host="[::2]:8173")


def test_origin_allowed_origins_setting_full_and_bare() -> None:
    allowed = ("https://docs.example.com", "api.example.com:9000")
    assert browser_origin_is_allowed(
        origin="https://docs.example.com", host="hub.local:8173", allowed_origins=allowed
    )
    assert browser_origin_is_allowed(
        origin="https://api.example.com:9000",
        host="hub.local:8173",
        allowed_origins=allowed,
    )
    # Full-origin entries also pin the scheme.
    assert not browser_origin_is_allowed(
        origin="http://docs.example.com", host="hub.local:8173", allowed_origins=allowed
    )
    assert not browser_origin_is_allowed(
        origin="https://other.example.com", host="hub.local:8173", allowed_origins=allowed
    )


def test_malformed_origin_is_denied() -> None:
    assert not browser_origin_is_allowed(origin="not an origin", host="hub.local:8173")
    # Sandboxed browsers send the literal "null" origin.
    assert not browser_origin_is_allowed(origin="null", host="hub.local:8173")


@pytest.mark.asyncio
async def test_ws_guard_native_client_without_origin_passes() -> None:
    ws, sent = _websocket(headers={})
    rejected = await reject_bad_websocket_origin(ws)
    assert rejected is False
    assert sent == []


@pytest.mark.asyncio
async def test_ws_guard_same_origin_passes() -> None:
    ws, sent = _websocket(headers={"origin": "http://hub.local:8173"})
    rejected = await reject_bad_websocket_origin(ws)
    assert rejected is False
    assert sent == []


@pytest.mark.asyncio
async def test_ws_guard_cross_origin_closes_before_accept() -> None:
    ws, sent = _websocket(headers={"origin": "http://evil.example"})
    rejected = await reject_bad_websocket_origin(ws)
    assert rejected is True
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    assert ws.application_state == WebSocketState.DISCONNECTED


@pytest.mark.asyncio
async def test_ws_guard_honors_allowed_origins_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "allowed_origins", "https://docs.example.com")
    ws, sent = _websocket(headers={"origin": "https://docs.example.com"})
    rejected = await reject_bad_websocket_origin(ws)
    assert rejected is False
    assert sent == []


def test_ws_handshake_is_denied_via_testclient() -> None:
    """Framework-level check: pre-accept close(1008) rejects the handshake,

    while an accepted handshake keeps the tty-style subprotocol flow. No real
    socket is bound; TestClient drives the ASGI app in-process.
    """
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        if await reject_bad_websocket_origin(websocket):
            return
        await websocket.accept(subprotocol="tty")
        await websocket.send_text("ok")
        await websocket.close()

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
            pass
    assert exc_info.value.code == 1008

    # Same-origin browser handshake, subprotocol flow intact.
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as connection:
        assert connection.receive_text() == "ok"

    # Native client: no Origin header at all.
    with client.websocket_connect("/ws") as connection:
        assert connection.receive_text() == "ok"


def test_origin_guard_on_real_app_via_testclient(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full production app rejects cross-site writes before routing."""
    from claude_hub.main import app

    monkeypatch.setattr(settings, "allowed_origins", None)
    client = TestClient(app)

    # Rejected before routing: the path need not even exist.
    rejected = client.post(
        "/api/does-not-exist",
        headers={"origin": "http://evil.example"},
        json={},
    )
    assert rejected.status_code == 403

    # Same-origin POST passes the guard (a route-level status, not 403,
    # proves the request reached routing rather than the guard).
    same_origin = client.post(
        "/api/does-not-exist",
        headers={"origin": "http://testserver"},
        json={},
    )
    assert same_origin.status_code != 403

    # A cross-origin GET is a read: CORS governs it, the guard must not 403.
    cross_get = client.get("/health", headers={"origin": "http://evil.example"})
    assert cross_get.status_code == 200
