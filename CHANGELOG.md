# Changelog

> Each entry corresponds to a merge or significant commit on `main`.
> For detailed bug analysis, see `docs/working-logs/` and `WORKLOG.md`.

## 2026-04-25

### 75f9d1c fix: terminal history replay misalignment with Playwright E2E tests

**核心问题：** 切换 Tab 或刷新页面重连终端时，scrollback 内容丢失、可见屏幕被重复渲染、历史和实时数据交错。

**根因：**
1. `Object.defineProperty(window, 'term', ...)` 拦截器被 ttyd 的 webpack bundle 绕过 — ttyd 在打包时捕获了原生 `Object.defineProperty` 引用，我们的拦截器从未被调用，导致 `hookTerm()`、`replayHistory()` 从未执行
2. 轮询检测到 `window.term` 时，ttyd 已调用 `term.open()` 并写完可见屏幕 — 此时清除 buffer 再只写 scrollback，可见屏幕变空且无新 WS 数据填充

**修复方案 — Phase A/B 双模式回放：**
- **Phase A**（`term.open()` 未调用）：只写 scrollback + `\x1b[NS` Scroll Up 序列把底部行推入 scrollback，让 ttyd WS 填充可见屏幕
- **Phase B**（`term.element` 存在，ttyd 已写完可见屏幕）：清除整个 buffer（`\x1b[H\x1b[2J\x1b[3J`），写入完整终端内容（scrollback + 可见屏幕），丢弃缓冲中 ttyd 的 WS 数据（它是重复的可见屏幕内容）

**关键改动：**
- 用 `setInterval` 轮询替代 `Object.defineProperty` 拦截器来检测 `window.term`
- `hookTerm()` 增加 `term.element` 检查：已存在时直接调用 `replayHistory(term, true)`，否则 hook `term.open()`
- 服务端 `capture-pane` 移除 `-E -1` 参数，返回完整终端内容（scrollback + 可见屏幕）
- `capture_history()` 增加 tmux session 不存在时的空字符串提前返回（ttyd 延迟创建 session）
- 添加 `__claudeHubReplayDone` 标志供测试轮询
- 移除 `if (!historyText) return;` 提前退出（hook/resize-guard 逻辑必须始终运行）

**新增 5 个 Playwright E2E 测试：**
- `test_scrollback_complete` — 200 行历史全部出现在 xterm scrollback
- `test_bottom_rows_preserved` — scrollback 行数与 tmux 一致
- `test_no_duplicate_visible_screen` — 无重复可见屏幕内容
- `test_empty_scrollback` — 空历史时干净加载
- `test_replay_with_active_output` — 历史和实时输出不交错

**CI 修复：**
- 修复 mypy 类型错误（conftest.py 缺类型注解、read_xterm_buffer 返回 Any）
- 添加缺失的 `client` AsyncClient fixture（test_health/test_tabs 需要）
- 添加 `types-requests` dev 依赖
- 移除 CI yaml 中未安装的 `--timeout=120` 标志

**迭代过程（本分支历次提交）：**
- `1cc28ed` 移除 `-J` flag，添加 write buffer 防止历史/实时数据交错
- `9015dac` 移动端键盘弹起 3 层防抖：CSS `100lvh`、xterm `onResize` debounce、`visualViewport` 键盘状态检测
- `1c153f0` 恢复 scrollback-only 回放，修复全量回放导致的可见屏幕重复
- `53a8780` 完整重写为 Phase A/B 模型 + 5 个 Playwright E2E 测试
- `6670033` CI 修复：mypy、client fixture、pytest-timeout

**Files**: terminal.py, ttyd_manager.py, test_terminal_replay.py, conftest.py, ci.yml, pyproject.toml

## 2026-04-13

### cd1e247 fix: preserve terminal scrollback across tab switches
- Tab switching no longer loses scrollback history
- **Files**: terminal.py, ttyd_manager.py, TerminalView.vue

## 2026-04-11

### 07300a6 feat: improve tab bar scrolling experience on mobile
- **Files**: TabBar.vue

### ffaddb2 chore: standardize backend port to 8173
- Consolidate all config, docs, scripts to use port 8173
- **Files**: README.md, config.py, docker/*, docs/DEPLOYMENT.md, scripts/*

### 5ccd61b fix: make backend CI checks pass
- Fix type annotations and import issues for mypy/black/isort
- **Files**: filesystem.py, tabs.py, terminal.py, main.py, ttyd_manager.py, tests/*

### 108108c fix: stabilize frontend lint step in CI
- Fix ESLint config and dependencies for CI
- **Files**: eslint.config.js, package.json

### 5394fea fix: align backend tooling and typing with CI checks
- Add missing type annotations across auth, api, models, services
- **Files**: api/*.py, auth/*.py, models/*.py, services/*.py, pyproject.toml

### 6e2172a fix: keep tmux CI session alive for validation
- **Files**: ci.yml

## 2026-04-10

### cc7682d fix: resolve terminal text selection by disabling tmux mouse mode
- Set `tmux mouse off` — tmux mouse mode intercepted all mouse events, preventing xterm.js native text selection
- Allow browser context menu when text is selected
- **Files**: terminal.py, ttyd_manager.py, TerminalView.vue

### e3f8ab2 fix: enable text selection and copy in terminal, prevent browser context menu
- Remove interfering CSS, add context menu guard for selected text
- **Files**: terminal.py, TerminalView.vue

## 2026-04-09

### 3679463 feat: add cursor agent terminal support
- New `AgentType.CURSOR` — launches user's shell instead of `claude` CLI
- Tab creation supports `agent_type` field (claude/cursor)
- **Files**: tabs.py, schemas.py, ttyd_manager.py, TabBar.vue, terminalStore.ts, types/index.ts, vite.config.ts, start.sh

## 2026-04-02

### 7e33500 fix: support updating commented env vars in start-temp-tunnel.sh
- **Files**: scripts/start-temp-tunnel.sh

## 2026-04-01

### ec55c80 fix: improve mobile terminal scrolling by removing aggressive CSS constraints
- **Files**: TerminalView.vue

### b44ba93 feat: skip auth for local network requests
- Private IPs (10.x, 172.16-31.x, 192.168.x, loopback) bypass Feishu auth
- **Files**: auth.py, dependencies.py, config.py

### c2fd589 feat: add tab rename and fix duplicate tab
- **Files**: TabBar.vue

### df930a0 feat: add layout memory and duplicate tab features
- Persist layout choice in localStorage, add duplicate tab button
- **Files**: TabBar.vue, terminalStore.ts

### 011f481 feat: add open_id whitelist support and improve WebSocket cookie parsing
- Add `AUTH_ALLOWED_OPEN_IDS` config, manually parse WS cookie header (FastAPI Cookie decorator unreliable on WS)
- **Files**: auth.py, dependencies.py, config.py

### 900bdf7 feat: add one-click temp tunnel scripts and update vite config
- `scripts/start-temp-tunnel.sh` — start backend + frontend + Cloudflare Tunnel
- `allowedHosts: true` in Vite config for tunnel support
- **Files**: vite.config.ts, scripts/*

### fbbbd47 feat: add Cloudflare Tunnel support for public hosting
- Cloudflared setup/run scripts, config example
- **Files**: scripts/*, docs/DEPLOYMENT.md

### efc9a70 feat: merge Feishu OAuth authentication and public deployment support
- Full Feishu OAuth 2.0 integration (login/callback/logout/session)
- Email whitelist, Nginx and frp config for public deployment
- DEPLOYMENT.md documentation
- **Files**: api/auth.py, auth/*.py, config.py, models/schemas.py, api/tabs.py, api/terminal.py, docker/*, docs/DEPLOYMENT.md

## 2026-03-27

### Initial: solo mode fix
- Fix solo mode to launch `IS_SANDBOX=1 claude --dangerously-skip-permissions` correctly
- Use `bash -c` wrapper instead of `tmux send-keys`
- Add file logging to `~/.claude_hub/logs/backend.log`
