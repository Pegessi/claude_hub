# Turn-bounded native sub-agent activity

## Goal and implementation

Task `de168696-54f1-4b38-b668-df7093378ee9`; feature worktree
`~/claude_hub_worktree/fix-stale-subagent-running`, branch
`codex/fix-stale-subagent-running`, base `fd5eaed`.

The timeline reducer accepted child lifecycle/activity independently of parent
turn boundaries. Completing the parent settled only its own tools; the dock,
inline region, header and thinking presenter retained active child state forever
when MASO omitted a terminal child event.

The reducer now closes unresolved child lifecycles and tools at parent completion
and on the first event of a newer turn. Successful completion maps to completed,
failed completion to failed, and interrupted/cancelled or superseded unfinished
turns to cancelled. Explicit child terminal outcomes remain unchanged. Late
activity remains readable but cannot reactivate a closed turn. Child and turn
render revisions invalidate thinking/dock caches. A later turn can still wake
the same provider thread.

No backend, provider transport, history persistence or UI layout changes.

## Stored-history evidence and discrepancy with the brief

Authenticated read-only GET of
`/api/workspaces/tabs/8aa85794-9c1b-44e3-aaf0-99575afc0744/stream/events?since_sequence=0&limit=5000`
returned **4492 events**, next_sequence=4492, has_more=false. The raw private
response is retained only in task-local `.artifacts/stored-history.json` (0600),
not committed. The committed 46-event fixture retains turn IDs, sequence numbers,
lifecycle statuses and each child's first activity; conversation/tool content is
omitted. The source starts mid-turn.

Before: running the actual `groupEventsIntoTurns` and `nativeSubagentRows` on
all 4492 events produced **9 active per-turn child records** and **5 running dock
rows**: perf_collector-33e1, toolkit_developer-12ba,
independent_reviewer-02e8, cross_model_reviewer-bff9, toolkit_fixer-b566.

The brief's exact four-name current-running list is **not** what this full history
produces. perf_collector-33e1, monitoring_collector-637a and
model_semantics_verifier-c3ae have stale active records at 230–232 in a parent
completed at **281**. The latter two have newer completed lifecycle events at
487 and 719; the dock deduplicates them onto those later records.
toolkit_agent_auditor-ad3a starts at 1840 but explicitly completes at **2170**,
before its parent completes at 2363. It is already completed before this fix.
Other parent completions include 2383, 2506, 2533, 2562 and 4492. This evidence
corrects the supplied diagnosis without broadening the implementation scope.

After: the same full-history projection has **zero running dock rows**. Its
unfinished prefix through sequence 232 still has all three genuinely live
collector/verifier rows. The committed fixture tests both outcomes, incremental
memo invalidation, newer/legacy turns, late replay, child tools, failure/cancel
outcomes and wake-up within an unfinished turn.

Repeated message IDs are streaming identities shared by multiple deltas, not
unique event IDs. Observed thinking groups generally contain different text
chunks (the largest has 1749 chunks); no evidence connects ID reuse to the stale
lifecycle defect. No message-ID deduplication changes were made.

## Negative oracle

With only the two product files temporarily replaced by their base versions:

```sh
cd frontend
node --test tests/subagentTurnBoundary.test.mjs
```

Exit **1**: `completed stored history must not leave running dock rows`
(expected [], got five running rows); boundary/memo checks also fail.
Restoring the fix: exit **0**, **5/5** tests pass. Full frontend suite:
**1544/1544** pass. Logs in `.artifacts/negative-oracle.log`,
`negative-oracle.exit`, `restored-oracle.log`, `restored-oracle.exit`.

Existing tests explicitly asserted that parent completion leaves children active.
Those assertions now enforce the requested opposite boundary contract; none were
deleted. The browser region expectation similarly changes from
`1 failed · 1 active` to `1 failed · 1 completed`. The native dock browser suite
retains all interaction/hydration checks and adds an unfinished-turn positive
oracle followed by completed replay, including row-state and screenshot evidence.

## Validation and handoff

- `./scripts/verify.sh all`: **PASS**, exit 0; backend **6890 passed,
  10 skipped**, frontend **1544 passed**; docs, checkout safety, formatting,
  backend/frontend types, lint and build all pass. Exact declared tool versions.
  Evidence: `.artifacts/verify-all-final.log` and `verify-all-final.exit`.
- `./scripts/verify.sh browser-mocks`: **PASS, 25/25**, including both requested
  suites and the composer thinking suite. Dedicated Vite port **5297**, mocked
  APIs, no live provider calls. Final evidence:
  `/tmp/claude-hub-browser-mocks.f1hawsdt/run/summary.json`.
- `./scripts/verify.sh terminal-hmr`: **PASS, 13/13** (104.75s), with
  `COREPACK_HOME=/home/tiger/.cache/node/corepack` and the existing Chromium
  executable at `~/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome`.
  Evidence: `/tmp/claude-hub-verify.cpc8k1kd`.
- Independent read-only review and follow-up review: **no actionable findings**;
  reviewer independently ran 50 focused tests and a mixed-provider boundary
  probe. Owner additionally checked the full 4492-event before/after projections.
- Browser screenshots under the final run's
  `runs/browser_native_subagent_dock/`: `08-live-running.png` shows the blue
  dot / **1 running**, `09-completed-no-running.png` shows **2 done**, no active
  row. Owner visually inspected the equivalent first-run screenshots as well.
- Final browser summary confirms Vite exit 0, no forced kill, no cleanup errors,
  and port no longer accepting connections. Verification runtimes removed.
- Initial validation failures were diagnosed, not ignored: backend-config Black
  formatting, outdated browser Active assertions (region and composer), Corepack
  cache missing from the HMR isolated HOME, and a concurrent worktree temporarily
  holding browser runner refusal port 59999. Composer live animation/fold checks
  were retained before interruption, with new post-interruption checks afterward.
  No unrelated process was stopped; retry waited for the shared test port.
- Staged safety and diff checks are run at commit time; final SHA and diffstat are
  included in the Task report. Terminal replay/performance checks were not run
  (no terminal code changed); neither is implied by `all`.
- Lesson applied: `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`
  — independent review covered the full diff, including browser contract changes.

Shared Hub ports 5173/8173 remain untouched. No main commit, merge, push or
deployment. Raw chat evidence and generated logs are not part of the commit.
The only acceptance qualification is the brief's exact four-name stale-list
mismatch, documented above; current full-history results are reported as observed.
