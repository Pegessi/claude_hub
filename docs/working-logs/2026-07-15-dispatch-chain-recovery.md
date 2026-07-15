# 2026-07-15: Dispatch chain recovery — GP review and continue prompt stalls

## Symptom

Two intermittent failure modes reported on the workspace orchestration loop:

1. **Worker fails to submit its Goal Packet review request.** The worker finishes
   the GP, posts `ready_for_review`, but the POST fails (network blip, backend
   restart mid-request, tmux Enter key doesn't submit the curl command). The
   task sits in WORKING forever with an IDLE worker.
2. **GP approved (`review_passed`) but the worker never resumes.** The reviewer
   posts `review_passed`, `_handle_goal_packet_review_report` calls
   `continue_task`, which calls `send_session_message` to send the "Continue
   workspace task from review" prompt. If tmux submit verification fails
   (`_submit_tmux_message` raises RuntimeError after 3 Enter retries), the
   exception propagated to the HTTP layer before this fix; the task state was
   already persisted as WORKING but the worker terminal never received the
   continue prompt and stayed permanently IDLE.

Both are "chain-break" failures: every individual step (POST handler, state
transition, reviewer selection, message construction) succeeds in isolation,
but a single tmux/network failure at the final "deliver prompt to terminal"
step leaves the task in an in-between state with no recovery path.

## Root causes found

Four independent gaps contributed:

### 1. Stall detector blind to continue/hard-recovery prefixes

`_detect_prompt_dispatch_stall` (in `_monitor.py`) iterated prompt prefixes
via `_expected_pending_prompt_prefix(session, task) -> str` which returned a
single string:

- `_PROMPT_PREFIX_REVIEW = "Review workspace task."` for reviewers
- `_PROMPT_PREFIX_INITIAL_DISPATCH = "New workspace task assigned."` for workers

The continue prompt (`"Continue workspace task from review."`) and the
hard-recovery prompt (`"⚠️  Your previous context was automatically cleared
because the"`) start with different first lines. A stuck continue prompt
therefore never matched any prefix, the Enter-retry step never fired, and the
escalation to `_mark_prompt_dispatch_stalled` never ran — so the worker sat at
a stale bash prompt forever.

### 2. `continue_task` had no send-failure handler

`_request_task_review` (in `_reports.py`) wraps its `send_session_message`
call in try/except and calls `_mark_prompt_dispatch_stalled` on failure.
`continue_task` (in `_dispatch.py`) did not — the RuntimeError from
`_submit_tmux_message` propagated out of the dispatch method and was turned
into an HTTP 400 by the API layer. But the caller had already mutated
`self.tasks[task.id]` to WORKING and persisted state. Net effect: the HTTP
caller sees 400, the board shows the task in WORKING with an IDLE worker, and
no stall-marker report exists to flag the problem to auto-recovery.

### 3. Auto-continue returned None for clean-idle workers

`_auto_continue_stopped_task` only sent a nudge when one of two pattern checks
matched:

- `_auto_continue_interruption_reason(output)` — API error / overloaded /
  rate-limit patterns.
- `_auto_continue_completion_reason(output)` — `ready_for_review`,
  `changed_files`, etc. (worker claiming to be done but missing its POST).

When the terminal showed a clean input prompt with no error and no
near-completion chatter — exactly the state after a failed continue delivery,
where the worker's shell is just sitting at `>` — neither check matched and
the function returned `None`. No nudge was sent and the monitor tick was a
no-op forever.

### 4. Reaper didn't catch GP-pending-without-dispatch

`_reap_stuck_reviews` (in `_tmux_queries.py`) recognized two wedged states:

- Review requested but reviewer not bound (no `review_session_id` past grace).
- Reviewer bound but idle past grace.

There's a third: REVIEWED-mode task in WORKING with a PENDING_REVIEW Goal
Packet but no `review_requested_at` timestamp. This happens when
`_handle_goal_packet_review_report` calls `_select_or_create_reviewer` (or
the subsequent `_rename_session_for_task` or reviewer /clear round-trip) and
an exception is thrown after the GP status is flipped to PENDING_REVIEW but
before `review_requested_at` is set. The existing reaper predicates never
matched because they require either `review_requested_at` to exist or the
task to be in `READY_FOR_REVIEW` trigger state. A safety-net clause was added.

## Fixes applied

### Prefix list expansion

Replaced `_expected_pending_prompt_prefix -> str` with
`_expected_pending_prompt_prefixes -> list[str]`. Four prefixes:

```python
_PROMPT_PREFIX_INITIAL_DISPATCH = "New workspace task assigned."
_PROMPT_PREFIX_REVIEW = "Review workspace task."
_PROMPT_PREFIX_CONTINUE = "Continue workspace task from review."
_PROMPT_PREFIX_HARD_RECOVERY = "⚠️  Your previous context was automatically cleared because the"
```

Hard-recovery prefix is conditionally included when
`session.hard_recovery_task_id == task.id` and `last_hard_recovery_at` is
newer than the appropriate reference timestamp (`started_at` for workers,
`review_requested_at` for reviewers).

Both callers — `_detect_prompt_dispatch_stall` in the monitor and
`_reviewer_prompt_still_pending` in the reaper — now iterate the full list
with `any(...)` semantics.

### continue_task send-failure handling

Mirrors the pattern in `_request_task_review`:

```python
try:
    await self.send_session_message(
        session.id,
        self._build_continue_prompt(self.tasks[task.id], payload, session),
    )
except Exception as exc:
    logger.exception(...)
    self._mark_prompt_dispatch_stalled(
        task_id=task.id,
        session_id=session.id,
        message="Continue prompt could not be submitted to the terminal; "
                "auto-recovery will nudge the worker. ...",
        message_zh="Continue prompt 未能提交到终端；自动恢复将提示 worker。...",
        report_state=AgentReportState.NEEDS_INPUT,
        sampled_at=_wm._now(),
    )
```

Endpoint returns 200 (state is consistent; auto-recovery will revive it).

### _mark_prompt_dispatch_stalled role-aware task status

Previously this helper unconditionally set
`task.status = WorkspaceTaskStatus.REVIEW` and `reviewed_at = sampled_at`.
That's correct when the stuck prompt is the reviewer's review prompt (the
task is already in REVIEW state from `_request_task_review` and simply
needs attention). It is *incorrect* when the stuck prompt is the worker's
initial-dispatch prompt or continue prompt: the task never finished its work
phase and should stay WORKING. Demoting to REVIEW loses the work phase and
fools the dispatch loop into thinking the worker requested review.

Fix: the function now checks `session.role`. Only REVIEWER stalls set
status=REVIEW; all other roles preserve `task.status` and skip setting
`reviewed_at`.

### ATTENTION+WORKING guard against stall reports

`_refresh_session_statuses` had a block (lines 81-106) that fires
`_request_task_review` when the runtime status is ATTENTION, the task is
WORKING, and no review is in flight. This exists so that when the ttyd
classifier sees "waiting for input" on a worker that has been running for a
while (a genuine `NEEDS_INPUT` report), a reviewer gets spun up to diagnose.

After the `_mark_prompt_dispatch_stalled` fix, the worker session is set to
ATTENTION/NEEDS_INPUT by the stall path (which is now correct — it does need
attention). But the ATTENTION+WORKING block then fires `_request_task_review`
on a stalled-delivery task, incorrectly moving it to REVIEW.

Added a guard:

```python
if not self._latest_report_has_risk(task.id, PROMPT_STUCK_RISK_LEVEL)
   and not state_policy.review_in_flight(...):
    # fire _request_task_review
```

A prompt-stall risk-level report means "this is a delivery failure, not a
worker request for review"; skip the auto-review, let auto-continue's nudge
loop recover the worker.

### Clean-idle nudge for workers

Added a third reason in `_auto_continue_stopped_task` — `idle_clean_prompt`
— which fires when the idle session has no interruption pattern AND no
completion pattern AND is not a reviewer. Previously this case returned
None. Now it dispatches a dedicated message
(`AUTO_CONTINUE_IDLE_PROMPT_MESSAGE`) that explicitly tells the worker to
read its state snapshot, inspect files, and resume; the message also covers
the "ready_for_review POST didn't reach the server" case by telling the
worker to re-POST if the task is already complete.

To avoid false positives (agents legitimately sit at a clean prompt for 10-30
seconds while reading files or between output bursts), the clean-idle case
uses a dedicated longer grace:

```python
AUTO_CONTINUE_CLEAN_IDLE_GRACE_SECONDS = 60
```

vs the existing `AUTO_CONTINUE_IDLE_GRACE_SECONDS = 20` for error/completion
cases. The grace is selected dynamically per-tick:

```python
effective_grace = (
    AUTO_CONTINUE_CLEAN_IDLE_GRACE_SECONDS
    if not _interruption_check and not _completion_check
    else AUTO_CONTINUE_IDLE_GRACE_SECONDS
)
```

Reviewers are excluded from the clean-idle nudge path (the stuck-review
reaper handles them with its own grace and dispatch-retry logic; sending
them an idle-clean-prompt nudge would pollute their review context).

### Reaper third-predicate for orphaned GP reviews

Added to `_reap_stuck_reviews`:

```python
elif (
    task.task_mode == WorkspaceTaskMode.REVIEWED
    and task.status == WorkspaceTaskStatus.WORKING
    and task.goal_packet is not None
    and task.goal_packet.status == GoalPacketStatus.PENDING_REVIEW
    and not state_policy.review_in_flight(
        task.review_requested_at, task.review_completed_at
    )
):
    needs_review_dispatch = True
```

And updated `_review_dispatch_in_reaper_grace` to treat
`goal_packet.updated_at` as a grace anchor when the GP is PENDING_REVIEW (so
a freshly-PENDING_GP doesn't get re-dispatched instantly). The trigger uses
`trigger_state = WorkspaceTaskStatus.WORKING` (not READY_FOR_REVIEW) so the
re-built review prompt carries Goal Packet approval instructions rather than
implementation-review instructions.

## Files changed

- `backend/claude_hub/services/workspace_manager/_constants.py`
  - Added `AUTO_CONTINUE_CLEAN_IDLE_GRACE_SECONDS = 60`
  - Added `AUTO_CONTINUE_IDLE_PROMPT_MESSAGE` multi-line nudge string
  - Added both to `__all__`
- `backend/claude_hub/services/workspace_manager/_monitor.py`
  - Replaced `_expected_pending_prompt_prefix` (singular) with
    `_expected_pending_prompt_prefixes` (plural, list return) including
    continue and hard-recovery prefixes with conditional inclusion
  - Added `idle_clean_prompt_reason` detection in
    `_auto_continue_stopped_task` with dynamic `effective_grace`
  - Fixed `_mark_prompt_dispatch_stalled` to only set task.status=REVIEW
    for reviewer-role sessions; preserves task status for workers/orchestrators
  - Added guard in `_refresh_session_statuses` ATTENTION+WORKING block to skip
    auto-_request_task_review when latest report has
    `risk_level=PROMPT_STUCK_RISK_LEVEL`
- `backend/claude_hub/services/workspace_manager/_dispatch.py`
  - Wrapped `continue_task`'s `send_session_message` in try/except with
    `_mark_prompt_dispatch_stalled` on failure (mirrors `_request_task_review`)
- `backend/claude_hub/services/workspace_manager/_tmux_queries.py`
  - Updated `_reviewer_prompt_still_pending` to iterate all prefixes
  - Added third wedge predicate in `_reap_stuck_reviews` for
    GP-pending-without-review_requested_at
  - Extended `_review_dispatch_in_reaper_grace` to use `goal_packet.updated_at`
    as grace anchor; added trigger_state=WORKING for GP recovery path
- `backend/tests/test_workspaces.py`
  - Updated `test_continue_task_marks_working_before_send_verification_failure`
    to expect 200 (not 400), worker session in NEEDS_INPUT/ATTENTION, and
    stall-marker `needs_input` report with `risk_level=prompt_dispatch_stalled`
  - Updated `test_monitor_surfaces_worker_prompt_stuck_in_input` to expect
    task stays WORKING (not demoted to REVIEW), session set to
    NEEDS_INPUT/ATTENTION

## Validation

- `pytest tests/test_workspaces.py` — 119 pass, 0 failures
- `mypy claude_hub/services/workspace_manager/` — Success: no issues found
- `black --check` and `isort --check-only` clean after auto-format

## Lessons / pitfalls

1. **Mirror error handling across symmetric paths.** The review-dispatch path
   already had try/except + stall marking; continue_task was added later and
   the author (me, previously) didn't copy the pattern. Any path that calls
   `send_session_message` after mutating task state must handle the
   RuntimeError from `_submit_tmux_message` or the state/delivery mismatch
   becomes permanent.
2. **Prompt-stall detection requires a registry of prefixes.** Every new
   prompt type (continue, hard-recovery, GP-plan-retry, resident-reboot
   reinjection, ...) adds a first-line that can be found sitting in the
   input box on Enter failure. A single-string return is a footgun; a list
   with conditional inclusion is the right shape.
3. **Hardcoding task.status transitions in helpers is dangerous.**
   `_mark_prompt_dispatch_stalled` assumed REVIEW because it was first
   written for reviewer stalls; generalizing it to worker stalls (continue
   and initial dispatch) exposed that the REVIEW transition was role-
   specific, not universal. Helpers that mutate task.status should take the
   target status as an argument or derive it from role/task state, not
   hardcode one phase.
4. **Grace periods should match the failure mode, not be uniform.** 20
   seconds is appropriate for an API error (the agent is visibly stuck); 60
   seconds is appropriate for a clean idle (agents legitimately think). The
   original single 20-second grace would have caused false-positive nudges
   on agents mid-thought.
5. **Reaper predicates must cover the "never set review_requested_at" case.**
   The existing reaper handled "review requested but reviewer not bound" and
   "reviewer bound but idle" but not "reviewer creation threw mid-flight
   leaving GP in PENDING_REVIEW with no timestamp." Whenever a state
   transition involves multiple writes (flip GP status, create reviewer, set
   review_requested_at, send prompt), each intermediate state needs a reaper
   predicate.
