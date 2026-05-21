# Claude Hub - Project Conventions

## Overview
Claude Terminal Hub is a web-based persistent Claude terminal service with tabbed interface.

## Tech Stack
- **Frontend**: Vue 3 (Composition API) + TypeScript + Vite + Pinia
- **Backend**: Python 3.11+ + FastAPI + WebSocket + uv
- **Terminal**: ttyd + tmux for terminal persistence
- **Package Manager**: pnpm (frontend), uv (backend)

## Development Workflow

### Frontend
- Run dev server: `cd frontend && pnpm dev`
- Build: `cd frontend && pnpm build`
- Lint: `cd frontend && pnpm lint`

### Backend
- Run dev server: `cd backend && uv run uvicorn claude_hub.main:app --reload`
- Install dependencies: `cd backend && uv sync --dev`
- Run tests: `cd backend && uv run pytest`

### Logs
- Backend logs are output to both console and file: `~/.claude_hub/logs/backend.log`

## Project Structure
```
claude_hub/
├── frontend/          # Vue 3 frontend application
├── backend/           # FastAPI backend application
├── docker/            # Docker configuration
├── docs/              # Documentation
│   └── working-logs/  # Development work logs (system overview, design, key issues)
└── .github/           # GitHub Actions workflows
```

## Development Branch Workflow
For all new feature development, bug fixes, UI changes, tests, documentation
changes, and managed workspace tasks:
1. **Start from clean `main`**: fetch/sync first, and do not edit directly on
   `main`.
2. **Create an isolated worktree with a new branch**:
   `git worktree add ../claude_hub-<slug> -b feat/your-feature-name main`.
3. **Develop inside that worktree**. Do not reuse another task's worktree or the
   shared main checkout for new work.
4. **For frontend changes**, run a dedicated dev/review server from that
   worktree on its own port. After the user confirms the result, or after the
   validation window is complete, stop that debug service before merging or
   leaving the task.
5. **Commit your changes** using conventional commits.
6. **Validate the branch** with tests/builds appropriate to the touched files.
7. **Update CHANGELOG.md** with the merge entry summary and key files for
   meaningful shipped changes.
8. **Merge into `main` after validation and review/approval**, then push
   `main`.

A user request to merge or push means finish the worktree branch-to-main flow
after validation; it is not permission to skip the worktree branch.

## Commit Convention
Use conventional commits:
- `feat: new feature`
- `fix: bug fix`
- `docs: documentation`
- `style: formatting`
- `refactor: code refactoring`
- `test: testing`
- `chore: maintenance`
- `ci: CI/CD related changes`

## CI Pipeline
CI runs automatically on all pushes to `main` and all pull requests targeting `main`. It includes:
- **Backend**: black, isort, mypy, pytest
- **Frontend**: ESLint, type check, build
- **ttyd**: Verify tmux and ttyd installation and basic functionality

## Working Logs
Significant development work should be documented in `docs/working-logs/` with the format `YYYY-MM-DD-topic.md`. Each log should include:
- **System Overview**: High-level architecture and component descriptions
- **Module Design**: Key modules and their responsibilities
- **Key Issues/Pitfalls**: Lessons learned, bugs encountered, and solutions

## Reference Docs
- **`ARCHITECTURE.md`** — Deep system architecture: data flows, module dependencies, key design decisions, terminal replay model
- **`CHANGELOG.md`** — MR-level change history: each merge to main with summary and affected files
- **`WORKLOG.md`** — Bug troubleshooting reference: symptom → root cause → fix → lesson
- **`docs/working-logs/`** — Detailed development logs per topic

## Terminal History Replay (Quick Reference)
When modifying `terminal.py` or `ttyd_manager.py`, understand the Phase A/B model:
- **Phase A**: `term.open()` not yet called → write scrollback only, use SU escape to push bottom rows into scrollback, leave visible screen blank for ttyd WS data
- **Phase B**: `term.open()` already called (element attached) → clear entire buffer, write full content, discard buffered WS data (would duplicate screen)
- **Write buffer**: `term.write()` is overridden during replay; live WS data is buffered and flushed after history write completes (5s safety timeout)
- Backend: `capture-pane -p -e -S -100000` returns scrollback + visible screen (no `-J` flag)

## Common Dev Scenarios
| Task | Key Files |
|------|-----------|
| Add new API endpoint | `api/*.py` (add route), `models/schemas.py` (add model), `api/__init__.py` (register router) |
| Change terminal rendering | `api/terminal.py` (injected CSS/JS), `services/ttyd_manager.py` (tmux config) |
| Change auth logic | `auth/dependencies.py` (guards), `auth/session.py` (session store), `api/auth.py` (routes), `config.py` (whitelist) |
| Add frontend component | `components/*.vue`, register in parent, add types to `types/index.ts`, add store logic if needed |
| Change layout/pane system | `stores/terminalStore.ts`, `LayoutSelector.vue`, `TerminalGridView.vue` |

## Dev Pitfalls
- **Pinia reactivity**: State (`ref`) and getters (`computed`) MUST use `storeToRefs()`. Actions can be destructured directly.
- **System proxy**: Backend clears proxy env vars at import in `terminal.py`. If `curl`/debugging fails with 502, add `--noproxy '*'`.
- **ttyd subprotocol**: WebSocket must use subprotocol `"tty"`. Both `accept(subprotocol="tty")` and `connect(subprotocols=["tty"])` are required.
- **httpx proxy**: Responses are auto-decompressed. Strip `content-encoding` header before forwarding to client.
- **Vite WS proxy**: Requires `ws: true` in proxy config entry for WebSocket upgrade.
- **WS cookie**: FastAPI `Cookie` decorator doesn't work reliably on WebSocket. Parse `websocket.headers["cookie"]` manually.
- **tmux mouse off**: Must keep `tmux set -g mouse off` — mouse mode intercepts all drag events, breaking xterm.js text selection.

## Debugging with Playwright
Playwright is installed in the backend venv and can simulate mobile touch events for debugging terminal UI issues (injected JS/CSS, scroll behavior, touch handling). Usage patterns:

```python
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
browser = p.chromium.launch(headless=True)
page = browser.new_page(
    viewport={'width': 390, 'height': 844},
    is_mobile=True,
    has_touch=True
)

# Listen for JS errors
errors = []
page.on('pageerror', lambda err: errors.append(str(err)))

page.goto('http://localhost:8173/api/terminal/proxy/<tab_id>/', timeout=10000)
page.wait_for_timeout(5000)  # wait for ttyd to load

# Check DOM state and CSS properties
result = page.evaluate('''() => {
  var vp = document.querySelector('.xterm-viewport');
  var vpObj = window.term._core.viewport;  // NOT window.term.viewport
  return { scrollTop: vp.scrollTop, overflowY: getComputedStyle(vp).overflowY };
}''')

# Simulate touch swipe via CDP (Playwright touchscreen.tap is full touch+release only)
cdp = page.context.new_cdp_session(page)
cdp.send('Input.dispatchTouchEvent', {
    'type': 'touchStart',
    'touchPoints': [{'x': cx, 'y': cy, 'id': 0}]
})
for i in range(1, 11):
    cdp.send('Input.dispatchTouchEvent', {
        'type': 'touchMove',
        'touchPoints': [{'x': cx, 'y': cy - 20*i, 'id': 0}]
    })
cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
```

Key notes:
- xterm viewport object is at `term._core.viewport`, not `term.viewport`
- CDP `Input.dispatchTouchEvent` simulates real touch events (unlike `dispatchEvent`)
- `document.body` is null when injected scripts run — use `document.documentElement`
- Playwright headless doesn't simulate browser inertial scroll — real device testing still needed for inertia verification

## gstack
Use /browse from gstack for all web browsing. Never use mcp__claude-in-chrome__* tools.
Available skills: /office-hours, /plan-ceo-review, /plan-eng-review, /plan-design-review,
/design-consultation, /design-shotgun, /design-html, /review, /ship, /land-and-deploy,
/canary, /benchmark, /browse, /connect-chrome, /qa, /qa-only, /design-review,
/setup-browser-cookies, /setup-deploy, /retro, /investigate, /document-release, /codex,
/cso, /autoplan, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
