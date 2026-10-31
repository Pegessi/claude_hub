# Workspace durable-state corruption quarantine and backup

## Scope and evidence

Task: `87763808-2a65-433e-a8ad-c19a0dea56ac` (branch `codex/rv-state-safety`,
worktree `~/claude_hub_worktree/rv-state-safety`). A cancelled hidden agent
left a complete uncommitted implementation; it was inspected, kept and
hardened in two small areas.

Durable JSON stores owned by the workspace manager:

- `index.json` (`$STATE_ROOT/index.json`) — workspace rows.
- `<workspace-id>/state.json` — tasks, sessions, reports and TaskMailbox
  events.
- `scheduled_tasks.json` — scheduled tasks and run history.

Previously a truncated/torn or structurally unparsable store behaved
differently per loader: the nested loader bubbled generic exceptions after
logging, the schedule loader logged and started empty. Any later save of the
in-memory view could silently replace evidence with a partial/empty store,
and an unknown future field (`extra_forbidden`) crashed startup outright.

## Change boundary

All changes stay inside the workspace-manager persistence/state/scheduling
layer, the `/health` endpoint and tests. No live `~/.claude_hub` data,
service, tmux or ttyd was touched.

### Rotating backup (`_persistence.py`)

- `_atomic_save_state_text` saves all three durable stores: before replacing
  the primary it copies the existing primary to a temp file in the same
  directory, fsyncs and `os.replace`s it onto `<store>.bak`, then writes the
  primary via the existing temp+replace path and fsyncs the directory. A
  failed copy leaves the prior backup untouched. Exactly one backup
  generation is retained.

### Fail-closed load (`_load_durable_json`)

- Missing primary with no backup is the normal empty case.
- Missing primary with an orphan `.bak` (external deletion/lost replace)
  restores the backup and records a marker event; an unusable backup is
  quarantined and the store starts empty. `_load_state` enters the nested
  loader when only `index.json.bak` exists.
- JSON decode error, non-UTF-8 bytes, structural validation failure or read
  `OSError` moves the primary to `<store>.corrupt-<UTC ts>` with a
  never-overwrite counter suffix, records/append an event in
  `<store>.quarantined` (SHA-256, reason, timestamp, recovery outcome),
  validates and restores the rotating backup once, or starts empty. A bad
  backup is itself moved to `<store>.bak.corrupt-<ts>`.
- Restored primary equals the just-validated backup bytes; the backup is
  retained as evidence.

### Structural validators

- Index: object with a `workspaces` list; every entry fully constructs
  `Workspace` through the normalizer.
- Workspace state: object; `tasks`/`sessions`/`reports` lists; unknown task
  fields rejected (legacy keys allowed only when legacy migration applies);
  every task/session/report constructs through its normalizer, including the
  migrated view of legacy payloads; `task_events` entries construct
  `TaskEvent`. The same validator runs against the backup.
- Scheduled tasks: object with two lists; entries must be objects and
  `extra_forbidden` validation errors fail the store; non-extra per-entry
  errors keep the prior "skip entry with a warning" behavior.

### Isolation and surfacing

- A quarantined workspace keeps its index row but loads no entities; other
  workspaces load normally, and later saves never overwrite `.corrupt-*`
  evidence or the marker.
- `state_recovery_warnings()` combines this process's quarantine events with
  on-disk markers (deduped by marker path); `/health` keeps
  `status: healthy` and adds a `warnings` array when markers exist.

## Salvage decisions

- Kept the cancelled agent's approach wholesale: quarantine marker, rotating
  backup, validators and 17 tests all passed on first run.
- Kept the existing fail-closed policy for task-graph cycles
  (`test_*_cycle_fails_closed`): a semantic graph error after structural
  validation still aborts startup; per-store isolation was not extended to
  semantic post-validation failures because those tests pin the stricter
  contract.
- Added: missing-primary/orphan-backup recovery, cross-workspace isolation
  test, dedup of in-memory vs on-disk warning for the same marker, and
  `_backup_path_for` on the static cross-mixin contract (mypy).

## Validation

- `tests/test_workspace_state_quarantine.py`: 21 tests, all tmp-runtime
  isolated (monkeypatched `STATE_ROOT`/`INDEX_FILE`/`SCHEDULED_TASKS_FILE`):
  missing/valid loads, corrupt/structural index, repeated quarantines,
  backup recovery, evidence preservation after save, per-workspace
  corruption with and without backup, OSError-directory root, unrelated
  workspace stays available, rotating backup behavior, corrupt backup,
  scheduled-tasks quarantine/recovery, orphan-backup recovery for index and
  workspace, `/health` warnings.
- Updated contracts: `test_task_storage_contract.py` (unknown fields now
  quarantine instead of crashing/rewriting), `test_task_execution_control.py`
  (forged managed assignment is quarantined and the good backup restored).
- Related regression set (scheduling, mailbox, migration, workspaces,
  snapshot, health, manager structure/types, cold-restart review): 672
  passed. Full backend suite: 5485 passed, 22 skipped, 1 failure
  (`test_maso_preflight.py::test_create_rechecks_unavailable_service_before_tmux_or_tab_state`)
  caused by a sibling feature backend listening on port 11004 on this shared
  machine ("Multiple local MASO services are ready"); the test is
  environment-sensitive, does not touch changed code, and fails the same way
  with mocked transport when a second local MASO port is live.
- `black --check`, `isort --check`, `mypy` (582 files): clean.

## Residual risks

- Quarantine evidence accumulates per event by design (no reaper); cleanup is
  an explicit operator action after reconciliation.
- Crash between backup restore and marker append leaves one recovered
  generation without a marker; the `.corrupt-*` file and logs remain.
- The legacy flat-file migration path keeps its previous load behavior.
- First save after upgrade has no backup yet; a torn first write still
  quarantines to empty.
