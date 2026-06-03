# Edit Todo Task

## System Overview

Agent Workspace todo tasks can now be corrected before dispatch. The feature
edits the existing task record's title and prompt/description through the
workspace task PATCH endpoint and refreshes the board after a successful save.

The edit path is intentionally limited to `todo` tasks. Once a task has been
queued, started, sent to review, or completed, its title and prompt may already
be part of terminal prompts, reports, or review context and should not be
rewritten silently.

## Module Design

- `backend/claude_hub/models/schemas.py` extends `WorkspaceTaskUpdate` with
  optional `title` and `prompt` fields.
- `backend/claude_hub/services/workspace_manager.py` trims task title/prompt
  on create and edit, rejects blank title/description text, and rejects
  title/prompt edits unless the task is still `todo`.
- `backend/claude_hub/api/workspaces.py` treats title/prompt as valid update
  payload fields and returns validation failures as HTTP 400 responses.
- `frontend/src/stores/workspaceStore.ts` exposes `updateTask()` for the shared
  PATCH route.
- `frontend/src/components/AgentWorkspaceView.vue` shows an Edit action on
  todo task cards and task detail, backed by a focused title/description modal.

## Key Issues/Pitfalls

- Do not allow active or completed task prompt edits without designing an audit
  and replay story; dispatched prompts and reviewer context would otherwise
  diverge from the task card.
- The edit modal does not support attachments, task mode, execution complexity,
  related task, or autonomy settings. Those fields affect dispatch semantics and
  need separate review if they become editable.
- API validation mirrors the UI's existing text requirements so direct API
  clients cannot save an empty task title or description.
