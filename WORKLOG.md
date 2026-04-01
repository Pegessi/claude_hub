# Claude Hub - Development Worklog

> AI-friendly troubleshooting reference. Each entry documents a real bug encountered during development, its root cause, and the fix applied.

## Bug #1: New Terminal Tab Creation Appears Silent

**Symptom**: Clicking "+" button and entering a tab name does nothing — no new tab appears, no error shown.

**Root Cause**: Pinia store state destructured without `storeToRefs()` in `TabBar.vue` and `App.vue`. Plain destructuring (`const { tabs, activeTabId } = store`) loses Vue reactivity — the template binds to a snapshot, not the live ref.

**Fix**: Use `storeToRefs(store)` for all reactive state/getters; keep actions accessed directly from `store`.

**Files Changed**:
- `frontend/src/components/TabBar.vue`
- `frontend/src/App.vue`

**Key Lesson**: In Pinia `setup()` stores, state (`ref`) and getters (`computed`) MUST be extracted via `storeToRefs()`. Actions (plain functions) can be destructured directly.

---

## Bug #2: Terminal iframe Shows Blank Black Screen

**Symptom**: Tab is created, ttyd process is running, but the iframe area is completely black with no content.

**Root Cause**: The backend HTTP proxy (`terminal.py`) forwards ttyd's response headers as-is, including `content-encoding: gzip`. However, `httpx.AsyncClient` automatically decompresses gzip responses before returning the body. The result: the browser receives an uncompressed body but the header claims it's gzip, so the browser fails to decode it → blank iframe.

**Fix**: Strip hop-by-hop and encoding headers (`content-encoding`, `transfer-encoding`, `content-length`, `connection`) from proxied responses, then set a fresh `content-length` on the actual (decompressed) body.

**Files Changed**:
- `backend/claude_hub/api/terminal.py`

**Key Lesson**: When using `httpx` as a reverse proxy, it transparently decompresses responses. You must remove the original `content-encoding` header before forwarding to the client, or the client will double-decompress and get garbage.

---

## Bug #3: WebSocket URL Rewriting Corrupts ttyd Page

**Symptom**: Even after fixing the gzip issue, the terminal shows "Press ↵ to Reconnect" — WebSocket fails immediately.

**Root Cause**: The proxy code rewrote `/ws` references in the HTML to redirect WebSocket connections. But:
1. `html.replace("/ws", ws_url)` is a global substring replace — it corrupts unrelated strings containing `/ws` (e.g. `/workspace`).
2. Multiple regex rules each matched the same `"/ws"` string, producing a doubled path like `/api/terminal/proxy/{id}/api/terminal/proxy/{id}/ws`.
3. **ttyd doesn't need rewriting at all** — its JS constructs the WebSocket URL dynamically from `window.location.pathname + "/ws"`, which is already correct when served behind the proxy path.

**Fix**: Remove all HTML rewriting logic entirely. ttyd's built-in relative URL construction handles proxy paths natively.

**Files Changed**:
- `backend/claude_hub/api/terminal.py`

**Key Lesson**: Before writing URL-rewriting proxy logic, check if the upstream app already uses relative URL construction (e.g. `window.location`). If so, rewriting is unnecessary and harmful.

---

## Bug #4: WebSocket Connection Fails with Code 1006

**Symptom**: ttyd page loads, but WebSocket connection drops immediately with code 1006 (Abnormal Closure). Console shows `WebSocket connection to 'ws://...' failed`.

**Root Cause**: ttyd's JavaScript opens WebSocket with subprotocol `["tty"]`: `new WebSocket(url, ["tty"])`. The backend proxy:
1. Called `websocket.accept()` without echoing back the `tty` subprotocol → browser rejects the handshake.
2. Called `websockets.connect(server_uri)` to ttyd without specifying the `tty` subprotocol → ttyd may reject or misbehave.

**Fix**: Accept with `websocket.accept(subprotocol="tty")` and connect with `websockets.connect(uri, subprotocols=["tty"])`.

**Files Changed**:
- `backend/claude_hub/api/terminal.py`

**Key Lesson**: When building a WebSocket reverse proxy, you must forward subprotocol negotiation. Check what subprotocols the upstream server expects (inspect JS source for `new WebSocket(url, [...])`).

---

## Bug #5: Vite Dev Proxy Doesn't Forward WebSocket

**Symptom**: WebSocket works when tested directly against backend (port 8000), but fails through Vite dev server (port 5173).

**Root Cause**: Vite proxy config for `/api` lacked `ws: true`. WebSocket upgrade requests starting with `/api/` were not being proxied to the backend.

**Fix**: Add `ws: true` to the `/api` proxy entry in `vite.config.ts`. Remove the unused standalone `/ws` proxy entry.

**Files Changed**:
- `frontend/vite.config.ts`

**Key Lesson**: Vite's `server.proxy` requires explicit `ws: true` per entry to handle WebSocket upgrade requests. HTTP and WebSocket proxying are configured independently.

---

## Bug #6: Terminal Runs Plain Shell Instead of Claude

**Symptom**: Terminal connects and is interactive, but shows a regular shell prompt instead of the Claude CLI.

**Root Cause**: `ttyd_manager.py` defaults to `os.environ.get("SHELL", "/bin/bash")` — the user's login shell, not the Claude CLI.

**Fix**: Add `default_command: str = "claude"` to `config.py` settings. Update `ttyd_manager.py` to use `settings.default_command` as the default.

**Files Changed**:
- `backend/claude_hub/config.py`
- `backend/claude_hub/services/ttyd_manager.py`

**Key Lesson**: Configurable defaults belong in the settings layer, not hardcoded in service code.

---

## Environment Notes

- **System proxy** (`http_proxy=127.0.0.1:7890`) was active on the dev machine. The backend already handles this by clearing proxy env vars at import time and using `trust_env=False` on httpx. But `curl` commands during debugging needed `--noproxy '*'` to avoid false 502 errors.
- **ttyd version**: 1.7.7 (Homebrew, macOS ARM). WebSocket subprotocol `tty` is mandatory in this version.
- **Claude CLI version**: 2.1.77 (Claude Code, installed via nvm/Node.js).
