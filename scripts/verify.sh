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
  backend-tests    Run the default non-browser backend scope with coverage
  frontend-lint    Run ESLint without fixes
  frontend-types   Run vue-tsc
  frontend-tests   Run the Node unit tests
  frontend-build   Build the Vite bundle
  terminal-hmr     Run all isolated terminal HMR cases (explicit only)
  browser-mocks    Run the five isolated mocked browser checks (explicit only)
  all              Run standard targets; browser-mocks and terminal-hmr remain explicit

Only backend-tests accepts focused pytest arguments after --. The default and
all/CI retain the default scope; focused results are labeled, not a default-suite pass.

browser-mocks and terminal-hmr require CLAUDE_HUB_BROWSER_EXECUTABLE
(an existing absolute path).
CLAUDE_HUB_BROWSER_PORT and CLAUDE_HUB_BROWSER_TIMEOUT are optional.

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

validate_junit_xml() {
  "$ROOT/backend/.venv/bin/python" - "$1" <<'PYTHON'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

root = ET.parse(Path(sys.argv[1])).getroot()
def local_name(element):
    return element.tag.rsplit("}", 1)[-1]
if local_name(root) not in {"testsuites", "testsuite"}:
    raise SystemExit("Not a JUnit test suite")
cases = [element for element in root.iter() if local_name(element) == "testcase"]
if not cases:
    raise SystemExit("JUnit report contains no test cases")
if any(local_name(child) in {"skipped", "failure", "error"} for case in cases for child in case):
    raise SystemExit("JUnit report contains skipped or failed cases")
for element in root.iter():
    if local_name(element) in {"testsuites", "testsuite"}:
        for field in ("skipped", "failures", "errors"):
            if int(element.attrib.get(field, "0")) != 0:
                raise SystemExit(f"JUnit report has nonzero {field}")
print(f"junit_cases={len(cases)} skipped=0 failures=0 errors=0")
PYTHON
}

run_backend_tests() {
  check_backend_tools
  local scope
  local -a pytest_args
  if [[ $# -eq 0 ]]; then
    scope=default
    pytest_args=(-xvs
      --ignore=tests/test_terminal_replay.py
      --ignore=tests/test_terminal_input_latency_perf.py
      --ignore=tests/test_terminal_hmr_recovery_e2e.py
      --cov=claude_hub --cov-report=xml:coverage.xml --cov-report=term-missing)
  else
    scope=focused
    pytest_args=("$@")
  fi
  run_backend_pytest backend-tests "$scope" "" 0 "${pytest_args[@]}"
}

run_backend_pytest() {
  local target="$1" scope="$2" runtime_tmp="$3" require_junit="$4"
  shift 4
  [[ -z "${CLAUDE_HUB_TEST_BACKEND_URL:-}" ]] || fail \
    "$target must not use an external backend"
  local parent evidence status pytest_status junit_status=0
  local -a pytest_args=("$@")
  parent="${CLAUDE_HUB_VERIFY_ARTIFACT_ROOT:-${TMPDIR:-/tmp}}"
  [[ -d "$parent" ]] || fail "artifact parent does not exist: $parent"
  evidence="$(mktemp -d "$parent/claude-hub-verify.XXXXXX")"
  mkdir -p "$evidence"/{home,tmp,xdg-config,xdg-state,xdg-cache,hub,state}
  runtime_tmp="${runtime_tmp:-$evidence/tmp}"
  [[ -d "$runtime_tmp" ]] || fail "runtime TMPDIR does not exist: $runtime_tmp"
  if [[ "$require_junit" == 1 ]]; then
    pytest_args+=(--junitxml="$evidence/junit.xml")
  fi
  printf 'Backend test target: %s; scope: %s; evidence: %s; TMPDIR: %s\n' \
    "$target" "$scope" "$evidence" "$runtime_tmp"
  {
    printf 'target=%s\nscope=%s\nruntime_tmp=%s\n' "$target" "$scope" "$runtime_tmp"
    printf 'e2e_timeout_scale_input=%s\n' "${CLAUDE_HUB_E2E_TIMEOUT_SCALE:-1}"
    printf 'pytest_arg=%q\n' "${pytest_args[@]}"
    git -C "$ROOT" rev-parse HEAD
    (cd "$ROOT/backend" && uv run --offline --no-sync python --version)
    uv --version
  } > "$evidence/environment.txt"
  status=0
  (
    export HOME="$evidence/home" TMPDIR="$runtime_tmp"
    export XDG_CONFIG_HOME="$evidence/xdg-config" XDG_STATE_HOME="$evidence/xdg-state"
    export XDG_CACHE_HOME="$evidence/xdg-cache"
    export CLAUDE_HUB_HOME="$evidence/hub" CLAUDE_HUB_STATE_ROOT="$evidence/state"
    export CLAUDE_HUB_TMUX_SOCKET="ch-verify-$$-${RANDOM}"
    export COVERAGE_FILE="$evidence/.coverage"
    # Test selection and plugin changes must be explicit, not inherited silently.
    unset PYTEST_ADDOPTS PYTEST_PLUGINS PYTEST_DISABLE_PLUGIN_AUTOLOAD
    if [[ "$scope" == default ]]; then
      unset GOAL_UI_REVIEW_URL CLAUDE_HUB_RUN_CHAT_SIDEBAR_E2E
    fi
    # conftest.py owns the safe default provider-network mode for pytest.
    unset CODEX_HOME TRAE_HOME CLAUDE_CONFIG_DIR
    unset CLAUDE_HUB_ALLOW_LIVE_RUNTIME CLAUDE_HUB_TOKEN CLAUDE_HUB_BASE_URL
    unset ANTHROPIC_API_KEY OPENAI_API_KEY CLAUDE_CODE_OAUTH_TOKEN
    cd "$ROOT/backend"
    uv run --offline --no-sync pytest "${pytest_args[@]}"
  ) 2>&1 | tee "$evidence/pytest.log" || status=$?
  pytest_status=$status
  printf '%s\n' "$pytest_status" > "$evidence/pytest-exit-code"
  if [[ "$require_junit" == 1 ]]; then
    validate_junit_xml "$evidence/junit.xml" > "$evidence/junit-validation.log" 2>&1 || junit_status=$?
    printf '%s\n' "$junit_status" > "$evidence/junit-validation-exit-code"
    if (( status == 0 && junit_status != 0 )); then status=$junit_status; fi
  fi
  printf '%s\n' "$status" > "$evidence/exit-code"
  printf 'Backend tests exit=%s (pytest=%s, junit=%s); evidence retained at %s\n' \
    "$status" "$pytest_status" "$junit_status" "$evidence"
  return "$status"
}

run_terminal_hmr() {
  check_backend_tools
  check_frontend_tools
  require_command tmux
  require_command ttyd
  [[ "${CLAUDE_HUB_BROWSER_EXECUTABLE:-}" == /* ]] || fail \
    "set CLAUDE_HUB_BROWSER_EXECUTABLE to an existing absolute browser executable"
  [[ -f "$CLAUDE_HUB_BROWSER_EXECUTABLE" && -x "$CLAUDE_HUB_BROWSER_EXECUTABLE" ]] || fail \
    "CLAUDE_HUB_BROWSER_EXECUTABLE must be an existing executable file"
  local runtime_tmp
  runtime_tmp="$(mktemp -d /tmp/ch-hmr-XXXXXX)"
  printf 'Terminal HMR short TMPDIR retained at %s\n' "$runtime_tmp"
  run_backend_pytest terminal-hmr focused "$runtime_tmp" 1 \
    -xvs tests/test_terminal_hmr_recovery_e2e.py
}

run_browser_mocks() {
  check_backend_tools
  check_frontend_tools
  [[ -n "${CLAUDE_HUB_BROWSER_EXECUTABLE:-}" ]] || fail \
    "set CLAUDE_HUB_BROWSER_EXECUTABLE to an existing absolute browser executable"
  local parent evidence
  parent="${CLAUDE_HUB_VERIFY_ARTIFACT_ROOT:-${TMPDIR:-/tmp}}"
  [[ -d "$parent" ]] || fail "artifact parent does not exist: $parent"
  evidence="$(mktemp -d "$parent/claude-hub-browser-mocks.XXXXXX")"
  printf 'Browser mocks evidence parent: %s\n' "$evidence"
  # The Python owner receives signals directly, without an intermediate shell trap.
  exec "$ROOT/backend/.venv/bin/python" "$ROOT/scripts/verify_browser_mocks.py" \
    --node "$(command -v node)" \
    --browser "$CLAUDE_HUB_BROWSER_EXECUTABLE" \
    --output "$evidence/run" \
    --port "${CLAUDE_HUB_BROWSER_PORT:-5291}" \
    --runner-timeout "${CLAUDE_HUB_BROWSER_TIMEOUT:-120}"
}

run_one() {
  local -a browser_tools=(
    "$ROOT/scripts/verify_browser_mocks.py" "$ROOT/frontend/tests/browser_lifecycle.py")
  local -a browser_scenarios=(
    "$ROOT/frontend/tests/browser_task_execution_panel.py"
    "$ROOT/frontend/tests/browser_agent_workspace_task_create.py"
    "$ROOT/frontend/tests/browser_chat_subagent_region.py"
    "$ROOT/frontend/tests/browser_feishu_provenance.py"
    "$ROOT/frontend/tests/browser_feishu_bot_components.py")
  case "$1" in
    browser-mocks) run_browser_mocks ;;
    terminal-hmr) run_terminal_hmr ;;
    docs)
      cmp "$ROOT/AGENTS.md" "$ROOT/CLAUDE.md"
      ;;
    backend-format)
      check_backend_tools
      (cd "$ROOT/backend" && uv run --offline --no-sync black --config pyproject.toml --check . "${browser_tools[@]}" "${browser_scenarios[@]}" \
        && uv run --offline --no-sync isort --settings-path pyproject.toml --check . "${browser_tools[@]}" "${browser_scenarios[@]}")
      ;;
    backend-types)
      check_backend_tools
      (cd "$ROOT/backend" && uv run --offline --no-sync mypy --config-file pyproject.toml . "${browser_tools[@]}")
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
  printf 'Not run by all: browser-mocks, terminal-hmr; replay/performance/manual UI checks remain separate.\n'
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
