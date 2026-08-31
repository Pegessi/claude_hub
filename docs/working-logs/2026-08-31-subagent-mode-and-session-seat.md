# 2026-08-31 — Subagent mode, worktree isolation, `/clear` seat check

## System Overview

Two problems landed on the same branch:

1. Agent-to-agent `subagent` task mode (no Goal Packet, no AI review, `failed`
   on session death/timeout).
2. A live incident where reviewer `/clear` was pasted into the main Claude
   seat after a worktree verification run rewrote live `state.json` / renamed
   tmux.

The live Hub on 5173/8173 and `~/.claude_hub/workspaces` are off-limits for
feature work. Isolation and tests stay inside the git worktree.

## Module Design

- `runtime_isolation.py` — linked worktree (`.git` is a file) resolves
  `~/.claude_hub/worktrees/<slug>` and `tmux -L ch-<slug>`. Explicit live
  `STATE_ROOT` or empty `CLAUDE_HUB_TMUX_SOCKET` from a worktree raises.
  Primary checkout is unchanged. `backend.lock` and backend logs follow
  the same runtime home so a worktree preview does not collide with the
  live 8173 process.
- `session_seat.py` — `validate_session_seat` requires
  `tmux_session == claude-hub-{tab_id[:8]}` and no other live tab claiming
  that name. `should_clear_reviewer_context` returns false for same
  reviewer + same task (reaper redispath).
- `send_session_message` / pump / `_ensure_session_ready_for_send` call the
  seat check. Mismatch fails closed; pump moves the call_id to uncertain.
  A missing pane also fails closed: send no longer calls
  `ensure_tab_tmux_session` to recreate the same name.
- Subagent: `timeout_seconds` defaults to `None` and is persisted from
  create. Failed detection is scoped to `subagent`. Frontend shows `failed`
  in the Working column.

## Key Issues / Pitfalls

- Official backend on `main` must keep writing the live state root. Isolation
  is worktree-only, not a lock on production writes.
- Auto-detect uses this package's repo root (`.git` file vs directory), not
  `cwd`.
- E2E helpers that talk to an already-running live backend stay on the
  default tmux server. Worktree unit/integration helpers use `tmux_command()`.
- `clear_context=true` still clears on first assignment to a reviewer. It
  must not clear again on the same task + same reviewer.
- `start_all_tabs` must list sessions via `tmux_command("ls")`. A bare
  `tmux ls` on a worktree backend enumerates the live default server.

## Isolated verification (2026-08-31)

Did not stop or restart live 5173 (node `19866`) / 8173 (python `16461` /
`16567`). Preview used `/tmp/claude_hub-subagent-preview`,
`tmux -L ch-subagent-preview`, backend `8299`, Vite `5274`.

- Second/third isolated startups logged
  `error connecting to .../ch-subagent-preview` and started 0 tabs. No
  live `claude-hub-*` names appeared.
- `POST /api/workspaces/{id}/tasks` with `task_mode=subagent` and
  `timeout_seconds=42` persisted. Isolated board and the 5274 Vite proxy
  both returned that workspace; its id is not in live
  `~/.claude_hub/workspaces/index.json`.
- Default tmux still had 22 sessions after preview processes were stopped.
- Frontend `taskAcceptance` unit tests: 11 passed, including failed-status
  Done visibility. No in-browser click-through (no browser tools).
- Isolated `task run` closed loop (8299 + `TTYD_BASE_PORT=19200` +
  `tmux -L ch-subagent-preview`, agent_type `terminal`): create/start →
  worker `completed` → `review` with `review_skipped_at` and no reviewer
  session → `task accept` → `done`. Isolated ttyd was `19201`; live
  `10001`/`10002` and default tmux were untouched.
- Isolated real-tmux `/clear` tests (`tmux -L ch-seat-*`): matching seat
  received `/clear`, victim pane did not; a killed pane was not recreated.
  No browser tools, so the board was not click-tested.
