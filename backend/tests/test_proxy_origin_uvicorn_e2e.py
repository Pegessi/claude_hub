"""Real-uvicorn proxy/Origin compatibility for the auth x proxy-trust merge.

The pure helper tests in ``test_proxy_trust.py`` construct ASGI scopes by hand.
These tests instead boot a **real uvicorn server** on a task-owned loopback
port (lifespan disabled, so no workspace/ttyd/tmux side effects) and drive it
over an actual TCP socket. The production :class:`BrowserOriginGuardMiddleware`
and the production trusted-peer helpers therefore run against a scope that
uvicorn itself populated -- including the real direct-peer address and the
rewritten ``Host`` a relay produces.

Covered compatibility contract:

- same-origin HTTP works on both IPv4 and IPv6 authorities;
- a foreign browser ``Origin`` on an unsafe method is rejected (403) before
  routing, while a cross-origin GET (safe method) passes;
- Origin-less native clients (the CLI and server-to-server Feishu webhooks)
  work;
- an external ``https`` Origin presented together with the rewritten internal
  loopback Host (the Merlin relay shape) is accepted only when allowlisted;
- ``X-Forwarded-Proto``/``X-Forwarded-For`` from the real loopback peer
  (trusted by default) drive the Secure-cookie scheme decision and client IP;
- the terminal WebSocket handshake accepts no-Origin/same-origin and rejects a
  foreign Origin before accept.

The untrusted-direct-peer negative case (a remote peer forging XFP) cannot be
produced over a loopback socket; it stays covered by the constructed-scope
cases in ``test_proxy_trust.py`` / ``test_auth.py``.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from typing import Iterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket

from claude_hub.auth.proxy import (
    client_ip_from_request,
    reject_bad_websocket_origin,
    scheme_from_request,
)
from claude_hub.config import settings
from claude_hub.main import BrowserOriginGuardMiddleware


def _build_app() -> FastAPI:
    """Minimal app carrying the production middleware and guarded routes."""
    app = FastAPI()
    # The single middleware is therefore the outermost layer, exactly as it is
    # when registered last on the production app.
    app.add_middleware(BrowserOriginGuardMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, str]:
        # Returns the values the production trust helpers derive from uvicorn's
        # real connection scope (peer address, scheme and rewritten Host).
        return {
            "client_ip": client_ip_from_request(request),
            "scheme": scheme_from_request(request),
            "host": request.headers.get("host", ""),
        }

    @app.post("/server-webhook")
    async def server_webhook() -> dict[str, str]:
        # Stand-in for an Origin-less server-to-server Feishu callback.
        return {"delivered": "ok"}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if await reject_bad_websocket_origin(websocket):
            return
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    return app


@contextmanager
def _serve(host: str) -> Iterator[str]:
    """Run the mini app under real uvicorn on a free loopback port; yield base URL."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.bind((host, 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            _build_app(),
            host=host,
            port=port,
            loop="asyncio",
            lifespan="off",
            access_log=False,
            log_config=None,
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = threading.Event()
        for _ in range(500):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("owned uvicorn server failed to start")
            deadline.wait(0.01)
        if not server.started:
            raise RuntimeError("owned uvicorn server timed out starting")
        host_part = f"[{host}]" if ":" in host else host
        yield f"http://{host_part}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "owned uvicorn server did not stop"
        listener.close()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_real_uvicorn_origin_policy_over_http(host: str) -> None:
    with _serve(host) as base_url:
        origin = f"http://{'[' + host + ']' if ':' in host else host}:{base_url.rsplit(':', 1)[1]}"
        with httpx.Client(base_url=base_url, trust_env=False, timeout=5) as client:
            # Cross-origin GET is a read: the unsafe-write guard must not block.
            assert (
                client.get("/health", headers={"origin": "http://evil.example"}).status_code == 200
            )

            # Same-origin unsafe write reaches the route (not 403).
            same = client.post("/echo", headers={"origin": origin})
            assert same.status_code == 200
            assert same.json()["host"].startswith("[" if ":" in host else host)

            # Foreign Origin on a write is rejected by the real middleware.
            foreign = client.post("/echo", headers={"origin": "http://evil.example"})
            assert foreign.status_code == 403

            # Origin-less CLI/server request reaches the route.
            no_origin = client.post("/server-webhook")
            assert no_origin.status_code == 200
            assert no_origin.json() == {"delivered": "ok"}


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_real_uvicorn_trusted_loopback_forwarded_headers(host: str) -> None:
    with _serve(host) as base_url:
        with httpx.Client(base_url=base_url, trust_env=False, timeout=5) as client:
            # The real direct peer is loopback, trusted by default: XFP flips
            # the effective scheme used by the Secure-cookie decision.
            https = client.post("/echo", headers={"x-forwarded-proto": "https"})
            assert https.status_code == 200
            assert https.json()["scheme"] == "https"

            # Garbage XFP cannot force HTTPS; uvicorn's real scheme is http.
            garbage = client.post("/echo", headers={"x-forwarded-proto": "gopher"})
            assert garbage.json()["scheme"] == "http"

            # A trusted-loopback relay's XFF supplies the client chain.
            xff = client.post("/echo", headers={"x-forwarded-for": "203.0.113.9"})
            assert xff.json()["client_ip"] == "203.0.113.9"

            # No forwarded headers: the real loopback socket peer is reported.
            direct = client.post("/echo")
            assert direct.json()["client_ip"] in {"127.0.0.1", "::1"}
            assert direct.json()["scheme"] == "http"


def test_real_uvicorn_external_https_origin_with_rewritten_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The browser presents the public https origin while the relay rewrites
    # Host to the internal loopback backend.
    external_origin = "https://hub.example.com"
    with _serve("127.0.0.1") as base_url:
        with httpx.Client(base_url=base_url, trust_env=False, timeout=5) as client:
            # Not allowlisted: rejected even though the TCP peer is loopback.
            denied = client.post(
                "/server-webhook",
                headers={"origin": external_origin, "host": "127.0.0.1:9999"},
            )
            assert denied.status_code == 403

            # Allowlist the exact external HTTPS origin (controller-managed).
            monkeypatch.setattr(settings, "allowed_origins", external_origin)
            allowed = client.post(
                "/server-webhook",
                headers={"origin": external_origin, "host": "127.0.0.1:9999"},
            )
            assert allowed.status_code == 200


def test_real_uvicorn_websocket_origin_handshake() -> None:
    import websockets.sync.client as ws_client
    from websockets.exceptions import InvalidStatus
    from websockets.typing import Origin

    with _serve("127.0.0.1") as base_url:
        port = base_url.rsplit(":", 1)[1]
        ws_url = f"ws://127.0.0.1:{port}/ws"

        # Native client without an Origin header connects (ttyd/websocat/CLI).
        with ws_client.connect(ws_url, open_timeout=5) as ws:
            assert ws.recv(timeout=5) == "ok"

        # Same-origin browser tab connects.
        same_origin = Origin(f"http://127.0.0.1:{port}")
        with ws_client.connect(ws_url, origin=same_origin, open_timeout=5) as ws:
            assert ws.recv(timeout=5) == "ok"

        # A foreign Origin is refused during the real uvicorn WS handshake.
        with pytest.raises(InvalidStatus) as exc:
            ws_client.connect(ws_url, origin=Origin("http://evil.example"), open_timeout=5)
        assert exc.value.response.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("header", ["origin", "host"])
@pytest.mark.parametrize("invalid_port", ["not-a-port", "65536", "-1"])
def test_real_uvicorn_invalid_header_ports_return_403(
    host: str, header: str, invalid_port: str
) -> None:
    with _serve(host) as base_url:
        authority = f"[{host}]" if ":" in host else host
        headers = {"origin": base_url, "host": base_url.removeprefix("http://")}
        headers[header] = (
            f"http://{authority}:{invalid_port}"
            if header == "origin"
            else f"{authority}:{invalid_port}"
        )
        with httpx.Client(base_url=base_url, trust_env=False, timeout=5) as client:
            assert client.post("/server-webhook", headers=headers).status_code == 403
            # Send a real upgrade with the same invalid Host/Origin. A URL-client
            # constructor would otherwise reject the malformed port before I/O.
            upgrade = {
                **headers,
                "connection": "Upgrade",
                "upgrade": "websocket",
                "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
                "sec-websocket-version": "13",
            }
            assert client.get("/ws", headers=upgrade).status_code == 403
