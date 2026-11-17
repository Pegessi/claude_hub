# 2026-11-18 Mobile modal soft-keyboard visualViewport adaptation

## Symptom

On mobile, focusing a text input inside a modal dialog (for example `SessionCreator`, `TaskCreateDialog`, `GoalSetupDialog`, `WorkspaceEditorDialog`, or `TaskEditDialog`) caused the browser to push the modal off the top edge of the screen when the soft keyboard opened, leaving only the bottom line of the dialog visible at the very top of the viewport above a blank blurred backdrop area, whereas `ChatUI`'s composer stayed anchored in the upper half of the screen above the keyboard.

## Root Causes

1. **Backdrop `inset: 0` + `padding-bottom: var(--keyboard-height)` double-subtraction and `offsetTop` mismatch**:
   - `ChatUI` kept its surface inside the visible upper half because `shell.css` anchored `.app[data-mode='terminal'][data-active-pane-chat='true']` to `position: fixed; top: var(--visual-viewport-offset-top, 0px); height: var(--visual-viewport-height, 100dvh)`.
   - By contrast, modal overlays (`.modal-overlay`, `.workspace-modal-overlay`, `.goal-dialog-backdrop`, etc.) used `position: fixed; inset: 0; overflow-y: auto` and on `@media (max-width: 640px)` applied `padding: 0 0 var(--keyboard-height, 0px)`.
   - When the mobile browser either shrank the layout viewport along with the visual viewport (Android Chrome / Feishu WebView) or panned `visualViewport.offsetTop > 0` (iOS Safari) or scrolled the `overflow-y: auto` backdrop during native focus-reveal, the overlay's content box was shifted above the visible viewport while its `padding-bottom` rendered as a blank `backdrop-filter: blur(6px)` region right above the keyboard.
2. **Non-chat `.app` shell not fixed to `visualViewport.offsetTop`**:
   - In `shell.css`, `position: fixed; top: var(--visual-viewport-offset-top, 0px)` was restricted to `.app[data-mode='terminal'][data-active-pane-chat='true']`, allowing document/window scroll in Terminal non-chat and Agent Workspace modes when focusing modal inputs.
3. **Component-level `100dvh` / `70dvh` overrides and `<16px` modal inputs**:
   - Several modal stylesheets (`TaskCreateDialog.vue`, `shared-compact-layout.css`, `ResidentAgentDialog.vue`, `AgentDirectoryDialog.vue`, `TaskDetailDialog.vue`, `EnvPresetManager.vue`, `feishu-bot-settings-dialog.css`, `AppearanceDialog.vue`, `tab-actions-menu.css`) still used raw `100dvh` or `70dvh` heights instead of `var(--visual-viewport-height, 100dvh)`.
   - Modal inputs outside `SessionCreator` and `AgentConfigFields` used `13px` (`--ch-font-size-md`) on mobile widths, which triggers iOS Safari's automatic zoom-and-pan when focused.
4. **Focused field scroll containment inside modal bodies**:
   - When `visualViewport` shrank after focusing an input near the bottom of a modal form, sticky action footers (`.modal-actions`, `.tam-modal-actions`, `.file-browser-footer`) could cover the focused field unless the modal's own scroll container adjusted its `scrollTop`.

## What Changed

- `frontend/src/features/app-shell/styles/overlays.css`:
  - On `@media (max-width: 768px), (any-pointer: coarse)`, anchor all modal/sheet overlays (`.modal-overlay`, `.workspace-modal-overlay`, `.goal-dialog-backdrop`, `.quota-modal-backdrop`, `.task-detail-overlay`, `.tam-overlay`, `.subagent-sheet-backdrop`, `.native-subagent-backdrop`, `.inbox-sheet-backdrop`, `.goal-sheet-backdrop`, `.dot-overlay`, `.archive-overlay`) to `top: var(--visual-viewport-offset-top, 0px); height: var(--visual-viewport-height, 100dvh); max-height: var(--visual-viewport-height, 100dvh); bottom: auto; overflow: hidden;` and remove `padding: 0 0 var(--keyboard-height, 0px)`.
  - Bound native `dialog[open]` sheets (`restart-dialog`, `bot-pool`, `appearance-dialog`) and `.goal-status-details--sheet` against `--visual-viewport-offset-top` and `--visual-viewport-height`.
  - Enforce `font-size: max(var(--ch-font-size-md), 16px)` on editable controls inside modals on mobile/coarse viewports.
- `frontend/src/features/app-shell/styles/shell.css`:
  - Apply `position: fixed; top: var(--visual-viewport-offset-top, 0px); left: 0; width: 100%` to `.app` across all modes on `@media (max-width: 768px), (any-pointer: coarse)`.
- Modal component styles (`TaskCreateDialog.vue`, `shared-compact-layout.css`, `ResidentAgentDialog.vue`, `AgentDirectoryDialog.vue`, `TaskDetailDialog.vue`, `EnvPresetManager.vue`, `feishu-bot-settings-dialog.css`, `AppearanceDialog.vue`, `tab-actions-menu.css`):
  - Replace raw `100dvh` / `70dvh` height/max-height bounds with `var(--visual-viewport-height, 100dvh)`.
- `frontend/src/utils/mobileInputFocus.ts` & `frontend/src/App.vue`:
  - Add `computeModalFieldScrollDelta`, `findModalScrollContainer`, and `syncModalViewportScroll` to keep `overlay.scrollTop = 0` and `window.scrollY = 0` while scrolling any focused modal input/textarea into the visible region between sticky headers and sticky action footers whenever `focusin` fires or `visualViewport` resizes/scrolls.
  - Refresh `App.vue` viewport metrics synchronously on `window` `resize` and reset `largestVisualViewportHeight` when `viewportHeight` returns to `window.innerHeight`.

## Verification

- `./scripts/verify.sh all` (docs, checkout-safety, backend-format, backend-types, backend-tests, frontend-lint, frontend-types, frontend-tests, frontend-build).
- `CLAUDE_HUB_BROWSER_EXECUTABLE=... ./scripts/verify.sh browser-mocks` (25/25 Playwright browser scenarios passing, including mobile soft-keyboard `visualViewport` shrink + `offsetTop` pan + focused-input scroll checks in `browser_chat_composer.py`, `browser_agent_workspace_task_create.py`, `browser_chat_goal_steer.py`, and `browser_service_restart.py`).
