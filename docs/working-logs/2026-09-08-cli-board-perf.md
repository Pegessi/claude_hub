# 2026-09-08 — CLI/board performance: pure snapshot board, direct task endpoint, tmux deadline

## System Overview

A reviewed task ("cli性能修复") to fix a CLI/board bottleneck diagnosed by a
prior agent. The board read path did too much work per request: it refreshed
session statuses via tmux, ran reconcile/cleanup steps, and the CLI single-task
commands (`task status/report/review`) located their target by pulling the
**entire board** — a 2.48MB payload taking 1.43–1.56s. Four fixes, mapped to
the eight approved acceptance criteria:

1. **Board is a pure snapshot.** `get_board()` performs NO tmux I/O and NO
   reconcile/cleanup. The six steps that the read path used to run moved to the
   background monitor loop (5s cadence).
2. **Direct single-task endpoint.** `GET /workspaces/{ws}/tasks/{task}` returns
   one task + its report history via O(1) dict lookup (no board scan). CLI
   `task status/report/review` use it when `--workspace-id` is given.
3. **tmux query deadline + cached fallback.** Local tmux capture/query paths
   are wrapped in `asyncio.wait_for` (≤2s). On timeout the status path returns
   the last cached `TerminalAgentStatus` instead of blocking.
4. **CLI summary pagination.** CLI list/summary reads pass `tasks_limit` so
   they no longer transfer full task history.

## Module Design

- `services/workspace_manager/_tmux_queries.py`
  - `get_board()` — pure snapshot: filter tasks by workspace, paginate
    (`board_pagination.paginate_board_tasks`), attach latest report per task,
    sessions, markdown docs. No `await` on tmux or `_refresh_session_statuses`.
  - `get_task_detail()` — O(1): workspace check, `tasks[task_id]`, attach that
    task's full report history. `KeyError` for unknown ws/task.
  - `_public_task()` — redact Feedback Reaper prompts (unchanged behavior).
- `services/workspace_manager/_workspaces.py`
  - `_reconcile_workspace_board_state()` — the four in-memory steps:
    `_reconcile_task_report_statuses`, `_reconcile_workspace_session_pointers`,
    `_cleanup_stale_orchestrator_assignments`, `_sync_workspace_tab_metadata`.
    Called per workspace each monitor tick.
  - `_background_monitor_loop()` — already calls `_refresh_session_statuses` +
    `_reconcile_workspaceboard_state` + `dispatch_workspace` per cycle; this is
    where the board's read-path work now lives.
- `services/ttyd_manager.py`
  - `_run_tmux_capture()` — `asyncio.wait_for(proc.communicate(), timeout)`; on
    `asyncio.TimeoutError`, kill the child (wait_for cancels `communicate()` but
    leaves the process) and re-raise so callers can distinguish a deadline.
  - `get_tab_agent_status()` — on `asyncio.TimeoutError` from
    `capture_history`/`capture_foreground_command`, return the last cached
    status; only classify OFFLINE if never sampled. The stale cache entry stays
    so the TTL check throttles re-queries while tmux is hung.
- `api/workspaces.py` — `GET /{ws}/tasks/{task}` → `get_task_detail`;
  `KeyError`→404 via `_task_public_http`. Static `/tasks/tree` is registered
  before the dynamic `/tasks/{task_id}` so it isn't shadowed.
- `cli/client.py` — `get_board(tasks_limit, tasks_cursor)` + `get_task_detail`.
- `cli/commands/tasks.py` — `task_list` (`--limit`/`--all`),
  `_task_detail_or_scan` (direct endpoint when `--workspace-id` given).
- `cli/commands/sessions.py`, `workspaces.py`, `feishu.py` — board reads pass
  `tasks_limit=MIN_BOARD_TASKS_LIMIT`; single-task reads use the direct endpoint.

## Key Issues / Pitfalls

- **Tests relied on the board read's side effects.** Removing
  `_refresh_session_statuses` from the read path meant tests asserting the board
  reflects fresh `runtime_status` saw stale cached status. Fixed by driving the
  refresh (or `_reconcile_workspace_board_state`) explicitly before the board
  read in the affected tests (10 in `test_workspaces.py`, 1 in
  `test_feishu_commands.py`).
- **`asyncio.TimeoutError` is not a `RuntimeError`.** Python 3.11+ aliases the
  builtin `TimeoutError` (an `OSError`). The API exception mapping had to
  handle it explicitly; `get_board`/`get_task_detail` raise `KeyError`/`ValueError`
  only.
- **`wait_for` cancels the coroutine, not the child.** The tmux subprocess
  keeps running after the deadline; must `proc.kill()` + `proc.wait()` to avoid
  a process leak.
- **Pre-existing flaky test, not a regression.**
  `test_recovery_real_ttyd.py::test_real_cold_restart_7tab_bijection` fails on
  `main` too (a 10s wall-clock oracle that's borderline on this machine). Not
  caused by these changes.

## Measurements (bench_board_perf.py)

| Read | Before | After | AC target |
| --- | --- | --- | --- |
| Single board GET | 0.65–1.0s | median 4.3ms / p95 15.3ms | <100ms |
| 8-concurrent board | 4.23s | median 28.7ms / p95 48.8ms | <500ms |
| Direct task GET | 1.43–1.56s / 2.48MB | median 0.7ms / 12.6KB | <200ms / <50KB |
