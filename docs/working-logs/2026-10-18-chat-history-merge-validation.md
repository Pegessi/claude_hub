# Chat history integration and cross-review

## Fixed candidates and scope

The user authorized cross-verification and integration into local `main`.
No remote push or shared-service restart is part of this integration.

- Reviewed main parent: `5e3da89b1e7f76aa859021b1c74fb8d9da4408c9`.
- Original feature: `7e1d0e3ef8469ab72b716347efdd1b4149bd292e`.
- Fixed merge: `2eb43899cf29c7d6b170bb74987fbc7441554f07`.
- Final runtime/test repair candidate: `8b7c58e54ab9975c8cafd415b52fb976e9b94056`.

The fixed merge has one conflict, in `CHANGELOG.md`. Both complete release
entries were retained. All 44 other changed paths match their contributing
parent, including file mode and object identity. The separate
[merge decision record](2026-10-18-chat-history-merge-decisions.json) covers
`2eb4389` only; its successful check does not establish semantic correctness or
validate later repairs.

## Independent review and repairs

A new backend reviewer checked the history API, store/index behavior, raw cursor
compatibility and preservation of the main-side service shutdown changes. No
normal-path backend blocker was found. Backend files are unchanged by `8b7c58e`.

A new frontend reviewer reproduced three concrete problems in the fixed merge:

1. An old detail request could finish after its descriptor grew and mark the
   larger range loaded without having fetched all of that range.
2. Previously cached process content could become visible when descriptor growth
   invalidated its loaded status. The summary projection must work for unfinished
   turns too, independently of completed-turn folding.
3. An explicit edit/reset could restore a warm cache with the old cursor after
   the backend truncated and renumbered history, showing superseded turns.

The repair binds request ownership to request identity, generation and the exact
range descriptor. Superseded requests are cancelled; stale success, error and
cleanup paths cannot modify their replacements. Pending detail projects only
visible reply/control parts and compact failed/cancelled child summaries.
Explicit reset evicts the active history snapshot; internal cached start/retry
still preserves valid history. `stop()` also aborts and clears detail requests.

Both the owner and the independent reviewer ran positive versions of the original
counterexamples. They verified the new descriptor stays idle after an obsolete
response, cached thinking remains absent from the summary, and edit cursors become
`[-1, 10, -1]` with only the edited question and new answer visible. The reviewer
also checked real abort rejection, new-range failure followed by retry, internal
retry cache preservation, and the final stop-cancellation increment. No actionable
frontend finding remained in that scope.

The browser regression uses the ordinary Edit message / Resend controls after
warming the cache through Chat switches. It asserts a fresh `since_sequence=-1`
read, the edited question/reply, and absence of the superseded turns. All browser
API requests are mocked; this is UI behavior evidence, not a production latency
measurement or a provider-backed test.

## Final validation

Checks below use the repaired `8b7c58e` candidate in its integration worktree.

| Check | Result |
| --- | --- |
| `./scripts/verify.sh all` | PASS |
| Backend default tests | 4,329 passed, 9 skipped; 218.56 s |
| Backend types | Success across 535 source files |
| Frontend unit tests | 1,037 passed, no failures or skips |
| Backend format, frontend lint/types/build, docs, checkout safety | PASS |
| `terminal-hmr` | 12 passed; 98.85 s |
| `terminal-replay` | 29 passed; 162.95 s |
| `browser-mocks` | 13/13 passed, including warm-cache edit/resend |
| `check-staged` and fixed-merge decision check | PASS before repair commit |
| Independent positive cache probes | PASS |

The first final browser launch failed before running any scenario because port
5298 was unavailable (`EADDRINUSE`). Its evidence is retained. The unchanged suite
passed all 13 scenarios on port 5307; no assertions were weakened. All runners
reported context/browser/driver cleanup complete. The task Vite process exited
normally and its port is closed; the harness does not claim full process-tree
exit verification.

Tool versions: Python 3.11.16, Node 24.21.0, pnpm 9.15.9, Playwright 1.58.0 with
its matching Chromium 1208. Vite still reports the existing large-chunk warning;
the build succeeds. The standard `all` target does not include browser or terminal
checks, which were invoked separately here.

Local evidence root:
`/home/tiger/claude_hub_workspace/chat-history-integration/`.
Final logs are `logs/final-verify-all.log`, `logs/final-browser-mocks*.log`,
`logs/final-terminal-hmr.log` and `logs/final-terminal-replay.log`; runner artifacts
are under `artifacts/final/`. Three owner-side positive probes and their JSON
results are under `probes/` and `logs/*-fixed.json`.

## Integration conditions and remaining boundary

The final browser rerun passed and the task Vite server exited. Local main was
rechecked clean at the reviewed parent before preparing the final fast-forward.
The main update must still recheck that parent immediately before applying it.
The original feature commit remains unchanged. This evidence-log commit contains
documentation only relative to the final repair candidate.

The original performance sample and API contract remain documented in
[the feature working log](2026-10-18-chat-history-lazy-load.md). No new production
latency claim is made by this integration. The running shared service is not
restarted or rebuilt here, so merging alone does not deploy the new UI/API.
