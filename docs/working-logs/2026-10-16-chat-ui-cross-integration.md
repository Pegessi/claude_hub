# Thinking and tool UI cross-validation and integration

## Fixed inputs and delivery boundary

- Primary local main at task start: `6de0b9cdbdd61bcb25fc24b0c532bb6b2621d353`.
- Fetched integration target: `071e12c8e558926d4868073a92644646d838edc7`.
- Task-local source candidate: `91e2e833645b2a6e60b78d8ae72c0175f4ecab69`,
  containing the prior Thinking and tool-card work on snapshot base `525b84e`.
- Dedicated linked worktree: `chat-ui-cross-integration-20261016`.

The source candidate came from a standalone task-local snapshot, so its patch
was applied to a branch rooted in the canonical repository's latest fetched
main, rather than importing the snapshot's unrelated Git root. The application
changes applied without conflicts. The two release entries were moved above
existing entries without changing upstream content.

The user authorized local integration after cross-validation. No push, build
installation into the running checkout, service restart, or deployment is part
of this task. The existing primary main remains untouched until the final
reviewed candidate can be fast-forwarded into it.

## Independent cross-review

Two fresh read-only reviewers worked without the previous review conclusions:

1. Timeline/reasoning reviewer traced provider status production, snapshot
   replacement, cache invalidation, child lifecycle and all part mutations.
   No reproducible correctness finding remained in that scope.
2. UI/resource reviewer traced native details, stable turn keys, memoization,
   payload DOM, resource cost and accessibility. The reviewer found that CSS
   animations inside closed child details continued to run.

The hidden-animation finding was fixed using CSS only: a closed subagent region
or child-thread ancestor disables its descendant thinking indicator animation.
A visible thinking-card summary is unaffected. Opening the ancestors restores
the original animation. No timer, observer, additional state, or provider
lifecycle change was added.

The browser gate now checks initial hidden state, thread close/reopen and region
close/reopen, asserting both computed animation name and actual Animation API
objects. Removing only the fix made the gate fail specifically at the hidden
animation assertion (`thinking-turn-*` instead of `none`); all other scenarios
passed. The fixed source was restored byte-for-byte. The fix also passed the
reviewer's SFC compilation/selector check and final read-only review.

A second proposed finding about group-status scans was checked against the
merge base and withdrawn: the same two `some`/`every` scans existed before this
candidate. The change does not claim constant-time status aggregation; it bounds
name construction and removes closed-body DOM. Open group-key metadata retains
only strings for the cached Chat's lifetime, as the existing process-fold state
does, and is not a payload-retention change.

## Validation

The task created a fresh locked backend environment with local editable-project
provenance and installed task-local pnpm; no existing virtual environment was
copied. Strict declared versions were used: Python 3.11.16, Node 24.21.0,
uv 0.12.15, pnpm 9.15.9, and Playwright 1.58.0 with an existing Chromium binary.

- `./scripts/verify.sh all`: passed.
  - Backend default scope: 3936 passed, 10 skipped, 34 warnings.
  - Frontend: 904 passed, zero failures/skips.
  - Docs, checkout safety, backend format/types, frontend lint/types/build passed.
- `./scripts/verify.sh browser-mocks`: all 11 scenarios passed after the fix.
- `./scripts/verify.sh terminal-hmr`: 12 passed, no failures/skips, unscaled.
- After the CSS/test correction, the backend format gate and affected frontend
  checks were rerun; no backend behavior changed.
- The deliberate no-fix browser run failed only in the new regression, as
  expected; it is retained separately and is not a release-validation failure.

The default backend skips cover opt-in UI probes, unavailable zsh, and the
clean-delivery provenance check while the candidate was uncommitted. They are
reported rather than counted as passes; the last is checked again after commit.
Existing Pydantic deprecation and Vite large-chunk warnings remain.

Artifacts are under `claude_hub_workspace/chat-ui-integration`:
`verify-all.log`, `backend-format-final.log`, `browser-mocks-final.log`,
`browser-mutation.log`, `terminal-hmr.log`, and their owned evidence directories.
The passing browser owner recorded normal Vite exit, a closed port and removed
temporary runtime. The mutation run also cleaned up its owned server. HMR used
its own backend, Vite and private tmux socket; shared services were not used.

No live-provider/OAuth integration, production browser acceptance, or standalone
terminal-replay target was run. The canonical Git integration uses a single
parent feature commit followed by fast-forward, so it introduces no two-parent
merge-resolution decisions requiring a fixed-merge record.
