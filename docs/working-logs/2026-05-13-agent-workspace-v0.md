# Agent Workspace V0

## System Overview

Agent Workspace V0 adds a human-orchestrated task board on top of the existing ttyd/tmux tab runtime. It is intentionally lighter than a background semantic orchestrator: the user creates tasks, starts a resident workspace agent terminal, dispatches tasks to that agent, sends follow-up messages, opens the underlying terminal tab, and moves tasks through todo, assigned, working, review, and done.

The primary execution path is now a resident workspace agent, usually Codex, running in the configured repository path. The older per-task worker path still exists in the backend for experimentation and can create git worktrees under `~/.claude_hub/projects/<workspace_id>/worktrees/<session_id>`, but it is no longer the frontend default. Workspace/task/session/report metadata is persisted in `~/.claude_hub/workspaces.json`.

## Module Design

- `backend/claude_hub/services/workspace_manager.py`: owns workspace/task/session state, resident agent creation, task dispatch, optional worktree worker creation, tmux prompt sending, reports, and best-effort runtime status refresh.
- `backend/claude_hub/api/workspaces.py`: exposes workspace board, task, spawn, and send-message APIs under `/api/workspaces`.
- `frontend/src/stores/workspaceStore.ts`: Pinia store for workspace list, board data, task mutations, spawning, and message sending.
- `frontend/src/components/AgentWorkspaceView.vue`: V0 board UI with workspace setup, task creation, worker spawning, status polling, and terminal tab handoff.
- `frontend/src/stores/appStore.ts`: persists the main app mode between terminal view and workspace view.

## Task Detail And Reports

The board now carries `reports` alongside tasks and sessions. The resident agent can append a progress report through `POST /api/workspaces/sessions/{managed_session_id}/reports` with a state such as `started`, `working`, `blocked`, `needs_input`, `ready_for_review`, or `completed`. Resident-agent reports include `task_id` so the timeline attaches to the correct task. Reports update task status, then appear in the task detail timeline.

The frontend uses task cards for triage and a persistent detail surface for evidence and actions. Desktop opens a right-side detail pane; mobile opens a bottom sheet constrained to the viewport. Detail shows prompt, session facts, branch/worktree, follow-up send form, delete action, and the report timeline.

Deleting a task removes the task and its reports, but keeps the resident agent terminal alive. If a session was directly tied to the deleted task, its `task_id` is cleared.

## Key Issues/Pitfalls

- The route path parameter for sending messages is named `managed_session_id`, not `session_id`, to avoid clashing with the auth dependency cookie parameter.
- Workspace API tests override `get_current_user`; otherwise `TestClient` can trigger a 401 when auth is enabled and the client host is not considered local.
- This V0 does not implement autonomous orchestration, semantic decomposition, automatic review, branch merge, or CI-driven task completion. Those are deliberately left as follow-up layers after validating the managed worker flow.
- `git worktree add` uses the configured `default_branch` as the base. If a repo only has `origin/main` locally, workspace creation succeeds but spawn may need a fetched local base branch.
- Worker reports are prompt-driven in this version. A stronger follow-up is to add a small local CLI wrapper so agents can run `claude-hub report ...` instead of hand-writing curl JSON.
- The resident agent runs in the main configured repository path. This matches the desired inspectable terminal flow but means execution is serial and not isolated per task.
- Task deletion does not cancel any in-flight agent work. If the resident agent is already executing that task, the user should send a follow-up instruction or stop the terminal.

## Validation

- `cd backend && uv run pytest`
- `cd frontend && pnpm build`
- Playwright smoke check with mocked workspace data at desktop and mobile widths.
