# Resident Behavior Optimization: Run-Now, Next-Run Visibility, Periodic Tasks

Date: 2026-07-01
Scope: backend (`workspace_manager`, schemas, api) + frontend
(`AgentWorkspaceView.vue`, `workspaceStore.ts`, `types`)
Task: workspace `d536f005-...` "resident行为优化" (reviewed mode, review_passed).

## Problem

The resident agent was intended to run *periodic tasks* OR act on *an updated
guiding directive* ("optimize the project"), but the interaction did not match
that intent. Three concrete complaints:

1. **Directive edits had unclear behavior** — editing the free-text directive
   gave no signal about *what* it changed or *when* it would take effect.
2. **No visibility into when the resident would next run** — the interval +
   activity-gated trigger is opaque; the UI only showed "last run".
3. **Periodic tasks were unmanageable** — recurring work had to be buried in
   the free-text directive, with no add/edit/remove/enable affordance.

## Solution overview

Four coordinated changes, all within the approved Goal Packet:

| Complaint | Change |
| --- | --- |
| Directive timing unclear | Split save affordances: **Save** = applies next scheduled cycle; **Save & run now** = save then trigger immediately. Hint text spells this out. |
| No next-run visibility | New advisory `resident_agent_next_run_at` + a live countdown ("next run in 4m", "due now", "queued", "paused") in the status card. |
| Can't trigger on demand | New `POST /{id}/resident/run` endpoint + "Run now" buttons (status card + agent-manager list). |
| Periodic tasks unmanageable | New structured `resident_agent_periodic_tasks` list (add/edit/remove/enable), persisted on the workspace, rendered into **both** resident prompts as an explicit every-cycle checklist. |

**Out of scope (deliberately):** per-task independent cron schedules, and any
rework of the existing event-gated trigger semantics. Periodic tasks all run
every cycle; they are a structured replacement for stuffing recurring work into
the directive, not a scheduler.

## Data model (`schemas.py`)

New Pydantic model:

```python
class ResidentPeriodicTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str
    enabled: bool = True
```

New workspace fields:

- `Workspace.resident_agent_periodic_tasks: list[ResidentPeriodicTask] = []`
- `Workspace.resident_agent_run_requested_at: Optional[datetime] = None`
  (manual run-now flag)
- `Workspace.resident_agent_next_run_at: Optional[datetime] = None`
  (advisory UI hint)
- `WorkspaceCreate.resident_agent_periodic_tasks: list[...] = []`
- `WorkspaceUpdate.resident_agent_periodic_tasks: Optional[list[...]] = None`
  (`None` = "not provided", so a PATCH that omits it leaves tasks untouched;
  an empty list clears them).

`ResidentPeriodicTask` is re-exported through `models/__init__.py` and the
`workspace_manager/_constants.py` `__all__` chain so the mixins see it via
`from ._constants import *`.

## Backend behavior (`_workspaces.py`)

### Prompt rendering — `_render_periodic_tasks_block`

Renders the **enabled** periodic tasks as a numbered "Recurring tasks to perform
EVERY cycle" checklist. Returns `""` when there are no enabled entries, so the
prompt stays **byte-for-byte identical** to the pre-feature text for any
workspace that never configured periodic tasks (backwards compatibility). The
block is injected after the directive block in both the normal maintenance
prompt and the master/Autopilot orchestrator prompt
(`_build_resident_master_prompt` gained a `periodic_block` parameter).

### Normalization — `_normalize_periodic_tasks`

Trims each `text`, drops entries that are blank after trimming, preserves order,
and keeps each client-supplied `id` (via `model_copy(update=...)`) so UI rows
stay stable across edits. Applied on both create and update.

### Manual run-now — `request_resident_run` + `_resident_agent_due`

`request_resident_run(workspace_id)`:

- `KeyError` if the workspace is missing (→ 404 at the API layer).
- `ValueError` if the resident is not enabled (→ 400) — nothing to run.
- Otherwise stamps `resident_agent_run_requested_at = now`.

`_resident_agent_due` gained a run-now override, **checked before the paused
early-return**:

```
due = enabled AND (run_requested
                   OR last_run is None
                   OR (activity_since AND elapsed >= debounce)
                   OR elapsed >= interval + jitter)
```

Semantics: run-now **respects Enable** (a disabled resident is never driven) but
**bypasses Pause** and the interval/activity gates — an explicit user request is
a deliberate one-off, valid even while auto-scheduling is paused.

`_run_resident_agent` clears `resident_agent_run_requested_at` when the cycle
actually fires (one-off consumed) and recomputes `resident_agent_next_run_at`
from the new `last_run`. Crucially, the pre-existing **WORKING-skip** defers a
busy resident *without* clearing the flag, so a run requested while the resident
is mid-cycle is not lost — it fires on the next idle tick.

### Next-run hint — `_resident_next_run_at`

Advisory only; mirrors the overdue-backstop arm of `_resident_agent_due`
(`last_run + interval + stable jitter`). Returns `None` when disabled, paused,
or `last_run is None` (bootstrap — due immediately, no future time to show). The
activity fast-path can still wake the resident *earlier* than this value; the UI
copy makes that explicit. Recomputed on every `update_workspace` (interval,
enable, or pause may have changed) and on every fire. **The authoritative
trigger remains `_resident_agent_due` — this field never gates execution.**

## API (`api/workspaces.py`)

New endpoint:

```
POST /api/workspaces/{workspace_id}/resident/run  -> Workspace
```

Calls `workspace_manager.request_resident_run`; `KeyError → 404`,
`ValueError → 400`. Returns the updated workspace with the run-now flag stamped.

## Frontend

### Store (`workspaceStore.ts`)

`runResidentNow(workspaceId)` — POSTs `/workspaces/{id}/resident/run`, merges the
returned `Workspace` into both `workspaces.value` and `board.value`.

### Component (`AgentWorkspaceView.vue`)

- **Periodic-task editor** in the resident sub-modal: a "Recurring tasks" field
  with an enabled-count badge, one row per task (enable checkbox + text input +
  remove button), an empty-state hint, and an "+ Add recurring task" button.
  Edits mutate a form-local draft (`workspaceForm.resident_agent_periodic_tasks`)
  cloned on open; `sanitizePeriodicTasks` trims/drops-blank before submit.
- **Directive timing clarity**: placeholder/hint now read "Saving applies on the
  next scheduled cycle. To apply a changed directive immediately, use 'Save &
  run now'." A new **Save & run now** primary button (edit mode + resident
  exists) saves then calls `runResidentNow`.
- **Run now** `LoadingButton` in the status card and the agent-manager list
  (disabled while a run is already queued).
- **Next-run countdown** in the status-card meta: `residentNextRunLabel` derives
  "queued" / "paused" / "due now" / "in {duration}" from
  `resident_agent_run_requested_at`, `resident_agent_paused`, and
  `resident_agent_next_run_at` against the shared elapsed clock.

### Types (`types/index.ts`)

Added `ResidentPeriodicTask` interface and the three new `Workspace` fields plus
the create/update variants.

## Tests

- `test_workspace_resident_agent.py`: +14 unit tests — periodic-task
  normalization (create/update), prompt rendering (enabled renders / empty omits
  / master mode renders), run-now flag stamping + missing/disabled errors,
  run-now overriding interval **and** pause, flag consumed on fire, flag
  **kept** when the resident is busy (WORKING-skip), the `_resident_next_run_at`
  helper, and next-run recompute on update.
- `test_workspaces.py`: +5 API tests — periodic-tasks roundtrip (create trims &
  drops blanks; PATCH replaces), run-now endpoint 200 + flag stamped, run-now
  404 (missing workspace), run-now 400 (resident disabled).

## Validation

- Backend: `black` clean (85 files), `isort` clean, `mypy` clean (59 files).
- Frontend: `eslint` clean, `vue-tsc` + `vite build` clean.
- Backend tests via the **canonical CI invocation**
  (`pytest --ignore=tests/test_terminal_replay.py
  --ignore=tests/test_terminal_input_latency_perf.py`): **536 passed, 0 failed**.
- New resident + API tests pass in isolation (60 + 118 respectively).

### Pre-existing full-suite event-loop pollution (not a regression)

Running `pytest` with **no ignores** fails ~48–50 tests with `RuntimeError:
asyncio.run() cannot be called from a running event loop`. This reproduces on a
**pristine `main` checkout** (48 failed / 493 passed) — the polluter is
`tests/test_terminal_input_latency_perf.py` (and `test_terminal_replay.py`),
which leak a running event loop that poisons every later `asyncio.run()` call.
This is exactly why CI `--ignore`s both files, and why CI is green on `main`.
The runtime also gives it away: 379s (full, polluted) vs 13s (CI path).
The optimization work here is fully green under the CI path.

## Key issues / pitfalls

- **Backwards compatibility**: the periodic block must render `""` (not a stray
  header) when there are no enabled tasks, or every legacy resident's prompt
  would change. Covered by `test_build_resident_prompt_no_periodic_block_when_empty`.
- **Run-now must survive a busy resident**: consuming the flag unconditionally
  would drop a request made while the resident is mid-cycle. The flag is cleared
  only when the cycle actually fires; the WORKING-skip defers without clearing.
- **`next_run_at` is advisory**: it mirrors the *backstop* arm only. The
  activity fast-path can fire earlier, so the UI never presents it as a hard
  guarantee, and it is never consulted by `_resident_agent_due`.
- **Do not run the full backend suite as one process to gauge green-ness** — use
  the CI ignore list, or the perf file's leaked loop will mask real results.
