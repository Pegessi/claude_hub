# 2026-09-23 — Scheduled hub_task: missing worker binding + no orphan convergence

## Summary

A scheduled `kind=hub_task` (the Hub-native "execute a task on a schedule"
action, shipped in c902cea) had **never actually completed since launch**. A
read-only investigation (`eacb2b81`) proved two layered defects. This change
fixes both and replaces the stub-heavy tests that let the bug ship with real
end-to-end coverage.

The task publishes a `system_internal / internal_kind="scheduled"`
`WorkspaceTask`, runs it on a fresh **caller-owned ephemeral orchestrator**, and
on completion is supposed to auto-DONE (skipping human review) and delete that
ephemeral session so it holds no resources.

## E1 — fatal: the task→session binding was never persisted

### Root cause

`WorkspaceManager._fire_hub_task` (`_scheduling.py`) created the internal task,
called `ensure_workspace_agent(..., ephemeral=True, caller_owned_ephemeral=True,
reuse_existing=False)`, set the task QUEUED, and then called
`_dispatch_task_to_session(internal_task, session)` directly.

`_dispatch_task_to_session` (`_dispatch.py`) wrote only the **session side** of
the relationship — `session.task_id` / `session.current_task_id` — and flipped
`task.status` to WORKING. Its task update did **not** include `session_id`.

Contrast the normal entrypoint `start_task`, which persists
`session_id = target.id` (`_dispatch.py`, the `task.model_copy(update={...
"session_id": target.id ...})` block) before the dispatch pass.

Consequences of `task.session_id is None`:

- The worker's first report hit report intake and was rejected 400 by
  `_ensure_session_may_report_task` (`_reports.py`): **"Task has no assigned
  worker session"** (unassigned tasks fail closed and cannot be claimed by a
  worker report). The task therefore never reached COMPLETED.
- The promised auto-DONE (`_handle_internal_task_report`) and
  `_auto_cleanup_scheduled_session` (delete the ephemeral) were unreachable.
- `abort_task` / `_release_task_session`, both keyed on `task.session_id`,
  could not find or interrupt/release the ephemeral.

### Why the tests missed it

`test_fire_hub_task_publishes_internal_task_and_dispatches` **stubbed
`_dispatch_task_to_session`**, so it never observed the missing binding; and the
completion tests called `_handle_internal_task_report` **directly**, bypassing
the real `create_report` intake guard that rejects an unassigned task. Both key
links in the broken chain were mocked away.

### Fix

`_dispatch_task_to_session` now persists the canonical `task.session_id` in the
**same crash-idempotent pre-send transaction** that already sets
`session.task_id` (claim-before-send), and re-reads the task before the
post-send update so the stale parameter object (whose `session_id` may still be
`None`) cannot clobber it. The post-send WORKING update also carries
`session_id`. `_recover_queued_task_ownership` was hardened the same way
(`session_id=task.session_id or session.id`) so a crash between claim and send
keeps the binding.

**Why the normal `start_task` path is unaffected:** a queued task only becomes
eligible for dispatch via `_next_queued_task`, which matches
`task.session_id == session_id`, so for that path the value is already correct
and the new write is an idempotent no-op — never a re-bind. The only behavior
change is that direct callers (the hub_task fire path) now get the binding they
were always meant to have. No double-bind on retry: call_id-dedup plus the
equality guard mean an in-flight attempt's session is never silently rebound.

## E2 — observed wedge: a dead early worker had no convergence path

### Root cause

In the live incident the ephemeral's tmux died within minutes, before the worker
ACKed. After the 300 s processing lease, dispatch turned `uncertain`
(fail-closed) and the session was STOPPED / OFFLINE. A reviewed, WORKING,
system task with a dead/missing session then had **no** exit:

- The merged cold-wake (9ce5b4c / d73dcba) covers native **chat_turn** only.
- `dispatch_workspace` / `_recover_queued_task_ownership` / migration all
  require a QUEUED task — this one is WORKING.
- The failure detector in `_refresh_session_statuses` applies session-death /
  timeout only to **SUBAGENT** tasks (reviewed/autonomous keep other paths).
- An `uncertain` dispatch is fail-closed against an automatic resend to the
  SAME session.

Result: the task hung in WORKING forever and the stopped ephemeral was never
reclaimed.

### Fix — `_converge_orphaned_hub_tasks`

A new sweep runs at the end of every `_tick_scheduled_tasks` (after the monitor
has refreshed session statuses). For each WORKING scheduled hub_task
(`system_internal and internal_kind == "scheduled"`) whose bound session is
**dead** — missing, `STOPPED`, `OFFLINE`, or in another workspace — and which is
past `HUBTASK_ORPHAN_GRACE_SECONDS` (60 s), per task under a dedicated per-task
asyncio lock:

1. **Redispatch** (while `dispatch_attempt < HUBTASK_ORPHAN_MAX_ATTEMPTS = 3`):
   spawn a FRESH caller-owned ephemeral worker first, then re-bind the task to
   it, bump `dispatch_attempt`, set QUEUED, and call the real
   `_dispatch_task_to_session`. The new attempt uses a brand-new
   `dispatch:{id}:{attempt}` call_id, so the fail-closed `uncertain` call on the
   dead session is **never** auto-resent. Only after the task is canonically
   bound to the live worker is the dead ephemeral deleted (deletion is ordered
   this way because `delete_session` refuses to remove a session still
   referenced by a non-terminal task).
2. **Fail + cleanup** once attempts are exhausted, or no replacement can be
   spawned / the redispatch raises: mark the task FAILED with a reason, record a
   system audit report, and best-effort delete the dead ephemeral.

The gate is keyed on a demonstrably **dead** session, never on age, so a healthy
long task — no matter how old — is untouched. Ordinary (human) reviewed tasks
are filtered out by the `internal_kind == "scheduled"` predicate, preserving the
independent-reviewer and long-task guarantees. Native chat_turn cold-wake and
the subagent reaper are separate code paths and are not modified. No double
fire: the per-task lock plus a re-read of live state under the lock collapse
overlapping ticks to one convergence action.

## Tests (real end-to-end, no stubbed key paths)

Added to `tests/test_scheduled_tasks.py`. Only the terminal control plane
(tmux/ttyd create/update/delete, paste, ready probe, stream discard) is faked —
exactly the side effects unavailable in CI. `_fire_hub_task`,
`ensure_workspace_agent`, `_dispatch_task_to_session`, `create_report`, and
`delete_session` all run for real.

- `test_e2e_hub_task_fire_binds_session_and_real_report_completes_and_cleans`:
  fire → real ephemeral spawn + real dispatch persists `task.session_id` →
  worker submits through the **real** `create_report` intake → DONE → ephemeral
  session and tab deleted.
- `test_e2e_hub_task_worker_dies_early_redispatches_then_completes`: first
  worker STOPPED/OFFLINE with an uncertain dispatch past grace → bounded
  redispatch to a fresh ephemeral (new call_id, dead one never re-sent, dead
  session deleted) → replacement completes through real intake.
- `test_e2e_hub_task_dead_worker_exhausts_retries_fails_and_cleans_session`:
  attempt budget exhausted → FAILED + session cleaned; a repeat sweep is a no-op
  (no respawn storm).
- `test_hub_task_orphan_convergence_leaves_healthy_and_non_scheduled_tasks_alone`:
  a healthy (WORKING-runtime) scheduled task even 6 h old is not touched, and a
  dead-worker ordinary human reviewed task keeps its pre-existing contract.

## Files

- `services/workspace_manager/_dispatch.py` — persist `task.session_id` in the
  pre-send claim and post-send WORKING update; harden queued-ownership recovery.
- `services/workspace_manager/_scheduling.py` — orphan convergence sweep +
  per-task convergence; tick hook.
- `services/workspace_manager/_state.py` — `_hubtask_orphan_locks`.
- `services/workspace_manager/_constants.py` — grace + max-attempts constants.
- `tests/test_scheduled_tasks.py` — four real end-to-end tests.
