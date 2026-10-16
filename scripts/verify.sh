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
       scripts/verify.sh merge-decisions --merge SHA [--record FILE]

Targets:
  docs             Check repository document invariants
  checkout-safety  Check committed HEAD for machine-local configuration
  check-staged     Check the index before committing (explicit only)
  merge-decisions Check fixed-merge decision records (explicit only;
                  not a semantic correctness check)
  backend-format   Check Black and isort
  backend-types    Type-check the complete backend, including tests
  backend-tests    Run the default non-browser backend scope with coverage
  frontend-lint    Run ESLint without fixes
  frontend-types   Run vue-tsc
  frontend-tests   Run the Node unit tests
  frontend-build   Build the Vite bundle
  terminal-hmr     Run all isolated terminal HMR cases (explicit only)
  terminal-replay  Run the ttyd/tmux replay E2E suite (needs ttyd, tmux and a
                   Playwright browser; explicit only)
  browser-mocks    Run the isolated mocked browser checks (explicit only)
  all              Run standard targets; browser, staged and merge checks are explicit

Only backend-tests accepts focused pytest arguments after --.
merge-decisions requires an explicit full merge SHA; its record covers that merge only. The default and
all/CI retain the default scope; focused results are labeled, not a default-suite pass.

browser-mocks and terminal-hmr require CLAUDE_HUB_BROWSER_EXECUTABLE
(an existing absolute path).
CLAUDE_HUB_BROWSER_PORT and CLAUDE_HUB_BROWSER_TIMEOUT are optional.

Install locked dependencies explicitly before running checks. Verification does
not install dependencies or browsers. Backend tests use a private runtime and
retain evidence, including on failure. Existing tests may start their own helper
processes; their fixtures own cleanup. This script never restarts the main Hub.
Standard local targets warn on version mismatches and continue with noncanonical
results. CI and CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 require exact versions.
browser-mocks also retains exact Python/Node/Playwright requirements. Missing
tools and dependencies still fail in every mode.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }
read_version() { tr -d '[:space:]' < "$ROOT/$1"; }

check_tool_version() {
  local tool="$1" expected="$2" actual="$3" strict_ci=0
  [[ "$actual" == "$expected" ]] && return 0
  case "${CI:-}" in
    ""|0|[Ff][Aa][Ll][Ss][Ee]) ;;
    *) strict_ci=1 ;;
  esac
  if [[ "$strict_ci" == 1 || "${GITHUB_ACTIONS:-}" == true ||
    "${CLAUDE_HUB_VERIFY_STRICT_VERSIONS:-}" == 1 ]]; then
    fail "$tool version mismatch: expected $expected, found $actual"
  fi
  printf 'WARNING: %s version mismatch (expected %s, found %s); continuing with a noncanonical toolchain. Results are not CI-equivalent.\n' \
    "$tool" "$expected" "$actual" >&2
}

backend_python() (
  unset PYTHONPATH PYTHONHOME PYTHONUSERBASE UV_ENV_FILE
  local -a python_command=(python -I)
  if [[ "${1:-}" == -m && "${2:-}" == pytest &&
    -n "${CLAUDE_HUB_PYTEST_TIMEOUT_SECONDS:-}" ]]; then
    [[ "$CLAUDE_HUB_PYTEST_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail \
      "CLAUDE_HUB_PYTEST_TIMEOUT_SECONDS must be a positive integer"
    require_command timeout
    # Keep the pipeline's tee outside the timeout process group so Python's
    # fatal-signal traceback and the final exit status remain observable.
    python_command=(timeout --signal=ABRT --kill-after=15s
      "$CLAUDE_HUB_PYTEST_TIMEOUT_SECONDS" python -I)
  fi
  uv run --offline --no-sync --no-config --no-env-file --project "$ROOT/backend" \
    "${python_command[@]}" "$@"
)

backend_tool_command() (
  local cwd="$1"
  shift
  export HOME="$BACKEND_TOOL_ROOT/home" TMPDIR="$BACKEND_TOOL_ROOT/tmp"
  export XDG_CONFIG_HOME="$BACKEND_TOOL_ROOT/config"
  export XDG_STATE_HOME="$BACKEND_TOOL_ROOT/state" XDG_CACHE_HOME="$BACKEND_TOOL_ROOT/cache"
  export CLAUDE_HUB_HOME="$BACKEND_TOOL_ROOT/hub"
  export CLAUDE_HUB_STATE_ROOT="$BACKEND_TOOL_ROOT/hub-state"
  export CLAUDE_HUB_TMUX_SOCKET="ch-verify-tools-$$-${RANDOM}"
  unset CODEX_HOME TRAE_HOME CLAUDE_CONFIG_DIR CLAUDE_HUB_ALLOW_LIVE_RUNTIME
  unset CLAUDE_HUB_TOKEN CLAUDE_HUB_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
  unset OPENAI_API_KEY CLAUDE_CODE_OAUTH_TOKEN
  unset PYTEST_ADDOPTS PYTEST_PLUGINS PYTEST_DISABLE_PLUGIN_AUTOLOAD
  unset PYTHONPATH PYTHONHOME PYTHONUSERBASE UV_ENV_FILE
  cd "$cwd"
  "$@"
)

check_backend_tools() {
  require_command uv
  [[ -x "$ROOT/backend/.venv/bin/python" ]] || fail \
    "backend/.venv is missing; install locked dependencies using the documented setup first"
  # Settings reads .env relative to the backend cwd. Do not read or move that file.
  [[ ! -e "$ROOT/backend/.env" && ! -L "$ROOT/backend/.env" ]] || fail \
    "verification refuses backend/.env; use a clean feature checkout (file left untouched)"
  BACKEND_TOOL_ROOT="$(mktemp -d /tmp/ch-verify-tools.XXXXXX)"
  mkdir -p "$BACKEND_TOOL_ROOT"/{home,tmp,config,state,cache,hub,hub-state,cwd}
  printf 'Backend provenance evidence: %s\n' "$BACKEND_TOOL_ROOT"
  local expected_uv expected_python actual_uv actual_python
  local version_reader='import json, sys; value = json.load(sys.stdin)["python"]; print(value.strip() if isinstance(value, str) else "")'
  expected_uv="$(read_version .uv-version)"
  actual_uv="$(backend_tool_command "$BACKEND_TOOL_ROOT/cwd" uv --version | awk '{print $2}')" || fail \
    "could not read uv version"
  [[ -n "$actual_uv" ]] || fail "could not read uv version"
  check_tool_version uv "$expected_uv" "$actual_uv"
  backend_tool_command "$BACKEND_TOOL_ROOT/cwd" backend_python \
    "$ROOT/scripts/check_backend_environment.py" --root "$ROOT" \
    > "$BACKEND_TOOL_ROOT/provenance.json" || fail \
    "backend environment provenance failed; see $BACKEND_TOOL_ROOT"
  expected_python="$(read_version .python-test-version)"
  actual_python="$(backend_tool_command "$BACKEND_TOOL_ROOT/cwd" backend_python \
    -c "$version_reader" < "$BACKEND_TOOL_ROOT/provenance.json")" || fail \
    "could not read Python version"
  [[ -n "$actual_python" ]] || fail "could not read Python version"
  check_tool_version Python "$expected_python" "$actual_python"
}

check_frontend_tools() {
  require_command node
  require_command pnpm
  local expected_node expected_pnpm actual_node actual_pnpm
  expected_node="$(read_version .node-test-version)"
  actual_node="$(node --version)" || fail "could not read node version"
  [[ -n "$actual_node" ]] || fail "could not read node version"
  check_tool_version Node "v$expected_node" "$actual_node"
  expected_pnpm="$(node -p "require(process.argv[1]).packageManager.split('@').at(-1)" \
    "$ROOT/frontend/package.json")" || fail "could not read declared pnpm version"
  [[ -n "$expected_pnpm" ]] || fail "could not read declared pnpm version"
  actual_pnpm="$(pnpm --version)" || fail "could not read pnpm version"
  [[ -n "$actual_pnpm" ]] || fail "could not read pnpm version"
  check_tool_version pnpm "$expected_pnpm" "$actual_pnpm"
  [[ -d "$ROOT/frontend/node_modules" ]] || fail "install locked frontend dependencies first"
}

validate_junit_xml() {
  "$ROOT/backend/.venv/bin/python" -I - "$1" <<'PYTHON'
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
  if [[ "${1:-}" == "--terminal-replay" ]]; then
    shift
    scope=terminal-replay
    pytest_args=(tests/test_terminal_replay.py -v --reruns 1 --reruns-delay 5)
  elif [[ $# -eq 0 ]]; then
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
    printf 'backend_provenance=%s\n' "$BACKEND_TOOL_ROOT/provenance.json"
    cat "$BACKEND_TOOL_ROOT/provenance.json"
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
    backend_python -m pytest "${pytest_args[@]}"
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
  # The Python browser runner also enforces exact Python/Node/Playwright pins.
  # Reject drift here so local users get one consistent gate, not a warning
  # followed by a failure in the Python browser runner.
  CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 check_backend_tools
  CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 check_frontend_tools
  [[ -n "${CLAUDE_HUB_BROWSER_EXECUTABLE:-}" ]] || fail \
    "set CLAUDE_HUB_BROWSER_EXECUTABLE to an existing absolute browser executable"
  local parent evidence
  parent="${CLAUDE_HUB_VERIFY_ARTIFACT_ROOT:-${TMPDIR:-/tmp}}"
  [[ -d "$parent" ]] || fail "artifact parent does not exist: $parent"
  evidence="$(mktemp -d "$parent/claude-hub-browser-mocks.XXXXXX")"
  printf 'Browser mocks evidence parent: %s\n' "$evidence"
  # The Python owner receives signals directly, without an intermediate shell trap.
  unset PYTHONPATH PYTHONHOME PYTHONUSERBASE
  exec "$ROOT/backend/.venv/bin/python" -I "$ROOT/scripts/verify_browser_mocks.py" \
    --node "$(command -v node)" \
    --browser "$CLAUDE_HUB_BROWSER_EXECUTABLE" \
    --output "$evidence/run" \
    --port "${CLAUDE_HUB_BROWSER_PORT:-5291}" \
    --runner-timeout "${CLAUDE_HUB_BROWSER_TIMEOUT:-120}"
}

run_one() {
  local _scenario
  local -a browser_scenarios=()
  local -a python_tools=(
    "$ROOT/scripts/verify_browser_mocks.py" "$ROOT/frontend/tests/browser_lifecycle.py")
  while IFS= read -r _scenario; do
    python_tools+=("$_scenario")
  done < <(find "$ROOT/scripts" -maxdepth 1 -name 'check_*.py' | sort)
  case "$1" in
    browser-mocks) run_browser_mocks ;;
    terminal-hmr) run_terminal_hmr ;;
    checkout-safety|check-staged)
      require_command python3
      require_command git
      if [[ "$1" == check-staged ]]; then
        python3 -I "$ROOT/scripts/check_checkout_safety.py" --repo "$ROOT" --staged
      else
        python3 -I "$ROOT/scripts/check_checkout_safety.py" --repo "$ROOT" --commit HEAD
      fi
      ;;
    docs)
      cmp "$ROOT/AGENTS.md" "$ROOT/CLAUDE.md"
      # One heading owns the entries. Resolving a CHANGELOG conflict by keeping
      # both sides' headings splits the log into same-named blocks: readers see
      # only the first and cannot tell the rest exists. Three such blocks hid
      # 194 of 276 entries for four months before anyone noticed.
      _changelog_heads=$(grep -c '^## Released$' "$ROOT/CHANGELOG.md" || true)
      if [ "$_changelog_heads" -ne 1 ]; then
        echo "ERROR: CHANGELOG.md has $_changelog_heads '## Released' headings; expected exactly 1" >&2
        return 1
      fi
      if grep -q '^## Unreleased$' "$ROOT/CHANGELOG.md"; then
        echo "ERROR: CHANGELOG.md still has an '## Unreleased' heading; main is released" >&2
        return 1
      fi
      ;;
    backend-format)
      check_backend_tools
      # Discovered, not listed: a hard-coded list silently drops every
      # scenario added later, which is how the Chat composer gate escaped
      # the formatter. browser_lifecycle.py is shared tooling, listed above.
      while IFS= read -r _scenario; do
        browser_scenarios+=("$_scenario")
      done < <(find "$ROOT/frontend/tests" -maxdepth 1 -name 'browser_*.py' \
        ! -name 'browser_lifecycle.py' | sort)
      if [ ${#browser_scenarios[@]} -eq 0 ]; then
        echo "ERROR: no browser scenarios discovered" >&2
        return 1
      fi
      backend_tool_command "$ROOT/backend" backend_python -m black \
        --config pyproject.toml --check . "${python_tools[@]}" "${browser_scenarios[@]}"
      backend_tool_command "$ROOT/backend" backend_python -m isort \
        --settings-path pyproject.toml --check . "${python_tools[@]}" "${browser_scenarios[@]}"
      ;;
    backend-types)
      check_backend_tools
      backend_tool_command "$ROOT/backend" backend_python -m mypy \
        --config-file pyproject.toml . "${python_tools[@]}"
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
    terminal-replay)
      run_backend_tests --terminal-replay
      ;;
    *) usage; fail "unknown target: $1" ;;
  esac
}

run_all() {
  local failures=0 target
  for target in docs checkout-safety backend-format backend-types backend-tests \
    frontend-lint frontend-types frontend-tests frontend-build; do
    printf '\n==> %s\n' "$target"
    if "$ROOT/scripts/verify.sh" "$target"; then
      printf '<== PASS %s\n' "$target"
    else
      printf '<== FAIL %s\n' "$target" >&2
      failures=$((failures + 1))
    fi
  done
  printf 'Not run by all: browser-mocks, terminal-hmr, terminal-replay, check-staged, merge-decisions; performance/manual UI checks remain separate.\n'
  ((failures == 0)) || fail "$failures verification target(s) failed"
}

case "${1:-}" in
  -h|--help) usage ;;
  "") usage; exit 2 ;;
  merge-decisions)
    shift
    require_command python3
    require_command git
    exec python3 -I "$ROOT/scripts/check_merge_decisions.py" check "$@"
    ;;
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
