# Provider preflight cross-validation and integration

The user requested independent cross-validation and integration if clean. This
continues Task `3362a288-8b45-4493-bff4-3497cf191531`; no deployment or production
restart is authorized by this integration.

## Fixed inputs and merge

- Parent main: `45c552c60c7635efe2580accc875b8840a67cc90`.
- Frozen repair: `0a2a8b14603e51352ffab37d0c2eb8b9c6dcf44c`.
- Integration merge: `e5fa9d25bb0598fcbb2d1ce34cdfd1001b929acb`, with exactly those
  two parents in that order.
- Repair and merge share tree `cbd8321778e053a85e3ce305e8ce078c0b27d937`.
  Git's fixed-merge check passed with zero decision paths. The independent
  reviewer separately verified parents, tree identity and semantic scope.

The merge was prepared in
`~/claude_hub_worktree/provider-preflight-main-integration`, not the dirty primary
checkout. This follow-up record adds documentation only after the fixed merge;
it does not alter the reviewed product code or tests.

## Independent validation

A fresh evaluator used the dedicated detached worktree
`~/claude_hub_worktree/provider-preflight-cross-check` and its own locked Python
3.11.16 environment. It reviewed the actual parent/candidate diff, native send
and launch path, route selection/cache, generated settings, redaction and
explicit route behavior. Final verdict: **PASS, no actionable findings**.

- Provider/native suites plus independent checks: **230 passed**.
- Strengthened final independent checks: **7 passed**.
- Immutable-parent negative control: **4 expected failures**, each at the
  original network preflight timeout rejection.
- Actual local HTTP servers stalled HEAD while a fake Claude child process read
  native-generated settings/stdin and submitted exactly one successful POST.
  Direct, configured proxy, NO_PROXY bypass and explicitly disabled lowercase
  proxy variants passed. Wire request targets distinguish proxy from direct use.
- Forced route timeouts still reject before POST. Cache checks verify that
  coalesced selections retain each caller's credentials and expire on the short
  failure TTL. Base settings remain unchanged.

Evidence is under the evaluator worktree's ignored
`.agent_office/provider-preflight-cross-check/`: `evaluation.json`,
`test_independent.py`, and `pytest.log` in `claude-hub-verify.nGUgfB`,
`claude-hub-verify.2EEkaT` and `claude-hub-verify.zcWOop`. An initial negative
control invocation lacked the external test's asyncio config; it was discarded
and repeated with `-c pyproject.toml`. Only the corrected causal failures are
counted above.

The full strict-toolchain check of the identical repair tree already passed:
3335 backend tests, 7 conditional skips, 781 frontend tests, formatting, types,
lint, build, docs and checkout safety. See the repair's
[validation record](2026-10-15-provider-preflight-resilience.md). That full suite
was not duplicated without a source change.

## Limits

The independent fixtures use shortened deadlines, a fake provider child and local
HTTP. They do not validate the installed Claude CLI, external HTTPS/CONNECT/SOCKS,
paid model inference, browser rendering or the live Hub. The running Hub and its
existing per-chat workaround are unchanged. Main publication is a Git delivery,
not activation of the new code in the resident service.
