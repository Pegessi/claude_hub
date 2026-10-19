# Dot execution switching: adversarial review

## Scope and ownership

- Review source: `292e9b0a43693f0e34e5cc6357c7b45f46d6754a`.
- Target and merge-base: `44e68edf89ce953fcc85b9dbd745dc2e129177aa`.
- Branch/check-out: `feat/dot-provider-edit`, this session's private code copy.
- Initiator-controlled Hub review Task: `094f8c3f-8e24-4e3f-a07f-78279f7d4f05`.
- No remote MR identity has been established. This is a local candidate review,
  not an assertion about a latest remote MR, merge, release or deployment.
- The primary checkout, shared Hub and live Dot settings remain out of scope.
  The session write boundary still takes precedence over the usual worktree root;
  no existing worktree or shared resource was relocated.

The initial implementation's passing gates did not establish these adversarial
invariants. Two independent backend reviews and a separate frontend/API review
found the following actionable cases.

## Confirmed initial findings

| ID | Failure | Attribution and evidence |
| --- | --- | --- |
| F1 | A missing previous Dot tab plus late failure strands rollback on an invalid schedule target. | New recovery path. `../review-lifecycle/test_missing_tab_recovery.py`. Restore must include target-derived provider metadata. |
| F2 | Generic tab lifecycle/configuration APIs can mutate a switch candidate or old tab during replacement. | New switch transaction does not share their ownership boundary. `../review-lifecycle/test_candidate_mutation_race.py`. |
| F3 | Cancellation during provider rotation/submit can retain the native turn guard and an unfinished visible turn. | Existing Exception-only cleanup gap, materially expanded by new provider-allocation awaits. `../review-lifecycle/test_rotation_cancellation.py` and `../review-providers/` probes. Accepted or uncertain provider delivery must not become a synthetic rejection. |
| F4 | Edit-resend sequence rewind lets an orphaned turn fall below Dot's journal cursor and bypass switch admission. | Existing cursor behavior; new switch safety gate incorrectly trusts it. `../review-lifecycle/test_journal_rewind_orphan.py`. |
| F5 | Generic tab provider/env updates bypass dedicated Dot switching, leaving restore hints and schedule provider metadata stale. | Existing generic entrypoint is incompatible with new execution ownership. `../review-lifecycle/test_sequential_backend_bypass.py`. |
| F6 | Identity Save can unmount a busy execution editor; its final busy-clear emit is ignored and the panel stays disabled. | New editor integration. Actual Vue/Pinia browser repro: `../review-ui/identity-execution-race.json`. |
| F7 | Deferred Chat refresh overwrites a newer user selection because activation was decided before the await. | New editor refresh path. Keyboard-driven browser repro: `../review-ui/refresh-navigation-race.json`. |

Evidence directories above are siblings of this repository under the task root,
not files intended for commit. The parent independently reran F1-F4 together:
**4 passed asserting the faulty behavior**, `/tmp/claude-hub-verify.Br9PlY`.
These are failure reproductions, not health-check passes.

## Correction boundaries

- Scheduler owns restoration of previously persisted schedule target/provider/
  enabled fields; rollback must not revalidate a dangling historical target as a
  new target or alter user timing/text.
- Dot's existing setup lock serializes Dot-affecting lifecycle/configuration
  changes. Generic stop/archive must not hold scheduler/Goal locks across native
  completion observers. Legacy setup environment seeding remains explicit.
- Admission uses transcript lifecycle evidence independently of journal progress.
- Provider submit cancellation stays with the existing delivery owner; accepted,
  rejected and uncertain delivery retain their distinct meanings.
- The UI prevents identity mutations during execution changes and releases the
  mounted editor's busy state before unmount. Conditional Chat activation occurs
  while applying the tab snapshot, before any subsequent await.

## Verified frontend corrections

- Focused Node tests: **69 passed**.
- Canonical Node 24.21.0 / pnpm 9.15.9 frontend lint, types and complete unit suite:
  **1,049 passed**. Log: `../review-ui/frontend-canonical.log`.
- Isolated complete mocked-browser gate: **14/14 passed**, including a held PUT
  with identity controls and synthetic form submission exercised.
  Evidence: `/tmp/claude-hub-browser-mocks.UHRDWq/run`.
- Actual-component targeted browser checks: identity guard, later keyboard
  navigation and forced editor unmount all passed.
  Evidence: `../review-ui/fixed-ui-verification.json`.
- Task-owned ports 5543 and 5544 were verified closed after cleanup. Browser
  helpers report closed contexts/browser/driver, not full process-tree proof.
- An earlier frontend run used pnpm 12.5.1 and was noncanonical; it is superseded
  by the strict pinned-toolchain run above. The first forced-unmount probe selected
  a Vue wrapper instead of the panel owner; the corrected owner traversal passed.

## Post-correction adversarial checks

- Independent frontend review found a related empty-pane case: selecting an empty
  pane retains the old global active-tab id. Snapshot replacement now also checks
  the selected pane. Empty-pane and superseded-fetch regressions passed in the
  **71-test** focused suite; the independent recheck found no remaining blocker
  in that correction.
- Independent lifecycle review found that a deleted tab can still own scheduled
  runs. The parent reproduced the public-delete then switch sequence (unexpected
  200, `/tmp/claude-hub-verify.o3j2hE`) and extended all admission/settlement checks
  to the stored old id, even without a live tab record. The complete execution
  suite then passed **26 tests** (`/tmp/claude-hub-verify.HMcCba`), and the independent
  recheck passed the same scope (`/tmp/claude-hub-verify.fJ4EVN`).
- Before that follow-up, the parent independently passed **173** combined
  Dot/service/tab/scheduler API tests (`/tmp/claude-hub-verify.8wYBb7`).
- A first cancellation correction was rejected during composition review despite
  passing direct native tests. Moving failure publication into a child task can
  deadlock a managed-Chat observer against the parent's task-reentrant workspace
  lock. Re-raising cancellation after confirmed acceptance also causes the
  managed-Chat owner to record uncertainty. Both were reproduced in
  `../review-lifecycle/test_c2_composition.py` (`/tmp/claude-hub-verify.NYUp4n`).
- The revised cancellation implementation keeps observers in the original task
  and returns the provider's actual settled result through the managed-Chat owner.
  Provider submission and persistence IO are shielded/joined; persisted events are
  not appended twice when cancellation repeats. MASO approval resolution uses
  the same per-event publisher without moving observers into another task.
  This extends the existing publication owner without changing workspace locks.
- Independent cancellation recheck closed the composition findings: native suite
  **194 passed**, managed runtime **30 passed**, and a real Workspace-lock MASO
  approval probe **1 passed**. Evidence: `/tmp/claude-hub-verify.7aO4CW`,
  `/tmp/claude-hub-verify.DIWgam`, `/tmp/claude-hub-verify.iSU0vE`.
  The implementer's combined provider/managed suite passed **268 tests** at
  `/tmp/claude-hub-verify.CWQb1j`.
- Final consumer checking caught that the broad environment guard also blocked
  the existing composer model picker. Only model/reasoning keys of the current
  provider may now change in place; credential/endpoint/solo changes and generic
  PUT execution edits remain rejected. Five provider HTTP cases and the complete
  **31-test** execution suite passed; independent recheck also closed this delta
  (`/tmp/claude-hub-verify.mln6db`, `/tmp/claude-hub-verify.mZEehj`).

## Final validation and conclusion

All bounded correction reviews are closed with no remaining confirmed blocker.
The final strict `scripts/verify.sh all` passed after every code correction:

- Backend: **4,556 passed / 10 skipped**, `/tmp/claude-hub-verify.KEqegV`.
- Frontend: **1,051 passed**; lint, types and production build passed.
- Backend formatting/types, docs and checkout safety passed.
- Full log: `../review-lifecycle/release-all.log`.
- The code diff against `292e9b0` had the same SHA-256 before and after the gate:
  `c632db058397902b41df99ec5099198242732a6adb6ef2ab43ec2247be96ab3b`.
- The unchanged frontend passed the full **14/14** mocked-browser gate at
  `/tmp/claude-hub-browser-mocks.5xXqNU/run`, and terminal HMR passed **12/12** at
  `/tmp/claude-hub-verify.wrojJx`. The later API model-picker exception has separate
  five-provider HTTP regression coverage described above.
- Earlier all-gate passes preceded follow-ups; only the final gate above is used
  as complete backend/candidate validation. The follow-up commit containing this
  record is the local reviewed deliverable.

No merge, push, deployment or live Dot change was performed. This is a bounded
local review conclusion, not an absolute absence-of-bugs claim.

Live provider inference, external credentials/quota, shared-service restart and
terminal replay have not been exercised. Provider evidence is source inspection
and isolated protocol/runtime mocks.
