# Fresh watchdog clocks for each Chat turn

## Scope and cause

Task: `4b9fac64-b034-4ddf-a3c9-3e52092b61e8`.
Base: `cb0e9dff497e9ca9301cc51c6b1fd576ce811544`.
Worktree: `~/claude_hub_worktree/silent-turn-investigation`.

A direct Codex Chat was cancelled 0.363462 seconds after its new turn started.
Its last idle `thread/goal/cleared` notification was 782.231424 seconds earlier.
The native consumer records idle control activity in `_last_event_at`, but the
new-turn reset previously left that timestamp intact. The next watchdog tick
therefore treated the newly submitted turn as already past the 600-second
inactivity threshold. Full event history and isolated causal controls confirmed
this mechanism before the user authorized the fix.

## Implementation

Use the existing `_reset_turn_watchdog_state()` shared by Hub send and
provider-initiated turn adoption. Set `_last_event_at` to the current monotonic
time at that boundary and clear `_turn_in_flight_since`; the consumer still
records the first observation of the new provider guard in the latter field.

Seeding at the boundary, rather than leaving activity unset until the consumer
next polls, gives both entry paths a current activity baseline immediately.
No additional timer, state owner, provider-specific branch or timeout setting
is introduced. The normal consumer activity updates, under-lock watchdog
recheck, and tool/approval/queue suppressions are unchanged.

## Regression tests

`backend/tests/test_agent_stream_turn_clocks.py` uses the real tailer, Codex
adapter and store with the existing fake transport. It covers:

- Recent and expired idle control activity, both at initial idle and after a
  normal completed turn: the next send receives a fresh clock and stays active.
- Provider-initiated starts: both clocks belong to the newly adopted turn.
- Genuine current-turn silence: the same error and durable cancelled completion
  are still emitted after the unchanged inactivity threshold.

Tests age only the instance timestamp, not the global monotonic clock, and
wait for the consumer's next read boundary rather than an arbitrary sleep.
They launch no provider or shared service. The new file produces 5 failures and
1 pass before the production fix. After the fix, 106 selected tests pass across
clock, watchdog, liveness, queue, approval and native Goal/Stop contracts
(351 other cases deselected). Black, isort and diff checks pass.

Evidence: `~/claude_hub_workspace/silent-turn-fix-20261105/`, including
`regression-before.log` and `regression-after.log`. The earlier incident evidence
is in the sibling `silent-turn-20261105/` directory.

## Final validation and review

- Independent reviewer `test-cleanup-review-0108` approved the complete runtime
  and regression-test change with no P0–P3 findings. The review checked both
  turn boundaries, the lack of an await between active-turn assignment and
  clock initialization, and the existing under-lock watchdog recheck.
- `verify.sh all` passed docs, checkout safety, backend formatting, mypy
  (615 source files) and the full backend scope: 5992 passed / 10 skipped /
  16 warnings. The initial command exited 1 because this new worktree did not
  yet have its frontend dependencies; none of its four frontend targets ran.
- Installed those dependencies with `pnpm install --offline --frozen-lockfile`,
  then ran the four missing targets: frontend lint, types, all 1197 unit tests
  and the Vite bundle passed. The existing >500 kB chunk warning remains.
  All standard targets have passed across these recorded runs; the initial
  failed `all` invocation is not relabeled as a success.
- Logs are `verify-all.log`, `frontend-install.log` and `verify-frontend.log`.
  The final test file SHA256 is
  `15837d96f371abf8ee84c7b29330e803da90dd287604deaada367125ab545bca`.
- Node unit tests used the task-local 512 MiB heap / 4 GiB address-space /
  concurrency-2 wrapper. Backend verification used the repository-owned private
  runtime; its runner removed temporary runtime directories and retained logs.
- Docs and explicit staged-file safety are checked immediately before commit.
  No browser/HMR/replay or live-provider test is claimed for this backend-only
  change; the existing mocked native protocol/Stop coverage was retained.

This is a code-only candidate: no main merge, push, live retry or shared-service
restart is performed, and the running service is not claimed to have the fix.
