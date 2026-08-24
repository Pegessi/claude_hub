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
- Use the Task Graph / TaskMailbox ([docs/TASK_GRAPH.md](docs/TASK_GRAPH.md)) so
  parent Tasks wait/ACK subtree events on `task:<task_id>` cursors
  (`claude-hub task tree/events/wait/ack/followup/start`). Worker and reviewer
  agents are ordinary Task session assignments; the optional Resident is an
  independent long-running agent (not a mailbox consumer).
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
uv run claude-hub workspace summary <WORKSPACE_ID>          # typed board summary
uv run claude-hub workspace docs <WORKSPACE_ID>             # Markdown docs + snapshot path
uv run claude-hub task list <WORKSPACE_ID>
uv run claude-hub task create <WORKSPACE_ID> --title "Fix bug" --prompt "..."
uv run claude-hub task status <TASK_ID> --workspace-id <WORKSPACE_ID>
uv run claude-hub task start <TASK_ID>
uv run claude-hub task send <WORKSPACE_ID> <TASK_ID> --message "also add a test"
uv run claude-hub task continue <TASK_ID> --message "keep going"
uv run claude-hub task abort <TASK_ID> --reason "superseded"
uv run claude-hub agent list <WORKSPACE_ID>
uv run claude-hub agent status <WORKSPACE_ID>
uv run claude-hub agent create <WORKSPACE_ID> --agent-type claude
uv run claude-hub session status <SESSION_ID>
uv run claude-hub session send <SESSION_ID> --message "continue"
uv run claude-hub session report <SESSION_ID> --state working --message "..."
uv run claude-hub lessons list <WORKSPACE_ID> --query terminal
uv run claude-hub lessons get <WORKSPACE_ID> <LESSON_ID>
uv run claude-hub --json task tree <WORKSPACE_ID> [<PARENT_TASK_ID>]
uv run claude-hub --json task events <WORKSPACE_ID> <TASK_ID> --subtree --since-sequence 0
uv run claude-hub --json task wait <WORKSPACE_ID> <TASK_ID> --subtree --since-sequence 0
uv run claude-hub --json task ack <WORKSPACE_ID> <TASK_ID> <SEQUENCE>
uv run claude-hub --json task followup <WORKSPACE_ID> <TASK_ID> --message "..."
```

Loopback requests bypass auth, so a local backend needs no token; commands exit
non-zero on API errors. Global options: `--base-url` / `CLAUDE_HUB_URL`,
`--token` / `CLAUDE_HUB_TOKEN`, `--cookie`, `--json`, `--config`, and `-v` /
`--verbose` (logs each request URL to stderr).

### Feishu interactive cards

These helpers are for an **external agent that is itself a Feishu bot** — it
sends cards to a human and receives the `card.action.trigger` callback in the
same process, then drives Hub through this CLI. Hub is not in the Feishu loop, so
it ships no sender, no token store, and no callback endpoint — just two
stateless, IO-free helpers that print JSON on stdout:

- `feishu build-card` — build a card's JSON (the agent sends it to Feishu itself).
- `feishu parse-action` — parse a raw `card.action.trigger` callback into a
  normalized `{token, action, form, operator_id, chat_id}` decision.

```bash
# Build a card; the printed `token` is embedded in every control so the later
# callback can be correlated. The agent POSTs `card` to Feishu's CreateMessage.
uv run claude-hub feishu build-card --kind approval --title "Deploy v2?" --body "All checks pass."
#  → {"kind":"approval","token":"x9…","card":{…}}

uv run claude-hub feishu build-card --kind needs_input --title "Release note?" \
    --body "One line" --field-name note
uv run claude-hub feishu build-card --kind plan_confirm --title T --body "..."

# Display-only cards render live workspace data and carry no token:
uv run claude-hub feishu build-card --kind status --workspace-id <WS>
uv run claude-hub feishu build-card --kind task   --workspace-id <WS> --task-id <T>
uv run claude-hub feishu build-card --kind overview --workspace-id <WS>
uv run claude-hub feishu build-card --kind task_detail --workspace-id <WS> --task-id <T>

# In the bot's card.action.trigger handler, parse the callback (arg or stdin).
# Match the returned `token` against the card you sent. Foreign cards → exit 1
# and a `null` line, so you can branch on the exit code.
uv run claude-hub feishu parse-action "$CALLBACK_JSON"
echo "$CALLBACK_JSON" | uv run claude-hub feishu parse-action
#  → {"token":"x9…","action":"approve","form":{},"operator_id":"ou_…","chat_id":"oc_…"}
```

Interactive kinds (`approval`, `needs_input`, `plan_confirm`, `task_detail`)
embed a correlation token; display-only kinds such as `status`, `overview`, and
`reports` carry none. See
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

- [docs/TASK_GRAPH.md](docs/TASK_GRAPH.md): Task Graph / TaskMailbox agent guide
  (`claude-hub task` primary)
- [CLAUDE.md](CLAUDE.md): project conventions and development workflow
- [CHANGELOG.md](CHANGELOG.md): merge-level change history
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): auth and public deployment setup
- [docs/working-logs/](docs/working-logs): implementation notes and incident logs

## License

MIT
