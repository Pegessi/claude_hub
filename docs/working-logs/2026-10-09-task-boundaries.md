# Stage three: explicit Task persistence scope and parent-owned creation flow

Date: 2026-10-09. Branch `feat/quality-stage3`, based on the stage-two candidate
`47d4e920d739c838d358ec26a94e237c8a655fbb`. Not merged, pushed, deployed or
restarted. No real provider, OAuth or Bot traffic took place.

## Why

Two responsibilities were implicit.

Report intake set a context variable that decided whether `_save_state()` wrote
one workspace or everything, and `_reports.py` snapshotted and restored
`TaskMailbox` private dictionaries directly. Callers could not see their own
commit scope, and the two mailbox domains were copied separately, so a rollback
could leave the call index pointing at a different event object than the event
list.

In the frontend, the Task creation form lived inside a `v-if` modal in
`AgentWorkspaceView.vue`. The request identity, frozen payload and credential
recovery it owned had to outlive the dialog, which made closing, reopening and
workspace switching hard to reason about.

## What changed

Backend:

- `_save_workspace_state(workspace_id)` is the explicit single-workspace commit.
  `_save_state()` explicitly writes the index and every workspace. Neither
  acquires locks or offers cross-file atomicity.
- Task registration, progress, handoff, runtime observation, followup, abort,
  delete, attachment edits and report intake pass the workspace explicitly. The
  persistence-scope context variable is gone; the lock-reentrancy one remains.
- `TaskMailbox` owns `snapshot_workspace`, `restore_workspace`, `lookup_call`,
  `wake_consumer` and a workspace-scoped `_persist`. A snapshot is one deep copy
  of the whole bundle, so index aliases keep referring to the same events, and it
  is copied again on restore so later changes cannot alter it. A missing key stays
  distinct from an empty collection. `lookup_call` returns a shallow read-only
  view; nested values, including the event, remain mutable.

Frontend:

- `useTaskCreateFlow.ts` holds the form, capability gating, frozen request,
  late-response guards and credential recovery; the parent creates it once and
  calls its workspace-change and disposal hooks.
- `TaskCreateDialog.vue` is presentation plus its own scoped styles, mounted
  permanently by the parent. Closing hides it, bumps the generation and clears
  draft attachments only.
- `utils/draftAttachments.ts` shares attachment conversion and preview cleanup;
  callers own their lists. `StructuredPane` keeps its separate draft model.

Documentation: `ARCHITECTURE.md` now states the current Task state and UI
ownership; `docs/AGENT_WORKFLOW.md` describes how to choose a change boundary.

## Verification

Run from this worktree with declared tool versions.

- `./scripts/verify.sh all` with `CLAUDE_HUB_E2E_TIMEOUT_SCALE=2`: see
  `full-all-stage3-rerun` in the stage evidence root.
- `./scripts/verify.sh browser-mocks`: five scenarios PASS; the Vite port stopped
  accepting connections afterwards. Each runner still reports
  `process_tree_exit: not_verified`.
- Scoped transaction suites (`test_task_mailbox.py`,
  `test_task_execution_control.py`, `test_report_intake_atomicity.py`,
  `test_task_attachment_transaction.py`, `test_task_graph.py`,
  `test_task_delete.py`, `test_task_followup_helper.py`,
  `test_task_abort_helper.py`): 146 passed.
- Twelve new boundary tests cover snapshot missing/empty keys, internal alias and
  cross-workspace isolation, read-only lookup, mailbox append/replay/ACK targeted
  writes, the full-state entry, the report wake filter, registration success and
  pre-commit failure retry, and scoped report creation plus replay.

Evidence of a real defect caught by the new mobile browser assertions: the
extracted dialog initially lacked the three-column `segmented-control--three`
rule, and the check failed with `[2, 2]` before the rule was restored.

## Limitations

- The browser harness previously omitted the application's global box reset and
  several design tokens, so its geometry did not match the running app. Those are
  now aligned, but harness fidelity is still not guaranteed in general.
- The isolated SSE restart timing check now uses the shared `scale_timeout`
  budget. The original ten-second expectation is printed, not asserted as stable
  on a loaded machine.
- Full-state saves remain in review, dispatch and other lifecycle paths. This
  change does not claim every operation or asynchronous continuation writes one
  workspace.
- An independent review reported that removing the context variable widened the
  write scope of task-edit dispatch. Both the stage author and the Master traced
  the baseline: the managed branch returns before the variable is set, and the
  initiator branch rejects `status`, so the reported path is unreachable. No code
  changed for it.
- No merge, push, deployment, restart or live provider/Bot verification.
