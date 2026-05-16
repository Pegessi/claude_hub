# Changelog

> Each entry corresponds to a merge or significant commit on `main`.
> For detailed bug analysis, see `docs/working-logs/` and `WORKLOG.md`.

## 2026-05-16

### feat: expand mobile terminal space while typing
- Drive the app shell height from `visualViewport` so the mobile keyboard does not double-shrink the terminal layout
- Enter a compact terminal mode while the keyboard is open, hiding nonessential chrome and tightening tab, pane, and mobile-control spacing
- Move the mobile split-layout shortcuts into a top-bar dropdown so the standalone layout row no longer consumes vertical space on phones
- Keep the mobile terminal tab bar anchored while the keyboard is open and smooth the compact layout plus floating virtual-key panel transitions
- Fold the mobile tab bar without dropping the terminal pane frame so the keyboard transition keeps a continuous border
- Animate mobile top chrome and pane-header collapse so the terminal frame slides with the keyboard instead of jumping into place
- Keep the floating virtual-key toggle pinned to the active viewport bottom during keyboard-open mode
- Coalesce terminal resize messages during mobile keyboard animation so xterm redraws only after the layout settles
- Replace the mobile keyboard folding chrome with a stable compact top bar and app menu so the terminal canvas does not resize when the keyboard opens
- Keep the mobile virtual-key overlay content-sized while tracking the visual viewport, preserving native xterm touch inertia
- Measure the browser's fixed-position keyboard baseline before shifting the mobile virtual-key button, avoiding duplicate upward movement on browsers that already anchor fixed controls to the visual viewport
- Give the mobile Agent Workspace view the same compact shell language as terminal mode, with a sticky workspace summary bar, primary task action, overflow menu, and slimmer agent status chips
- **Files**: App.vue, AgentStatusFloatingPanel.vue, AgentWorkspaceView.vue, LayoutSelector.vue, MobileControls.vue, TabBar.vue, TerminalGridView.vue, TerminalPane.vue, TerminalView.vue

### fix: avoid false pending workspace dispatch
- Treat submitted Claude slash-command output and older prompt echoes as completed sends, so queued workspace tasks are not blocked after a successful `/clear`
- Add regression coverage for the Claude `/clear` output shape that kept the H20 workspace task queued
- **Files**: workspace_manager.py, test_workspaces.py

### fix: replay remote tab tmux history
- Capture scrollback from the remote tmux session for remote tabs so reconnect/history replay includes the agent's actual remote terminal history instead of only the local SSH wrapper screen
- Keep local tmux capture as a fallback when remote SSH capture fails, using non-interactive SSH options to avoid blocking page load
- Add backend coverage for remote history preference, local fallback, and remote capture command construction
- **Files**: ttyd_manager.py, test_ttyd_manager.py

### fix: harden terminal replay hold on Linux CI
- Extend the full-replay hold window and perform a final replay before marking history as complete, so late ttyd initial frames cannot collapse xterm scrollback immediately before E2E assertions
- Verify the xterm buffer contains expected scrollback before publishing replay readiness, with a short post-ready watchdog for late Linux runner redraws
- Normalize styled tmux prompts in terminal E2E comparisons and wait for the expected xterm buffer depth before asserting scrollback state
- **Files**: terminal.py, conftest.py, test_terminal_replay.py

### fix: stabilize terminal replay CI and refresh README
- Replace synchronous browser history preload with an async preload gate before hooking xterm, so Chromium on Linux CI reliably receives tmux history before replay
- Keep full terminal replay writes buffered until ttyd's initial frame stream goes quiet, preventing late frames from collapsing scrollback to only visible rows
- Allow terminal replay E2E tests to bind a temporary backend URL so local validation can avoid the live 8173 service
- Update README, backend package description, and current Agent Workspace screenshot to reflect the workspace-agent, remote-tab, clipboard-image, and validation flows
- **Files**: terminal.py, conftest.py, README.md, backend/README.md, pyproject.toml, agent_workspace_demo.png

### fix: match terminal padding to rendered canvas background
- Compute the light-mode terminal inset color through the same canvas filter used by xterm so the padding matches the rendered terminal surface
- **Files**: TerminalView.vue

### fix: soften embedded terminal edge padding
- Restore a small terminal-colored inset around xterm content so light mode feels less crowded without reintroducing page-colored gutters
- **Files**: TerminalView.vue

### fix: fill embedded terminal viewport edge-to-edge
- Remove ttyd's default embedded terminal padding and stretch the xterm screen/canvas to the pane edges so light mode no longer shows white gutters
- **Files**: TerminalView.vue

### fix: refit terminal canvas after light theme layout changes
- Trigger ttyd/xterm resize from the active iframe after theme, tab, and container-size changes so the terminal canvas fills the pane in light mode
- **Files**: TerminalView.vue

### fix: align compact done task cards
- Prevent crowded task columns from flex-shrinking task cards below their content height
- Make Done task cards an explicit compact single-line surface so titles and status badges stay vertically centered
- **Files**: AgentWorkspaceView.vue

### style: polish workspace and terminal surfaces
- Refine workspace cards, columns, task detail sections, and report timeline to reduce visual noise and clarify hierarchy
- Lighten terminal tabs, layout controls, and active pane treatment while keeping dark/light theme tokens consistent
- Add shared radius and motion tokens for future frontend polish
- **Files**: App.vue, AgentWorkspaceView.vue, TabBar.vue, LayoutSelector.vue, TerminalPane.vue

## 2026-05-15

### fix: reopen completed review tasks from live runtime work
- Treat later live Working activity after the review grace window as a valid Review-to-Working transition for both `ready_for_review` and `completed` reports
- Keep the immediate post-report grace window so a completion report's own terminal output does not reopen the task
- **Files**: workspace_manager.py, test_workspaces.py

### feat: add button-level loading feedback
- Add a reusable loading button component and pending-action helper for frontend interactions
- Show per-control processing feedback for workspace switching, workspace task actions, agent management, follow-up sends, terminal tab creation/duplication/closing, directory browsing, status refresh, login redirect, and logout
- Keep pending state scoped by task, agent, session, tab, or browser action so unrelated controls remain usable
- **Files**: LoadingButton.vue, usePendingActions.ts, AgentWorkspaceView.vue, TabBar.vue, AgentStatusFloatingPanel.vue, LayoutSelector.vue, LoginView.vue

### docs: require branch-based agent development
- Add an agent-facing `AGENTS.md` entrypoint that points to `CLAUDE.md` and forbids direct development on `main`
- Clarify that small fixes, documentation changes, and managed workspace tasks must still use a feature/fix branch or isolated worktree before merging back
- **Files**: AGENTS.md, CLAUDE.md

### feat: make workspace agents manageable
- Rename the workspace agent entry point to agent management and show the existing agent list before the add-agent form
- Add visible delete actions to the agent status strip and management modal, with disabled-state hints while an agent still owns open tasks
- **Files**: AgentWorkspaceView.vue

### fix: enable terminal image paste for Claude tabs
- Reuse the browser-image-to-macOS-clipboard paste bridge for Claude Code tabs as well as Codex tabs, so pasted screenshots can reach the TUI through Ctrl+V
- **Files**: TerminalView.vue

### fix: keep ready reports authoritative
- Keep `ready_for_review` and `completed` reports as the authoritative task state instead of reopening Review tasks from raw terminal Working samples
- Preserve runtime-based Review-to-Working recovery when the assigned terminal shows new Working activity after the review timestamp, covering direct terminal follow-ups
- Add a short grace window after explicit ready reports so the reporting agent's own terminal activity cannot immediately reopen the task
- Add an explicit `working` report when a Review task is continued through the workspace flow so follow-up work has a durable state transition
- Restore tasks whose latest report is ready/completed back to Review during board reconciliation unless the task has later explicit or runtime Working activity
- Make auto-continue prompts semantic: interruption-like idle output asks the agent to continue, while completion-like idle output asks the agent to submit the missing final report
- **Files**: workspace_manager.py, test_workspaces.py

### fix: restore main ci checks
- Keep terminal history full replay buffered briefly after xterm accepts the replay write so late ttyd initial screen frames cannot collapse reconstructed scrollback on Linux CI
- Apply backend Black/isort cleanup for files that were failing formatting/import-order gates
- Fix backend mypy failures that were hidden behind the earlier formatting stop, including terminal status typing, remote workspace path fallback, and TerminalTab test construction
- Relax mypy's untyped-def requirement for tests while keeping production code strict
- **Files**: terminal.py, remote.py, models/__init__.py, ttyd_manager.py, workspace_manager.py, pyproject.toml, test_tabs.py, test_ttyd_manager.py, test_workspaces.py

### feat: support image attachments in workspace tasks
- Let task creation and follow-up instructions accept pasted image attachments from the browser clipboard
- Persist image attachments under the workspace state directory, show previews in task detail, and include attachment file paths in the agent prompt
- Add backend validation for supported image types and attachment size limits, plus test coverage for pasted-image persistence
- **Files**: schemas.py, workspace_manager.py, workspaces.py, test_workspaces.py, AgentWorkspaceView.vue, workspaceStore.ts, types/index.ts

## 2026-05-14

### fix: classify Codex selection prompts as attention
- Treat Codex interactive menus with `Enter to select`, arrow-key navigation, or `Esc to cancel` as Attention instead of Working
- Keep active work detection on interrupt-oriented hints such as `Esc to interrupt` and Claude spinner status lines
- Add backend coverage for Codex selection-menu status classification
- **Files**: ttyd_manager.py, test_ttyd_manager.py

### fix: keep continued review tasks in working
- Prevent board reconciliation from restoring a stale `ready_for_review` report over a later continue transition
- Mark review tasks as Working before sending follow-up text to the agent so tmux submit verification failures cannot leave the board in Review while the agent is active
- Move review tasks back to Working when the assigned agent shows new working runtime activity after the review timestamp, covering direct terminal-tab follow-ups
- Keep `completed` reports in Review unless the task is explicitly continued, even if the terminal has later runtime activity
- Auto-send `please continue` only when an assigned Working task's idle agent shows a recognized interruption such as `API Error: 400 unknown error`
- Add backend coverage for stale review reconciliation, completed-report review stability, direct-tab runtime continuation, interrupted-idle auto-continue, normal-idle suppression, and continue-send failure ordering
- **Files**: workspace_manager.py, test_workspaces.py

### feat: archive completed workspace task records
- Write a per-workspace `task_records/{completed_at}-{task_id}.json` archive whenever a task is marked Done
- Include task/session snapshots, agent reports, an ordered timeline, changed files, validation, risks, and final summary in the archive
- Keep archived task records independent from task deletion so completed work remains reviewable after board cleanup
- **Files**: workspace_manager.py, test_workspaces.py

### fix: reopen review tasks from follow-up send
- Route follow-up sends on review tasks through the task continue API so the board moves the task back to Working immediately
- Preserve generic session sends for non-review tasks
- **Files**: AgentWorkspaceView.vue

### feat: show workspace agent runtime cards
- Add a visible current-workspace agent status strip to Agent Workspace, matching the terminal status panel's dot and pill language
- Show each agent's role/type, runtime text, detail, current task, queued count, target, and quick-open action
- Poll terminal agent status while the workspace view is mounted so the cards reflect live terminal state
- Keep the agent status strip horizontally scrollable on mobile
- **Files**: AgentWorkspaceView.vue

## 2026-05-13

### feat: add remote tab launch support
- Add Local/Remote run targets to the new-tab modal, including remote server selection, remote working directory input and browsing, auto-reconnect, and mobile-friendly scrolling
- Discover remote profiles from `~/.claude_hub/remote_profiles.json` and SSH config `Host` aliases
- Add a remote filesystem listing API over SSH so remote working directories can be browsed before launch
- Launch remote tabs through the local ttyd/tmux layer into SSH, prefer remote tmux persistence when available, and fall back to direct agent startup when remote tmux is missing
- Bootstrap common NVM Node paths before starting Claude or Codex so Merlin machines with non-login shell PATH differences can still find agent CLIs
- Preserve local tab behavior while persisting and duplicating remote launch configuration
- Add backend coverage for remote command construction and shell compatibility
- **Files**: schemas.py, remote_profiles.py, remote.py, tabs.py, ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, types/index.ts

## 2026-05-12

### c3e0c64 feat: color tab indicator dot by agent runtime status
- Bind the per-tab indicator dot to agent status from the store: idle green, working yellow, attention purple, offline gray
- Add a soft glow on working and attention so active or waiting tabs are easier to spot
- Reuse the palette from AgentStatusFloatingPanel for consistency
- **Files**: TabBar.vue

### 3a48945 fix: stop agent status panel from flickering between working and attention
- Replace broad substring scans over the last 18 lines with anchored checks on the bottom 5 lines so historical scrollback no longer drives classification
- Strip ANSI escapes before matching and hashing so cursor blinks stop churning the activity hash; remove the "hash changed → working" heuristic that was the main flicker source
- Drop the `bypass permissions` attention pattern — Claude Code shows it as a permanent footer in bypass mode and was forcing every idle tab into Attention
- Tighten ATTENTION to explicit prompts (`do you want to proceed`, `(y/n)`, `[y/n]`, `press enter to continue`); WORKING keys off `esc to interrupt` / `ctrl+c to interrupt` / `esc to cancel`
- Rename ATTENTION display text to "Agent waiting for input"; IDLE remains "Idle" and is the default fallback
- **Files**: ttyd_manager.py

## 2026-04-28

### de5c9b8 fix: restore terminal cursor position after history replay
- Add tmux cursor coordinates (`cursor_x`, `cursor_y`) to the terminal history API response
- Restore xterm's cursor after initial history replay and idle history resync so the prompt cursor appears in the input line instead of the bottom row
- Add a Playwright regression test that compares xterm cursor coordinates against tmux pane coordinates
- **Files**: terminal.py, ttyd_manager.py, test_terminal_replay.py

### 7b93181 fix: stabilize terminal history while live output is streaming
- Reconcile xterm with tmux history after live output bursts go idle, restoring complete wrapped output that ttyd may skip in the live stream
- Tighten bottom-position detection so idle resync only rewrites the buffer when the user is truly at the bottom
- Preserve user history views while scrolling, including near-bottom views that show both older history and new output
- Add Playwright coverage for touch/wheel scroll alignment, wrapped live output continuity, and near-bottom resync protection
- **Files**: terminal.py, test_terminal_replay.py

### 81cb44c fix: persist tab order updates
- Persist drag-and-drop tab ordering so refreshing the web UI keeps the user's custom tab order
- Add backend coverage for saving and returning ordered tab lists
- Add `.agent_office/` to `.gitignore` for local workflow artifacts
- **Files**: .gitignore, tabs.py, test_tabs.py

## 2026-04-27

### c379b9f feat: add codex backend solo mode
- Add `AgentType.CODEX` and launch Codex tabs with the `codex` CLI by default
- Add Codex solo mode using `codex --ask-for-approval never --sandbox workspace-write`
- Extend the new-tab modal to choose Claude, Codex, or Terminal backends, with solo mode available for Claude and Codex
- Add backend tests for Codex command construction and tmux reattach behavior
- **Files**: schemas.py, ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, types/index.ts

### 31af616 fix: restore ci checks after codex backend merge
- Apply black formatting to `ttyd_manager.py`
- Add the missing `MonkeyPatch` type annotation for backend mypy
- Avoid the frontend ESLint `no-undef` error from the browser `EventListener` type alias
- **Files**: ttyd_manager.py, test_ttyd_manager.py, App.vue

### 5609dbf ci: update uv setup and split replay tests
- Update GitHub Actions to use `astral-sh/setup-uv@v7` instead of the stale `0.5.x` version selector
- Keep terminal replay tests in the dedicated Playwright job and exclude them from the generic backend pytest job
- **Files**: ci.yml

### be03355 fix: stabilize terminal replay in ci
- Use full terminal replay after `term.open()` to avoid Ubuntu headless xterm scrollback loss during CI
- **Files**: terminal.py

### 40702ad fix: run codex solo mode without sandbox limits
- Change Codex solo mode to launch with `codex --ask-for-approval never --sandbox danger-full-access`
- Update the Codex solo mode UI description and command construction test
- **Files**: ttyd_manager.py, test_ttyd_manager.py, TabBar.vue

## 2026-04-26

### feat: mobile UX improvements — viewport sync, key reliability, combo keys, inertial scroll

**4 个移动端体验问题修复：**

1. **键盘弹出时视口错乱** — 添加 `visualViewport` API 监听，键盘弹出时设置 `--keyboard-height` CSS 变量，App 容器和 MobileControls 自动适配
2. **虚拟按键切换 Tab 后失效** — 添加 terminal-ready 信号（iframe→parent postMessage）+ 按 key 队列缓存，Tab 切换后自动 flush
3. **缺少组合键** — 重组虚拟键盘布局：移除 PgUp/PgDn，加入方向键到主行，新增 Ctrl+C/D/L/A/E 和 Shift+Tab 快捷按钮，Ctrl/Shift 粘滞修饰键支持 Ctrl+任意字母
4. **终端历史滚动无惯性** — 通过阅读 xterm.js 源码定位根因并修复（详见下方）

**惯性滚动修复（6 次迭代）：**

迭代过程中发现三个杀死惯性滚动的机制：
- xterm 的 `handleTouchMove` 手动设 `scrollTop += delta`（替换浏览器原生滚动，无惯性）
- xterm 的 `_innerRefresh` 每帧重置 `scrollTop = ydisp * rowHeight`（行对齐，打断惯性）
- xterm 的 `.xterm-screen` 层遮住 `.xterm-viewport`，触摸事件到不了 viewport 元素

最终修复（3 层方案）：
- CSS: `.xterm-screen { pointer-events: none }` 让触摸穿透到 `.xterm-viewport`
- JS: `term._core.viewport.handleTouchMove` → no-op，阻止 xterm 手动设 scrollTop
- JS: 拦截 `_innerRefresh`，触摸+fling 期间跳过 scrollTop 重置

关键发现：viewport 对象在 `term._core.viewport`（非 `term.viewport`），`document.body` 在脚本执行时为 null（需用 `document.documentElement`）

**改动文件：**
- `backend/claude_hub/api/terminal.py` — 注入 CSS（pointer-events, -webkit-overflow-scrolling）+ JS（触摸穿透、handleTouchMove no-op、_innerRefresh hook、terminal-ready postMessage、Ctrl+字母/Shift+Tab 编码）
- `frontend/src/App.vue` — visualViewport 同步 + `--keyboard-height` CSS 变量
- `frontend/src/components/MobileControls.vue` — 重组键盘布局 + 快捷按钮 + Ctrl/Shift 粘滞修饰 + 自动释放
- `frontend/src/components/TerminalView.vue` — terminal-ready 信号 + key 队列 + Ctrl+字母/Shift+Tab 处理

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
