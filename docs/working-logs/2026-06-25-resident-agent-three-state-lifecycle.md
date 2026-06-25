# Resident Agent: Three-State Lifecycle (Enable / Pause / Delete)

Date: 2026-06-25
Scope: backend only (`workspace_manager`, schemas, tests)

## Overview

The per-workspace resident agent is a standing, self-driven session that wakes
on an interval to maintain the workspace (lesson catalog, task proposals). It
now has a clean three-state lifecycle controlled from the workspace config:

| State | Field(s) | Session/tab | Auto scheduling |
| --- | --- | --- | --- |
| **Enable** (master switch) | `resident_agent_enabled` | created/reused while ON; torn down when turned OFF | runs while enabled and not paused |
| **Pause** | `resident_agent_paused` (NEW) | kept alive (user can open the terminal and chat manually) | suppressed (no self-drive runs) |
| **Delete** | via `delete_session` on the resident session | fully removed (session + tab) | disabled afterward (`enabled=False`) |

The key distinction: **Pause keeps the session; Disable and Delete tear it
down.**

## Module design

### schemas.py

New field `resident_agent_paused`:

- `Workspace.resident_agent_paused: bool = False`
- `WorkspaceCreate.resident_agent_paused: bool = False`
- `WorkspaceUpdate.resident_agent_paused: Optional[bool] = None`

### `_workspaces.py`

- `_resident_agent_due`: returns `False` when `resident_agent_enabled` is False
  **or** when `resident_agent_paused` is True. Order: disabled OR paused -> not
  due. Paused therefore stops automatic runs while leaving the session intact.
- `update_workspace`:
  - Handles `resident_agent_paused` (set when not None).
  - **Disable teardown**: when `resident_agent_enabled` flips True -> False in
    this update, the resident is torn down the same way as the launch-config
    invalidation path: clear `resident_agent_session_id`, pop the
    `ManagedSession` (the now-session-less tab becomes an orphan that the
    existing `_prune_orphan_workspace_tabs` reconciler removes — `update_workspace`
    is sync and cannot await `delete_tab`), and additionally reset
    `resident_agent_last_run_at` to None so a future re-enable starts clean.
  - Pausing (`resident_agent_paused=True`) deliberately does NOT trigger
    teardown: `resident_agent_session_id` and the `ManagedSession` stay intact.

### `_sessions.py`

- `delete_session`: when the deleted session is a workspace's resident (scan
  `self.workspaces` for `resident_agent_session_id == session_id`, mirroring the
  existing `dispatcher_session_id` clearing), it clears
  `resident_agent_session_id`, resets `resident_agent_last_run_at` to None, and
  sets `resident_agent_enabled=False`. Disabling on delete is deliberate: Delete
  means "stop it", not "restart next tick" — otherwise the next resident tick
  would recreate the session the user just deleted.
- Dispatcher + resident clearing are merged into a single `model_copy` on the
  workspace.

## Key issues / pitfalls

- `update_workspace` is synchronous, so it cannot `await ttyd_manager.delete_tab`.
  Both disable and launch-config invalidation rely on the orphan-tab pruner to
  remove the now-session-less tab. Only `delete_session` / `delete_workspace`
  (both async) call `delete_tab` directly.
- The resident does not own tasks (it proposes unassigned TODOs; its own
  session id is not set on proposed tasks), so `delete_session`'s non-DONE-task
  refusal generally does not block deleting a resident.
- Pause must never tear down: keeping the session alive is the whole point of
  Pause (manual chat without auto-drive).

## Tests (`tests/test_workspace_resident_agent.py`)

- `test_resident_due_false_when_paused`
- `test_update_workspace_disable_tears_down_resident`
- `test_update_workspace_pause_keeps_resident_session`
- `test_delete_session_clears_resident_pointer_and_disables`
