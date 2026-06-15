# Claude Hub

Persistent web dashboard for terminal-based AI agents.

Claude Hub keeps Claude, Codex, and shell sessions alive in `tmux`, serves them
through `ttyd`, and adds an Agent Workspace board for queueing tasks, dispatching
resident agents, sending follow-up instructions, and reviewing progress reports.

![Claude Hub Agent Workspace](docs/screenshots/agent_workspace_demo.png)

## What It Does

### Agent Workspace

- Manage one or more local or remote workspaces.
- Keep resident Claude or Codex agents running in persistent terminal tabs.
- Add tasks with optional pasted image attachments.
- Dispatch tasks to a specific agent or let the workspace choose the available agent.
- Track task state across Todo, Queued, Working, Review, and Done columns.
- Send follow-up messages from the task detail panel without leaving the board.
- Record agent reports with changed files, validation, risks, and review status.
- Preserve completed task records under the workspace state directory.

### Persistent Terminals

- Create Claude, Codex, or shell terminal tabs backed by `ttyd` and `tmux`.
- Switch, duplicate, rename, delete, and drag-reorder tabs.
- Use multi-pane layouts from single pane up to 3x3.
- Keep scrollback and visible terminal state aligned across reloads.
- Open remote SSH-backed tabs with optional remote working directories.
- Monitor best-effort agent runtime state: idle, working, attention, or offline.
- Paste local clipboard images into Claude or Codex terminal UIs through the
  browser-to-macOS clipboard bridge.
- Use mobile controls for common terminal shortcuts, including Ctrl+V.
- Toggle dark and light themes.

### Deployment And Access

- Optional Feishu OAuth for public deployments.
- Open ID and email whitelist support.
- Cloudflare Tunnel helper scripts for temporary or persistent public URLs.
- Local-network requests can bypass auth for trusted development use.

## Tech Stack

- **Frontend**: Vue 3, TypeScript, Vite, Pinia
- **Backend**: Python 3.11+, FastAPI, WebSocket, uv
- **Terminal runtime**: ttyd, tmux
- **Validation**: backend pytest/mypy/black/isort, frontend ESLint/type-check/build,
  and Playwright terminal replay E2E tests

## Requirements

- Python 3.11+
- Node.js 20+
- pnpm
- uv: <https://docs.astral.sh/uv/getting-started/installation>
- tmux
- ttyd
- cloudflared, optional for public tunnel scripts

## Quick Start

Install or verify local dependencies:

```bash
./setup.sh
```

Start backend and frontend together:

```bash
./start.sh
```

Open the app:

- Frontend: <http://localhost:5173>
- Backend API: <http://localhost:8173>
- API docs: <http://localhost:8173/docs>

Stop services with `Ctrl+C` in the `start.sh` terminal, or run:

```bash
./stop.sh
```

## Manual Development

Backend:

```bash
cd backend
uv sync --dev
uv run uvicorn claude_hub.main:app --reload --host 0.0.0.0 --port 8173
```

Frontend:

```bash
cd frontend
pnpm install
pnpm dev
```

## Public Tunnel

Start backend, frontend, and a temporary Cloudflare Tunnel URL:

```bash
./scripts/start-temp-tunnel.sh
```

The script starts all services, prints a `trycloudflare.com` URL, updates `.env`
with the public URL, and reminds you to update Feishu redirect settings when
auth is enabled.

For persistent deployment options, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## CLI

`claude-hub` is a terminal client over the Agent Workspace REST API, useful for
both humans and agents driving the hub from a shell.

```bash
cd backend
uv sync                       # installs the `claude-hub` entry point
uv run claude-hub --help      # or: uv run python -m claude_hub.cli --help
```

Configuration precedence: flags > env > config file > defaults.

```bash
export CLAUDE_HUB_URL=http://127.0.0.1:8173   # default
export CLAUDE_HUB_TOKEN=...                    # only for remote/non-loopback backends
# or ~/.config/claude-hub/config.toml:  [default] \n base_url = "..." \n token = "..."
```

Common commands (add `--json` to any for machine-readable output):

```bash
uv run claude-hub workspace list
uv run claude-hub workspace create --name demo --path /path/to/repo
uv run claude-hub workspace board <WORKSPACE_ID>            # alias: status
uv run claude-hub task list <WORKSPACE_ID>
uv run claude-hub task create <WORKSPACE_ID> --title "Fix bug" --prompt "..."
uv run claude-hub task start <TASK_ID>
uv run claude-hub task send <WORKSPACE_ID> <TASK_ID> --message "also add a test"
uv run claude-hub task continue <TASK_ID> --message "keep going"
uv run claude-hub task abort <TASK_ID> --reason "superseded"
uv run claude-hub agent list <WORKSPACE_ID>
uv run claude-hub agent create <WORKSPACE_ID> --type claude
uv run claude-hub session send <SESSION_ID> --message "continue"
uv run claude-hub session report <SESSION_ID> --state working --message "..."
uv run claude-hub lessons list <WORKSPACE_ID> --query terminal
uv run claude-hub lessons get <WORKSPACE_ID> <LESSON_ID>
```

Loopback requests bypass auth, so a local backend needs no token; commands exit
non-zero on API errors. Global options: `--base-url` / `CLAUDE_HUB_URL`,
`--token` / `CLAUDE_HUB_TOKEN`, `--cookie`, `--json`, `--config`, and `-v` /
`--verbose` (logs each request URL to stderr).

### Feishu interactive cards

The `feishu` group lets an agent ask a human for a decision over Feishu and
block until they answer — push an interactive card to a chat, then long-poll
for the click. The Feishu long-connection bot (`claude-hub feishu-bot`) must be
running to relay the human's choice back; set `$FEISHU_APP_ID` /
`$FEISHU_APP_SECRET` (or pass `--app-id`/`--app-secret`).

```bash
# Alias a chat id so agents don't paste oc_… everywhere.
uv run claude-hub feishu bind ops --chat-id oc_abc123
uv run claude-hub feishu bindings            # list   /  feishu unbind ops

# Ask a human and BLOCK until they click (the agent's main use):
uv run claude-hub --json feishu send-card --kind approval --to ops \
    --title "Deploy v2?" --body "All checks pass." --wait --timeout 120
#  → {"status":"resolved","action":"approve","operator_id":"ou_…"}  (or "timeout")

# Free-text reply, custom field name, or a plan confirmation:
uv run claude-hub --json feishu send-card --kind needs_input --to ops \
    --title "Release note?" --body "One line" --field-name note --wait
uv run claude-hub feishu send-card --kind plan_confirm --to ops --title T --body "..." --wait

# Display-only cards render live workspace data (--wait is rejected for these):
uv run claude-hub feishu send-card --kind status --to ops --workspace-id <WS>
uv run claude-hub feishu send-card --kind task   --to ops --workspace-id <WS> --task-id <T>

uv run claude-hub feishu send-card --kind approval --title T --body B --dry-run  # print JSON, don't send
uv run claude-hub --json feishu result <TOKEN>                                   # poll a decision
```

Interactive kinds (`approval`, `needs_input`, `plan_confirm`) embed a correlation
token; display kinds (`status`, `task`) carry none. See
`docs/working-logs/2026-06-16-feishu-card-cli.md` for the design and a smoke test.

## Authentication

Feishu OAuth is optional. For public access, configure either an Open ID
whitelist or an email whitelist:

```env
AUTH_ALLOWED_OPEN_IDS=ou_xxx
AUTH_ALLOWED_EMAILS=user@example.com
```

Open ID whitelisting is preferred when available. Full setup details are in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Validation

Run the same checks used by CI:

```bash
cd backend
uv run black --check .
uv run isort --check .
uv run mypy .
uv run pytest -xvs --ignore=tests/test_terminal_replay.py
uv run pytest tests/test_terminal_replay.py -v
```

```bash
cd frontend
pnpm run lint:check
pnpm run build
```

The terminal replay tests require `tmux`, `ttyd`, and Playwright Chromium.

## Project Structure

```text
claude_hub/
├── frontend/          # Vue application
├── backend/           # FastAPI application and terminal/workspace services
├── docs/              # Deployment notes, screenshots, and working logs
├── scripts/           # Local startup and tunnel helpers
├── docker/            # Docker configuration
└── .github/           # GitHub Actions workflows
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the mandatory development workflow
(isolated worktrees, conventional commits, validation steps, and CHANGELOG
rules). **No change — even a small one — should be made directly on `main`.**

## Reference Docs

- [CLAUDE.md](CLAUDE.md): project conventions and development workflow
- [CHANGELOG.md](CHANGELOG.md): merge-level change history
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): auth and public deployment setup
- [docs/working-logs/](docs/working-logs): implementation notes and incident logs

## License

MIT
