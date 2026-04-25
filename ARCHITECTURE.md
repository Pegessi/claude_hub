# Claude Hub - Architecture Guide

> AI-friendly deep architecture reference. Read this to understand the system before making changes.

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (Vue 3)                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────────────┐  │
│  │ TabBar    │  │ Layout   │  │ TerminalPane (iframe)        │  │
│  │ (create/  │  │ Selector │  │  → loads /api/terminal/     │  │
│  │  switch/  │  │ (1x1→3x3)│  │    proxy/{tab_id}/          │  │
│  │  reorder) │  │          │  │  → WS /api/terminal/ws/     │  │
│  └─────┬─────┘  └─────┬────┘  │    {tab_id}                 │  │
│        │              │       └──────────────┬───────────────┘  │
│        └──────────────┼──────────────────────┘                  │
│                       │  fetch /api/tabs                        │
│                       │  WS /api/terminal/ws/{id}               │
└───────────────────────┼─────────────────────────────────────────┘
                        │
                  Vite proxy (dev) / Nginx (prod)
                  forwards /api/* → backend:8173
                        │
┌───────────────────────┼─────────────────────────────────────────┐
│                  FastAPI Backend (:8173)                         │
│                       │                                          │
│  ┌────────────────────┼───────────────────────────────────┐     │
│  │ API Layer          │                                   │     │
│  │  /api/tabs         │  CRUD + ordering                  │     │
│  │  /api/terminal/*   │  HTTP proxy + WS proxy + history  │     │
│  │  /api/auth/*       │  Feishu OAuth + session           │     │
│  │  /api/fs/*         │  Directory browsing               │     │
│  └────────────────────┼───────────────────────────────────┘     │
│                       │                                          │
│  ┌────────────────────┼───────────────────────────────────┐     │
│  │ Service Layer      │                                   │     │
│  │  TTYDManager ──────┼──→ manages N TTYDProcess          │     │
│  │  ConnectionManager │    WS connection tracking          │     │
│  └────────────────────┼───────────────────────────────────┘     │
│                       │                                          │
│  ┌────────────────────┼───────────────────────────────────┐     │
│  │ Auth Layer         │                                   │     │
│  │  dependencies.py ──┼──→ get_current_user (HTTP)        │     │
│  │                   ──┼──→ get_current_user_ws (WS)      │     │
│  │  feishu.py         │    OAuth token exchange            │     │
│  │  session.py        │    In-memory session store         │     │
│  └────────────────────┘                                   │     │
└────────────────────────────────────────────────────────────┘     │
                        │                                          │
          For each tab, one ttyd process on port 10xxx             │
          ttyd attaches to a tmux session for persistence          │
                        │                                          │
┌───────────────────────┼─────────────────────────────────────────┐
│  ttyd :10xxx  ────→  tmux session: claude-hub-{id[:8]}         │
│                       │                                          │
│  ttyd serves xterm.js via HTTP on 127.0.0.1:10xxx              │
│  ttyd proxies terminal I/O via WebSocket (subprotocol "tty")   │
│  tmux keeps the shell/Claude alive across ttyd restarts        │
└─────────────────────────────────────────────────────────────────┘
```

## Core Data Flows

### 1. Creating a Tab

```
User clicks "+" → POST /api/tabs {name, shell, cwd, solo_mode, agent_type}
  → TTYDManager.create_tab()
    → generate UUID, allocate port (10000+)
    → TTYDProcess.__init__(tab_id, port, name, ...)
    → process.start()
      → _ensure_tmux_server()
      → ttyd --port {port} --interface 127.0.0.1 --writable
          tmux new-session -A -s claude-hub-{id[:8]} [shell/cwd]
      → _configure_tmux() (mouse off, history 100K, status off)
    → save to ~/.claude_hub/tabs.json
    → update ~/.claude_hub/tab_order.json
  → Frontend receives tab object, loads iframe → /api/terminal/proxy/{tab_id}/
```

### 2. Terminal Connection (WebSocket)

```
Browser iframe loads ttyd HTML (via our proxy)
  → ttyd JS opens WS to /api/terminal/ws/{tab_id}?session_id=xxx
    → Backend authenticates via get_current_user_ws()
    → Backend connects to ws://127.0.0.1:{port}/ws (subprotocol "tty")
    → Bidirectional proxy: client ↔ backend ↔ ttyd ↔ tmux
```

**Why proxy instead of direct iframe?**
- ttyd listens on 127.0.0.1 only (security)
- Backend injects custom CSS/JS into ttyd HTML (touch scrolling, history replay)
- Backend controls authentication for both HTTP and WS

### 3. Terminal History Replay

When the browser refreshes or reconnects, ttyd's xterm.js starts with a blank screen. The backend injects a JS script that:

**Phase A** (term.open() NOT yet called):
- Fetch full tmux history via `GET /api/terminal/history/{tab_id}`
- Write scrollback lines to xterm buffer before ttyd renders the screen
- Use Scroll Up (SU) escape to push bottom lines into scrollback, leaving visible screen blank for ttyd WS data
- Buffer live WS writes until history replay completes

**Phase B** (term.open() already called, element attached):
- Clear entire xterm buffer (screen + scrollback) with `\x1b[H\x1b[2J\x1b[3J`
- Write full terminal content from tmux capture
- Discard buffered WS data (would duplicate visible screen)
- Resume normal WS flow after replay

**Backend side**: `TTYDProcess.capture_history()` runs `tmux capture-pane -p -e -S -100000` to get the full terminal content (scrollback + visible screen).

### 4. Authentication Flow

```
Browser → GET /api/auth/login → 302 redirect to Feishu OAuth
  → User authorizes → Feishu redirects to /api/auth/callback?code=xxx
    → Backend exchanges code for access_token + refresh_token
    → Backend fetches user info (open_id, name, email)
    → Check whitelist: open_id first → email second → allow all if neither configured
    → Create in-memory session, set HTTP-only cookie
    → 302 redirect to frontend

Subsequent requests:
  → Cookie "claude_hub_session" → get_current_user() dependency
  → Local network IPs (10.x, 172.16-31.x, 192.168.x) → auto-passed as "local" user
  → WebSocket auth: session_id from query param OR manually parsed from cookie header
```

**Why manual cookie parsing for WebSocket?** FastAPI's `Cookie` decorator doesn't reliably work with WebSocket connections, so `get_current_user_ws()` manually parses `websocket.headers["cookie"]` using `http.cookies.SimpleCookie`.

## Module Reference

### Backend Modules

| Module | Key Classes/Functions | Depends On |
|--------|----------------------|------------|
| `main.py` | `app`, `lifespan()` | api_router, ttyd_manager, config |
| `config.py` | `Settings`, `settings` | pydantic-settings, .env |
| `api/tabs.py` | `list_tabs`, `create_tab`, `update_tab`, `delete_tab`, `update_tab_order` | auth.dependencies, ttyd_manager, models |
| `api/terminal.py` | `websocket_endpoint`, `proxy_terminal_request`, `get_terminal_history`, `proxy_websocket` | auth.dependencies, ttyd_manager, httpx, websockets |
| `api/auth.py` | `login`, `callback`, `get_me`, `check_auth`, `logout` | auth.feishu, auth.session, auth.dependencies, config |
| `api/filesystem.py` | Directory listing endpoints | auth.dependencies |
| `auth/dependencies.py` | `get_current_user`, `get_current_user_ws`, `optional_user`, `is_local_network_request` | auth.session, config, models |
| `auth/feishu.py` | `get_feishu_auth_url`, `get_user_access_token`, `get_user_info` | httpx, config |
| `auth/session.py` | `create_session`, `get_session`, `delete_session`, `LoginSession` | models.User |
| `services/ttyd_manager.py` | `TTYDProcess`, `TTYDManager`, `ttyd_manager` (global) | config, models |
| `services/session_manager.py` | `ConnectionManager`, `connection_manager` (global) | — |
| `models/schemas.py` | `AgentType`, `TerminalTab`, `User`, `LoginSession`, etc. | pydantic |

### Frontend Modules

| Module | Key Responsibilities |
|--------|---------------------|
| `App.vue` | Root: auth gate → TabBar + LayoutSelector + TerminalGridView |
| `stores/terminalStore.ts` | Tab CRUD, layout/pane management, tab ordering |
| `stores/authStore.ts` | Auth state, login/logout, auth check |
| `components/TabBar.vue` | Tab creation dialog, tab switching, drag-reorder, rename |
| `components/LayoutSelector.vue` | Grid layout picker (1x1 through 3x3) |
| `components/TerminalGridView.vue` | CSS Grid renderer for panes |
| `components/TerminalPane.vue` | Single pane: iframe loading /api/terminal/proxy/{id}/ |
| `components/TerminalView.vue` | Terminal interaction handlers (context menu, etc.) |
| `components/MobileControls.vue` | Mobile-specific controls (send keys to active pane) |
| `views/LoginView.vue` | Feishu OAuth login page |
| `types/index.ts` | TypeScript interfaces |

## State Persistence

| File | Location | Content |
|------|----------|---------|
| `tabs.json` | `~/.claude_hub/tabs.json` | Array of tab configs (id, name, port, shell, cwd, solo_mode, agent_type, created_at) |
| `tab_order.json` | `~/.claude_hub/tab_order.json` | Ordered array of tab IDs |
| tmux sessions | tmux server | Session `claude-hub-{id[:8]}` per tab; survives backend restart |
| session store | In-memory (backend) | LoginSession objects keyed by session_id; lost on backend restart |
| layout preference | `localStorage` (browser) | Key `claude_hub_layout_type`, e.g. "2x2" |

**Lifecycle**:
- Backend startup: `_load_state()` from tabs.json → `_load_order()` → `start_all_tabs()` (reattaches existing tmux sessions)
- Tab creation: append to processes dict → save tabs.json → append to tab_order → save tab_order.json
- Tab deletion: `stop(kill_tmux=True)` → remove from processes → save both files
- Backend shutdown (lifespan): `cleanup()` → stop ttyd processes but keep tmux sessions alive

## Key Design Decisions

### ttyd + tmux for Terminal Persistence

**Choice**: ttyd renders xterm.js, tmux keeps the shell alive.

**Alternatives considered**:
- **xterm.js directly**: No persistence — browser close kills the shell
- **tmux only (no ttyd)**: Would need to build our own xterm.js ↔ tmux bridge
- **Gotty**: Less maintained, similar approach

**Trade-off**: ttyd adds a layer (backend → ttyd → tmux instead of backend → tmux), but gives us a battle-tested xterm.js frontend with WebSocket support for free. The extra hop latency is negligible for terminal I/O.

### Reverse Proxy Instead of Direct iframe

**Choice**: Backend proxies all ttyd HTTP and WS traffic through `/api/terminal/proxy/{tab_id}/`.

**Reasons**:
1. **Security**: ttyd binds to 127.0.0.1 only, never exposed directly
2. **Injection**: Backend injects custom CSS/JS into ttyd HTML for touch scrolling and history replay
3. **Auth**: Single auth layer for all endpoints (HTTP and WS)
4. **CORS**: Backend controls which origins can access terminals

### Phase A/B History Replay Model

**Problem**: On page refresh, ttyd creates a fresh xterm.js instance. The terminal is blank until ttyd's WebSocket connects and delivers the current screen. But scrollback history is lost.

**Solution**: Inject a script that:
- Fetches tmux history and writes it to xterm buffer
- Handles timing: ttyd may have already rendered the screen (Phase B) or not yet (Phase A)
- Buffers live WS data to prevent interleaving with history writes

**Key subtlety**: In Phase B, buffered WS data must be *discarded* (not flushed) because it contains the visible screen that our full replay already wrote.

### Local Network Auth Bypass

**Choice**: Requests from private IPs (10.x, 172.16-31.x, 192.168.x, loopback) skip Feishu auth entirely.

**Reason**: In home/LAN deployment, requiring OAuth is friction. Users on the local network are implicitly trusted. This makes the default (no Feishu config) experience seamless.

## Agent Types

| Type | Shell | Solo Mode Behavior |
|------|-------|--------------------|
| `claude` | `claude` CLI | `IS_SANDBOX=1 claude --dangerously-skip-permissions` then fallback to `$SHELL` |
| `cursor` | `$SHELL` (bash/zsh) | N/A (cursor is a GUI app, terminal just provides shell) |

## Environment & Proxy Handling

The backend clears all proxy environment variables at import time in `terminal.py`:
```python
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ.pop("HTTP_PROXY", None)  # etc.
```
And uses `httpx.AsyncClient(trust_env=False, proxy=None)`.

**Why**: Dev machines often run system proxies (e.g. Clash on port 7890). Without this, httpx would route localhost ttyd requests through the proxy, causing 502 errors.

## CI Pipeline

GitHub Actions runs on every push to `main` and PRs targeting `main`:

1. **Backend**: `black --check`, `isort --check`, `mypy`, `pytest`
2. **Frontend**: ESLint, `vue-tsc` type check, `vite build`
3. **ttyd**: Verify tmux and ttyd are installed, basic functionality check
