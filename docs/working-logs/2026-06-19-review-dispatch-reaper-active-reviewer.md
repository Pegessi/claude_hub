# Fallback reaper re-dispatches a genuinely-working reviewer

Date: 2026-06-19
Task: `review dispatch bug` (0229e284), reviewed mode
Incident task: `85b2b765` ("cli飞书协作", autonomous)

## Symptom

The Feishu review card for "cli飞书协作" showed TWO `ready_for_review`
entries. The second (10:21) was a "fallback reaper" re-dispatch
("重新分派卡住的 review 任务（fallback reaper）；之前的 reviewer 分派未完成")
posted while a reviewer (cb-reviewer-1) was actively reviewing — producing a
confusing duplicate report / wrong-looking status.

## Root cause (evidence)

Timeline for task `85b2b765` from `backend.log` + workspace `state.json`:

| time | event |
| --- | --- |
| 09:59:23 | `continue_task` reopens task → WORKING, `review_cycle`=6, clears `review_requested_at` (keeps `review_session_id=cb-reviewer-1`) |
| 10:17:18 | worker posts `ready_for_review` (cycle 6) |
| 10:17:19 | normal `_after_report_recorded` → autonomous → `_request_task_review` **dispatches cb-reviewer-1**: 93 KB review prompt sent to tmux `claude-hub-498ec73c` |
| 10:17:19 → ~10:20:08 | reviewer model reads the 93 KB prompt silently — no terminal frame change → `runtime_status` classified IDLE, `last_activity_at` goes stale |
| 10:21:08 | fallback reaper fires: `review_in_flight=True` AND `_reviewer_is_active=False` (bound reviewer momentarily IDLE) AND 60 s grace lapsed → **re-dispatches the same working reviewer** (2nd 90 KB prompt), posts duplicate `ready_for_review` card |
| 10:33:03 | cb-reviewer-1 posts `review_passed` — it was working the whole time |

Both dispatches went to the **same** reviewer session (tmux
`claude-hub-498ec73c` = cb-reviewer-1). The normal path was never broken; the
reaper simply mistook a thinking reviewer for a stuck one.

### The defect

`_reviewer_is_active` (in `_tmux_queries.py`) returns False when
`reviewer.runtime_status == AgentRuntimeStatus.IDLE`, even when the reviewer
session is still **bound to the task** (`task_id == task.id`) and **not
stopped**. The terminal classifier (`_classify_agent_status`) reports IDLE
between bursts of model output, and a reviewer reading/thinking over a large
prompt produces no frame change for minutes. The reaper's grace
(`REVIEW_REAPER_DISPATCH_GRACE_SECONDS = 60`) is anchored to
`last_activity_at`, which only advances on a frame-hash change — so a silent
"reading the prompt" window of >60 s makes the reaper declare the dispatch
stuck and re-dispatch a healthy reviewer.

Crucially, a review prompt is only sent after `_submit_tmux_message` *verifies*
submission (it raises if the prompt stays in the input box). So once
`_request_task_review` returns without raising, the reviewer has the prompt and
any subsequent IDLE is "thinking", not "stuck".

## Fix

Make the fallback reaper require **positive evidence of a failed dispatch**
before re-dispatching a review that already has a bound reviewer, instead of
treating transient IDLE as "stuck".

A new reaper-only predicate `_reviewer_dispatch_stuck(task)` returns True
(allow re-dispatch) only when:

- there is no `review_session_id`, or
- the reviewer session is missing, or
- the reviewer session is `STOPPED`, or
- the reviewer is bound to a *different* task.

A reviewer that is bound to THIS task and not stopped is presumed mid-review
and is never reaped, no matter how long it sits IDLE.

`_reviewer_is_active` is left unchanged (it is also used by the agent-report
recovery path in `_after_report_recorded`, `continue_task`, and
`_task_updates`, where IDLE-means-available is correct). Only the two reaper
branches in `_reap_stuck_reviews` switch from `not _reviewer_is_active(task)`
to `_reviewer_dispatch_stuck(task)`.

### Backstop for genuine silent send-failure

If `_request_task_review` set the reviewer binding but the prompt never landed
(e.g. send raised after binding), the reviewer would be bound + IDLE forever
and the stricter predicate would not recover it. To keep that recovery, the
reaper additionally re-dispatches a bound + IDLE reviewer when its review
prompt is still verifiably pending in the tmux input box
(`_capture_tmux_output` + `_message_still_in_input`) — the same signal the
monitor's `_detect_prompt_dispatch_stall` already trusts. A reviewer that is
genuinely working has already submitted the prompt, so its input box is empty
and it is never reaped.

## Tests

`backend/tests/test_workspaces.py`: add cases asserting the reaper does NOT
re-dispatch when the reviewer is bound to the task and IDLE (regression for
this incident), and still DOES re-dispatch when the reviewer is
missing/STOPPED/cross-bound.
