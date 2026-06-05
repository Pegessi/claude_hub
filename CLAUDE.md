# Claude Hub - Agent Entry Guide

> `AGENTS.md` and `CLAUDE.md` must remain identical. Update both files in the
> same commit. This guide is the short, always-read entry point; detailed design
> history and debugging recipes live in the linked docs.

## Project

Claude Terminal Hub is a web-based persistent terminal service with a tabbed
interface and a workspace orchestration layer that drives multiple agents
(Claude / Cursor / plain Terminal) against the same workspace.

## Stack

- **Frontend**: Vue 3 (Composition API) + TypeScript + Vite + Pinia
- **Backend**: Python 3.11+ + FastAPI + WebSocket + uv
- **Terminal**: ttyd + tmux for terminal persistence
- **Package managers**: pnpm for frontend, uv for backend

## Mandatory Workflow

Do not develop directly on `main`. For feature work, bug fixes, UI changes,
tests, documentation changes, and managed workspace tasks:

1. Start from clean `main`: fetch/sync first.
2. Create an isolated worktree and branch:
   `git worktree add ../claude_hub-<slug> -b feat/your-feature main`.
3. Work only inside that task worktree.
4. For frontend changes, run a dedicated dev/review server from that worktree
   on its own port and stop it before merging or leaving the task.
5. Commit changes with conventional commits.
6. Run validation appropriate to the touched files.
7. Update `CHANGELOG.md` for meaningful shipped changes.
8. Merge into `main` only after validation and review/approval, then push
   `main`.

A user request to merge or push means complete this branch-to-main flow. It is
not permission to skip the worktree branch.

## Commit And CI

Use conventional commits: `feat:`, `fix:`, `docs:`, `style:`, `refactor:`,
`test:`, `chore:`, or `ci:`.

CI runs on pushes to `main` and pull requests targeting `main`:

- Backend: black, isort, mypy, pytest
- Frontend: ESLint, type check, build
- ttyd: tmux/ttyd installation and basic functionality

## Protected Local State

Do not delete, reset, or overwrite untracked or unrelated files. Treat local
noise such as `.cursor/`, `tasks/`, `tmp_remote_media/`, captured logs
(`*.log`), and ad-hoc probe scripts (`abl_*.json`, `nccl_*`,
`pure_pytorch_*`, `run_*.sh`, `summarize_mem*.py`, `sweep_*.sh`) as protected
unless the user explicitly asks to modify them.

## Commands

Frontend:

- Dev server: `cd frontend && pnpm dev`
- Build: `cd frontend && pnpm build`
- Lint: `cd frontend && pnpm lint`

Backend:

- Dev server: `cd backend && uv run uvicorn claude_hub.main:app --reload`
- Install dependencies: `cd backend && uv sync --dev`
- Tests: `cd backend && uv run pytest`

Logs:

- Backend logs: `~/.claude_hub/logs/backend.log`

## Project Map

```text
claude_hub/
├── frontend/          # Vue 3 frontend application
├── backend/           # FastAPI backend application
├── docker/            # Docker configuration
├── docs/              # Documentation
│   └── working-logs/  # Detailed design and debugging logs
└── .github/           # GitHub Actions workflows
```

## Task Navigation

Use this table before reading broad context. Load only the docs relevant to the
task.

| Task shape | Read first |
| --- | --- |
| Architecture / data flow | `ARCHITECTURE.md` |
| Recent shipped behavior | `CHANGELOG.md` |
| Bug symptom history | `WORKLOG.md` |
| Workspace task lifecycle, reports, Goal Packet | `docs/working-logs/2026-05-23-workspace-goal-packet-v1.md` |
| Workspace state / review routing policy | `docs/working-logs/2026-05-23-state-machine-assessment.md` |
| Autonomous mode and evaluator loop | `docs/working-logs/2026-05-26-autonomous-mode-v1.md` |
| Review profiles and reviewer evidence | `docs/working-logs/2026-05-26-review-profiles-v1.md` |
| Auto Mode sub-agent orchestration | `docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md` |
| Long-running autonomous timing / heartbeat | `docs/working-logs/2026-06-04-auto-mode-observability.md` |
| Feedback harness / lesson retrieval plan | `docs/working-logs/2026-06-06-feedback-harness-plan.md` |
| Terminal replay, ttyd, tmux, Playwright terminal debug | `docs/terminal-debugging.md` |
| Deployment | `docs/DEPLOYMENT.md` |

## Common Edit Areas

| Task | Key files |
| --- | --- |
| Add API endpoint | `backend/claude_hub/api/*.py`, `backend/claude_hub/models/schemas.py`, `backend/claude_hub/api/__init__.py` |
| Change terminal rendering | `backend/claude_hub/api/terminal.py`, `backend/claude_hub/services/ttyd_manager.py`, `docs/terminal-debugging.md` |
| Change auth | `backend/claude_hub/auth/dependencies.py`, `backend/claude_hub/auth/session.py`, `backend/claude_hub/api/auth.py`, `backend/claude_hub/config.py` |
| Add frontend component | `frontend/src/components/*.vue`, parent component, `frontend/src/types/index.ts`, relevant store |
| Change layout/pane system | `frontend/src/stores/terminalStore.ts`, `frontend/src/components/LayoutSelector.vue`, `frontend/src/components/TerminalGridView.vue` |
| Change workspace orchestration | `backend/claude_hub/services/workspace_manager.py`, `backend/claude_hub/services/workspace_state_policy.py`, `backend/claude_hub/api/workspaces.py`, `frontend/src/stores/workspaceStore.ts` |

## Agent Types

- `claude`: default Claude Code CLI session.
- `cursor`: Cursor CLI (`agent`); always runs in YOLO mode by default and the
  solo-mode toggle does not apply.
- `terminal`: plain user-shell session for free-form interactive work.

## Pitfalls

- **Pinia reactivity**: use `storeToRefs()` for state refs and computed getters.
  Actions can be destructured directly.
- **System proxy**: backend clears proxy env vars at import in `terminal.py`.
  If `curl`/debugging fails with 502, add `--noproxy '*'`.
- **ttyd subprotocol**: WebSocket must use subprotocol `tty`; both
  `accept(subprotocol="tty")` and `connect(subprotocols=["tty"])` are required.
- **httpx proxy**: responses are auto-decompressed. Strip `content-encoding`
  before forwarding to the client.
- **Vite WS proxy**: WebSocket proxy entries require `ws: true`.
- **WS cookie**: FastAPI `Cookie` is unreliable on WebSocket. Parse
  `websocket.headers["cookie"]` manually.
- **tmux mouse off**: keep `tmux set -g mouse off`; mouse mode intercepts drag
  events and breaks xterm.js text selection.

## Working Logs

Significant development work should add a focused log under
`docs/working-logs/YYYY-MM-DD-topic.md` with:

- System overview
- Module design
- Key issues / pitfalls

Keep root agent files short. When a lesson becomes stable, add a navigation cue
here and put the details in a working log, `REVIEW.md`, tests, or policy code.
