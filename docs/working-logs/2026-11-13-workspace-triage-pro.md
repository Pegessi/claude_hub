# 2026-11-13 Workspace Linear Triage Pro real wiring

## Context

The classic multi-column Agent Workspace board required scanning four status columns,
agent cards, and a modal detail dialog to locate tasks awaiting human review or
decision, and accepting a task left caller-owned ephemeral sessions behind unless
cleaned up via CLI.

Following three high-craft MASO Gemini 4 Argon (`gemini-4-argon_reasoning_high`)
design explorations, the operator selected `1 · Linear Triage Pro` and requested
wiring it into the live Workspace store/API surface before moving on to Feishu
patrol automation.

## What changed

- `frontend/src/features/workspace/presentation/triageProModel.ts`: pure view-model
  projection mapping `WorkspaceTask`, `AgentReport[]`, `ManagedSession`, and
  `TaskLifecycle` into `NEEDS YOU` / `ACTIVE` / `RECENT` buckets, short task codes
  (`MR-xxxx` / `PR-xxxx` / `#HEX`), Heterogeneous Consensus Matrix (`implementer -> reviewer`,
  review trajectory, criteria/validation highlights), deduplicated semantic artifact links,
  and an epoch-grouped `TriageSpineNode[]` timeline.
- `frontend/src/features/workspace/components/WorkspaceTriagePro.vue`: two-pane
  triage surface (360px accessible `<button>` task rail + architectural right detail
  spec frame, inline Steer composer with epoch-guarded image paste, on-demand
  `/stream/events` transcript preview + one-click archived tab restore, and default-closed
  `<details>` disclosure for prompt/abort/delete/`TaskExecutionPanel`).
- `frontend/src/stores/workspace/actions.ts` & `useWorkspaceTaskActions.ts`: added
  `cleanupTaskSession` and `acceptAndCleanupTask` / `cleanupCompletedTaskSession` so
  accepting a task automatically releases caller-owned ephemeral sessions and warns
  with a retryable `Release session` button if cleanup is skipped or fails.
- `useWorkspaceRuntime.ts`, `WorkspaceHeader.vue`, `WorkspacePage.vue`: added
  `workspaceLayoutMode` (`'triage'` default, persisted in `localStorage` and overridable
  via `?ws_layout=board|triage`) with a reversible `Classic Board` <-> `Triage View`
  toggle that clears `selectedTaskId` when switching to board mode so `TaskDetailDialog`
  never intercepts pointer events unexpectedly.

## Adversarial cross-review (Codex, 4 rounds to PASS)

- **R1 -> R2**: reset steering drafts on `selectedTaskId` transitions; gated `J/K`,
  `S`, and `Cmd/Ctrl+Enter` shortcuts behind all reactive workspace dialogs,
  menus, markdown previews, and image lightboxes; captured `shouldCleanupSession`
  and `originWorkspaceId` prior to awaiting `updateTaskStatus`; surfaced warning
  notifications + `Release session` retry on non-benign cleanup skips/errors; converted
  rail rows to native `<button type="button">` controls with `aria-current="true"`.
- **R2 -> R4**: staged clipboard image pastes behind `steerSelectionEpoch` and
  invalidated both `steerSelectionEpoch` and `transcriptEpoch` inside
  `onBeforeUnmount` so an in-flight async image decode cannot leak into another task
  or into Classic Board after layout toggle.

## Verification

- `./scripts/verify.sh frontend-types` — PASS
- `./scripts/verify.sh frontend-lint` — PASS
- `./scripts/verify.sh frontend-tests` (1488 unit tests including `triageProModel.test.mjs`
  and `workspaceStructure.test.mjs`) — PASS
- `./scripts/verify.sh frontend-build` — PASS
- Mocked Playwright workspace suite (`browser_agent_workspace_task_create.py`,
  `browser_workspace_detail.py`, `browser_workspace_picker_delete.py`,
  `browser_task_execution_panel.py`) at 1280px, 720px, and 390px — all PASS.
