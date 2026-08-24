#!/bin/bash
# Isolated real-backend + claude-hub agent-tree CLI E2E.
# Parent spawn/wait/ack/replay go through the new CLI. The managed child
# POSTs its own report. Does not touch localhost:8173 or ~/.claude_hub.
# Usage (from this worktree):
#   bash scripts/agent-tree-cli-e2e/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BACKEND="$ROOT/backend"
PYTHON="${CLAUDE_HUB_E2E_PYTHON:-$BACKEND/.venv/bin/python3}"
HOME_DIR="${CLAUDE_HUB_E2E_HOME:-/tmp/claude_hub_e2e_e7ef4928/home}"
REPO_DIR="${CLAUDE_HUB_E2E_REPO:-/tmp/claude_hub_e2e_e7ef4928/repo}"
PORT="${CLAUDE_HUB_E2E_PORT:-19273}"
TTYD_BASE="${CLAUDE_HUB_E2E_TTYD_BASE:-19200}"
TMUX_PREFIX="${CLAUDE_HUB_E2E_TMUX_PREFIX:-claude-hub-cli-e2e-}"

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

exec "$PYTHON" "$ROOT/scripts/agent-tree-cli-e2e/run_e2e.py"
