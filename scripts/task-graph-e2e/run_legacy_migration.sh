#!/bin/bash
# Legacy raw-state cold-start migration E2E (resident_root + linked AgentRun fixture).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BACKEND="$ROOT/backend"
PYTHON="${CLAUDE_HUB_E2E_PYTHON:-$BACKEND/.venv/bin/python3}"
SUITE_ROOT="${CLAUDE_HUB_E2E_SUITE_ROOT:-/tmp/claude_hub_legacy_migration_e2e}"
HOME_DIR="${CLAUDE_HUB_E2E_HOME:-$SUITE_ROOT/home}"
REPO_DIR="${CLAUDE_HUB_E2E_REPO:-$SUITE_ROOT/repo}"
PORT="${CLAUDE_HUB_E2E_PORT:-19175}"
TTYD_BASE="${CLAUDE_HUB_E2E_TTYD_BASE:-19300}"
TMUX_PREFIX="${CLAUDE_HUB_E2E_TMUX_PREFIX:-claude-hub-lm-e2e-}"

mkdir -p "$HOME_DIR" "$REPO_DIR" "$SUITE_ROOT"
: > "$HOME_DIR/backend.stdout.log"

export CLAUDE_HUB_E2E_SUITE_ROOT="$SUITE_ROOT"
export CLAUDE_HUB_E2E_SOURCE_ROOT="$ROOT"
export CLAUDE_HUB_E2E_HOME="$HOME_DIR"
export CLAUDE_HUB_E2E_REPO="$REPO_DIR"
export CLAUDE_HUB_E2E_BACKEND="$BACKEND"
export CLAUDE_HUB_E2E_PYTHON="$PYTHON"
export CLAUDE_HUB_E2E_PORT="$PORT"
export CLAUDE_HUB_E2E_TTYD_BASE="$TTYD_BASE"
export CLAUDE_HUB_E2E_TMUX_PREFIX="$TMUX_PREFIX"
export PYTHONPATH="$BACKEND"
unset VIRTUAL_ENV

exec "$PYTHON" "$ROOT/scripts/task-graph-e2e/run_legacy_migration_e2e.py"
