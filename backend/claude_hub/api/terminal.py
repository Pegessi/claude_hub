import logging
import re
import socket
from collections.abc import Sequence
from socket import IPPROTO_TCP, TCP_NODELAY
from typing import Optional
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel
from websockets.typing import Subprotocol

from ..auth.dependencies import get_current_user, get_current_user_ws
from ..auth.proxy import reject_bad_websocket_origin
from ..models import AgentType, User
from ..services.agent_stream.providers.spec import all_specs
from ..services.ttyd_manager import ttyd_manager

# The four local CLI providers emit terminal probe responses the proxy must
# filter; MASO has no TUI and the terminal pseudo-provider emits no probes.
_PROBE_RESPONSE_FILTER_PROVIDERS = frozenset(
    agent_type for agent_type, spec in all_specs().items() if spec.binary is not None
)
from ._terminal_injection import (
    FULL_HISTORY_LINES,
    INITIAL_AGENT_HISTORY_LINES,
    INITIAL_AGENT_REPLAY_MIN_LINES,
    INITIAL_TERMINAL_HISTORY_LINES,
    inject_terminal_html,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/terminal", tags=["terminal"])


def _set_tcp_nodelay(sock: Optional[socket.socket]) -> None:
    """Enable TCP_NODELAY on a socket when possible.

    Terminal traffic is dominated by tiny frames (1–5 bytes per keystroke).
    Leaving Nagle's algorithm enabled (the kernel default for TCP sockets)
    introduces a 40–200 ms write-delay while the kernel waits for more data
    to coalesce.  For the interactive terminal path that delay is
    user-visible and defeats the entire purpose of the browser-side SAB
    + Atomics fast path.

    This helper is defensive: it silently no-ops on ``None``, non-TCP
    sockets (e.g. AF_UNIX), and platforms where ``TCP_NODELAY`` is not
    available.
    """
    if sock is None:
        return
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return
    try:
        sock.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)
    except (OSError, AttributeError):
        # Not all transports expose a real socket; ignore silently.
        pass


def _create_nodelay_socket_for_uri(server_uri: str) -> Optional[socket.socket]:
    """Create a TCP socket pre-configured with TCP_NODELAY for ``server_uri``.

    ``websockets.connect`` (v13+) forwards extra ``**kwargs`` to
    ``asyncio.loop.create_connection``, which accepts a ``sock`` parameter
    carrying a pre-connected socket.  We open the socket synchronously
    (localhost, so connection latency is effectively zero) so we can set
    TCP_NODELAY before any bytes are written.

    Returns ``None`` if the URI cannot be parsed into a TCP host/port or
    the socket cannot be configured — in that case the caller falls back
    to the default asyncio socket creation path.
    """
    try:
        parsed = urlparse(server_uri)
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            return None
        sock = socket.create_connection((host, port), timeout=5)
    except (OSError, ValueError):
        return None
    _set_tcp_nodelay(sock)
    return sock


def _build_nodelay_httpx_transport() -> httpx.AsyncHTTPTransport:
    """Build an AsyncHTTPTransport with TCP_NODELAY on every outbound socket.

    The HTTP proxy path serves the ttyd HTML bundle plus injected scripts;
    while those are larger responses, the handshake and early frames still
    benefit from disabled Nagle buffering, and more importantly this keeps
    our socket posture consistent across both proxy transports.

    We prefer the typed ``socket_options`` kwarg when available (httpx
    >=0.27) and fall back to a subclass hook for older versions so the
    build does not break on pinned versions.
    """
    socket_options: list[tuple[int, int, int]] = [
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
    ]
    try:
        return httpx.AsyncHTTPTransport(socket_options=socket_options)
    except TypeError:
        # Older httpx does not accept socket_options; fall back to a
        # subclass override that patches the socket after open.
        base_transport = httpx.AsyncHTTPTransport()

        class _NodelayTransport(type(base_transport)):  # type: ignore[misc,valid-type]
            async def _open_socket(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
                sock = await super()._open_socket(*args, **kwargs)
                _set_tcp_nodelay(sock)  # type: ignore[arg-type]
                return sock

        return _NodelayTransport(verify=getattr(base_transport, "_verify", True))


# HTTP client for proxying (no proxy for localhost connections)
transport = _build_nodelay_httpx_transport()
client = httpx.AsyncClient(
    timeout=None,
    verify=False,
    proxy=None,
    transport=transport,
    trust_env=False,
)


class TerminalHistoryResponse(BaseModel):
    tab_id: str
    lines: int
    history: str
    cursor_x: Optional[int] = None
    cursor_y: Optional[int] = None


_TERMINAL_PROBE_RESPONSE_RE = re.compile(
    r"^(?:\x1b\[\>[0-9]+(?:;[0-9]+){1,3}c|\x1b\[\?[0-9;]*c|\x1b\[[0-9;]+R)$"
)


def _ttyd_input_payload(message: str | bytes) -> str:
    if isinstance(message, bytes):
        try:
            text = message.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    else:
        text = message

    # ttyd client input frames are prefixed with "0"; direct terminal websocket
    # clients may send the payload without that protocol byte.
    if text.startswith("0"):
        return text[1:]
    return text


def is_generated_terminal_probe_response(message: str | bytes) -> bool:
    """Return True for xterm.js replies that should not become agent prompt input."""
    payload = _ttyd_input_payload(message)
    return bool(_TERMINAL_PROBE_RESPONSE_RE.fullmatch(payload))


async def proxy_websocket(
    client_ws: WebSocket,
    server_uri: str,
    subprotocols: Optional[Sequence[Subprotocol]] = None,
    *,
    filter_terminal_probe_responses: bool = False,
) -> None:
    """Proxy WebSocket messages between client and ttyd."""
    try:
        # Pass a pre-connected, TCP_NODELAY-configured socket when possible
        # to disable Nagle's algorithm before the first byte is sent. The
        # `sock` kwarg is forwarded by websockets.connect (v13+) to
        # asyncio.loop.create_connection, which accepts a pre-connected
        # socket. The pre-connect is cheap (localhost).
        nodelay_sock = _create_nodelay_socket_for_uri(server_uri)
        if nodelay_sock is not None:
            server_ws_ctx = websockets.connect(
                server_uri,
                subprotocols=subprotocols,
                proxy=None,
                sock=nodelay_sock,  # type: ignore[arg-type]
            )
        else:
            server_ws_ctx = websockets.connect(
                server_uri,
                subprotocols=subprotocols,
                proxy=None,
            )
        async with server_ws_ctx as server_ws:
            # Forward messages from client to server
            async def client_to_server() -> None:
                try:
                    while True:
                        data = await client_ws.receive()
                        if data["type"] == "websocket.receive":
                            if "text" in data:
                                if (
                                    filter_terminal_probe_responses
                                    and is_generated_terminal_probe_response(data["text"])
                                ):
                                    logger.debug(
                                        "Dropped generated terminal probe response for agent tab"
                                    )
                                    continue
                                await server_ws.send(data["text"])
                            elif "bytes" in data:
                                if (
                                    filter_terminal_probe_responses
                                    and is_generated_terminal_probe_response(data["bytes"])
                                ):
                                    logger.debug(
                                        "Dropped generated terminal probe response for agent tab"
                                    )
                                    continue
                                await server_ws.send(data["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug(f"Client to server error: {e}")

            # Forward messages from server to client
            async def server_to_client() -> None:
                try:
                    async for message in server_ws:
                        if isinstance(message, str):
                            await client_ws.send_text(message)
                        else:
                            await client_ws.send_bytes(message)
                except Exception as e:
                    logger.debug(f"Server to client error: {e}")

            # Run both forwarding tasks
            import asyncio

            client_task = asyncio.create_task(client_to_server())
            server_task = asyncio.create_task(server_to_client())

            done, pending = await asyncio.wait(
                [client_task, server_task], return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()

    except Exception as e:
        logger.error(f"Proxy error: {e}")


@router.websocket("/ws/{tab_id}")
async def websocket_endpoint(
    websocket: WebSocket, tab_id: str, session_id: Optional[str] = Query(None)
) -> None:
    """WebSocket endpoint for terminal connections (proxies to ttyd)."""
    # Reject cross-site browser handshakes before accepting (CSWSH); native
    # clients without an Origin header are unaffected.
    if await reject_bad_websocket_origin(websocket):
        return

    # Authenticate user
    user = await get_current_user_ws(websocket, session_id)
    if not user:
        await websocket.close(code=4001)
        return

    tab = await ttyd_manager.ensure_tab_running(tab_id)
    if not tab:
        await websocket.close(code=4004)
        return

    await websocket.accept(subprotocol="tty")

    ttyd_ws_uri = f"ws://127.0.0.1:{tab.port}/ws"
    filter_probe_responses = tab.agent_type in _PROBE_RESPONSE_FILTER_PROVIDERS

    try:
        await proxy_websocket(
            websocket,
            ttyd_ws_uri,
            subprotocols=[Subprotocol("tty")],
            filter_terminal_probe_responses=filter_probe_responses,
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket terminal error for tab {tab_id}: {e}")


@router.websocket("/proxy/{tab_id}/ws")
async def proxy_ttyd_websocket(
    websocket: WebSocket, tab_id: str, session_id: Optional[str] = Query(None)
) -> None:
    """Proxy WebSocket connections directly to ttyd's /ws endpoint."""
    # Reject cross-site browser handshakes before accepting (CSWSH); native
    # clients without an Origin header are unaffected.
    if await reject_bad_websocket_origin(websocket):
        return

    # Authenticate user
    user = await get_current_user_ws(websocket, session_id)
    if not user:
        await websocket.close(code=4001)
        return

    tab = await ttyd_manager.ensure_tab_running(tab_id)
    if not tab:
        await websocket.close(code=4004)
        return

    await websocket.accept(subprotocol="tty")

    ttyd_ws_uri = f"ws://127.0.0.1:{tab.port}/ws"
    filter_probe_responses = tab.agent_type in _PROBE_RESPONSE_FILTER_PROVIDERS

    try:
        await proxy_websocket(
            websocket,
            ttyd_ws_uri,
            subprotocols=[Subprotocol("tty")],
            filter_terminal_probe_responses=filter_probe_responses,
        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket proxy error for tab {tab_id}: {e}")


@router.get("/history/{tab_id}", response_model=TerminalHistoryResponse)
async def get_terminal_history(
    tab_id: str,
    lines: int = Query(FULL_HISTORY_LINES, ge=100, le=FULL_HISTORY_LINES),
    current_user: User = Depends(get_current_user),
) -> TerminalHistoryResponse:
    """Get captured tmux history for replaying terminal scrollback."""
    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")

    try:
        history = await ttyd_manager.get_tab_history(tab_id, lines)
        cursor = await ttyd_manager.get_tab_cursor_position(tab_id)
    except Exception as e:
        logger.error(f"Failed to capture history for tab {tab_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to capture terminal history") from e

    if history is None:
        raise HTTPException(status_code=404, detail="Tab not found")

    return TerminalHistoryResponse(
        tab_id=tab_id,
        lines=lines,
        history=history,
        cursor_x=cursor["cursor_x"] if cursor else None,
        cursor_y=cursor["cursor_y"] if cursor else None,
    )


@router.get("/proxy/{tab_id}")
async def get_terminal_proxy_root(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Redirect to the proxied ttyd page (with trailing slash for correct relative URL resolution)."""
    tab = await ttyd_manager.ensure_tab_running(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return RedirectResponse(url=f"/api/terminal/proxy/{tab_id}/")


@router.api_route(
    "/proxy/{tab_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_terminal_request(
    tab_id: str,
    path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Proxy HTTP requests to ttyd."""
    tab = await ttyd_manager.ensure_tab_running(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")

    # Build the ttyd URL
    ttyd_url = f"http://127.0.0.1:{tab.port}/{path}"

    # Get query parameters
    query_params = request.query_params

    # Get headers (remove host to avoid issues)
    headers = dict(request.headers)
    headers.pop("host", None)

    # Get request body
    body = await request.body()

    try:
        # Forward the request
        response = await client.request(
            method=request.method,
            url=ttyd_url,
            params=query_params,
            headers=headers,
            content=body,
            follow_redirects=True,
        )

        # Build clean response headers, dropping hop-by-hop and encoding
        # headers that become invalid after httpx auto-decompression.
        hop_by_hop = {"transfer-encoding", "content-encoding", "content-length", "connection"}
        response_headers = {
            k: v for k, v in response.headers.items() if k.lower() not in hop_by_hop
        }

        # ttyd constructs WebSocket URLs dynamically from window.location,
        # so no HTML rewriting is needed when served behind our proxy path.
        raw = await response.aread()

        # Inject custom CSS and JS to improve touch scrolling behavior
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            try:
                html = raw.decode("utf-8")
                html = inject_terminal_html(
                    html,
                    tab_id=tab_id,
                    agent_type_value=tab.agent_type.value,
                    execution_target_value=tab.target.value,
                )
                raw = html.encode("utf-8")
                response_headers["content-length"] = str(len(raw))
            except Exception as e:
                logger.warning(f"Failed to inject custom code: {e}")

        return StreamingResponse(
            iter([raw]),
            status_code=response.status_code,
            headers=response_headers,
        )
    except Exception as e:
        logger.error(f"Proxy request error: {e}")
        raise HTTPException(status_code=502, detail="Error proxying to terminal")


@router.get("/proxy-iframe/{tab_id}")
async def proxy_terminal_iframe(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Redirect to ttyd's web interface via our proxy."""
    tab = await ttyd_manager.ensure_tab_running(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    # Use our proxy endpoint
    return RedirectResponse(url=f"/api/terminal/proxy/{tab_id}/")
