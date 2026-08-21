#!/bin/bash
# Isolated real-CLI Agent Tree E2E. Harness observes only; the managed CLI POSTs the report.
# Usage (from this worktree, after the successor commit):
#   git rev-parse HEAD
#   bash scripts/agent-tree-e2e/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BACKEND="$ROOT/backend"
PYTHON="${CLAUDE_HUB_E2E_PYTHON:-$BACKEND/.venv/bin/python3}"
HOME_DIR="${CLAUDE_HUB_E2E_HOME:-/tmp/claude_hub_e2e_f6bf8165/home}"
REPO_DIR="${CLAUDE_HUB_E2E_REPO:-/tmp/claude_hub_e2e_f6bf8165/repo}"
PORT="${CLAUDE_HUB_E2E_PORT:-19173}"
TTYD_BASE="${CLAUDE_HUB_E2E_TTYD_BASE:-19100}"
TMUX_PREFIX="${CLAUDE_HUB_E2E_TMUX_PREFIX:-claude-hub-e2e-}"

mkdir -p "$HOME_DIR" "$REPO_DIR"
: > "$HOME_DIR/backend.stdout.log"

export CLAUDE_HUB_E2E_HOME="$HOME_DIR"
export CLAUDE_HUB_E2E_REPO="$REPO_DIR"
export CLAUDE_HUB_E2E_BACKEND="$BACKEND"
export CLAUDE_HUB_E2E_PYTHON="$PYTHON"
export CLAUDE_HUB_E2E_PORT="$PORT"
export CLAUDE_HUB_E2E_TTYD_BASE="$TTYD_BASE"
export CLAUDE_HUB_E2E_TMUX_PREFIX="$TMUX_PREFIX"
export PYTHONPATH="$BACKEND"

exec "$PYTHON" "$ROOT/scripts/agent-tree-e2e/run_e2e.py"
