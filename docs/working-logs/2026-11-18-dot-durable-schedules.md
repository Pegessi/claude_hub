# Dot durable schedules across rotation and replacement

Task: `e83d2540-a651-447b-b720-a55548a51659`  
Base: `1d92bce968343bf95c463f7e7e8e615bdf13158d`  
Branch: `feat/preserve-scheduled-tasks-across-e83d2540`

## Goal and root cause

Dot's provider conversation is disposable; the Hub Chat tab is durable across
conversation rotations. MASO `rotate_conversation()` closes the old stream,
POSTs the old session's `/exit`, and creates a new provider session. Native cron
jobs target provider agent IDs, not Hub tabs, and Hub had no rebind operation.
That proves the lifetime mismatch, not whether MASO deletes the stored job,
disables it or retains an orphan. Keep `/exit`: Stop/recovery uses it to fence
deferred cancellation finalizers from the next turn.

A second problem is independent of MASO: Dot's execution replacement only
snapshotted wake/tidy schedules, rejected enabled custom schedules and left
disabled custom schedules behind. Deleted-tab setup also lost the old tab
reference before custom schedules could migrate.

## Implementation and ownership

- Use the existing Hub scheduler, not a second cron migration layer. Dot's fixed
  `TURN_RULES` apply even to customized `DOT.md` roles. Shared provider runtime
  guidance likewise forbids provider-native `CreateCronJob` and specifies
  `claude-hub schedule create --kind chat_turn --tab-id "$CLAUDE_HUB_TAB_ID"`.
  Creation still requires a user-requested scheduled follow-up/check.
- MASO creation requests set `runtime_options: {"cron.enabled": false}`.
  Before each message, read per-session runtime options, preserve unrelated
  overrides when patching older sessions, and require effective cron disabling.
  Missing/ineffective policy blocks message delivery; no prompt-only fallback.
  This applies to Hub-owned MASO sessions generally because all have the same
  disposable provider lifetime. It does not change the global MASO configuration.
- Extend the existing replacement snapshot with custom `chat_turn` IDs. Refresh
  it after asynchronous retirement and before synchronous migration/identity
  commit, covering schedules created while retirement was in progress.
  Rollback restores only targets still owned by the candidate and retains the
  current enabled flag, without installing custom IDs as Dot state keys. User
  Stop/retarget edits during retirement or crash recovery are not reverted.
- Deleted-tab recovery reuses the same lock/transaction/rollback path. Retargeting
  preserves schedule ID, name, message, enabled flag, timing, next due time and
  history. The provider field follows the new tab. Completed one-shots are not
  rearmed; custom disabled schedules stay disabled, including those the scheduler
  had already disabled for a deleted target. Explicit backend switches also keep
  standing choices unchanged. Deleted-tab setup retains the historical standing
  repair behavior: wake/tidy are rearmed/rescheduled and retired defaults updated.
  Other tabs and non-Chat schedules are
  not migrated. While replacement is pending, timer fire and run-now cannot
  stamp/advance a schedule on either old or candidate tab; deleted-target
  disabling also waits for recovery. This prevents startup scheduler activity
  from consuming a one-shot before Dot rolls back its candidate.
  Pending/uncertain runs and non-Chat enabled schedules still block
  replacement rather than silently transferring in-flight work.

The brief's `runtime_guidance.py` and `dot/session.py` now live at
`services/agent_stream/providers/guidance.py` and `services/dot/runtime/session.py`.

## Evidence and verification

- Read-only provider investigation and independent spot-check: local MASO v0.9.0
  OpenAPI exposes creation `runtime_options` and session GET/PATCH
  `/api/sessions/{session_id}/runtime-options`. The runtime catalog and a
  read-only existing-session response expose boolean `cron.enabled`,
  `session_restart_required=false`, `overrides` and `options[].effective`.
  No live policy writes, cron creation or service restarts were performed.
- Focused Dot tests: 106 passed, evidence `/tmp/claude-hub-verify.vc51xcwv`.
- Focused MASO tests: 53 passed, evidence `/tmp/claude-hub-verify.gm6dfi5t`.
  Provider HTTP is mocked. Covers create/rotation policy payloads, resume before
  message delivery, preservation of unrelated runtime options, fail-closed
  unsupported/ineffective policy and retry, plus existing exit/rotation tests.
- Custom schedule regressions cover recurring/disabled/future/completed one-shot
  preservation, unrelated tabs, pending-run rejection, partial and final-commit
  failure, retry, interrupted migration, old-vs-candidate queued runs and late
  schedule creation. Initial MASO regression used a nonexistent cancellation
  method; corrected to the production `cancel_active_turn` contract before pass.
- Local independent review found a P2: pre-migration rollback could undo a
  concurrent user Stop/retarget. Confirmed against the unlocked schedule PATCH
  path and fallible provider retirement, then fixed ownership-aware rollback.
  Six new regressions cover disable/retarget before and after migration and
  retirement failure. Dot execution suite: 47 passed, evidence
  `/tmp/claude-hub-verify.m6r5d3dr`. Re-review confirmed the Stop fix, then found
  one-shot consumption by the scheduler before interrupted migration recovery.
  Reproduced through the real fire/queue path (two failures before the fix,
  `/tmp/claude-hub-verify.joymtmgp`), added the pre-stamp fence, and verified
  exactly one restored-target occurrence after recovery. Dot execution tests:
  49 passed, `/tmp/claude-hub-verify.5karg253` (provider drain mocked).
- Full verification before that final scheduler fence: all standard targets
  passed, including 6861 backend tests (10 skipped) and frontend checks; evidence
  `/tmp/claude-hub-verify.592s2ygm`, `/tmp/dot-schedules-verify-reviewed.log`.
- Round-1 local independent re-review: no remaining actionable findings; both
  P2s closed. Reviewer inspected the regression red/green artifacts and the
  full cumulative diff (read-only, without running tests or mutating services).
- Shared scheduler/Dot/MASO focused suite: 215 passed,
  `/tmp/claude-hub-verify.4mp_wuml`.
- Round-1 `./scripts/verify.sh all`: **PASS** on `58de998`. Backend: 6863
  passed, 10 skipped, 17 warnings in 502.85s; evidence
  `/tmp/claude-hub-verify.mcuain5d`. Docs, checkout safety, backend format/types,
  frontend lint/types/tests/build also passed. Full log:
  `/tmp/dot-schedules-verify-release.log`. No declared-toolchain version drift
  warnings. Browser/manual UI checks were not run (no frontend change).
- Final document update checked with `./scripts/verify.sh docs`; staged content
  checked with `./scripts/verify.sh check-staged` before committing.

## Managed review cycle 2

The managed reviewer rejected `58de998` after reproducing a missing coverage
window: with Dot's tab deleted but no pending replacement marker yet, a scheduler
tick disabled standing and custom schedules. The same window exists after a
failed replacement rolls back to a missing old tab. The new setup replacement
path also let `DotExecutionConflict` and provider/runtime errors escape HTTP.

- Added seven red regressions, all failing on `58de998`:
  `/tmp/claude-hub-verify.oh_9d_kg` (7 failed, 1 preexisting selected test passed).
- A single scheduler predicate now covers both pending execution targets and a
  missing tab still owned by Dot. Sweep and fire/run-now share it. Full tick
  regressions cover before setup and after interrupted-replacement rollback,
  without disabling custom tasks, advancing timers or consuming one-shots.
- Deleted-tab setup explicitly selects normal standing recovery *during*
  replacement, while the old target is still distinguishable. The existing
  normal branch rearms wake/tidy, recomputes stale next occurrences, and upgrades
  retired messages/legacy wake timing. Explicit backend switches retain their
  all-choice-preserving migration mode; custom schedules retain disabled and
  completed status in both modes. This repairs standing schedules already
  disabled by the old scheduler or queued-run target validation.
- An intermediate post-migration refresh only upgraded retired text/timing but
  could not rearm/reschedule because the old target had already been replaced.
  Independent re-review caught this; reproduced with a new disabled-standing
  regression (`/tmp/claude-hub-verify.v8d5hmli`, failed), then moved recovery into
  the migration as above.
- `/api/dot/setup` now returns 409 for ownership conflicts and 503 for runtime,
  MASO availability and tab startup failures; existing 400/429 mappings remain.
- Focused Dot/execution/API/scheduler suites: **224 passed** in 11.87s,
  `/tmp/claude-hub-verify.sa23gxat`. Regression tests exercise the real timer and
  HTTP application; provider processes and drain are mocked.
- With complete standing recovery: **225 passed** in 11.97s,
  `/tmp/claude-hub-verify.edxzf9a7`. The additional regression proves usable,
  future-due wake/tidy while custom disabled schedules remain unchanged.
- Cycle-2 local independent re-review: no remaining actionable blocker; missing
  target fencing, standing recovery and HTTP mappings are closed. On a failed
  deleted-tab setup, rollback restores ownership, not already-applied standing
  default/timer repairs; the missing-target fence keeps them from firing before
  retry. Custom schedule rollback and user Stop/retarget preservation are unchanged.
- Cycle-2 final `./scripts/verify.sh all`: **PASS**, backend **6871 passed,
  10 skipped, 17 warnings** in 502.98s; evidence
  `/tmp/claude-hub-verify.7koa93av`, full log
  `/tmp/dot-schedules-verify-cycle2-final.log`. All docs/safety/format/types and
  frontend lint/types/tests/build passed (1532 frontend tests). No declared
  toolchain drift warnings. Final docs/index checked again before commit.

## Boundaries / handoff

No main Hub or MASO process was stopped/restarted. No deployment or main merge is
part of delivery. Existing provider-native jobs are not converted or recovered:
inspect the old provider job if accessible and recreate the still-needed intent
with an explicitly authorized Hub schedule. The actual live `/exit` impact on
stored native jobs and live tool disappearance after the switch are not asserted
as tested; API contract checks and mocked HTTP regressions are separate evidence.
Older MASO without runtime options is intentionally fail-closed, not silently
compatible. Non-MASO transports receive guidance, not a new provider enforcement
API. Human acceptance remains required after the managed independent review.

Relevant lesson applied: `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`
(review must cover the full candidate diff, not a predeclared file list).
