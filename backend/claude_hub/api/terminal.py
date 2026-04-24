import json
import os

# Disable all proxies for localhost connections
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("https_proxy", None)
os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

import logging
from collections.abc import Sequence
from typing import Optional

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
from ..models import User
from ..services import ttyd_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/terminal", tags=["terminal"])

# HTTP client for proxying (no proxy for localhost connections)
transport = httpx.AsyncHTTPTransport()
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


async def proxy_websocket(
    client_ws: WebSocket,
    server_uri: str,
    subprotocols: Optional[Sequence[Subprotocol]] = None,
) -> None:
    """Proxy WebSocket messages between client and ttyd."""
    try:
        async with websockets.connect(server_uri, subprotocols=subprotocols) as server_ws:
            # Forward messages from client to server
            async def client_to_server() -> None:
                try:
                    while True:
                        data = await client_ws.receive()
                        if data["type"] == "websocket.receive":
                            if "text" in data:
                                await server_ws.send(data["text"])
                            elif "bytes" in data:
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
    # Authenticate user
    user = await get_current_user_ws(websocket, session_id)
    if not user:
        await websocket.close(code=4001)
        return

    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        await websocket.close(code=4004)
        return

    await websocket.accept(subprotocol="tty")

    ttyd_ws_uri = f"ws://127.0.0.1:{tab.port}/ws"

    try:
        await proxy_websocket(websocket, ttyd_ws_uri, subprotocols=[Subprotocol("tty")])
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket terminal error for tab {tab_id}: {e}")


@router.websocket("/proxy/{tab_id}/ws")
async def proxy_ttyd_websocket(
    websocket: WebSocket, tab_id: str, session_id: Optional[str] = Query(None)
) -> None:
    """Proxy WebSocket connections directly to ttyd's /ws endpoint."""
    # Authenticate user
    user = await get_current_user_ws(websocket, session_id)
    if not user:
        await websocket.close(code=4001)
        return

    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        await websocket.close(code=4004)
        return

    await websocket.accept(subprotocol="tty")

    ttyd_ws_uri = f"ws://127.0.0.1:{tab.port}/ws"

    try:
        await proxy_websocket(websocket, ttyd_ws_uri, subprotocols=[Subprotocol("tty")])
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket proxy error for tab {tab_id}: {e}")


@router.get("/history/{tab_id}", response_model=TerminalHistoryResponse)
async def get_terminal_history(
    tab_id: str,
    lines: int = Query(100000, ge=100, le=100000),
    current_user: User = Depends(get_current_user),
) -> TerminalHistoryResponse:
    """Get captured tmux history for replaying terminal scrollback."""
    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")

    try:
        history = await ttyd_manager.get_tab_history(tab_id, lines)
    except Exception as e:
        logger.error(f"Failed to capture history for tab {tab_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to capture terminal history") from e

    if history is None:
        raise HTTPException(status_code=404, detail="Tab not found")

    return TerminalHistoryResponse(tab_id=tab_id, lines=lines, history=history)


@router.get("/proxy/{tab_id}")
async def get_terminal_proxy_root(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Redirect to the proxied ttyd page (with trailing slash for correct relative URL resolution)."""
    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return RedirectResponse(url=f"/api/terminal/proxy/{tab_id}/")


@router.api_route(
    "/proxy/{tab_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
async def proxy_terminal_request(
    tab_id: str,
    path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Proxy HTTP requests to ttyd."""
    tab = ttyd_manager.get_tab(tab_id)
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
                # Add custom styles and scripts before </head>
                custom_code = """
    <style>
      /* Improve layout and touch scrolling behavior */
      html, body {
        overscroll-behavior: none;
        -webkit-overflow-scrolling: touch;
        /* Use full viewport height so keyboard doesn't shrink the layout */
        height: 100lvh;
      }
      body {
        margin: 0;
        padding: 0;
        overflow: hidden;
      }
      .xterm-viewport {
        touch-action: pan-y !important;
        -webkit-overflow-scrolling: touch !important;
      }
    </style>
"""
                history_replay_code = f"""
    <script>
      (function () {{
        const TAB_ID = {json.dumps(tab_id)};
        const HISTORY_LINES = 100000;
        let historyText = '';

        try {{
          const xhr = new XMLHttpRequest();
          xhr.open('GET', `/api/terminal/history/${{TAB_ID}}?lines=${{HISTORY_LINES}}`, false);
          xhr.send(null);
          if (xhr.status >= 200 && xhr.status < 300) {{
            const payload = JSON.parse(xhr.responseText || '{{}}');
            if (payload && typeof payload.history === 'string') {{
              historyText = payload.history;
            }}
          }}
        }} catch (error) {{
          console.debug('claude-hub history preload failed', error);
        }}

        // NOTE: Do NOT early-return when historyText is empty.  The
        // hook and resize-guard logic below must run regardless of
        // whether there is history to replay.

        const normalizedHistory = historyText.replace(/\\r?\\n/g, '\\r\\n');
        let currentTerm = undefined;
        let replayed = false;

        function replayHistory(term, fullReplay) {{
          if (!term || replayed || typeof term.write !== 'function') return;
          replayed = true;

          // The history API now returns the FULL terminal content
          // (scrollback + visible screen) from tmux capture-pane.
          const lines = normalizedHistory.replace(/\\r/g, '').split('\\n');
          // Drop the trailing empty string from split('foo\\n') → ['foo','']
          if (lines.length > 0 && lines[lines.length - 1] === '') {{
            lines.pop();
          }}
          if (lines.length === 0) {{
            term.__claudeHubReplayDone = true;
            return;
          }}

          // Buffer live writes until history replay completes to prevent
          // history and real-time data from interleaving in the xterm buffer.
          const buffer = [];
          let historyDone = false;
          const originalWrite = term.write.bind(term);

          term.write = function(data, cb) {{
            if (historyDone) {{
              return originalWrite(data, cb);
            }}
            buffer.push({{ data, cb }});
            return undefined;
          }};

          function flushBuffer() {{
            if (historyDone) return;
            historyDone = true;
            term.__claudeHubReplayDone = true;
            term.write = originalWrite;
            // Phase B (fullReplay): ttyd already rendered the visible screen
            // and buffered WS data contains duplicate visible-screen content
            // that would overwrite our replay.  Discard the entire buffer —
            // our replay already wrote the complete terminal state.
            // New real-time output will arrive after flush and be written
            // normally through the restored term.write.
            if (!fullReplay) {{
              for (const item of buffer) {{
                originalWrite(item.data, item.cb);
              }}
            }}
            buffer.length = 0;
          }}

          // Safety timeout: release buffer if callback never fires
          const safetyTimer = setTimeout(flushBuffer, 5000);

          if (fullReplay) {{
            // Phase B: term.open() was already called by ttyd and the
            // visible screen is already rendered.  Clear the entire buffer
            // (screen + scrollback) and write the full terminal content
            // from scratch.  The last `rows` lines will land on the
            // visible screen; the rest becomes scrollback.
            // The \\x1b[3J clears scrollback, \\x1b[H\\x1b[2J clears screen.
            originalWrite('\\x1b[H\\x1b[2J\\x1b[3J' + lines.join('\\r\\n'), function() {{
              clearTimeout(safetyTimer);
              flushBuffer();
            }});
          }} else {{
            // Phase A: term.open() has NOT been called yet (our hook will
            // fire it).  Only scrollback lines need to be written; the
            // visible screen will be delivered by ttyd's WebSocket.
            // After writing scrollback, use Scroll Up (SU) to push the
            // bottom `rows` lines (which land on the visible screen) into
            // scrollback so the visible screen is left blank for ttyd.
            var scrollUpSeq = '\\x1b[' + (term.rows || 24) + 'S';
            originalWrite(lines.join('\\r\\n') + '\\r\\n' + scrollUpSeq, function() {{
              clearTimeout(safetyTimer);
              flushBuffer();
            }});
          }}
        }}

        function hookTerm(term) {{
          if (!term || term.__claudeHubHistoryHooked || typeof term.open !== 'function') return;
          term.__claudeHubHistoryHooked = true;
          // If term.open() was already called (element is attached to DOM),
          // ttyd has already written the visible screen content.  We must
          // do a full replay (clear + rewrite everything) and discard ttyd's
          // buffered WS data (which would duplicate the visible screen).
          // Otherwise, hook term.open so replay runs right after ttyd
          // calls it — only scrollback is written, ttyd WS fills the screen.
          if (term.element) {{
            replayHistory(term, true);
            setupResizeGuard(term);
          }} else {{
            const originalOpen = term.open.bind(term);
            term.open = function(...args) {{
              const result = originalOpen(...args);
              replayHistory(term, false);
              setupResizeGuard(term);
              return result;
            }};
          }}
        }}

        // ---- Mobile keyboard resize debounce ----
        // When the virtual keyboard appears/disappears on mobile,
        // the viewport shrinks/expands, causing xterm.js to resize
        // repeatedly. With large scrollback this triggers a feedback
        // loop: resize -> tmux redraw -> xterm re-render -> layout shift
        // -> another resize. We break this loop by debouncing resize
        // events and using visualViewport API to detect genuine
        // keyboard state changes vs transient layout jank.
        const DEBOUNCE_MS = 150;

        function setupResizeGuard(term) {{
          if (!term || term.__claudeHubResizeGuarded) return;
          term.__claudeHubResizeGuarded = true;

          // Debounce ttyd's resize-to-tmux path: xterm.js onResize
          // callback is intercepted so only the final stable dimension
          // is forwarded to tmux, preventing intermediate resizes from
          // triggering expensive redraws on large scrollback buffers.
          if (typeof term.onResize === 'function') {{
            const origOnResize = term.onResize;
            let lastArgs = null;
            let pending = false;
            let timer = null;

            term.onResize = function(cols, rows) {{
              lastArgs = [cols, rows];
              if (pending) return;
              pending = true;
              timer = setTimeout(function() {{
                pending = false;
                if (lastArgs) origOnResize.apply(term, lastArgs);
                lastArgs = null;
              }}, DEBOUNCE_MS);
            }};
          }}

          // On mobile, visualViewport.resize fires many times as the
          // keyboard animates in/out. We only act when the keyboard
          // reaches a stable state (visible or hidden), and ignore
          // all intermediate transitions.
          if (window.visualViewport) {{
            let keyboardVisible = false;
            let vvTimer = null;

            window.visualViewport.addEventListener('resize', function() {{
              clearTimeout(vvTimer);
              vvTimer = setTimeout(function() {{
                const vv = window.visualViewport;
                const nowKeyboard = (vv.height < window.innerHeight * 0.8);

                if (nowKeyboard !== keyboardVisible) {{
                  keyboardVisible = nowKeyboard;
                  // Trigger a single, stable fit after keyboard
                  // animation is complete
                  try {{
                    if (term.fitAddon) term.fitAddon.fit();
                    else if (typeof term.fit === 'function') term.fit();
                  }} catch(e) {{}}
                }}
                // Keyboard state unchanged = transient jank, ignore
              }}, DEBOUNCE_MS);
            }});
          }}
        }}

        // ttyd uses Object.defineProperty(window, 'term', ...) internally,
        // and its bundled copy of Object.defineProperty was captured
        // before our script ran.  So we cannot intercept it via the
        // global.  Instead, poll for window.term being set and hook it
        // once it appears.  We also check immediately in case it was
        // already set.
        function tryHookTerm() {{
          if (window.term && typeof window.term === 'object' && !window.term.__claudeHubHistoryHooked) {{
            currentTerm = window.term;
            hookTerm(window.term);
            return true;
          }}
          return false;
        }}

        // Try immediately (ttyd may have already set it)
        if (!tryHookTerm()) {{
          // Not set yet — poll until it appears (ttyd sets it during init)
          const pollTimer = setInterval(function() {{
            if (tryHookTerm()) {{
              clearInterval(pollTimer);
            }}
          }}, 50);
          // Safety: stop polling after 10 seconds
          setTimeout(function() {{ clearInterval(pollTimer); }}, 10000);
        }}
      }})();
    </script>
"""
                # Insert before </head>
                if "</head>" in html:
                    html = html.replace("</head>", custom_code + history_replay_code + "</head>")
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
    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    # Use our proxy endpoint
    return RedirectResponse(url=f"/api/terminal/proxy/{tab_id}/")
