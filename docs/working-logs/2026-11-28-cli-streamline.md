# CLI Command Surface Streamlining & Per-Task Session Unification

Date: 2026-11-28

## Context

After `feat/flow-streamline-v1` replaced the shared resident worker pool with
per-task ephemeral Chat sessions, task contracts (`goal`, `done_when`,
`out_of_scope`), and archive-on-done lifecycle, the `claude-hub` CLI still
carried legacy command splits, UI-only REST passthroughs, O(W) board scans, and
a `task run` implementation that called `ensure_agent(reuse_existing=True)` on
shared seats.

## Key Changes

1. **Backend execution config & O(1) lookup endpoints**:
   - Added `model`, `env_preset`, and `cwd` (validated mutually exclusive with
     `auto_worktree=True` and restricted to canonical workspace/worktree paths
     on local targets) to `WorkspaceTaskCreate`, `WorkspaceTaskUpdate`,
     `WorkspaceTask`, and `StartTaskRequest`.
   - Unified `workspace_manager.start_task` and `DotService.delegate` onto one
     per-task ephemeral session creation path (`MASO_BASE_URL` discovery, model
     env injection, preset propagation, and target `cwd` / worktree resolution).
   - Added O(1) `GET /api/workspaces/tasks/{task_id}`,
     `GET /api/workspaces/tasks/{task_id}/status?reports_limit=0&include_brief=false`,
     and `GET /api/workspaces/sessions/{managed_session_id}` endpoints, with
     404 fallback in CLI helpers when talking to older Hub servers.

2. **CLI surface consolidation (19 -> 13 top-level groups)**:
   - Removed UI-only `filesystem` (`fs`), `clipboard`, `system`, and `terminal`
     groups (`api raw` remains available for ad-hoc REST debugging).
   - Separated `agent` (100% cross-provider Subagent: `run | send | result |
     list | cancel | close`) from `session` (`create | list | status | logs |
     send | report | archive | unarchive | delete`).
   - Consolidated `workspace` (`list | ensure | status [--full] | update |
     delete | attachment-get`), `lessons` (absorbing `feedback` and
     `task feedback reap`), `dot` (`dot patrol [--run]`, `dot scout [--run]`,
     `dot cards [query]`), `schedule` (`update --enabled/--disabled`,
     `runs --cancel/--clear`, `CLAUDE_HUB_TAB_ID` / `cwd` defaults), and `tab`
     (`list --archived`, `logs` for both Chat and Terminal, `archive`,
     `unarchive`).
   - Rebuilt `task run` as a 1-step contract-backed isolated task launcher
     (`create_task` + `start_task`, never `ensure_agent(reuse_existing=True)`),
     merged `get`/`report`/`review`/`brief` into `task status [--reports N]
     [--brief]`, absorbed `tree` into `task list --tree [--root]`, standardized
     `<task_id>`-first argument ordering, and added fresh-call CAS auto-read on
     `task progress` while requiring explicit `--expected-*` on `--call-id`
     retries.
   - Added read-only fail-closed `resolve_workspace_id` matching local `cwd`
     against `list_workspaces()` without creating workspaces or guessing across
     multiple matches.

3. **Guidance, docs, and tests**:
   - Synchronized runtime guidance (`guidance.py`), Dot `TOOL_GUIDE`, task
     lifecycle notices, workspace snapshots, Feishu card CLI hints,
     `AGENTS.md` / `CLAUDE.md`, `README.md`, `docs/AGENT_WORKFLOW.md`,
     `docs/TASK_GRAPH.md`, `docs/DOT.md`, and CLI/backend test suites.
