# Autonomous Mode V1

## System Overview

Autonomous Mode V1 is an additive extension to Agent Workspace. A task now has
a `task_mode` of `direct`, `reviewed`, or `autonomous`; existing tasks default
to `reviewed` during state normalization. Autonomous tasks store an
`AutonomyPolicy` and an `AutonomousRun` directly on the task so the board can
keep its coarse task columns while the detail panel shows fine-grained run
state.

The implementation keeps the final human acceptance gate unchanged. An
autonomous run can pass evaluation, but the task remains in Review until a
human marks it Done.

## Module Design

### Backend Models

`backend/claude_hub/models/schemas.py` defines the new task mode and autonomy
types:

- `WorkspaceTaskMode`
- `AutonomyPolicy`
- `AutonomousRun`
- `RubricCriterion`
- `EvaluationReport`
- `AutonomousIteration`

All autonomy fields are optional except when a task is explicitly autonomous.
`WorkspaceManager._normalize_task_item()` fills defaults for autonomous tasks
and strips autonomy state from direct/reviewed tasks.

### State Policy

`workspace_state_policy.py` owns pure autonomous mappings:

- worker report state to autonomous phase
- evaluator/reviewer report state to autonomous decision
- evaluator decision plus iteration budget to next phase
- autonomous phase back to the coarse task board status

The manager keeps side effects such as reviewer creation, tmux messages,
state persistence, and continuation prompts.

Direct tasks bypass automatic AI-review routing. When a Direct task posts a
review-gate report, the backend moves it to the existing Review/Done human gate
without creating a reviewer unless the report explicitly sets
`review_decision=request` or a human uses Request review.

### Report Flow

For Autonomous Mode V1, existing reviewer sessions also act as evaluators.
When an autonomous worker submits a review-gate report, the backend requests an
evaluator even if the worker asks to skip review. Evaluator outcomes map as
follows:

- `review_passed` -> run phase `passed`, task Review, human acceptance
  requested
- `review_failed` -> run phase `revising` and a targeted continue prompt when
  budget remains
- `review_failed` at max iteration -> run phase `exhausted`, task Review
- `review_needs_input` -> run phase `waiting_for_human`, task Review

Evaluation reports are stored on the autonomous run. If an evaluator posts a
structured `evaluation_report`, that payload is preserved; otherwise the
manager synthesizes a minimal evaluation record from the review report.

### Frontend

`AgentWorkspaceView.vue` adds a three-way mode selector to Add Task. When
Autonomous is selected, the form exposes max iterations, strictness, web
research, and artifact-review toggles. Task cards show a compact Auto round
badge. Task detail renders an Autonomous Run panel with phase, iteration,
score, threshold, strictness, artifact policy, next action, and evaluation
history.

## Key Issues / Pitfalls

- Keep autonomy metadata additive. Old workspace JSON must parse without
  autonomy fields.
- Do not let autonomous workers self-skip evaluation. The backend forces an
  evaluator for autonomous review-gate reports.
- Do not send Direct tasks through reviewed-task auto-review policy. Direct is
  for optional, user-triggered review.
- `passed` is not `done`. Autonomous pass only requests human acceptance.
- Budget enforcement belongs in pure policy; tmux continuation remains a
  manager side effect.
- Avoid a new Operator surface in V1. The UI should stay inside Agent
  Workspace and remain board/detail oriented.

## Validation

- `cd backend && uv run pytest tests/test_workspace_state_policy.py tests/test_workspaces.py -q`
- `cd backend && uv run black --check .`
- `cd backend && uv run isort --check-only .`
- `cd backend && uv run mypy .`
- `pnpm --dir frontend lint`
- `pnpm --dir frontend build`
- `git diff --check`
