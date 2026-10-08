# Remove the unpublished ChatWork layer

## Decision and baseline

The user requested complete removal of the obsolete ChatWork feature, rather
than maintaining its history/stop-only compatibility surface. At inspection,
local `main` was `a0e4a617` and cached `origin/main` was `c37b7f3`; neither had
ChatWork source files or runtime references. This is not a fresh remote-main
verification.

The cleanup continues from candidate `25c2b0ca` on `feat/agent-workflow-v2` in
`~/claude_hub_worktree/agent-workflow-v2`. It does not merge main, push, deploy,
restart a service, read real credentials/state, or invoke a real provider/Bot.

## Removed functionality

- All `/api/tabs/{tab_id}/work` routes and the dedicated workspace-manager mixin.
  There is no replacement 410 route, read-only listing, or stop-only endpoint.
- ChatWork cards, polling, result notifications, and their StructuredPane mount.
  Task details no longer display the old linked-work fields.
- ChatWork-only Task, schedule, report, and capabilities fields, plus the CLI
  capability projection. Ordinary Task reporting fingerprints are unchanged.
- The old scheduler branches for linked checks, ownership, orphan cleanup, and
  detached-work handling. They are not converted into another task manager.
- Dedicated ChatWork tests and the obsolete manual live harness. Its private
  descendant-process guard and exclusive tests have no remaining consumer and
  are removed too. Historical test artifacts outside the checkout are untouched.

Historical working logs remain labeled as historical evidence; current entry
points and the changelog no longer advertise the removed commands as supported.

## Preserved behavior

- One WorkspaceTask collection, explicit execution responsibility, authorized
  progress, and same-ID handoff. Initiator-owned Tasks still do not enter managed
  dispatch, review, recovery, or cleanup implicitly.
- All four ordinary schedule kinds: terminal message, new session, Hub Task,
  and native Chat turn. Their targets, run history, deterministic Chat turn IDs,
  cooldown, pending-run recovery, stale reaping, and deleted-target handling stay.
- Lock-time schedule rereads and persistence before side effects. A concurrent
  disable/delete must still prevent a stale fire request from executing.
- Existing report transactions, review/acceptance, Task mailbox, and managed
  cleanup. An orphaned ordinary scheduled Hub Task retains its bounded recovery.
- Independent feedback scanning/capture/reaping, provider-network policy, ttyd
  isolation, Bot pool/provenance, and native-subagent display.

The shared fields `scheduled_task_id`, `internal_kind`, Task source references,
feedback source tab IDs, and ordinary completion timestamps are not ChatWork
fields and are retained.

## Persisted-state boundary

Simply deleting the old fields while silently ignoring unknown input is unsafe:
a stored linked monitor also has a valid ordinary Hub-Task kind, title, message,
and interval. Dropping its distinguishing fields could make it execute as a
normal recurring schedule.

The cleanup uses ordinary schema validation rather than a ChatWork migration
layer. `WorkspaceTask`, `WorkspaceTaskCreate`, `ScheduledTask`,
`ScheduledTaskCreate`, and `ScheduledTaskUpdate` now reject extra fields;
`WorkspaceTaskUpdate` already did. Other model policies are unchanged.

- Schedule records are parsed before core recovery, which can otherwise save
  state. Unknown schedule fields abort loading; previously skipped non-extra
  invalid entries keep their existing handling.
- Raw Task fields are checked before the existing Agent Tree migration can
  discard or overwrite rows. Only that existing migration's `agent_run_id` key
  is allowed during migration, and migrated Tasks are validated before writing
  either backup or source. Nested and flat loaders propagate unknown-field errors.
- Ordinary main-shaped records remain readable. An independent AST comparison
  confirmed all main fields remain in the six relevant models and the synthetic
  full-shape fixtures cover every original Task/schedule field.

No live files are migrated, disabled, deleted, or repaired by this development
task. Unpublished test snapshots need a separate, explicit operator decision
before reuse; tests here use fresh private runtime homes. A bad record's source
file is not rewritten, and startup does not proceed to execution.

This work does not add an instance-wide, multi-workspace persistence transaction.
Existing unrelated migration semantics are not a claim of cross-file atomicity.

## Verification

Only executions performed by the Workspace Master in the canonical worktree
count as acceptance evidence. Existing locked Python/frontend dependencies and
Chromium were reused without downloads. Backend runs use private HOME/XDG/runtime
state; browsers use owned numeric-loopback servers with mocked API and blocked
WebSocket/cross-origin access. There was no real Hub/provider/Bot invocation.

| Scope | Result | Evidence directory |
| --- | --- | --- |
| Full frontend | 699 passed; lint with zero warnings, TypeScript, build passed | `/tmp/claude-hub-remove-chatwork-frontend.dTYT7a` |
| Task/CLI, scheduler API, migration, mailbox/report, feedback | 243 passed; all 122 backend source files passed mypy | `/tmp/claude-hub-remove-chatwork-backend.mCeAgb` |
| New storage/API/fire-lock/fingerprint boundaries | 52 passed | `/tmp/claude-hub-remove-chatwork-storage.8yYtjk` |
| Other CLI, Task Graph/API, helpers, provider network, ttyd | 504 passed, 1 skipped while the delivery checkout was dirty | `/tmp/claude-hub-remove-chatwork-contracts.r6wnp2` |
| Managed execution and cold-restart compatibility | 159 passed in 875.66 seconds | `/tmp/claude-hub-remove-chatwork-managed.kCQyRs` |
| Formatting | Changed backend/current browser regressions passed Black/isort; the older Bot fixture retains its pre-existing style outside the two-line removal | `/tmp/claude-hub-remove-chatwork-format-final.Wg960Q` |
| Removal checks | Diff whitespace and matching entry guides passed; no ChatWork runtime references; preserved scheduler hooks checked | `/tmp/claude-hub-remove-chatwork-final-checks.lb4oKQ` |
| Task details browser | PASS; 3 progress and 3 handoff requests; no unknown requests/page errors; port 48601 closed | `/tmp/claude-hub-v2-panel-browser.ibzj477_` |
| Workspace view browser | PASS; 7 create requests; no unknown requests/page errors; port 55245 closed | `/tmp/claude-hub-v2-parent-browser.q_6cir40` |
| Full Chat/subagent browser | PASS; 210 mocked API requests, no unknown requests/page errors; port 46203 closed | `/tmp/claude-hub-remove-chatwork-subagent-browser.gsl5e8gs` |

The clean-checkout Git-provenance assertion is run separately after committing.
Earlier test counts are not added to these results. Removed dedicated tests are
not presented as remaining coverage: ordinary lock-time disable/delete/message
replacement and save-before-action regressions were recreated on normal
ScheduledTask records. Existing scheduled Hub Task report-to-DONE/cleanup and
bounded dead-worker recovery tests were retained.

The full Chat test no longer serves an empty result for the removed `/work`
request. An accidental old request now fails the unknown-request assertion. Its
unrelated Bot fixture was updated to the current empty pool contract; it does
not exercise real Bot pairing.

The frontend build still emits its non-blocking large-chunk warning. Python test
logs retain existing deprecation and duplicate OpenAPI-operation-ID warnings.
Formatting corrections were checked for identical Python ASTs; unrelated
formatting in the older Bot browser fixture was left unchanged.
Final formatting checks and the managed compatibility regression passed.
Backend deletion, strict loading, shared call sites, and the new tests
received independent object-level review; the frontend removal boundary was also
reviewed against exact objects.

Real provider/OAuth/Bot acceptance remains outstanding and is not implied by
mocked browser or unit tests. The older v2 and removed-harness results remain
historical evidence, not substitutes for this cleanup's regressions.