# Claude Hub - Architecture Guide

> AI-friendly deep architecture reference. Read this to understand the system before making changes.

## System Overview

```
┌───────────────────────────────────────────────────────────────────────┐
│                           Browser (Vue 3)                              │
│ ┌──────────┐ ┌──────────────┐ ┌────────────────────────────────────┐ │
│ │ TabBar   │ │ Layout       │ │ TerminalPane (iframe)               │ │
│ │ create/  │ │ Selector     │ │ → loads /api/terminal/proxy/{id}/  │ │
│ │ switch/  │ │ (1x1→3x3)    │ │ → WS /api/terminal/ws/{id}         │ │
│ │ reorder  │ │              │ │                                    │ │
│ └────┬─────┘ └──────┬───────┘ └──────────────┬─────────────────────┘ │
│      │              │                        │                       │
│      │              │  ┌─────────────────────▼──────────────────┐    │
│      │              │  │ AgentWorkspaceView                     │    │
│      │              │  │   • Task board (Todo/Queued/Working/   │    │
│      │              │  │     Review/Done columns)               │    │
│      │              │  │   • Task detail panel + reports        │    │
│      │              │  │   • Workspace / agent management      │    │
│      │              │  │   • Feedback lessons catalog          │    │
│      │              │  │   • Mobile controls + clipboard bridge│    │
│      │              │  └─────────────────────┬──────────────────┘    │
│      └──────────────┼────────────────────────┤                       │
│                     │  fetch /api/tabs       │ /api/workspaces/*     │
│                     │  WS /api/terminal/ws/* │ /api/system/*         │
└─────────────────────┼────────────────────────┼───────────────────────┘
                      │                        │
                Vite proxy (dev) / Nginx (prod)
                forwards /api/* → backend:8173
                      │                        │
┌─────────────────────┼────────────────────────┼───────────────────────┐
│                FastAPI Backend (:8173)                                  │
│                    │                        │                         │
│  ┌─────────────────▼────────────────────────▼───────────────┐         │
│  │ API Layer                                                 │         │
│  │  /api/tabs             CRUD + ordering                   │         │
│  │  /api/terminal/*       HTTP proxy + WS proxy + history   │         │
│  │  /api/auth/*           Feishu OAuth + session             │         │
│  │  /api/fs/*             Directory browsing                │         │
│  │  /api/clipboard/*      macOS image clipboard bridge      │         │
│  │  /api/remote/*         SSH-backed remote profile mgmt    │         │
│  │  /api/system/*         Network status, host info, env    │         │
│  │  /api/workspaces/*     Workspace + task lifecycle API    │         │
│  └───────────────────────────────┬───────────────────────────┘         │
│                                  │                                     │
│  ┌────────────────────────────────▼──────────────────────────────┐    │
│  │ Service Layer                                                  │    │
│  │  TTYDManager ──────────────────── N x TTYDProcess (ttyd+tmux) │    │
│  │  ConnectionManager                WS connection tracking      │    │
│  │  ┌─────────────────────────────────────────────────────────┐  │    │
│  │  │ WorkspaceManager (mixin package: 19 submodules)         │  │    │
│  │  │   _tasks, _sessions, _dispatch, _reports, _review,      │  │    │
│  │  │   _state, _persistence, _messaging, _monitor,          │  │    │
│  │  │   _normalize, _task_updates, _workspaces, _artifacts,  │  │    │
│  │  │   _attachments, _constants, _prompts, _feedback,       │  │    │
│  │  │   _tmux_queries                                         │  │    │
│  │  └─────────────────────────────────────────────────────────┘  │    │
│  │  WorkspaceStatePolicy     Review routing / status transitions│    │
│  │  RemoteProfileManager     SSH profile auto-discovery + CRUD  │    │
│  │  FeedbackLessonsStore     Catalog of learned lessons        │    │
│  └───────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ Auth Layer                                                    │    │
│  │  dependencies.py ──► get_current_user (HTTP)                 │    │
│  │                   ──► get_current_user_ws (WS)               │    │
│  │                   ──► optional_user, is_local_network_request│    │
│  │  feishu.py          OAuth token exchange + user info         │    │
│  │  session.py         File-backed LoginSession store           │    │
│  └──────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────┘
                              │
          For each tab, one ttyd process on port 10xxx
          ttyd attaches to a tmux session for persistence
                              │
┌─────────────────────────────▼─────────────────────────────────────────┐
│  ttyd :10xxx  ────►  tmux session: claude-hub-{id[:8]}                │
│                                                                       │
│  ttyd serves xterm.js via HTTP on 127.0.0.1:10xxx                    │
│  ttyd proxies terminal I/O via WebSocket (subprotocol "tty")         │
│  tmux keeps the shell/Claude alive across ttyd restarts              │
└───────────────────────────────────────────────────────────────────────┘
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
| `main.py` | `app`, `lifespan()` startup/shutdown, logging bootstrap | api_router, service singletons, config |
| `config.py` | `Settings` (pydantic-settings) — env + defaults for all runtime knobs | pydantic-settings, .env |
| `models/schemas.py` | Pydantic v2 schemas — `AgentType`, `TerminalTab`, `User`, `LoginSession`, workspace/task/report models, etc. | pydantic |
| **API layer** | | |
| `api/tabs.py` | `list_tabs`, `create_tab`, `update_tab`, `delete_tab`, `update_tab_order`, terminal reorder/duplicate | auth.dependencies, ttyd_manager, models |
| `api/terminal.py` | `websocket_endpoint`, `proxy_terminal_request`, `get_terminal_history`, `proxy_websocket` — ttyd HTTP/WS reverse proxy + history replay injection | auth.dependencies, ttyd_manager, httpx, websockets |
| `api/auth.py` | `login`, `callback`, `get_me`, `check_auth`, `logout` — Feishu OAuth + cookie session lifecycle | auth.feishu, auth.session, auth.dependencies, config |
| `api/filesystem.py` | Server-side directory listing for workspace/file pickers (`safe_list_dir`, `normalize_path`) | auth.dependencies |
| `api/clipboard.py` | macOS browser → agent image bridge: save PNG from browser clipboard, poll, attach to task form | auth.dependencies |
| `api/remote.py` | SSH-backed remote profile CRUD + auto-discovery from `~/.ssh/config` | auth.dependencies, remote_profiles service |
| `api/system.py` | Host runtime introspection: network access (IPv4/IPv6, proxy reachability), public URL, environment presets | auth.dependencies |
| `api/workspaces.py` | Workspace CRUD, task lifecycle (create/update/review/dispatch), resident-agent sessions, goal packet, reports, feedback lessons | auth.dependencies, workspace_manager, workspace_state_policy |
| **Auth layer** | | |
| `auth/dependencies.py` | `get_current_user`, `get_current_user_ws`, `optional_user`, `is_local_network_request`, `get_client_ip`, XFF parsing | auth.session, config, models |
| `auth/feishu.py` | `get_feishu_auth_url`, `get_app_access_token`, `get_user_access_token`, `refresh_user_access_token`, `get_user_info` | httpx, config |
| `auth/session.py` | File-backed random-id session store: `create_session`, `get_session`, `delete_session`, `LoginSession` model | models.User |
| **Service layer** | | |
| `services/ttyd_manager.py` | `TTYDProcess` + `TTYDManager` — per-tab ttyd/tmux lifecycle, env/tunnel setup, status classifier, history capture, remote launch | config, models |
| `services/session_manager.py` | `ConnectionManager` — active WS connection tracking (subscribe/broadcast patterns) | — |
| `services/workspace_state_policy.py` | Pure stateless policy module: status transitions, review routing, auto-continue, review-profile inference, goal packet | workspace_manager models |
| `services/remote_profiles.py` | `RemoteProfileManager` — CRUD + `~/.ssh/config` Host auto-discovery | pydantic |
| `services/feedback_lessons.py` | `FeedbackLessonStore` — workspace-scored lesson catalog, persistence, retrieval prompts | models |
| **WorkspaceManager mixin package** (19 files, one logical class) | See table below | cross-mixin via `self.*` |
| | `__init__.py` — Composes all mixins into `WorkspaceManager`, public `initialize()` / `shutdown()` | all mixins |
| | `_workspaces.py` — Workspace CRUD + listing | _persistence, _state |
| | `_tasks.py` — Task CRUD, ordering, status mutation helpers | _persistence, _state |
| | `_task_updates.py` — User-initiated task edits, Todo→Queued dispatch, attachment add/remove | _tasks, _attachments, _dispatch |
| | `_sessions.py` — Resident agent session lifecycle (spawn/stop/attach-to-task) + managed tabs | _tasks, ttyd_manager |
| | `_dispatch.py` — Dispatch policies: manual → specific agent, workspace auto-assign, goal packet reviewers | _sessions, _messaging, workspace_state_policy |
| | `_messaging.py` — tmux-based message injection into resident agent terminals + reply parsing | _tmux_queries |
| | `_monitor.py` — Polling monitor loop: tmux pane capture, status inference, auto-continue, late-report suppression, reopen heuristic | _tmux_queries, workspace_state_policy |
| | `_reports.py` — `AgentReport` ingestion, changed-files parse, idempotency, reviewer pipeline routing | _review, _persistence |
| | `_review.py` — Review report ingestion, `review_passed`/`review_failed`/`needs_input` handling, stale-verdict guard | _reports, workspace_state_policy, _dispatch |
| | `_feedback.py` — Feedback lesson generation + catalog storage from completed task journeys | feedback_lessons service, _persistence |
| | `_normalize.py` — Workspace/task JSON in-place migration (schema evolution) | — |
| | `_state.py` — In-memory workspace board state cache, dirty tracking, index | _persistence |
| | `_persistence.py` — JSON on-disk format: index.json, per-workspace state.json, snapshots, atomic writes | _state, _normalize |
| | `_attachments.py` — Task image attachments storage, filename sanitization, upload → workspace storage | _tasks |
| | `_artifacts.py` — Changed-file report rendering, goal packet/REVIEW.md rendering, export file path helpers | — |
| | `_prompts.py` — Prompt templates: agent system context injection, goal packet format, review rubric | — |
| | `_tmux_queries.py` — Low-level tmux pane capture, status text classifier (idle/working/attention/offline), interrupt injection | _constants |
| | `_constants.py` — Timeouts, state machine column definitions, filesystem roots (patch seam for tests) | — |

### Frontend Modules

| Module | Key Responsibilities |
|--------|---------------------|
| **App shell** | |
| `App.vue` | Root: auth gate → Terminal view (TabBar + LayoutSelector + TerminalGridView) vs Agent Workspace view switcher |
| `main.ts` | Vue + Pinia bootstrap |
| `views/LoginView.vue` | Feishu OAuth login page + auth-error rendering |
| **Stores (Pinia)** | |
| `stores/authStore.ts` | Auth state, login/logout, `checkAuth()`, local-network bypass detection |
| `stores/appStore.ts` | App-level UI: active view (terminals vs workspace), theme, sidebar toggle state |
| `stores/terminalStore.ts` | Tab CRUD, layout/pane management (1x1 → 3x3 grid), tab ordering, error state, env preset wiring |
| `stores/workspaceStore.ts` | Workspace CRUD, task board state, dispatch, detail panel, attachments, managed sessions |
| **Terminal components** | |
| `components/TabBar.vue` | Tab creation dialog, tab switching, drag-reorder, rename, duplicate, delete, file-browser for cwd |
| `components/LayoutSelector.vue` | Grid layout picker (1x1 through 3x3) + visual preview |
| `components/TerminalGridView.vue` | CSS Grid renderer for pane slots, pane <-> tab assignment |
| `components/TerminalPane.vue` | Single pane: iframe loading `/api/terminal/proxy/{id}/`, lifecycle, postMessage bridge |
| `components/TerminalView.vue` | Terminal interaction glue: postMessage key dispatch, clipboard-image upload, context-menu, mobile controls bridge |
| `components/MobileControls.vue` | Mobile-specific soft-buttons (Ctrl, Alt, Esc, arrow keys, paste, etc.) |
| **Workspace components** | |
| `components/AgentWorkspaceView.vue` | Task board (5-column drag-drop), task detail panel, report/review rendering, modals for workspace/task/agent/env/lessons management, file browser, clipboard attachments |
| `components/AgentAvatar.vue` | Small agent/caller badge: type icon + name |
| `components/AgentStatusFloatingPanel.vue` | Per-agent floating status chip grid: idle/working/attention/offline |
| `components/LoadingButton.vue` | Generic button with loading + success/error state |
| `components/MarkdownContent.vue` | Markdown rendering (marked + DOMPurify), inline path mention linking |
| `components/EnvPresetManager.vue` | Launch-env preset CRUD, hide/unhide, workspace-level assignment |
| `components/NetworkAccessMenu.vue` | Realtime network status indicator + tunnel / reachability info (system API data) |
| **Composables / utilities** | |
| `composables/useLaunchEnvPresets.ts` | Preset read/edit/hide helpers + `localStorage` cache |
| `composables/usePendingActions.ts` | Pending-action counter + reactive `isPending(key)` helper |
| `utils/taskAbort.ts` | `AbortController` keyed registry with leak-safe cleanup |
| **Types** | |
| `types/index.ts` | All TypeScript interfaces for backend Pydantic schemas (TerminalTab, WorkspaceTask, AgentReport, etc.) |

## State Persistence

| File | Location | Content |
|------|----------|---------|
| `tabs.json` | `~/.claude_hub/tabs.json` | Array of tab configs (id, name, port, shell, cwd, solo_mode, agent_type, created_at, env_preset) |
| `tab_order.json` | `~/.claude_hub/tab_order.json` | Ordered array of tab IDs |
| tmux sessions | tmux server | Session `claude-hub-{id[:8]}` per tab; survives backend restart |
| session store | `~/.claude_hub/sessions.json` | File-backed `LoginSession` dict (keyed by random session_id) — Feishu tokens + user info |
| `launch_env_settings.json` | `~/.claude_hub/launch_env_settings.json` | Agent launch env per tab (tunnel scripts, profile paths) |
| Workspace index | `~/.claude_hub/workspaces/index.json` | ID → workspace metadata mapping |
| Per-workspace state | `~/.claude_hub/workspaces/<id>/state.json` | Full workspace board: tasks, sessions, dispatch plan |
| Per-workspace artifacts | `~/.claude_hub/workspaces/<id>/` | `snapshot.md`, `goal_packet.md`, `REVIEW_*.md`, agent report files |
| Task attachments | `~/.claude_hub/workspaces/<id>/attachments/` | Pasted images / uploaded files referenced by tasks |
| Feedback lessons | `~/.claude_hub/workspaces/<id>/lessons.json` | Scored, context-tagged lessons learned from this workspace's runs |
| Agent env profiles | Per-tab generated scripts under `~/.claude_hub/tunnel/` or tab-specific settings | Proxy scripts, launch env JSON (deleted on tab stop) |
| Backend logs | `~/.claude_hub/logs/backend.log` | Rolling file log (all backend logging mirrored here) |
| layout preference | `localStorage` (browser) | Key `claude_hub_layout_type`, e.g. "2x2" |
| launch env presets | `localStorage` (browser) | Custom/hidden preset state + user-created profiles |
| terminal tab ordering (UI cache) | `localStorage` (browser) | Per-workspace cached order, reconciled with backend on load |

**Lifecycle**:
- Backend startup (lifespan):
  - `TTYDManager._load_state()` from `tabs.json` → `_load_order()` → `start_all_tabs()` (reattaches existing tmux sessions; starts a fresh ttyd for each)
  - `WorkspaceManager.initialize()`: load workspace index → migrate each state via `_normalize` → build in-memory board cache → start monitor loop
- Tab creation: append to `processes` dict → atomically save `tabs.json` → append to `tab_order` → save `tab_order.json`
- Tab deletion: `process.stop(kill_tmux=True)` → remove from dict → atomic rewrite both JSON files
- Workspace/task mutation: update in-memory cache (mark dirty) → `_persistence` layer atomic write + fsync
- Backend shutdown:
  - `TTYDManager.cleanup()`: stop all ttyd processes — keep tmux sessions alive (for persistence across restart)
  - `WorkspaceManager.shutdown()`: cancel monitor loop, flush pending writes, close all background async tasks

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
