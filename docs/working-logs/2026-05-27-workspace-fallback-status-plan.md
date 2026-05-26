# Workspace Fallback Status Detection Plan

## Context
The current workspace fallback relies on structured reports and selected terminal
output markers. If a task stays in `working`, the runtime is idle, and no
structured report was ever recorded, the fallback can miss the task because
there is no interruption marker and no completion marker to react to.

Observed case: the ZZZ gen task `希希芙pose`
(`88646344-d04b-4148-a694-37f1e0423a6c`) appeared to have finished or stopped
from the UI, but still showed `working`. Inspection showed an autonomous task
at iteration `1/5`, an idle runtime, no review in flight, and no structured
reports.

## Goal
Improve the fallback so it detects ambiguous idle `working` tasks and asks the
agent to report its real state, without guessing that the task is complete.

## Proposed Behavior
- Keep structured agent reports as the source of truth for task completion,
  blocking, and human acceptance.
- Preserve the existing output-based fallback for interruption markers,
  completion markers, and busy terminal output.
- Add a `missing_initial_report` branch:
  - applies only when a task is `working`, the runtime is idle, no review is in
    flight, `latest_report_state` is missing, and an idle grace period has
    elapsed;
  - sends a status-check prompt asking the agent to either report
    `started`/`working` and continue, report `completed`/`ready_for_review`, or
    report `blocked`/`needs_input`;
  - records the fallback action as `status_check` for observability.
- Consider a longer ambiguous-idle threshold for tasks whose latest report is
  `started` or `working` but whose terminal output has no actionable markers.
- When max fallback attempts are reached, surface attention or `needs_input`
  diagnostics; do not mark the task review-skipped or human-acceptance-ready.

## Acceptance Criteria
- A no-report idle `working` task receives exactly one status-check prompt after
  the configured grace period.
- A recently active task does not receive a status-check prompt before the grace
  period elapses.
- Existing interruption-marker fallback still sends the continue prompt.
- Existing completion-marker fallback still asks for the missing final report.
- Tasks with review already requested but not completed are ignored by fallback.
- Max-attempt handling does not create a Done-ready or human-acceptance-ready
  task state.

## Validation Plan
- Add focused backend tests around `_auto_continue_stopped_task` for no-report
  idle, recent activity, review-in-flight, completion marker, interruption
  marker, and max-attempt paths.
- Run:
  - `cd backend && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_workspaces.py -q`
  - `cd backend && uv run black --check . && uv run isort --check-only . && uv run mypy .`

## Out Of Scope
- Automatically inferring successful completion from terminal output alone.
- Changing reviewed/direct/autonomous final human acceptance semantics.
- Creating a full evaluator for stale task recovery.
