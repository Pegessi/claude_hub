#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export CLAUDE_HUB_E2E_HOME="${CLAUDE_HUB_E2E_HOME:-/tmp/claude-hub-board-lazyload-e2e}"
export CLAUDE_HUB_E2E_REPO="${CLAUDE_HUB_E2E_REPO:-$CLAUDE_HUB_E2E_HOME/repo}"
export CLAUDE_HUB_E2E_BACKEND="${CLAUDE_HUB_E2E_BACKEND:-$ROOT/backend}"
export CLAUDE_HUB_E2E_PORT="${CLAUDE_HUB_E2E_PORT:-19177}"
export CLAUDE_HUB_E2E_FRONTEND_PORT="${CLAUDE_HUB_E2E_FRONTEND_PORT:-5177}"
cd "$ROOT/backend"
exec uv run python "$ROOT/scripts/workspace-board-lazyload-e2e/run_e2e.py"
