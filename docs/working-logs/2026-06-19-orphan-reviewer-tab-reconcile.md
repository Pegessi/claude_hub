# Orphan reviewer-tab reconciliation (2026-06-19)

## Symptom

A workspace appeared to have "started many reviewers", but those reviewers did
not respond, and they could not be deleted from the "Manage Agents" UI. Two
reported defects:

1. Many reviewers exist that never do any work (don't respond to dispatch).
2. Those reviewers are invisible in "Manage Agents" and cannot be deleted there.

## Diagnosis

Live evidence (workspace `37f14b36`): the board reported **4 reviewer
sessions** but `ttyd_manager` held **10 reviewer terminal tabs** for that
workspace → **6 orphan reviewer tabs** with `workspace_role=reviewer` and the
workspace's `workspace_id`, but **no backing `ManagedSession`** (names matched
old completed tasks, e.g. `review上报异常` ×5, `lessons功能`).

Why the two symptoms:

- **Don't respond**: dispatch and review routing target `ManagedSession`
  objects. An orphan tab has no session, so nothing is ever sent to it.
- **Invisible / undeletable**: the "Manage Agents" modal lists `board.sessions`
  (`workspaceStore` → `reviewerAgents`/`temporaryReviewers`, derived from
  `board.sessions`), not terminal tabs. An orphan tab has no session row, so it
  has no Manage Agents entry and therefore no Delete button. The
  `DELETE /api/workspaces/sessions/{id}` route only knows sessions.

## Root cause

Terminal tabs and managed sessions are two **separate** persistence domains:

- `ttyd_manager` owns `processes` (terminal tabs), persisted to its own
  `STATE_FILE`, started independently by `start_all_tabs()` on boot.
- `workspace_manager` owns `sessions` (`ManagedSession`), persisted per
  workspace in `index.json` / workspace state files.

A `ManagedSession` is normally created together with its tab
(`_create_managed_session` → `ttyd_manager.create_tab`) and removed together
(`delete_session` / `_cleanup_reviewer_for_terminal_task` →
`ttyd_manager.delete_tab`). But if a session is dropped **without** deleting its
tab — e.g. historical temporary-reviewer lifecycle desync (cf. commits
`77d4015`, `5ec888a`) — the tab is left behind permanently. Nothing reconciled
tabs against sessions, so orphans accumulated across restarts.

## Fix

`backend/claude_hub/services/workspace_manager/_tmux_queries.py`:
`_prune_orphan_workspace_tabs(workspace_id) -> int`.

It deletes managed tabs that have no backing session, conservatively:

- Only tabs whose `tab.workspace_id == workspace_id` are considered → **manual
  terminal tabs (no `workspace_id`) are never touched**.
- A tab whose id appears as some session's `tab_id` is kept (**live session**).
- A tab created within `ORPHAN_TAB_PRUNE_GRACE_SECONDS` (60s) is kept, to avoid
  racing the gap between `create_tab` and session registration in
  `_create_managed_session` (the reconciler can run from a concurrent board
  fetch during that window).

Deletion uses the existing `ttyd_manager.delete_tab` (kills the tmux session
too). Failures are logged and skipped, never raised.

### Call sites

- `get_board` — opening the workspace board reconciles, so the leak self-heals
  the moment a user views the workspace.
- `_dispatch_workspace_locked` — alongside the existing stale-reviewer cleanup
  and reaper, so dispatch passes also keep tabs in sync.

Both already run under the dispatch lock / board path, so no new scheduler.

## Pitfall captured

Tab lifecycle and session lifecycle are **not** automatically coupled. Any code
that removes a `ManagedSession` must also delete its tab, or rely on this
reconciler. When adding new session-removal paths, prefer routing through a
helper that deletes the tab; the reconciler is a safety net, not a license to
leak.

## Validation

- `backend/tests/test_orphan_tab_reconcile.py` — 6 unit tests: prune-orphan,
  preserve-manual, preserve-live-session, preserve-within-grace,
  ignore-other-workspace, mixed set. All pass.
- `black`, `isort`, `mypy` clean on touched files.
- Full backend suite: 403 passed. The 19 `asyncio.run()/Runner.run() cannot be
  called from a running event loop` failures are **pre-existing, order-dependent
  event-loop teardown pollution** — verified identical on clean `origin/main`
  (same 19 fail, 397 pass without the new tests) and all pass in isolation.
