# Cross-review and integration of per-turn watchdog clocks

## Fixed merge

The user requested another independent cross-review, followed by local-main
integration if no blocking issue remained.

- Previous main: `aa359fd17ccd804cbf296db3644552481e140667`.
- Fix: `6238e9e1d386de33fc611356d4a091eadd326a35`.
- Merge base: `cb0e9dff497e9ca9301cc51c6b1fd576ce811544`.
- Reviewed merge: `bf1a0f408700155e51a440f235bf098cb36cc3e9`.
- Reviewed tree: `ab64196f0b39033b8f3e0369dae324bd7a989b67`.

The fixed candidate was created in the isolated `silent-turn-integration`
worktree. Main had advanced with the Subagents feature and CLI Chat default.
The only conflict was the top of `CHANGELOG.md`: retain the new watchdog entry
and both existing main entries without rewriting or dropping their contents.
Runtime code, the six new regression cases and the original fix log are
byte-identical to the source fix. Relative to the current main, only those
four intended files change.

The adjacent `2026-11-05-watchdog-clock-merge-decisions.json` binds the sole
CHANGELOG decision to the immutable merge, both parents, base and Git version.
`verify.sh merge-decisions` reports complete record coverage. Its structural
check does not substitute for the independent semantic review below.

## Independent review

Fresh reviewer `watchdog-cross-review-6744` reviewed the complete introduced
change and relevant parent updates without relying on the earlier review.
It confirmed both new-turn boundaries, synchronous clock initialization,
under-lock watchdog rechecks, unchanged timeout/wait semantics and the test
failure evidence. No P0–P3 finding or incomplete fix requirement remained.

The reviewer independently ran the six new cases plus four existing watchdog
cases: 10 passed in 2.37 seconds. Its first invocation used the wrong relative
path after the runner changed into backend (zero collected); the corrected
`tests/...` invocation passed. No provider or shared service was started.

## Validation of the fixed candidate

Evidence root: `~/claude_hub_workspace/silent-turn-integration-20261105/`.

- Initial `verify.sh all`: docs, checkout safety, backend formatting and types
  passed. The backend suite then hung in the existing monitor log-rate-limit
  test during cancellation. The outer 465-second timeout returned 124; the
  inner backend runner recorded pytest/backend exit 130 and removed its owned
  runtime. This invocation did not pass or reach the frontend targets.
- The affected monitor test, its `_workspaces.py` implementation, backend
  configuration and lockfile are unchanged from the previously passing fix
  snapshot. No source change was made to hide the failure. The complete monitor
  module passed independently: 12 passed in 0.35 seconds.
- The unchanged fixed candidate's complete default backend scope was rerun:
  **6019 passed, 9 skipped, 16 warnings**, exit 0, in 344.61 seconds.
- The four previously unrun frontend targets also passed: lint, types,
  **1224 unit tests**, and Vite build. The existing >500 kB chunk warning remains.
- The reviewer read these final logs/provenance and confirmed that each standard
  target has a passing result for the same fixed candidate. The original
  combined-run timeout remains explicitly recorded, not relabeled as a pass.
- Key files: `verify-all.log`, `monitor-focused.log`,
  `verify-backend-retry.log`, `verify-frontend.log`; independent test evidence is
  under `reviewer/claude-hub-verify.mi04zb_4/`.

The two metadata documents added after the merge do not change runtime or test
inputs. They receive docs and staged-safety checks separately. Browser/HMR/replay
and live-provider acceptance are not claimed for this backend-only increment.

## Integration boundaries

Fast-forward main only from the recorded previous-main commit to the reviewed
merge plus these documentation records. Preserve the unrelated untracked
`docs/design/agent-liveness-guarantees.md` in the primary checkout; it is not
part of the integration. No push, target-chat resend, shared-service restart or
deployment is included in this authorization.
