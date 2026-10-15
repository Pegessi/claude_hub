# CI runner temp context repair

## Scope and cause

Task `6add20f6-a158-4e64-bc78-efce4e2a222e`, workspace
`79f6acfd-dbde-4951-ab3d-8abceeec1736`. Base
`273b430df8b58449156d03c0eb59818828ed7c8c`; branch
`fix/ci-runner-temp-context`; owned worktree
`~/claude_hub_worktree/ci-runner-temp-fix`.

GitHub run `38679783749` rejected `.github/workflows/ci.yml` before starting
jobs: browser-mocks and terminal-hmr used `runner.temp` in job-level `env`.
[GitHub's context availability table](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#context-availability)
excludes `runner` from that location.

## Repair

Each browser job now writes `PLAYWRIGHT_BROWSERS_PATH` and
`CLAUDE_HUB_VERIFY_ARTIFACT_ROOT` to `GITHUB_ENV` in its first shell step, using
the runner-provided `RUNNER_TEMP`. GitHub documents that
[environment-file values apply to subsequent job steps](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#setting-an-environment-variable).
The browser install path and separate mock/HMR evidence directories remain the
same. All existing steps, dependency caches, tool pins, strict verification,
runtime isolation, timeouts and always-run seven-day artifact uploads remain.
The HMR job's timeout scale stays `3`.

No production backend, primary runtime, merge, push or restart is part of this
branch delivery. The parent task owns independent review and integration.

## Validation

Dependencies were installed from this checkout with
`uv sync --frozen --extra dev --python 3.11.16` and
`pnpm install --frozen-lockfile`; the virtual environment was freshly created.

- No existing `actionlint` was found in PATH or the targeted local/system tool
  directories. No additional tool was installed. A bounded check used this
  checkout's locked PyYAML 6.0.3 and the GitHub references above: the complete
  workflow parses; the documented job-env context audit detects all four
  original invalid references and none after the repair.
- Executed both new shell steps with a temporary runner path containing spaces,
  checked exact resulting browser/evidence paths, preserved an existing env-file
  entry, and checked the variables in a simulated subsequent step. All passed.
- Compared parsed workflow structures against the fixed base after normalizing
  only the intended initializers and four moved variables: identical. All 40
  shell run blocks passed `bash -n` after substituting Actions expressions.
  Script and output: `.agent_office/ci-runner-temp-context/validate-workflow.py`
  and `workflow-validation.log` in the same directory.
- `CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 ../scripts/verify.sh all`, run from this
  worktree's `frontend/`, passed all standard targets: docs, committed-checkout
  safety, backend formatting/types/default tests, frontend lint/types/tests and
  production bundle build. Exact declared Python 3.11.16, uv 0.12.15, Node
  24.21.0 and pnpm 9.15.9 were used. Backend types checked 252 files; backend
  tests: 3335 passed, 7 skipped (opt-in browser and clean-delivery Git-provenance
  cases), 33 warnings. Frontend tests: 781 passed, none failed or skipped. The
  existing Vite bundle-size warning remains.
  Log: `.agent_office/ci-runner-temp-context/verify-all.log`; isolated backend
  evidence: `/tmp/claude-hub-verify.HZtxua`.
- Final document checks, `git diff --check`, repeated bounded workflow validation
  and `./scripts/verify.sh check-staged` passed before commit.

This bounded validation is not a full Actions schema/context linter or an actual
GitHub-hosted run. Browser/HMR execution and GitHub system dependency installation
were not repeated locally for this workflow-only repair. The parent task owns
the post-push GitHub result.
