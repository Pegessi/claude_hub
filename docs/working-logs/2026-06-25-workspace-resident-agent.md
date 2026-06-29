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
  `resident_agent_interval_minutes`, `resident_agent_directive`,
  `resident_agent_type`, `resident_agent_env`, `resident_agent_solo_mode`,
  `resident_agent_master_mode`.
- Role string: `"resident"`.
- Delete route: `DELETE /api/workspaces/{workspace_id}` → 204.

## Update: configurable agent_type / env / solo_mode (parity with normal agents)
The resident is no longer hardcoded to `AgentType.CLAUDE` with no env. The
`Workspace` model carries `resident_agent_type` (default `CLAUDE`),
`resident_agent_env` (default `{}`), and `resident_agent_solo_mode` (default
`True`); `WorkspaceCreate` accepts them (same defaults) and `WorkspaceUpdate`
exposes them as `Optional[...] = None` (None = unchanged; `resident_agent_env`
replaces wholesale when provided). `_run_resident_agent` builds the
`EnsureWorkspaceAgentRequest` from these workspace fields, so the resident gets
the same agent runtime, env vars, and solo-mode treatment as any normal
workspace agent (the session/ttyd layer already consumed `agent_type`/`env`/
`solo_mode`, so no change there).

- **TERMINAL resident = no self-drive prompt.** A `TERMINAL` resident is a plain
  user shell with no LLM agent listening, so the self-drive prompt would just be
  dumped as literal shell input. For `TERMINAL` we still create/track an openable
  tab and advance `resident_agent_last_run_at` (so it does not churn every tick),
  but skip the prompt on BOTH paths: the create path is suppressed in
  `_build_session_bootstrap_prompt` (returns `""` for TERMINAL+RESIDENT, and
  `ensure_workspace_agent` skips sending an empty bootstrap), and the reuse path
  is guarded directly in `_run_resident_agent`. `CLAUDE` / `CURSOR` / `CODEX`
  are CLI LLM agents and receive the same curl-based resident prompt as before
  (bootstrap routing is by `role`, independent of `agent_type`).

- **Changing resident launch config invalidates and recreates the session.**
  `agent_type` / `env` / `solo_mode` are launch-time properties applied only on
  the CREATE path (the `EnsureWorkspaceAgentRequest` in `_run_resident_agent`);
  the reuse path re-drives the live `resident_agent_session_id` session and does
  NOT rebuild the request. So if `update_workspace` only stored the new config it
  would be silently ignored while a session is alive — worst case `claude ->
  terminal` would keep sending the self-drive prompt to the stale `claude`
  session forever (TERMINAL suppression never triggers because the live session
  is still `claude`). Fix: when an update changes any of
  `resident_agent_type` / `resident_agent_env` / `resident_agent_solo_mode` to a
  DIFFERENT value (helper `_resident_launch_config_changed`), `update_workspace`
  clears `resident_agent_session_id` (set to `None`) and drops the old
  `ManagedSession` from `self.sessions`, so the next monitor tick recreates the
  resident with the new launch config. A no-op write of the same value does NOT
  recreate. Tab teardown: `delete_session`/`delete_workspace` tear down the tab
  with `await ttyd_manager.delete_tab(...)`, but both are async and
  `update_workspace` is sync — so we deliberately do NOT call the async teardown
  here. Dropping the `ManagedSession` row leaves the old tab as a session-less
  orphan, which the existing `_prune_orphan_workspace_tabs` reconciler removes on
  the monitor loop. This keeps sync code sync-safe and reuses the established
  orphan-tab pruner instead of inventing a second teardown path.

## Update: master mode (opt-in self-iteration on the resident's own worktree)

> **⚠️ SUPERSEDED — historical only.** This describes the FIRST cut of master
> mode (resident self-iterates on its own git worktree, writes code, commits).
> That behavior was **removed**: the `_resident_worktree_slug` helper is deleted
> and the master prompt was fully rewritten. Master mode is now an autonomous
> ORCHESTRATOR — see **"Update: master mode is now an orchestrator (not a
> coder)"** below for the current design. The text in this section is kept for
> history; do not treat it as the live contract.

The base resident is deliberately read-only: it proposes `TODO` tasks and curates
lessons but posts no reports, so a healthy idle resident is visually
indistinguishable from a stuck one ("looks busy but nothing shows"). **Master
mode** is an opt-in answer to that: a new boolean `resident_agent_master_mode`
(default `False`) on `Workspace` / `WorkspaceCreate` (concrete `bool = False`) and
`WorkspaceUpdate` (`Optional[bool] = None`, None = unchanged), wired through
`create_workspace` (constructor copy) and `update_workspace` (guarded block).

- **Two prompts, one builder.** `build_resident_agent_prompt(workspace, base_url,
  session_id)` gained a third arg (`session_id`) and branches on
  `workspace.resident_agent_master_mode`. OFF returns the legacy prompt **byte-for-
  byte unchanged**; ON delegates to `_build_resident_master_prompt(...)`. Both call
  sites pass the live session id: the create/bootstrap path
  (`_prompts._build_session_bootstrap_prompt` → `self._report_base_url(session)`,
  `session.id`) and the reuse path (`_run_resident_agent`, `session.id`). The id is
  needed so the master prompt can hand the resident a **session-scoped** report
  curl (`POST /api/workspaces/sessions/{session_id}/reports`).
- **What master mode authorizes.** The prompt tells the resident to (1)
  **self-provision its own git worktree** on a `resident/<slug>` branch and work
  ONLY inside it — provisioning is **idempotent** (reuse the dir if present; else
  re-attach the branch without `-b` if the branch survived a removed dir; else
  create both fresh), matching CLAUDE.md RULE #1; (2) do **one bounded enrichment
  iteration per wake** then stop (no tight loop); (3) commit only on its own
  branch; and (4) post a **heartbeat report** every cycle. Hard constraints are
  absolute: NEVER merge / rebase-onto-shared / push / force-push / delete outside
  the worktree, NEVER touch the main checkout, NEVER auto-start or dispatch tasks
  or spawn workers. A human integrates the branch later. The agent self-provisions,
  so **no backend git/worktree machinery and no cleanup code were added.**
- **`_resident_worktree_slug` is workspace-unique.** `session_prefix` is derived
  from the (non-unique) workspace name, so the slug appends a short `workspace.id`
  suffix to avoid two same-named workspaces colliding on `resident/<slug>` /
  `../resident-<slug>` when they share a parent repo.
- **Toggling master mode does NOT respawn the resident.** Unlike the launch-config
  fields above, `resident_agent_master_mode` is deliberately **excluded** from
  `_resident_launch_config_changed` and from the `disabling_resident` path. The
  prompt is recomputed fresh every cycle (both paths funnel through
  `build_resident_agent_prompt`), so flipping the flag takes effect on the next
  tick with no disruptive session/tab teardown. The worktree is agent-owned, so no
  cwd/launch property changes either.
- **Heartbeat cannot self-retrigger the activity gate.** The resident now posts
  reports, but `_workspace_activity_since` already excludes reports whose
  `session_id == resident_agent_session_id` (and `resident_agent_session_id` is
  persisted BEFORE the prompt is sent), so a heartbeat never re-arms the
  activity fast-path. The transient `state:"working"` heartbeat does not stall the
  resident either: `_refresh_session_statuses` overwrites `runtime_status` from live
  ttyd state on every tick before `_tick_resident_agents` runs.
- **Frontend legibility.** `AgentWorkspaceView.vue` adds the Master-mode checkbox
  (mirrors the paused toggle through form default / create+save payload / reset /
  edit-hydrate), a `· Master` pill on the summary row, a **Master** badge on the
  resident status card, and a `latestResidentReport` computed (newest report whose
  `session_id` matches the resident) rendered as a "last run … ago" + latest-
  heartbeat meta line — reusing the existing `parseTimestampMs` /
  `formatElapsedDuration` / `elapsedClockMs` helpers. This is what finally makes the
  resident's per-cycle work visible on the board.

### Update: resident lifecycle buttons

The Resident Agent config popup's bottom row previously held a single **Done**
button that only hid the sub-modal; the resident fields were persisted solely
when the parent workspace modal was saved. That bottom row now carries three
explicit lifecycle buttons plus a Done, all in `AgentWorkspaceView.vue`
(frontend-only — the backend already supports everything via existing PATCH
semantics):

- **Create resident** — sends the full resident payload with
  `resident_agent_enabled: true`. The next monitor tick spawns the resident
  session. Disabled once a resident already exists; the "exists" check reads the
  **saved** workspace (`activeWorkspace.value?.resident_agent_enabled`), not the
  editable form flag.
- **Pause / Resume** — toggles `resident_agent_paused` (label flips on
  `activeWorkspace.value?.resident_agent_paused`). The backend deliberately
  keeps the session + tab alive on pause; it only stops auto-scheduling.
- **Delete resident** — confirms via `window.confirm`, then sends
  `resident_agent_enabled: false`. The backend's `disabling_resident` path
  clears the session pointer, drops the `ManagedSession` (the orphan-tab pruner
  removes the tab), and resets `last_run_at`. This deletes **only** the resident
  agent, **not** the whole workspace (no `deleteWorkspace` call).

All three are just `workspaceStore.updateWorkspace(id, <partial payload>)` —
no new endpoint. They **act immediately via PATCH in edit mode**, where the
workspace already exists, and refresh the board through the store (each wrapped
in `runPending('resident:create' | 'resident:pause' | 'resident:delete', …)`
for per-button loading state via `LoadingButton`). The Create payload reuses a
new local `buildResidentPayload(overrides)` helper that returns the resident
`resident_agent_*` slice from the form; `handleSaveWorkspace` was refactored to
use the same helper so the payload shape lives in one place.

**Create-mode behavior (chosen: disabled, not hidden).** In create mode there
is no workspace id to PATCH, so the three lifecycle buttons are **disabled**
(via the `isResidentCreateMode` computed) and a hint says the resident is
configured here and created together with the workspace by the parent "Create
workspace" button (`handleCreateWorkspace` already sends the resident fields).
Keeping the buttons present-but-disabled (rather than hidden) keeps the row
layout stable between create and edit and makes the immediate-API model honest
without faking an id or calling create from the sub-modal.

**Directive-timing hint.** A `<p class="modal-hint">` under the directive
textarea clarifies that a changed directive is saved immediately but only takes
effect on the resident's next scheduled cycle — it does not re-run right away
(保存后于下个周期生效，不会立即重新运行).

Note: an `activeWorkspace`-based pause toggle already existed for the resident
**status card** (`toggleResidentPaused` / `workspace:resident-pause`); the new
sub-modal handlers (`handleToggleResidentPause` / `resident:pause`) are the
modal-scoped equivalents and also mirror the new value back into
`workspaceForm` so the sub-modal checkboxes stay in sync.

### Update: resident modal UI polish

A UI-polish pass addressed three user complaints about the Resident Agent
sub-modal. The **Working Directory** field (the cwd input + Browse button) was
removed entirely: the resident should just use the workspace's own directory.
This is safe because the backend already falls back via
`local_cwd = payload.cwd or workspace.path` (and `_resolve_remote_cwd` →
`workspace.remote_cwd` → profile default) in
`workspace_manager/_sessions.py`, so leaving `resident_agent_cwd` empty makes
the resident run in the workspace dir automatically. The form field, payloads,
reset, and hydration for `resident_agent_cwd` are kept intact (it just stays
`''` → sent as undefined/null), so nothing round-trips incorrectly; only the UI
control and the code that auto-populated it were removed
(`handleResidentTargetChange` no longer touches cwd, `openResidentDirectoryBrowser`
is deleted, and the now-dead `agentBrowserContext` ref + `'resident'`
`browserPlacement` branch + `AgentBrowserContext` type were dropped so the
directory browser serves only the Add-Agent form). The bottom lifecycle button
labels were shortened from "Create resident" / "Delete resident" to **Create** /
**Delete** (the modal title and single-word Pause already supply context), and a
shared `.modal-actions button { white-space: nowrap; }` rule was added so action
labels never wrap/overflow below the button. Finally all resident copy was
rewritten to pure, concise English (no mixed Chinese), with each hint kept to
1-2 rendered lines.

### Update: schedule-legibility copy

A user asked why the resident was "working" when **Master mode was off and no
task was running**. This is by design: `_resident_agent_due` fires on the
**overdue backstop** (`elapsed >= interval_minutes*60 + jitter`) regardless of
Master mode or active tasks, so an enabled-but-idle resident still wakes every
interval to do read-only upkeep (lesson maintenance + TODO proposals, no
reports). The confusion was a copy problem, not a behavior bug — the old
**Enable** hint ("runs on a schedule … propose follow-up tasks") read as if it
only acted when there was something to dispatch.

Fix (copy only, no behavior change):

- **Enable hint** now states it "wakes every interval on its own — even when
  idle, with no task running — to maintain lessons and propose follow-up tasks.
  It never picks up normal workspace tasks." This makes the backstop tick
  explicit and reiterates the resident never takes over normal task dispatch.
- **Master mode hint** now leads with "Changes what each cycle does, not
  whether it runs." — disambiguating it from the Enable/interval scheduling
  controls. On = self-iterate on its own worktree + heartbeat; Off = read-only
  maintenance, no reports; never merges to main.

The decision (vs. changing the trigger to stay idle when there's no activity)
was to keep the backstop intact and fix legibility only — the periodic idle
pass is intentional so lesson hygiene and TODO surfacing keep moving on
long-lived workspaces.

### Update: master mode is now an orchestrator (not a coder)

The first cut of Master mode made the resident **write code on its own git
worktree** and commit there. The user redefined the feature: Master mode should
let the resident act as an **autonomous orchestrator / product-owner** — iterate
on the workspace's requirements, create tasks, drive their execution on existing
worker agents, and **accept (验收)** the results — **without writing code itself
and without adding or deleting agents/reviewers**. The whole worktree
self-iteration behavior (and the `_resident_worktree_slug` helper) was removed;
this is now a prompt-only change with no backend logic, schema, or route edits.

**Master-mode cycle (orchestrator).** Each wake, one bounded pass:
1. `GET /api/workspaces/{ws}/board` → read recent tasks + sessions + directive,
   decide what's needed next.
2. Find usable workers = sessions with `role=="orchestrator"`, not stopped,
   runtime idle/working. **If none exist → do NOT create one; degrade to
   proposal-only (TODO tasks, no dispatch) and say so in the heartbeat.**
3. Create ≤3 **DIRECT-mode** tasks (`"task_mode":"direct"`).
4. Dispatch each via `POST /tasks/{id}/start` with an explicit
   `target_session_id` pointing at an existing orchestrator.
5. Accept its own finished tasks: when `status=="review"`, validate then
   `PATCH {status:"done"}`; if unsatisfactory, `POST /tasks/{id}/continue` with
   feedback (same worker, no spawn). Only ever touches tasks it created.
6. Post a heartbeat report.

**Why DIRECT mode is mandatory.** DIRECT completion routes straight to `review`
awaiting acceptance and **never spawns a reviewer** (`_reports.py:476-485`).
`reviewed`/`autonomous` modes call `_request_task_review` →
`_select_or_create_reviewer`, which **spawns an ephemeral REVIEWER session** when
none is idle (`_review.py:88-102`) — that would violate the resident's "never add
agents" rule. So the orchestrator prompt hard-requires `task_mode:"direct"`.

**Why dispatch is safe re: no-new-agents.** `start_task` auto-creates a default
agent **only when the workspace has zero agents** (`_dispatch.py:46-58`). The
prompt forbids starting tasks unless ≥1 orchestrator already exists and always
passes `target_session_id`, so dispatch reuses an existing session (queuing
behind a busy one is allowed; no spawn). Acceptance via PATCH→done is a pure
state change that frees the session and never spawns.

**Activity-gate nuance.** `_workspace_activity_since` gates the fast-path on task
*outcome* timestamps (not creation) and excludes the resident's own reports, so
creating a TODO + posting a heartbeat does **not** re-arm the resident. A
dispatched task reaching `review`/`done` belongs to the **orchestrator** session
and DOES count as genuine workspace activity (bounded by the activity debounce) —
by design, not a loop.

**Known best-effort limitation.** "Only accept/continue tasks YOU created" is
prompt-enforced; there is no backend ownership tag on resident-created tasks.
Stamping them is out of scope for this change.

Toggling Master mode still must NOT respawn the resident session (the prompt is
recomputed every cycle), so `resident_agent_master_mode` remains excluded from
`_resident_launch_config_changed`. The Master-mode UI hint copy was updated from
the worktree wording to the orchestrator description.
