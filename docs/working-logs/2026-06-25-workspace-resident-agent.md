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
- `_resident_agent_due(workspace, now)` — pure due-check: disabled → never;
  `last_run_at is None` → due immediately; otherwise due once
  `now - last_run_at >= interval`.
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
