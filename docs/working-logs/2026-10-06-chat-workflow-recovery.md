# Chat workflow integration: recovery boundaries

## Scope

This follow-up applies to the workflow candidate after integration with main
`c37b7f3`. It preserves existing Task, report, and ScheduledTask state models.
There is no new scheduler and no automatic replay of uncertain linked work.

## Confirmed failures and fixes

- One-shot Chat-linked tasks are ordinary reviewable tasks, not internal scheduled
  tasks. Both orphan-scan predicates previously omitted them. Include linked
  tasks in both predicates, retain the grace period, and mark a dead-worker
  execution failed with an explicit recovery request instead of redispatching it.
  Cleanup still checks the recorded owned session/tab identity and must not delete
  a shared or reassigned worker.
- Schedule updates replace stored objects. A fire call waiting behind another
  operation must reload the record after acquiring its lock; otherwise an older
  enabled object can launch work after Pause, Stop, or deletion.
- Reconciliation keeps a snapshot while cleanup awaits I/O. A later schedule or
  task can be deleted during that await. Reload each record before use, skip
  deleted records, and do not recreate a deleted TODO task from the old snapshot.
  Stop ignores only the exact missing-task KeyError after interruption, not
  unrelated failures.

The deletion explanation relies on actual suspension during cleanup/interruption,
not an assumed suspension between an unlocked lock check and acquisition.

## Validation

In `/home/tiger/claude_hub_worktree/chat-workflow-main-sync`, with existing real
Python dependencies and task-owned HOME/XDG/state/tmux settings:

- Before the production fix, all 19 new parameterized regressions failed on
  `3173f7d`. Failures included tasks remaining WORKING, extra dispatch after
  Pause/Stop/deletion, missing-record KeyErrors, and a deleted TODO reappearing.
- After the fix, `test_chat_work_recovery_boundaries.py`, `test_chat_work.py`, and
  `test_scheduled_tasks.py` passed: **122 tests**.
- Complete backend mypy passed: **118 source files**. Black/isort and diff checks
  also passed for the changed files.

Evidence: `/tmp/claude-hub-takeover-tests.KvXCMa/workflow-lifecycle-before/` and
`workflow-lifecycle-after/`. These are deterministic tests with mocked execution,
not real provider acceptance. The live smoke harness still needs safety work;
the production service and external Bot resources were not touched.
