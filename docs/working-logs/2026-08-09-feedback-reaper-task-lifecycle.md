# Feedback Reaper Managed Task Lifecycle

## System Overview

Workspace lesson summarization already created a `WorkspaceTask` with
`system_internal=true` and `internal_kind=feedback_reaper`. The bug was that
`system_internal` controlled two unrelated concerns:

1. completion bypasses ordinary AI review and human acceptance; and
2. the task is hidden from the board, snapshot, queued count, and the assigned
   session's `task_id` / `current_task_id`.

The second behavior made a real agent assignment look like `no task`. When the
agent failed on an oversized prompt, operators could see an offline agent but
not the task that still blocked deletion. The summary trigger also committed
task-record inputs to the processed index before the agent completed, so a
failed run could consume its own retry input.

The fix preserves the internal completion policy while making the managed task
and assignment observable through the normal workspace APIs.

## Module Design

### Visible lifecycle ownership

- `WorkspaceManager.get_board()` includes Feedback Reaper tasks.
- board serialization replaces the bounded but still large Reaper prompt with
  a short system-task description; the persisted task keeps the full prompt
  for dispatch/retry.
- task and summary-run lifecycle rows are persisted before prompt assembly, so
  a prompt construction failure is visible as a retryable Todo instead of an
  untracked HTTP error.
- snapshots include their task row and exact current task id.
- managed-session responses keep the internal task id and include internal
  queued tasks in `queued_count`.
- completion still follows `_handle_internal_task_report()`: no reviewer is
  spawned, the task becomes Done, the session is released, and a system-audit
  report plus delete-safe task record are written.

### Per-workspace trigger idempotency

`summarize_workspace_feedback()` uses a workspace-scoped asyncio lock. Before
preparing input it finds any non-Done, non-aborted Feedback Reaper task:

| Existing state | Trigger behavior |
| --- | --- |
| Working / valid Queued | Return the existing `FeedbackSummaryRun` |
| Todo with no pending package | Rebuild the prompt on the same task/run |
| Todo with a pending package | Redispatch the same task/run |
| Missing task/session assignment | Reset to visible Todo, then redispatch |
| Done or manually aborted | Prepare a fresh run if input remains |

This makes both sequential and concurrent triggers converge on one active run
without relying on a read-then-create race.

### Deferred processed-index commit

After the 100K prompt budget selects the final digest set, the store writes a
bounded `<run-id>.pending.json` package beside the summary-run audit. It holds
only the paths and compact processed-record dumps needed for a later commit.

- successful `completed` / `ready_for_review`: commit staged entries to
  `feedback/index.json`, persist the completed run, then remove the pending
  package;
- prompt construction exception: task/run remain visible in Todo with a
  blocked system-audit report; retry rebuilds and stages the same run;
- dispatch exception: task returns to Todo, pending input remains;
- completion persistence exception: task returns to Todo, the agent is
  released, and the pending package remains for idempotent commit/retry;
- `blocked` / `needs_input`: task returns to Todo, agent is released, pending
  input remains for same-task retry;
- manual abort: run becomes terminal with `skipped_reason=manually_aborted`,
  pending input is discarded, and the task is archived as Done;
- task deletion: run becomes terminal with `skipped_reason=task_deleted`,
  pending input is discarded, and records remain eligible for a fresh run.

The existing prompt-size, fingerprint, post-budget alignment, and oldest-first
carry-over contracts are unchanged.

## Validation

- `257` focused feedback, workspace-state-policy, and workspace API tests pass.
  These cover visible board/snapshot/session ownership, duplicate triggers,
  prompt/dispatch/completion failures, `needs_input`, completion, abort, and
  deletion recovery.
- Black, isort, mypy, and `git diff --check` pass.
- The full backend suite reports `579 passed, 61 failed`; all 61 failures match
  the pytest-asyncio running-event-loop baseline already documented on `main`.

## Live Cleanup

Before mutation, workspace `37f14b36-9d5c-4e88-866f-441d6c8367c8` contained:

- task `45e5e7eb-7551-4388-ab01-0f2c67fd5848`, Done,
  `system_internal=true`, `internal_kind=feedback_reaper`;
- five state reports for that task;
- no session row for former agent `cb-agent-2-8b1de3`.

`DELETE /api/workspaces/tasks/45e5e7eb-7551-4388-ab01-0f2c67fd5848`
returned HTTP 204. The post-check found zero task rows, zero active reports, and
zero matching sessions. The delete-safe task record
`task_records/2026-06-08T17-06-24-45e5e7eb-7551-4388-ab01-0f2c67fd5848.json`
and summary-run audit `2691d152-dfcf-4ae4-a51b-8ac450848887` remain for bounded
historical recovery.

## Key Issues / Pitfalls

- Do not use `system_internal` as a UI visibility flag. It expresses review and
  completion policy, while task/session ownership must stay observable.
- Do not mark summary inputs processed at dispatch time. Terminal success is
  the commit boundary; recoverable failures must retain the pending package.
- Keep duplicate detection and creation under the same per-workspace lock.
  Read-then-create without serialization reintroduces competing Reaper tasks.
- A manually aborted Reaper task is archived as Done instead of restartable
  Todo because its staged input has been intentionally discarded. The next
  trigger creates a fresh task and reselects the unprocessed records.
- Deleting a task removes active state and reports but intentionally retains the
  delete-safe task record and summary-run audit.
