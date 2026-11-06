# Sub-agent workspace attribution and parent-Chat jump tips (2026-11-03)

> Superseded 2026-11-06: `ChatSubagentJumpCard` was replaced by the Agents dock
> (top-right pill / wide-pane rail, mobile bar + sheet). See CHANGELOG.

InfoWall P0 d2627d2f: 「Subagent 默认归属对应 Agent Workspace 并增加类 Codex
跳转 Tips 卡片」。Branch `feat/subagent-workspace-and-tips` from main `dd681a7`.

## Problem

A Hub sub-agent (`claude-hub agent run`, `POST /api/subagents`) was created as a
plain direct Chat tab with no `workspace_id`, so even sub-agents launched from a
managed workspace session landed in the ungrouped Terminal surfaces. The backend
already serialized `parent_tab_id`/`subagent_id` and anticipated display-only
workspace attribution on direct Chats (`api/tabs.py` fence comment), but nothing
stamped the child at creation and the frontend never consumed the linkage or the
`/api/subagents` records.

## Backend changes

- `services/subagents.py`: a new `SubagentRuntime.resolve_workspace(parent, cwd)`
  seam resolves attribution at `_create_locked` time and forwards
  `workspace_id/workspace_name` to `create_tab` (role and managed session stay
  empty, so the child is never promoted to a managed Chat). The production
  resolver (`_resolve_subagent_workspace`) inherits the parent tab's workspace
  when it still exists and the child cwd validates against it via
  `validate_local_agent_cwd_for_workspace` (same git common-dir, including linked
  worktrees; path containment for non-git); a REMOTE parent inherits
  unconditionally. Otherwise it falls back to a read-only cwd lookup. Resolution
  failures are logged and never block the spawn.
- `services/workspace_manager/workspaces/lifecycle.py`: new
  `find_workspace_for_local_path` — read-only identity lookup (no creation,
  ambiguous multi-worktree identity -> None).
- `services/workspace_manager/tmux/review_reaper.py`: orphan pruning skips
  role-less direct Chats carrying workspace metadata, while role-bearing managed
  orphans are still reaped. Without this guard the first board load after the
  60 s grace would have deleted the attributed sub-agent tab.
- `models/subagent.py`: `SubagentSummary.last_summary` — a whitespace-collapsed,
  280-char bounded preview of the last turn's final text.

## Frontend changes

- `types/terminal.ts` gains `parent_tab_id`/`subagent_id`. Existing
  `manualTabs = !workspace_id` / `managedTabs = Boolean(workspace_id)` predicates
  move the attributed child out of the tab strip, Terminal Status panel and cwd
  chat sidebar into the Workspace Agents panel (grouped by workspace name, with
  a new `sub` marker).
- `features/workspace/components/WorkspaceAgentStatus.vue` +
  `composables/useWorkspaceRuntime.ts`: a third Subagents view listing live
  role-less child tabs attributed to the active workspace, with 5 s polling
  only while active and one-click jump into the child Chat.
- `features/chat/components/ChatSubagentJumpCard.vue` +
  `controllers/useParentSubagents.ts` + `presentation/subagentJump.ts`: compact
  card mounted above the composer in the parent Chat (direct and managed). It
  polls `GET /api/subagents?parent_tab_id=…&include_closed=true` every 5 s while
  mounted, resets on pane tab switches, tolerates failed fetches, caps at 10
  chips, and jumps via `setActiveTab` — calling `unarchiveTab` first when the
  child was archived after a non-kept run.

## Deliberate boundaries

- Native provider-local Task/Agent-tool sub-agents have no Hub tab (transcript
  regions only) and are unaffected.
- No new CLI flags or request fields; no auto-creation of workspaces; no change
  to POST /api/tabs, fork/duplicate or Dot tab creation; no workspace rename
  resync for display-only tabs; Feishu surfaces untouched.

## Validation

- Backend: 5994 passed / 10 skipped (`./scripts/verify.sh backend-tests`);
  `backend-types` (614 files) and `backend-format` clean. Focused additions in
  `test_subagents.py` (inheritance matrix, resolver policy, failure isolation,
  summary), `test_orphan_tab_reconcile.py`, `test_workspace_identity.py`,
  `test_workspace_manager_structure.py` (explicit delayed-import allowlist).
- Frontend: 1185/1185 node tests, vue-tsc, eslint and vite build clean; new
  tests `subagentJump.test.mjs`, `useParentSubagents.test.mjs`,
  `subagentJumpCard.test.mjs`; the workspace characterization digests were
  deliberately re-baselined after verifying the only native-tree delta is the
  Subagents toggle, the managed-grid gate and the sub-agent list.
- `./scripts/verify.sh browser-mocks`: all 17 scenarios pass on an owned Vite
  port, including the new `browser_subagent_jump_card.py` (card render,
  workspace grouping, live jump, archived restore; screenshots inspected).
  Existing strict-mock scenarios gained an empty `GET /api/subagents` branch
  because every Chat now polls it.

## Not verified

No live-provider `claude-hub agent run` against a running Hub (the live service
was never restarted); service-level fakes plus the mocked full-app browser gate
are the evidence. Workspace rename does not resync display-only child tabs.
