# 2026-06-25 — Workspace Resident Self-Driven Agent + Delete Workspace

## System overview

Two related backend additions to the workspace orchestration layer:

1. **Resident self-driven agent** — an optional, per-workspace standing Claude
   session that wakes on a fixed interval and autonomously maintains the
   workspace. It is a new managed-session role (`resident`) that does **not**
   participate in normal task dispatch or review. Each wake-up cycle it:
   - performs any recurring/periodic tasks named in the user directive,
   - maintains the workspace lesson catalog (create/merge genuinely new lessons,
     archive stale ones — only when justified),
   - **proposes** new tasks in `TODO` status for the user to approve. It never
     auto-starts work, spawns workers, merges branches, or performs destructive
     actions.

2. **Delete workspace** — a backend method + `DELETE /api/workspaces/{id}` route
   that tears down a workspace completely (sessions + their ttyd tabs, tasks,
   reports, in-memory dicts, and the on-disk state dir). No prior delete path
   existed for whole workspaces.

## Module design

### Schema (`models/schemas.py`)
- New role `WorkspaceSessionRole.RESIDENT = "resident"`.
- `Workspace` gains: `resident_agent_enabled` (bool, default False),
  `resident_agent_interval_minutes` (int, default 60),
  `resident_agent_session_id` (server-managed), `resident_agent_directive`
  (free-text), `resident_agent_last_run_at` (server-managed datetime).
- `WorkspaceUpdate` exposes the three user-editable fields: `enabled`,
  `interval_minutes`, `directive`.
- `WorkspaceCreate` accepts the same three optional fields with the same
  defaults.

### Manager (`services/workspace_manager/_workspaces.py`)
- `create_workspace` / `update_workspace` persist the new fields. Interval is
  validated `>= 1` on update (and clamped to `>= 1` on create); directive is
  trimmed, empty → `None`.
- `build_resident_agent_prompt(workspace, base_url)` — module-level function
  that renders the self-drive prompt (concise, English, with curl examples for
  the lessons + tasks endpoints and explicit "TODO only / no destructive
  actions" constraints). Re-exported from the package `__init__` so the prompts
  mixin can reach it via `_wm.build_resident_agent_prompt`.
- `_resident_agent_due(workspace, now)` — pure, event-gated due-check (see
  **Trigger design (event-gated)** below). Disabled → never; `last_run_at is
  None` → due once (bootstrap); else due when there is real activity since the
  last run and the debounce has elapsed, OR the interval+jitter backstop has
  elapsed.
- `_workspace_activity_since(workspace_id, since)` / `_resident_jitter_seconds(
  workspace, interval_seconds)` — helpers backing the trigger (activity gate and
  stable jitter, respectively).
- `_tick_resident_agents()` — called at the end of `_background_monitor_loop`
  (after per-workspace dispatch). Iterates workspaces, runs each due one inside
  its own try/except so one failure can't abort the tick.
- `_run_resident_agent(workspace)` — reuses the tracked resident session
  (`resident_agent_session_id`); skips if that session is currently `working`
  (without advancing the timer, so it retries next tick); otherwise creates a
  `RESIDENT`/`claude` session via `ensure_workspace_agent(reuse_existing=False)`,
  sends the self-drive prompt, and stamps `resident_agent_last_run_at = now`.
- `delete_workspace(workspace_id)` — `KeyError` if missing; deletes every
  session's ttyd tab unconditionally (no non-DONE guard, unlike
  `delete_session`), purges `tasks`/`reports`/`sessions`/`workspaces` entries for
  the id, `shutil.rmtree(STATE_ROOT/<id>, ignore_errors=True)`, then
  `_save_state()`.

### Prompts (`_prompts.py`)
- `_build_session_bootstrap_prompt` routes `RESIDENT` to
  `build_resident_agent_prompt`, so the bootstrap and the cycle message stay
  coherent.

### API (`api/workspaces.py`)
- `DELETE /{workspace_id}` → 204, maps `KeyError` → 404. Registered after
  `PATCH /{workspace_id}`; the literal two-segment `/tasks/...` and
  `/sessions/...` delete routes never collide with the single-segment
  `/{workspace_id}`.

## Trigger design (event-gated)

The resident trigger was revised from pure fixed-interval polling to an
**event-driven / activity-gated** design ("Option C") while keeping the cheap
monitor tick as the wakeup. Three layers:

1. **5s cheap tick (wakeup only).** `_background_monitor_loop` runs every
   `WORKSPACE_MONITOR_INTERVAL_SECONDS = 5` and calls `_tick_resident_agents()`.
   The tick is just an opportunity to evaluate `_resident_agent_due`; it does
   not itself fire the agent. This keeps reaction latency low without coupling it
   to the configured interval.
2. **Activity gate + debounce (the event path).** `_workspace_activity_since(
   workspace_id, since)` returns True when there is a real task **outcome** or
   external progress since the last run — concretely, a NON `system_internal`
   task whose `completed_at` / `reviewed_at` / `human_accepted_at` is newer than
   the last run, or a non-resident report created after it. It deliberately does
   NOT count mere task `created_at` / `updated_at`. `system_internal` tasks are
   excluded, and (defense-in-depth) any task/report whose `session_id` matches
   the workspace's `resident_agent_session_id` is also ignored. When such
   activity exists AND at least `RESIDENT_ACTIVITY_DEBOUNCE_SECONDS = 300`
   (5 min) have elapsed since the last run, the resident fires. The debounce
   floor coalesces bursts so a flurry of task outcomes produces at most one run
   per window rather than one run per event.

   > **Self-retrigger subtlety (why outcomes, not creations).** The resident's
   > prompt makes it PROPOSE tasks via `POST /tasks`. Those are
   > non-`system_internal` tasks whose `created_at`/`updated_at` land *after*
   > the just-stamped `resident_agent_last_run_at`. An earlier draft gated on
   > creation/update, so each proposal made `_workspace_activity_since` report
   > "due" again every debounce window (~300s) forever — defeating the hourly
   > cadence and burning LLM calls. The `WORKING`-skip did not help (it only
   > skips while busy; once idle the agent re-fired). Gating on *outcomes*
   > (`completed_at`/`reviewed_at`/`human_accepted_at`, all `None` on a fresh
   > TODO) makes the resident's own proposals invisible to the gate, so the loop
   > cannot form. This also better matches the resident's purpose — it exists to
   > learn from task *records* (real completions), not from the act of creating a
   > TODO. Reports stay an activity signal because workers (not the resident)
   > post them; the `session_id == resident_agent_session_id` exclusion guards
   > against a future prompt that might change that.
3. **Overdue backstop (idle path).** Even with no activity, the resident still
   gets a periodic pass once the full `resident_agent_interval_minutes` plus a
   stable per-workspace jitter offset have elapsed. This is the legacy
   fixed-interval behavior, demoted to a safety net for idle-but-enabled
   workspaces.

**Stable SHA-256 jitter.** `_resident_jitter_seconds` derives a deterministic
offset in `[0, interval_seconds)` from `sha256(workspace.id)` (first 8 bytes,
big-endian, mod interval). We deliberately avoid Python's builtin `hash()`
(randomized per process via `PYTHONHASHSEED`) and any time/random source, so the
offset is identical across processes and restarts and is unit-testable. The
jitter desynchronizes wake-ups across many workspaces that share one interval,
avoiding the thundering-herd / synchronized-poll problem where every workspace's
backstop lands on the same monitor tick.

**Bootstrap.** `last_run_at is None` → due once. The first run stamps the
activity/timer baseline; it does not re-fire on every empty boot because the
baseline is set as soon as it runs.

**The `due()` boolean (verbatim from `_resident_agent_due`):**

```python
if not workspace.resident_agent_enabled:
    return False

last_run = workspace.resident_agent_last_run_at
if last_run is None:
    # Bootstrap: run once to establish the baseline.
    return True

elapsed = now - last_run
interval_seconds = max(1, workspace.resident_agent_interval_minutes) * 60

# Activity-gated fast path: react to real work, but no more than once per
# debounce window.
debounce = timedelta(seconds=RESIDENT_ACTIVITY_DEBOUNCE_SECONDS)
if elapsed >= debounce and self._workspace_activity_since(workspace.id, last_run):
    return True

# Overdue backstop: fixed interval + stable jitter keeps idle workspaces
# ticking and desynchronizes wake-ups across workspaces.
jitter = self._resident_jitter_seconds(workspace, interval_seconds)
backstop = timedelta(seconds=interval_seconds + jitter)
return elapsed >= backstop
```

Net: `due = enabled AND (last_run is None OR (activity_since AND elapsed >=
debounce) OR elapsed >= interval + jitter)`.

## Pitfalls

- **`reuse_existing` only matches ORCHESTRATOR sessions.**
  `ensure_workspace_agent` with a non-dispatcher/non-reviewer role and
  `reuse_existing=True` would reuse an *orchestrator* via
  `_first_available_workspace_agent` (ORCHESTRATOR-only). The resident agent must
  therefore track and reuse its own session through
  `workspace.resident_agent_session_id` and pass `reuse_existing=False`.
- **RESIDENT must stay out of dispatch/review.** Verified: `_workspace_agents`
  (the dispatch target source) filters to `ORCHESTRATOR`, and all reviewer paths
  filter to `REVIEWER`. A `resident` session is naturally excluded from both.
- **Package attribute shadowing in tests.** `claude_hub.services.__init__`
  rebinds the name `workspace_manager` to the *singleton*, so
  `import claude_hub.services.workspace_manager as _wm` yields the
  `WorkspaceManager` instance, not the module. Use
  `importlib.import_module(...)` to get the module (needed to monkeypatch
  `STATE_ROOT` / `INDEX_FILE`). `INDEX_FILE` is also bound into the
  `_persistence` and `_state` submodule globals via `from ._constants import *`,
  so hermetic tests patch it in both submodules plus the package.
- **mypy `has-type` on rebinding inherited dicts.** `delete_workspace`
  reassigns `self.tasks` / `self.reports` (initialized in `_StateMixin`); add
  annotation-only declarations on `_WorkspacesMixin` so mypy can type the
  comprehension rebinds.

## Coherence contract (frontend must match)
- User-editable field names: `resident_agent_enabled`,
  `resident_agent_interval_minutes`, `resident_agent_directive`.
- Role string: `"resident"`.
- Delete route: `DELETE /api/workspaces/{workspace_id}` → 204.
