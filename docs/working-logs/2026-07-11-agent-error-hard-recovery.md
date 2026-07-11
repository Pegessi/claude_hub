# Agent Error Hard-Context Recovery

Date: 2026-07-11
Scope: backend (`workspace_manager/_monitor.py`, `_prompts.py`, `_constants.py`, schemas, ttyd_manager)

## Problem

Long-running Claude agents (workers and reviewers) occasionally hit persistent
API errors (4xx/5xx, overloaded, rate-limited) that leave the TUI stuck on an
error dialog. The existing auto-continue mechanism sends up to 10 soft text
prompts ("please continue from the last actionable step"), but these prompts
are delivered as tmux paste-buffer input — they land as text in the terminal,
which cannot dismiss an error dialog. The agent stays wedged until a human
notices and manually interrupts + clears context.

Additionally, auto-continue only fired for workers during WORKING phase;
reviewers stuck on API errors during REVIEW were never prodded at all.

## Solution: Escalation ladder with hard recovery

When soft auto-continue prompts fail to revive a **Claude** agent after
`AUTO_CONTINUE_SOFT_ATTEMPTS_BEFORE_HARD_RECOVERY=3` attempts, the monitor
escalates to **hard recovery**:

1. **Interrupt** — send Escape (dismiss dialog) then a single Ctrl-C (raise
   KeyboardInterrupt) via `_interrupt_session()`.
2. **Wait** `INTERRUPT_SETTLE_SECONDS=1.0` for the TUI to return to the input
   prompt.
3. **Clear context** — send `/clear` as a tmux message to wipe the corrupted
   in-context window. The CLI conversation id (--session-id) is preserved;
   only the context window is reset.
4. **Wait** `CLEAR_CONTEXT_SETTLE_SECONDS=1.5` for `/clear` to complete.
5. **Re-inject** a role-specific recovery prompt that restates workspace/task
   info, goal packet, prior reports (for reviewers), and the report endpoint.

After hard recovery, soft-attempt counter resets to 0; hard-attempt counter
increments. After `AUTO_CONTINUE_MAX_HARD_RECOVERIES=2` failed hard recoveries:

- **Workers** → session marked `NEEDS_INPUT`, task moved to `REVIEW` (same
  semantics as the existing soft-attempt exhaustion path).
- **Reviewers** → `_release_stale_reviewer_for_task()` clears the binding so
  the existing reviewer reaper (`_reap_stuck_reviews`) re-dispatches a fresh
  reviewer session. This is consistent with how other reviewer failure modes
  are handled.

A 30-second cooldown (`AUTO_CONTINUE_MIN_INTERVAL_SECONDS * 2`) after a hard
recovery prevents the monitor from immediately firing another prompt before
the agent has had time to produce output.

Hard recovery is **Claude-only** because `/clear` is a Claude CLI slash
command. Codex and Cursor agents continue on the existing soft-prompt-only
path (which works for them because they don't show modal error dialogs the
same way Claude does).

## Module design

### `models/schemas.py`

- `TerminalTab.agent_session_id: Optional[str]` — surfaces the CLI
  conversation UUID (already tracked in `TTYDProcess.agent_session_id` and
  used for `--resume` on reboot) on the schema for diagnostic logging and
  API access.
- `ManagedSession` gains three fields:
  - `hard_recovery_task_id: Optional[str]` — task that hard recovery was
    last attempted for (drives per-task counter reset on new task).
  - `hard_recovery_attempts: int = 0`
  - `last_hard_recovery_at: Optional[datetime]` — drives cooldown.

### `services/ttyd_manager.py`

- `TTYDProcess.to_schema()` now includes `agent_session_id=self.agent_session_id`
  in the returned `TerminalTab`.

### `services/workspace_manager/_constants.py`

New constants:

| Constant | Value | Purpose |
|---|---|---|
| `AUTO_CONTINUE_SOFT_ATTEMPTS_BEFORE_HARD_RECOVERY` | 3 | Soft prompts before hard recovery |
| `AUTO_CONTINUE_MAX_HARD_RECOVERIES` | 2 | Max hard recoveries per task before giving up |
| `INTERRUPT_SETTLE_SECONDS` | 1.0 | Wait after Escape+Ctrl-C before /clear |
| `CLEAR_CONTEXT_SETTLE_SECONDS` | 1.5 | Wait after /clear before re-pasting prompt |
| `AUTO_CONTINUE_REVIEWER_MESSAGE` | (str) | Phase-specific continue message for reviewers |
| `HARD_RECOVERY_WORKER_MESSAGE` | (str) | Preamble for worker recovery prompts |
| `HARD_RECOVERY_REVIEWER_MESSAGE` | (str) | Preamble for reviewer recovery prompts |

### `services/workspace_manager/_normalize.py`

- `_normalize_session_item` backfills defaults (`None`, `0`, `None`) for the
  three new fields when loading persisted state from older versions.

### `services/workspace_manager/_monitor.py`

**`_auto_continue_stopped_task`** changes:

- Fires for `task.status in {WORKING, REVIEW}` (previously WORKING only).
- Per-phase ownership guards: during WORKING, only `task.session_id` matches
  are prodded; during REVIEW, only `task.review_session_id` matches. This
  prevents the idle worker from being endlessly auto-prompted while review
  is in flight, and vice-versa.
- `review_in_flight` check is made phase-aware: returns early (no auto-continue)
  for workers during review_in_flight; allows reviewers through (review_in_flight
  is True while they are actively reviewing, which is exactly when they need
  reviving on errors).
- Completion-pattern detection (ready_for_review, "validation:", etc.) is
  **skipped for reviewers**, since reviewers post review_passed/review_failed
  verdicts rather than completion reports. Without this skip, an idle reviewer
  whose pane shows a report summary would be incorrectly classified as "task
  complete — nudge to report" instead of "error — nudge to continue review".
- Hard recovery escalation: when `interruption_reason` is set AND agent_type is
  CLAUDE AND `attempts >= 3` AND `hard_attempts < 2`, calls
  `_perform_hard_recovery()`.
- Hard-recovery cooldown: if `last_hard_recovery_at` is within 30s, returns
  WORKING without sending another prompt.
- Exhaustion paths (hard_attempts >= 2 OR attempts >= 10) branch on role:
  reviewers are released for re-dispatch; workers go to NEEDS_INPUT/REVIEW
  (preserving pre-existing behavior).
- Soft continue message uses `AUTO_CONTINUE_REVIEWER_MESSAGE` for reviewers,
  `AUTO_CONTINUE_MESSAGE` for workers.

**`_perform_hard_recovery(session, task, sampled_at, interruption_reason, soft_attempts, hard_attempts)`** (new):

1. Validates workspace exists.
2. Looks up `agent_session_id` via `_agent_session_id_for_session()` and logs a
   warning with all diagnostic context.
3. Calls `_interrupt_session(session)`, sleeps `INTERRUPT_SETTLE_SECONDS`.
4. Calls `self.send_session_message(session.id, "/clear")`, sleeps
   `CLEAR_CONTEXT_SETTLE_SECONDS`.
5. Builds role-specific prompt:
   - Reviewer: uses `_build_hard_recovery_reviewer_prompt()` with trigger
     report (looked up via `_latest_report_for_task()`).
   - Worker: uses `_build_hard_recovery_worker_prompt()`.
6. Sends the recovery prompt via `send_session_message()`.
7. Returns session update dict resetting soft_attempts to 0, incrementing
   hard_attempts, and recording `last_hard_recovery_at`.

**`_latest_report_for_task(task_id)`** (new helper): returns the most recent
report for a task sorted by `created_at`, or None. Used to fetch the trigger
report for reviewer recovery prompts.

**Counter reset paths** — hard_recovery fields are reset to None/0/None in:

- `_release_task_session` (worker task completed/aborted)
- `_release_reviewer_session` (reviewer verdict applied or aborted)
- `_cleanup_reviewer_for_terminal_task` (reviewer tab cleanup)
- `_assign_current_task` (session reassigned to a new task)

### `services/workspace_manager/_prompts.py`

Three new methods:

- **`_build_hard_recovery_worker_prompt(workspace, task, session, interruption_reason)`** —
  builds post-recovery prompt for workers: HARD_RECOVERY_WORKER_MESSAGE
  preamble, error reason, workspace/task metadata, agent_session_id if
  available, state snapshot path, task description, approved Goal Packet
  JSON, autonomous-mode reminder, and the curl report endpoint.
- **`_build_hard_recovery_reviewer_prompt(workspace, task, session, trigger_report, interruption_reason)`** —
  builds post-recovery prompt for reviewers: HARD_RECOVERY_REVIEWER_MESSAGE,
  error reason, workspace/task metadata, agent_session_id, worker session
  id, state snapshot path, task description, Goal Packet JSON, trigger
  report JSON, recent task reports JSON (last 12), resume instruction, and
  curl endpoint.
- **`_agent_session_id_for_session(session)`** — helper that calls
  `ttyd_manager.get_tab(session.tab_id)` and returns `tab.agent_session_id`
  if available, None on any error.

### `services/workspace_manager/_dispatch.py`, `_reports.py`, `_sessions.py`, `_tmux_queries.py`

- `_dispatch_task_to_session`, `continue_task`: reset hard_recovery fields
  when dispatching or continuing a task (new work starts fresh).
- `_request_task_review`: reset hard_recovery fields when binding a reviewer.
- `delete_task`: reset hard_recovery fields on all sessions bound to the
  deleted task.
- `_cleanup_stale_reviewer_assignments`: reset hard_recovery fields when
  clearing stale reviewer bindings.

## Key issues / pitfalls

- **Why not use soft prompts forever?** Soft prompts are tmux paste-buffer
  input. They land as text in the terminal. When Claude is stuck showing a
  modal error dialog (e.g., "API error — press Enter to retry" or "overloaded"
  screens), pasted text goes nowhere — the dialog consumes Enter but ignores
  bulk text. Sending Escape + Ctrl-C dismisses the dialog and raises
  KeyboardInterrupt, returning Claude to its main input prompt where `/clear`
  actually works.
- **Why reset soft_attempts to 0 after hard recovery?** After /clear the agent
  starts with a fresh context. It makes sense to give it the same escalation
  budget again (3 soft → 1 more hard), bounded by the hard cap of 2 total
  hard recoveries to prevent infinite loops.
- **Why Claude-only?** `/clear` is a Claude Code CLI slash command. Codex and
  Cursor have different reset mechanisms. In practice, the modal error dialog
  problem described above is primarily seen with Claude's TUI; Codex/Cursor
  agents appear to recover with soft prompts alone. If needed, agent-specific
  recovery commands can be added later by dispatching on `session.agent_type`
  in `_perform_hard_recovery`.
- **`send_session_message` for `/clear`?** Yes — `send_session_message` calls
  `_ensure_session_ready_for_send` (waits for input prompt), then
  `_send_tmux_message` which pastes via load-buffer + paste-buffer -p -r
  (bracketed paste) and submits with Enter. This correctly sends `/clear`
  as a command that the Claude CLI executes. For no-attachment calls (like
  `/clear`), `_append_attachment_block` returns the message unchanged.
- **agent_session_id lookup is on-demand**: we deliberately do NOT duplicate
  the session-id on ManagedSession. TTYDProcess already holds it (pinned per
  tab and used for --resume on reboot), and looking it up via ttyd_manager
  when needed avoids a second source of truth that could drift.
- **Reviewers during review_in_flight**: the old code returned early from
  auto-continue whenever `review_in_flight` was True. This blocked reviewer
  recovery because review_in_flight is True during the entire REVIEW phase.
  The fix: only return early when `task.status == WORKING` (worker idle while
  reviewer is active). Reviewers during REVIEW are allowed through.
- **Completion patterns for reviewers**: reviewers don't post ready_for_review;
  they post review_passed/review_failed/review_needs_input. Running completion
  detection on reviewer output produces false positives (e.g., a reviewer
  reading a report containing "validation: ..." would be told to "post your
  report" when they haven't finished reviewing).
- **hard_recovery_task_id initialization**: initial dispatch/continue/review
  sets `hard_recovery_task_id=None` (not task.id). The field is only set to
  task.id when a hard recovery actually fires (in `_perform_hard_recovery`'s
  return dict). This makes the counter guard (`hard_attempts =
  session.hard_recovery_attempts if session.hard_recovery_task_id == task.id
  else 0`) correctly return 0 before any hard recovery.

## Tests (`tests/test_hard_recovery.py`)

13 new unit tests covering:

- `agent_session_id` field presence and default on TerminalTab
- Hard recovery fields presence and defaults on ManagedSession
- Constant values and invariant (soft max > hard threshold)
- `_normalize_session_item` backfills defaults for old state
- `_normalize_session_item` preserves existing hard recovery state
- Escalation threshold logic (2 soft attempts → no escalation; 3 → yes)
- Hard recovery skipped for Codex/Cursor agents
- Hard recovery caps at MAX_HARD_RECOVERIES (2)
- Worker vs reviewer role detection from task/session bindings
- `_latest_report_for_task` returns most recent report
- `_latest_report_for_task` returns None when no reports exist

## Validation

- **black / isort**: clean on all touched production files
- **mypy**: clean on all 10 touched production source files
- **pytest**: 151 unit tests pass (13 new + 138 existing synchronous tests
  in test_hard_recovery, test_workspace_state_policy, test_feedback_lessons,
  test_tabs, test_session_manager, test_orphan_tab_reconcile)
- 54 pre-existing async test failures ("Runner.run() cannot be called from
  a running event loop") are test infrastructure issues that exist on main
  and are unrelated to this change.
