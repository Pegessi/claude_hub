# Task Execution Flow Complexity

## System Overview
Agent Workspace now separates lifecycle mode from execution complexity. `task_mode`
continues to define Direct, Reviewed, and Autonomous review behavior. The new
`execution_complexity` field is an execution hint that changes prompt guidance
without changing task status transitions or reviewer routing.

The three values are:
- `auto`: default for new and legacy tasks. The worker must decide whether the
  task is simple or complex and state the chosen strategy in the first working
  report.
- `simple`: the worker should execute directly in the assigned session and keep
  planning compact.
- `complex`: the worker should act as orchestrator, decompose the work, delegate
  bounded implementation/testing/research/review subtasks to subagents when the
  runtime supports them, then personally integrate and validate the result.

## Module Design
- `backend/claude_hub/models/schemas.py` defines
  `WorkspaceTaskExecutionComplexity` and adds it to create/update/task schemas.
- `backend/claude_hub/services/workspace_manager.py` normalizes missing or bad
  legacy values to `auto`, persists the selected value, and injects complexity
  blocks into dispatcher, assignment, and review prompts.
- `frontend/src/types/index.ts` mirrors the API field in TypeScript.
- `frontend/src/components/AgentWorkspaceView.vue` adds the Auto/Simple/Complex
  selector in Add Task and shows the selected execution style in the assignment
  detail facts.
- `backend/tests/test_workspaces.py` covers default persistence, legacy
  normalization, complex assignment prompt guidance, and reviewer prompt
  visibility.

## Research Inputs
The prompt shape followed a conservative pattern from comparable agent systems:
- Claude Code subagents: clear specialist descriptions, isolated context, and
  concise returned summaries.
- AutoGen teams: use single agents for simpler tasks and teams for complex work
  that needs diverse expertise.
- OpenAI Agents handoffs: make delegation/handoff behavior explicit in prompts.
- OpenHands repository skills: keep always-loaded repository guidance succinct.

## Key Issues/Pitfalls
- Do not overload `task_mode`. Review lifecycle and execution style are separate
  concerns.
- V1 is prompt-level orchestration. A backend dispatcher that automatically
  provisions worker sessions is a larger scheduling feature and should be a
  separate design.
- Reviewers need the selected complexity so they can catch both over-process on
  simple tasks and under-decomposition on complex tasks.

## Validation
- `cd backend && uv run pytest tests/test_workspaces.py -q`: 58 passed.
- `cd backend && uv run black --check .`: passed after formatting.
- `cd backend && uv run isort --check-only .`: passed.
- `cd backend && uv run mypy .`: passed.
- `pnpm --dir frontend lint`: passed after installing frontend dependencies in
  the isolated worktree.
- `pnpm --dir frontend build`: passed.
- `git diff --check`: passed.
