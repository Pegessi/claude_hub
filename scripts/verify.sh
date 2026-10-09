#!/usr/bin/env bash
# Shared local/CI checks. Environment installation is an explicit separate step.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_PYTHON_DOWNLOADS=never
export COREPACK_ENABLE_NETWORK=0 COREPACK_ENABLE_AUTO_PIN=0

usage() {
  cat <<'EOF'
Usage: scripts/verify.sh TARGET
       scripts/verify.sh backend-tests -- PYTEST_ARGS...

Targets:
  docs             Check repository document invariants
  backend-format   Check Black and isort
  backend-types    Type-check the complete backend, including tests
  backend-tests    Run the existing default pytest scope with coverage
  frontend-lint    Run ESLint without fixes
  frontend-types   Run vue-tsc
  frontend-tests   Run the Node unit tests
  frontend-build   Build the Vite bundle
  all              Run all targets and report each result independently

Only backend-tests accepts focused pytest arguments after --. The default and
all/CI retain the full scope; focused results are labeled and are not a full pass.

Install locked dependencies explicitly before running checks. Verification does
not install dependencies or browsers. Backend tests use a private runtime and
retain evidence, including on failure. Existing tests may start their own helper
processes; their fixtures own cleanup. This script never restarts the main Hub.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }
read_version() { tr -d '[:space:]' < "$ROOT/$1"; }

check_backend_tools() {
  require_command uv
  local expected_uv expected_python actual_python
  expected_uv="$(read_version .uv-version)"
  [[ "$(uv --version | awk '{print $2}')" == "$expected_uv" ]] || fail "use uv $expected_uv"
  [[ -x "$ROOT/backend/.venv/bin/python" ]] || fail \
    "backend/.venv is missing; install locked dependencies using the documented setup first"
  expected_python="$(read_version .python-test-version)"
  actual_python="$(cd "$ROOT/backend" && uv run --offline --no-sync python -c \
    'import platform; print(platform.python_version())')"
  [[ "$actual_python" == "$expected_python" ]] || fail \
    "backend environment must use Python $expected_python; found $actual_python"
}

check_frontend_tools() {
  require_command node
  require_command pnpm
  local expected_node expected_pnpm
  expected_node="$(read_version .node-test-version)"
  [[ "$(node --version)" == "v$expected_node" ]] || fail "use Node $expected_node"
  expected_pnpm="$(node -p "require(process.argv[1]).packageManager.split('@').at(-1)" \
    "$ROOT/frontend/package.json")"
  [[ "$(pnpm --version)" == "$expected_pnpm" ]] || fail "use pnpm $expected_pnpm"
  [[ -d "$ROOT/frontend/node_modules" ]] || fail "install locked frontend dependencies first"
}

run_backend_tests() {
  check_backend_tools
  [[ -z "${CLAUDE_HUB_TEST_BACKEND_URL:-}" ]] || fail \
    "the default test target must not use an external backend"
  local parent evidence status scope
  local -a pytest_args
  if [[ $# -eq 0 ]]; then
    scope=full
    pytest_args=(-xvs
      --ignore=tests/test_terminal_replay.py
      --ignore=tests/test_terminal_input_latency_perf.py
      --cov=claude_hub --cov-report=xml:coverage.xml --cov-report=term-missing)
  else
    scope=focused
    pytest_args=("$@")
  fi
  parent="${CLAUDE_HUB_VERIFY_ARTIFACT_ROOT:-${TMPDIR:-/tmp}}"
  [[ -d "$parent" ]] || fail "artifact parent does not exist: $parent"
  evidence="$(mktemp -d "$parent/claude-hub-verify.XXXXXX")"
  mkdir -p "$evidence"/{home,tmp,xdg-config,xdg-state,xdg-cache,hub,state}
  printf 'Backend test scope: %s; evidence: %s\n' "$scope" "$evidence"
  {
    printf 'target=backend-tests\nscope=%s\n' "$scope"
    printf 'pytest_arg=%q\n' "${pytest_args[@]}"
    git -C "$ROOT" rev-parse HEAD
    (cd "$ROOT/backend" && uv run --offline --no-sync python --version)
    uv --version
  } > "$evidence/environment.txt"
  status=0
  (
    export HOME="$evidence/home" TMPDIR="$evidence/tmp"
    export XDG_CONFIG_HOME="$evidence/xdg-config" XDG_STATE_HOME="$evidence/xdg-state"
    export XDG_CACHE_HOME="$evidence/xdg-cache"
    export CLAUDE_HUB_HOME="$evidence/hub" CLAUDE_HUB_STATE_ROOT="$evidence/state"
    export CLAUDE_HUB_TMUX_SOCKET="ch-verify-$$-${RANDOM}"
    export COVERAGE_FILE="$evidence/.coverage"
    # Test selection and plugin changes must be explicit, not inherited silently.
    unset PYTEST_ADDOPTS PYTEST_PLUGINS PYTEST_DISABLE_PLUGIN_AUTOLOAD
    # conftest.py owns the safe default provider-network mode for pytest.
    unset CODEX_HOME TRAE_HOME CLAUDE_CONFIG_DIR
    unset CLAUDE_HUB_ALLOW_LIVE_RUNTIME CLAUDE_HUB_TOKEN CLAUDE_HUB_BASE_URL
    unset ANTHROPIC_API_KEY OPENAI_API_KEY CLAUDE_CODE_OAUTH_TOKEN
    cd "$ROOT/backend"
    uv run --offline --no-sync pytest "${pytest_args[@]}"
  ) 2>&1 | tee "$evidence/pytest.log" || status=$?
  printf '%s\n' "$status" > "$evidence/exit-code"
  printf 'Backend tests exit=%s; evidence retained at %s\n' "$status" "$evidence"
  return "$status"
}

run_one() {
  case "$1" in
    docs)
      cmp "$ROOT/AGENTS.md" "$ROOT/CLAUDE.md"
      ;;
    backend-format)
      check_backend_tools
      (cd "$ROOT/backend" && uv run --offline --no-sync black --check . \
        && uv run --offline --no-sync isort --check .)
      ;;
    backend-types)
      check_backend_tools
      (cd "$ROOT/backend" && uv run --offline --no-sync mypy .)
      ;;
    frontend-lint|frontend-types|frontend-tests|frontend-build)
      check_frontend_tools
      local script
      case "$1" in
        frontend-lint) script=lint:check ;;
        frontend-types) script=typecheck ;;
        frontend-tests) script=test:unit ;;
        frontend-build) script=build:bundle ;;
      esac
      (cd "$ROOT/frontend" && pnpm run "$script")
      ;;
    *) usage; fail "unknown target: $1" ;;
  esac
}

run_all() {
  local failures=0 target
  for target in docs backend-format backend-types backend-tests \
    frontend-lint frontend-types frontend-tests frontend-build; do
    printf '\n==> %s\n' "$target"
    if "$ROOT/scripts/verify.sh" "$target"; then
      printf '<== PASS %s\n' "$target"
    else
      printf '<== FAIL %s\n' "$target" >&2
      failures=$((failures + 1))
    fi
  done
  ((failures == 0)) || fail "$failures verification target(s) failed"
}

case "${1:-}" in
  -h|--help) usage ;;
  "") usage; exit 2 ;;
  backend-tests)
    shift
    if [[ $# -gt 0 ]]; then
      [[ "$1" == "--" ]] || fail "focused pytest arguments require --"
      shift
      [[ $# -gt 0 ]] || fail "provide pytest arguments after --"
    fi
    run_backend_tests "$@"
    ;;
  all)
    [[ $# -eq 1 ]] || fail "exactly one target is required"
    run_all
    ;;
  *)
    [[ $# -eq 1 ]] || fail "exactly one target is required"
    run_one "$1"
    ;;
esac
