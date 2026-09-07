# 2026-09-08 — Scheduled Tasks (Cron / Interval / One-off) + Mobile Floating-Ball Fix

## System overview

Two unrelated changes shipped on this branch:

1. **Mobile floating-ball fix** — the `MobileControls` floating keyboard ball
   (bottom-right) was still rendered over the chat UI (`StructuredPane`). It is
   now hidden there; it only shows over the plain terminal view.
2. **Scheduled-task mechanism** — a durable schedule that fires an action when
   its next-run time arrives, modeled on Codex's scheduled executions but
   extended with a Hub-native task variant. This is the bulk of the work.

A `ScheduledTask` (model in `backend/claude_hub/models/schemas.py`) has exactly
one schedule field — `run_at` (one-off), `cron` (5-field), or
`interval_seconds` — and one of three **kinds**, which map to the three
variants the user asked for:

| Kind | User's variant | Behavior on fire |
| --- | --- | --- |
| `session_message` | **A** — agent self-scheduling | Send a message to an existing managed session. An agent calls the `claude-hub` CLI (which hits the scheduling API) to register a schedule that re-messages its own session. |
| `new_session` | **B** — manual creation, "new session to execute" | Create a new session in a workspace and send it a message. One-shot only. |
| `hub_task` | **C** — Hub-native task scheduling | Publish a system-internal task on a caller-owned ephemeral orchestrator. The task runs the normal reviewed flow; on worker completion it is auto-DONE (skipping human review) and the ephemeral session is auto-deleted — no agent / reviewer resources held. |

Schedules are persisted to `STATE_ROOT/scheduled_tasks.json` (atomic write) and
hydrated at startup after the core workspace state. The tick is driven by the
5-second background monitor loop (`_background_monitor_loop` →
`_tick_scheduled_tasks`).

## Module design

### `models/schemas.py`

- `ScheduledTaskKind` — enum: `session_message`, `new_session`, `hub_task`.
- `ScheduledTask` — durable schedule. Holds the schedule spec, the kind-specific
  payload (`session_id` / `workspace_id` + `agent_type` / `task_title` +
  `message`), and run bookkeeping: `enabled`, `next_run_at`, `last_run_at`,
  `run_count`, `last_status`, `last_error`.
- `ScheduledTaskCreate` / `ScheduledTaskUpdate` — create and patch payloads.
  `kind` is immutable on update (delete + recreate to change it).
- `ScheduledTaskRunResult` — what the run-now endpoint returns: `id`,
  `last_run_at`, `last_status`, `last_error`. Deliberately omits `enabled` /
  `next_run_at` (see the frontend pitfall below).

### `services/workspace_manager/_scheduling.py` (mixin)

All scheduling logic lives in `_SchedulingMixin`, composed into
`WorkspaceManager`. It does `from ._constants import *`, which binds
`ScheduledTask*`, `asyncio`, `logger`, `json`, `uuid`, `datetime`, `timedelta`,
`Optional`, and the request/role enums into its namespace.

- **Persistence** — `_load_scheduled_tasks` / `_save_scheduled_tasks`. The save
  uses `model_dump(mode="json")` (datetimes → ISO strings) and
  `_atomic_write_text`.
- **CRUD** — `create_scheduled_task`, `list_scheduled_tasks`,
  `get_scheduled_task`, `update_scheduled_task`, `delete_scheduled_task`.
  - Names are stripped; whitespace-only names are rejected.
  - On update, when any schedule field changes the other two are normalized to
    `None` (so switching cron → run_at doesn't leave two set) and `next_run_at`
    is recomputed.
- **Validation** — `_validate_scheduled_task_fields` enforces: exactly one
  schedule field; cron is parseable; and per-kind payload requirements
  (`session_id` exists for `session_message`; `workspace_id` exists for
  `new_session` / `hub_task`; `task_title` set for `hub_task`). It also enforces
  the **`new_session` one-shot restriction** (see pitfalls).
- **Cron parser** — self-contained 5-field parser.
  `_CRON_FIELD_RANGES = ((0,59),(0,23),(1,31),(1,12),(0,6))`. Supports `*`,
  ranges (`1-5`), lists (`a,b,c`), steps (`*/10`, `5/10`). The DOM/DOW match
  uses the standard **OR rule**: if both DOM and DOW are restricted, a day
  matches if *either* matches; if one is `*`, only the other is used. Python's
  weekday (Mon=0) is converted to cron DOW with `(dt.weekday() + 1) % 7`.
- **Next-run computation** — `_compute_next_run` (dispatches on the schedule
  field) and `_next_cron_run` (minute-stepping search with month/day/hour
  skip-ahead for speed).
- **Tick + fire** — `_tick_scheduled_tasks` (iterates enabled, due tasks) and
  `_fire_scheduled_task` (the core fire routine, shared by the tick and manual
  run-now).
- **Fire actions** — `_fire_new_session` and `_fire_hub_task`, plus the
  `_best_effort_delete_session` helper.

### `api/scheduled_tasks.py`

REST router under `/api/scheduled-tasks`:

- `GET ""` — list.
- `POST ""` — create (201; 400 on validation error).
- `GET "/{id}"` — fetch (404 if missing).
- `PATCH "/{id}"` — update (404 / 400).
- `DELETE "/{id}"` — delete (204; 404 if missing).
- `POST "/{id}/run"` — manual run-now. Stamps and advances the schedule just
  like a tick fire. Returns **400** if the task is disabled and **500** if the
  fire side-effect failed (the task's `last_status` / `last_error` are still
  persisted either way).

### `cli/commands/schedule.py`

`claude-hub schedule` command group: `list`, `get`, `create`, `update`, `delete`,
`enable`, `disable`, `run`. The CLI client raises `HubError` on any `status_code
>= 400`, so `schedule run` exits non-zero automatically when the run endpoint
returns 500. This is the primitive an agent uses for self-scheduling (variant A).

### Frontend

- `stores/scheduledTasksStore.ts` — Pinia store: `tasks`, `isLoading`,
  `isMutating`, `error`, `enabledCount`, and the CRUD/run actions.
- `components/ScheduledTasksPanel.vue` — modal panel with a list view (per-task
  enable toggle, Run, Edit, Delete) and an edit view (kind picker, schedule
  picker, kind-specific payload). Opened from the toolbar.

## Key issues / pitfalls

These were all caught by the sub-agent review and fixed before staying in the
branch:

- **Double-fire (no lock).** The 5s tick and a manual run-now (or two run-now
  clicks) could both stamp and fire the same task. Fix: a per-task
  `asyncio.Lock` (`self._sched_fire_locks` in `_state.py`) taken in
  `_fire_scheduled_task`, with an **in-lock eligibility re-check** so a task
  already advanced by a concurrent fire is not re-fired.
- **Ephemeral-session leak on fire failure.** `_fire_new_session` /
  `_fire_hub_task` created an ephemeral session, then if the send/dispatch threw,
  the session was stranded. Fix: wrap the send/dispatch in try/except →
  best-effort delete the session (`_best_effort_delete_session`, never raises) →
  re-raise. `_fire_hub_task` also marks the internal task `FAILED`.
- **Recurring `new_session` leaks a session per fire.** A `new_session` task on
  a cron/interval spawns a fresh ephemeral session every fire with no completion
  signal to clean it up. Fix: `new_session` is restricted to one-shot `run_at`;
  recurring "execute on a schedule" should use `hub_task`, which auto-cleans its
  ephemeral session on completion.
- **Failed fire returned 200 / exit 0.** A fire whose side-effect threw was
  persisted as `last_status=error` but the API still returned 200 and the CLI
  exited 0. Fix: `run_scheduled_task` raises `RuntimeError` on fire failure →
  the API returns 500 and the CLI exits non-zero.
- **Feb-29-only cron killed by a 366-day window.** `_next_cron_run` searched
  only 366 days ahead, so a schedule like `0 0 29 2 *` (whose next match can be
  up to ~4 years out) was falsely reported as having no future run. Fix: the
  search window is extended to ~4 years (`366 * 4` days).
- **Whitespace-only names.** Create/update accepted a name of only whitespace.
  Fix: strip and reject empty.
- **Frontend: `runTask` left `enabled` / `next_run_at` stale.** The run result
  omits these fields, but they change on fire (a one-shot is disabled, a
  recurring task's `next_run_at` advances). The local copy only updated
  `last_run_at` / `last_status` / `last_error` / `run_count`, leaving the row
  showing a disabled one-shot as still enabled. Fix: `runTask` sets `isMutating`
  and re-fetches authoritative state (`fetchTasks({ silent: true })`) after the
  fire.
- **Frontend: list-action errors were invisible.** `formError` was set by the
  list actions (toggle / run / delete) but only rendered inside the edit view, so
  a failed list action showed nothing. Fix: hoist the error into a shared
  dismissible banner (with `role="alert"`) rendered above the mode-specific
  body.
- **Frontend: toggle had no accessible name.** The visually-hidden checkbox had
  no label text for screen readers. Fix: add an `aria-label`.
- **mypy gap (structural, not yet fixed).** `pyproject.toml` overrides
  `module = "claude_hub.services.workspace_manager.*"` with
  `disable_error_code = ["attr-defined", "no-any-return"]`. Cross-mixin
  `self.<async-method>` calls resolve to `Any`, so a missing `await` is invisible
  to mypy. **Runtime tests are the safety net** — the 47 scheduled-task tests
  exercise every fire path.

## Validation

- Backend: `black`, `isort`, `mypy` clean on the touched files.
- Backend suite (excluding the known-hanging Playwright e2e files):
  **1348 passed, 3 failed** — the 3 failures (`test_root_endpoint`,
  `test_legacy_resident_mailbox_routes_are_gone`,
  `test_real_cold_restart_7tab_bijection`) are pre-existing/environmental and
  unrelated to scheduling.
- `test_scheduled_tasks.py`: **47 tests pass**, covering CRUD, cron parsing,
  next-run computation (incl. Feb-29), tick eligibility, all three fire actions,
  the fire lock (concurrent double-fire), and the error-propagation paths.
- Frontend: ESLint clean, `vue-tsc` type check + `vite build` clean, 276 unit
  tests pass.
