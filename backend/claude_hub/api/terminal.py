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
    cursor_x: Optional[int] = None
    cursor_y: Optional[int] = None


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

    tab = await ttyd_manager.ensure_tab_running(tab_id)
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

    tab = await ttyd_manager.ensure_tab_running(tab_id)
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
    "/proxy/{tab_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
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
      /* Mobile touch scrolling with native inertia.
         xterm.js layers (bottom to top): .xterm-viewport (scrollable),
         .xterm-screen, canvas (render), canvas.xterm-link-layer.
         Touch events land on the topmost canvas, never reaching
         .xterm-viewport, so browser-native inertial scroll never fires.
         Fix: let touches pass through .xterm-screen to .xterm-viewport.
         On mobile, users scroll more than they select text/click links,
         so this is the right trade-off. Text selection via long-press
         still works because the textarea helper captures it. */
      .xterm-screen {
        pointer-events: none !important;
      }
      .xterm-viewport {
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
        let historyCursorX = null;
        let historyCursorY = null;
        let historyLoaded = false;

        function markHistoryLoaded() {{
          historyLoaded = true;
          tryHookTerm();
        }}

        fetch(`/api/terminal/history/${{TAB_ID}}?lines=${{HISTORY_LINES}}`, {{
          cache: 'no-store',
          credentials: 'same-origin',
        }})
          .then(function(response) {{
            if (!response.ok) throw new Error('history preload failed: ' + response.status);
            return response.json();
          }})
          .then(function(payload) {{
            if (payload && typeof payload.history === 'string') {{
              historyText = payload.history;
            }}
            if (payload && Number.isInteger(payload.cursor_x) && Number.isInteger(payload.cursor_y)) {{
              historyCursorX = payload.cursor_x;
              historyCursorY = payload.cursor_y;
            }}
          }})
          .catch(function(error) {{
            console.debug('claude-hub history preload failed', error);
          }})
          .finally(markHistoryLoaded);

        // Do not leave the terminal blank if the history endpoint stalls.
        setTimeout(function() {{
          if (!historyLoaded) markHistoryLoaded();
        }}, 3000);

        // NOTE: Do NOT early-return when historyText is empty.  The
        // hook and resize-guard logic below must run regardless of
        // whether there is history to replay.

        let currentTerm = undefined;
        let replayed = false;

        function normalizedHistoryText() {{
          return historyText.replace(/\\r?\\n/g, '\\r\\n');
        }}

        function cursorSeq(cursorX, cursorY) {{
          if (!Number.isInteger(cursorX) || !Number.isInteger(cursorY)) return '';
          return '\\x1b[' + (cursorY + 1) + ';' + (cursorX + 1) + 'H';
        }}

        function replayHistory(term, fullReplay) {{
          if (!term || replayed || typeof term.write !== 'function') return;
          replayed = true;

          // The history API now returns the FULL terminal content
          // (scrollback + visible screen) from tmux capture-pane.
          const lines = normalizedHistoryText().replace(/\\r/g, '').split('\\n');
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
          const FULL_REPLAY_MIN_HOLD_MS = 2500;
          const FULL_REPLAY_QUIET_MS = 750;
          const FULL_REPLAY_MAX_HOLD_MS = 8000;
          const FULL_REPLAY_VERIFY_DELAY_MS = 350;
          const FULL_REPLAY_VERIFY_ATTEMPTS = 8;
          const FULL_REPLAY_WATCH_MS = 2000;
          const FULL_REPLAY_WATCH_INTERVAL_MS = 100;
          let fullReplayHoldStartedAt = 0;
          let lastBufferedAt = 0;
          let fullReplayHoldTimer = null;
          let fullReplayFinalizing = false;
          let fullReplayVerifyAttempts = 0;
          const replayPayload = '\\x1b[H\\x1b[2J\\x1b[3J' + lines.join('\\r\\n') + cursorSeq(historyCursorX, historyCursorY);

          term.write = function(data, cb) {{
            if (historyDone) {{
              return originalWrite(data, cb);
            }}
            if (fullReplay) {{
              lastBufferedAt = Date.now();
            }}
            buffer.push({{ data, cb }});
            return undefined;
          }};

          function flushBuffer() {{
            if (historyDone) return;
            if (fullReplayHoldTimer) {{
              clearTimeout(fullReplayHoldTimer);
              fullReplayHoldTimer = null;
            }}
            clearTimeout(safetyTimer);
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
            startPostReplayWatch();
          }}

          function hasExpectedReplayBuffer() {{
            if (!fullReplay) return true;
            const active = term.buffer && term.buffer.active;
            if (!active) return true;
            const rows = term.rows || 24;
            if (lines.length > rows && active.baseY <= 0) return false;
            return active.length >= Math.min(lines.length, rows);
          }}

          function startPostReplayWatch() {{
            if (!fullReplay) return;
            const watchUntil = Date.now() + FULL_REPLAY_WATCH_MS;
            function watch() {{
              if (!historyDone) return;
              if (!hasExpectedReplayBuffer()) {{
                originalWrite(replayPayload, function() {{}});
              }}
              if (Date.now() < watchUntil) {{
                setTimeout(watch, FULL_REPLAY_WATCH_INTERVAL_MS);
              }}
            }}
            setTimeout(watch, FULL_REPLAY_WATCH_INTERVAL_MS);
          }}

          function verifyFullReplayBeforeDone() {{
            setTimeout(function() {{
              if (hasExpectedReplayBuffer() || fullReplayVerifyAttempts >= FULL_REPLAY_VERIFY_ATTEMPTS) {{
                flushBuffer();
                return;
              }}
              fullReplayVerifyAttempts++;
              originalWrite(replayPayload, verifyFullReplayBeforeDone);
            }}, FULL_REPLAY_VERIFY_DELAY_MS);
          }}

          function finishFullReplay() {{
            if (!fullReplay) {{
              flushBuffer();
              return;
            }}
            if (fullReplayFinalizing) return;
            fullReplayFinalizing = true;
            if (fullReplayHoldTimer) {{
              clearTimeout(fullReplayHoldTimer);
              fullReplayHoldTimer = null;
            }}
            // A final replay right before releasing the buffer overwrites any
            // late ttyd initial-screen frames that arrived during the hold.
            // On Linux CI, a later resize/initial-frame burst can still
            // collapse scrollback; verify the xterm buffer before setting
            // the public replay-done flag used by E2E readiness checks.
            originalWrite(replayPayload, verifyFullReplayBeforeDone);
          }}

          function finishFullReplayWhenQuiet() {{
            if (!fullReplay) {{
              flushBuffer();
              return;
            }}
            const now = Date.now();
            const elapsed = now - fullReplayHoldStartedAt;
            const quietFor = lastBufferedAt ? now - lastBufferedAt : elapsed;
            if (
              elapsed >= FULL_REPLAY_MAX_HOLD_MS ||
              (elapsed >= FULL_REPLAY_MIN_HOLD_MS && quietFor >= FULL_REPLAY_QUIET_MS)
            ) {{
              finishFullReplay();
              return;
            }}
            const waitMs = Math.max(
              50,
              Math.min(
                FULL_REPLAY_MAX_HOLD_MS - elapsed,
                Math.max(FULL_REPLAY_MIN_HOLD_MS - elapsed, FULL_REPLAY_QUIET_MS - quietFor)
              )
            );
            fullReplayHoldTimer = setTimeout(finishFullReplayWhenQuiet, waitMs);
          }}

          // Safety timeout: release buffer if callbacks never fire.
          const safetyTimer = setTimeout(function() {{
            if (fullReplay && !fullReplayFinalizing) {{
              finishFullReplay();
              return;
            }}
            flushBuffer();
          }}, FULL_REPLAY_MAX_HOLD_MS + 5000);

          if (fullReplay) {{
            // Phase B: term.open() was already called by ttyd and the
            // visible screen is already rendered.  Clear the entire buffer
            // (screen + scrollback) and write the full terminal content
            // from scratch.  The last `rows` lines will land on the
            // visible screen; the rest becomes scrollback.
            // The \\x1b[3J clears scrollback, \\x1b[H\\x1b[2J clears screen.
            originalWrite(replayPayload, function() {{
              // ttyd can still deliver its initial screen payload after
              // xterm accepts the replay write, especially under the Linux
              // CI binary. Keep term.write buffered until that stream has
              // gone quiet so late duplicate initial frames cannot collapse
              // reconstructed scrollback back to only the visible screen rows.
              fullReplayHoldStartedAt = Date.now();
              finishFullReplayWhenQuiet();
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
            setupHistoryResyncAfterReplay(term);
          }} else {{
            const originalOpen = term.open.bind(term);
            term.open = function(...args) {{
              const result = originalOpen(...args);
              // xterm.js behavior differs across browser/platform versions
              // when large writes happen immediately after open(): in
              // Ubuntu CI, the Phase A scroll-up strategy can leave only
              // the visible screen in the buffer. Full replay is more
              // deterministic: clear after open(), write tmux's complete
              // captured content, and discard ttyd's duplicate initial WS
              // payload while replay is in progress.
              replayHistory(term, true);
              setupResizeGuard(term);
              setupHistoryResyncAfterReplay(term);
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

        // ---- History resync after live-output bursts ----
        // ttyd is attached to tmux as a terminal client, so under fast
        // wrapped output tmux can optimize what it sends to the client and
        // skip scrollback that is still present in tmux history. After live
        // output goes idle and the user is at the bottom, reconcile xterm
        // with tmux history so newly produced long/wrapped output is complete.
        const RESYNC_IDLE_MS = 700;

        function setupHistoryResync(term) {{
          if (!term || term.__claudeHubHistoryResyncHooked || typeof term.write !== 'function') return;
          term.__claudeHubHistoryResyncHooked = true;

          const writeThrough = term.write.bind(term);
          let writeGeneration = 0;
          let timer = null;
          let resyncing = false;
          let pendingWhenBottom = false;
          const resyncBuffer = [];

          function bufferService() {{
            return term._core && term._core._bufferService;
          }}

          function isAtBottom() {{
            const buffer = term.buffer && term.buffer.active;
            const service = bufferService();
            if (!buffer) return true;
            const viewportEl = document.querySelector('.xterm-viewport');
            const domAtBottom = !viewportEl ||
              viewportEl.scrollTop >= viewportEl.scrollHeight - viewportEl.clientHeight - 1;
            return !(service && service.isUserScrolling) &&
              buffer.viewportY === buffer.baseY &&
              domAtBottom;
          }}

          function scheduleResync() {{
            if (resyncing) return;
            if (timer) clearTimeout(timer);
            timer = setTimeout(runResync, RESYNC_IDLE_MS);
          }}

          function noteLiveWrite() {{
            writeGeneration++;
            if (isAtBottom()) {{
              pendingWhenBottom = false;
              scheduleResync();
            }} else {{
              pendingWhenBottom = true;
            }}
          }}

          term.write = function(data, cb) {{
            if (resyncing) {{
              resyncBuffer.push({{ data, cb }});
              return undefined;
            }}
            const result = writeThrough(data, cb);
            noteLiveWrite();
            return result;
          }};

          const viewportEl = document.querySelector('.xterm-viewport');
          if (viewportEl) {{
            viewportEl.addEventListener('scroll', function() {{
              if (pendingWhenBottom && isAtBottom()) {{
                pendingWhenBottom = false;
                scheduleResync();
              }}
            }}, {{ passive: true }});
          }}

          // Run one idle reconciliation after setup. On a brand-new tab the
          // initial history preload can be empty because tmux creates the
          // session lazily after ttyd connects; this brings the prompt and
          // cursor back into the same xterm buffer once tmux history exists.
          scheduleResync();

          async function fetchHistorySnapshot() {{
            const response = await fetch(`/api/terminal/history/${{TAB_ID}}?lines=${{HISTORY_LINES}}`);
            if (!response.ok) throw new Error('history resync failed: ' + response.status);
            const payload = await response.json();
            return {{
              history: payload && typeof payload.history === 'string' ? payload.history : '',
              cursorX: payload && Number.isInteger(payload.cursor_x) ? payload.cursor_x : null,
              cursorY: payload && Number.isInteger(payload.cursor_y) ? payload.cursor_y : null,
            }};
          }}

          async function runResync() {{
            timer = null;
            if (!isAtBottom() || resyncing) {{
              pendingWhenBottom = true;
              return;
            }}

            const generationAtStart = writeGeneration;
            let snapshot = null;
            try {{
              snapshot = await fetchHistorySnapshot();
            }} catch (error) {{
              console.debug('claude-hub history resync failed', error);
              return;
            }}

            if (generationAtStart !== writeGeneration || !isAtBottom()) {{
              pendingWhenBottom = !isAtBottom();
              if (isAtBottom()) scheduleResync();
              return;
            }}

            const lines = snapshot.history.replace(/\\r?\\n/g, '\\r\\n').replace(/\\r/g, '').split('\\n');
            if (lines.length > 0 && lines[lines.length - 1] === '') {{
              lines.pop();
            }}
            if (lines.length === 0) return;

            resyncing = true;
            writeThrough('\\x1b[H\\x1b[2J\\x1b[3J' + lines.join('\\r\\n') + cursorSeq(snapshot.cursorX, snapshot.cursorY), function() {{
              resyncing = false;
              while (resyncBuffer.length > 0) {{
                const item = resyncBuffer.shift();
                writeThrough(item.data, item.cb);
              }}
              try {{
                if (typeof term.scrollToBottom === 'function') {{
                  term.scrollToBottom();
                }}
              }} catch (error) {{}}
              if (resyncBuffer.length > 0 || generationAtStart !== writeGeneration) {{
                scheduleResync();
              }}
            }});
          }}
        }}

        function setupHistoryResyncAfterReplay(term) {{
          if (!term) return;
          if (term.__claudeHubReplayDone) {{
            setupHistoryResync(term);
            return;
          }}
          var tries = 0;
          var iv = setInterval(function() {{
            tries++;
            if (term.__claudeHubReplayDone || tries > 300) {{
              clearInterval(iv);
              setupHistoryResync(term);
            }}
          }}, 50);
        }}

        // ---- Mobile scrolling: native inertia ----
        // xterm.js registers touchstart/touchmove on the .xterm element (parent).
        // Its handleTouchMove does scrollTop += delta manually — no inertia.
        // Its _innerRefresh snaps scrollTop = ydisp * rowHeight each frame.
        //
        // Strategy:
        // 1. Override viewport.handleTouchMove/handleTouchStart to no-ops so
        //    xterm's listener callback does nothing useful even if it fires
        // 2. Let browser-native scroll on .xterm-viewport (overflow-y:scroll) work
        // 3. Keep xterm's _innerRefresh intact. Live output can advance
        //    viewportY/baseY while the user is touching history; blocking
        //    _innerRefresh lets scrollTop go stale and makes rendered
        //    history appear mixed with new output until touchend.
        var _isTouchScrolling = false;
        var _touchScrollTimer = null;

        function _getTerm() {{
          var term = window.ttyd && window.ttyd.terminal ? window.ttyd.terminal : window.term;
          return term || null;
        }}

        function _getViewportObj() {{
          var term = _getTerm();
          return term ? (term.viewport || (term._core && term._core.viewport) || null) : null;
        }}

        function _markUserScrolling() {{
          var term = _getTerm();
          var bufferService = term && term._core && term._core._bufferService;
          if (bufferService) {{
            bufferService.isUserScrolling = true;
          }}
        }}

        function _scrollAwayFromBottom(deltaY) {{
          var term = _getTerm();
          var buffer = term && term.buffer && term.buffer.active;
          if (!term || !buffer || buffer.viewportY < buffer.baseY - 1) return;

          var vpObj = _getViewportObj();
          var rowHeight = (vpObj && vpObj._currentRowHeight) || 15;
          var lines = Math.max(1, Math.ceil(Math.abs(deltaY) / rowHeight));
          if (typeof term.scrollLines === 'function') {{
            term.scrollLines(-lines);
            return;
          }}

          var viewportEl = document.querySelector('.xterm-viewport');
          if (viewportEl) {{
            viewportEl.scrollTop = Math.max(0, viewportEl.scrollTop - lines * rowHeight);
          }}
        }}

        function enableNativeTouchScroll() {{
          var vpObj = _getViewportObj();
          var viewportEl = document.querySelector('.xterm-viewport');
          var xtermEl = document.querySelector('.xterm');
          if (!vpObj || !viewportEl) return false;
          if (vpObj.__claudeHubNativeTouch) return true;
          vpObj.__claudeHubNativeTouch = true;

          // 1. Neutralize xterm's touch handlers on the viewport object.
          //    xterm's touchmove listener on .xterm calls viewport.handleTouchMove()
          //    which does scrollTop += delta. Replace with no-op.
          vpObj.handleTouchStart = function() {{}};
          vpObj.handleTouchMove = function() {{ return true; }};

          // Desktop wheel/trackpad scrolls can race with fast live output
          // while the viewport is still at the bottom. Mark user scrolling
          // in the capture phase so xterm stops following new output before
          // its normal wheel handler processes the scroll delta.
          function handleWheelStart(event) {{
            const target = event.target;
            const insideTerminal = !target || target === window || target === document ||
              (target.closest && target.closest('.xterm'));
            if (insideTerminal && event.deltaY < 0) {{
              _markUserScrolling();
              _scrollAwayFromBottom(event.deltaY);
            }}
          }}
          viewportEl.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});
          if (xtermEl) {{
            xtermEl.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});
          }}
          window.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});

          // 2. Track touch state on the viewport element
          var touchStartY = null;
          viewportEl.addEventListener('touchstart', function(event) {{
            _isTouchScrolling = true;
            touchStartY = null;
            if (event.touches && event.touches.length > 0) {{
              touchStartY = event.touches[0].clientY;
            }}
            if (_touchScrollTimer) {{
              clearTimeout(_touchScrollTimer);
              _touchScrollTimer = null;
            }}
          }}, {{ passive: true }});

          viewportEl.addEventListener('touchmove', function(event) {{
            if (touchStartY !== null && event.touches && event.touches.length > 0) {{
              if (event.touches[0].clientY - touchStartY > 8) {{
                _markUserScrolling();
              }}
            }}
          }}, {{ passive: true }});

          viewportEl.addEventListener('touchend', function() {{
            _touchScrollTimer = setTimeout(function() {{
              _isTouchScrolling = false;
              _touchScrollTimer = null;
            }}, 500);
          }}, {{ passive: true }});

          return true;
        }}

        // Poll until both viewport object and DOM element are ready
        function tryEnable() {{
          if (enableNativeTouchScroll()) return;
          var tries = 0;
          var iv = setInterval(function() {{
            tries++;
            if (enableNativeTouchScroll() || tries > 100) clearInterval(iv);
          }}, 100);
        }}
        tryEnable();

        // ttyd uses Object.defineProperty(window, 'term', ...) internally,
        // and its bundled copy of Object.defineProperty was captured
        // before our script ran.  So we cannot intercept it via the
        // global.  Instead, poll for window.term being set and hook it
        // once it appears.  We also check immediately in case it was
        // already set.
        function tryHookTerm() {{
          if (!historyLoaded) {{
            return false;
          }}
          if (window.term && typeof window.term === 'object' && !window.term.__claudeHubHistoryHooked) {{
            currentTerm = window.term;
            hookTerm(window.term);
            // Notify parent that terminal is ready for key input
            if (window.parent && window.parent !== window) {{
              window.parent.postMessage({{
                type: 'terminal-ready',
                tabId: TAB_ID
              }}, '*');
            }}
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
    tab = await ttyd_manager.ensure_tab_running(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    # Use our proxy endpoint
    return RedirectResponse(url=f"/api/terminal/proxy/{tab_id}/")
