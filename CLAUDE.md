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
For all new feature development:
1. **Create a new branch**: `git checkout -b feat/your-feature-name`
2. **Develop your feature** on the branch
3. **Commit your changes** using conventional commits
4. **Push the branch** and create a pull request (or merge locally)
5. **Wait for CI to pass**
6. **Merge into main**

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

## gstack
Use /browse from gstack for all web browsing. Never use mcp__claude-in-chrome__* tools.
Available skills: /office-hours, /plan-ceo-review, /plan-eng-review, /plan-design-review,
/design-consultation, /design-shotgun, /design-html, /review, /ship, /land-and-deploy,
/canary, /benchmark, /browse, /connect-chrome, /qa, /qa-only, /design-review,
/setup-browser-cookies, /setup-deploy, /retro, /investigate, /document-release, /codex,
/cso, /autoplan, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn.
