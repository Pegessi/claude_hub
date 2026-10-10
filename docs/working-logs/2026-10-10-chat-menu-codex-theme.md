# Chat session menu and Codex-inspired themes

## Goal

Make the Chat sidebar's session actions easier to understand and bring both
application themes closer to Codex's restrained developer-tool character. The
sidebar should expose permanent deletion, avoid redundant menu-based ordering,
and retain direct drag reordering.

## Behavior

- The shared session menu now ends with a separated **Delete** action on both
  the sidebar and TabBar surfaces. It opens an explicit permanent-deletion
  dialog, focuses **Cancel**, supports Escape/overlay dismissal, and returns
  focus to the trigger after cancellation. The dialog remains open when the
  API reports failure.
- The sidebar no longer renders **Move up** or **Move down**, and the associated
  Alt+Arrow path was removed with it. Pointer drag remains the single ordering
  interaction and still persists the complete tab order.
- Successful deletion now returns an explicit boolean to callers and removes
  any browser-persisted pin for the deleted session. Existing TabBar and
  archived-session confirmation surfaces also stay open when deletion fails.
- Dark uses a Radix Slate / VS Code-inspired surface ladder instead of stacking
  near-black panels. Light mirrors those roles with cool mist neutrals. A calm
  blue family is split across selected surfaces, focus borders, foreground
  accents, and primary buttons so active controls are visible without turning
  the whole interface blue. Semantic status colors remain unchanged.
- Active mode, layout, sidebar item, pane, and assistant avatar now share the
  selected-surface language. Empty panes use a theme-aware line icon and
  brighter guidance instead of a dim system emoji.

## Validation

- Focused interaction, store, theme, and semantic-color tests passed.
- Full frontend unit suite after the researched palette revision: `709 passed`; the added tests
  cover delete success/failure, stale-pin cleanup, menu removal, palette tokens,
  accent contrast, separation of selected/focus/primary roles, and a repository-
  wide guard against translucent keyboard-focus rings.
- `pnpm lint:check`, `pnpm typecheck`, and `pnpm build` passed.
- Playwright reviewed the worktree frontend on `127.0.0.1:5184` in Dark and
  Light at 1440x980. It verified the menu stays inside the viewport, Move
  actions are absent, Delete opens the confirmation with initial Cancel focus,
  Escape closes it, pointer movement enters drag state, and the console has no
  errors or warnings. No session was deleted. The one test reorder write was
  restored and verified against the backend before continuing.
- A second Playwright visual pass covered the researched Dark/Light palette,
  2x2 empty-pane hierarchy, and real assistant avatars. Both themes rendered
  without console errors or warnings.
- Independent diff review found and fixed insufficient Dark primary-button
  contrast. The researched revision review then caught foreground accents reused
  as white-text fills and translucent focus rings; filled controls now use the
  contrast-safe strong role, keyboard focus uses a solid contrast-safe ring, and
  decorative halos retain their separate translucent role.
- Final repository-wide `./scripts/verify.sh all` (pinned Node 24.21.0, uv
  0.12.15, Python 3.11.16, and pnpm 9.15.9) passed docs, backend formatting
  and types, frontend lint and types, all 709 frontend tests, and the production
  build. The backend suite stopped at `1803 passed, 1 skipped` because
  the unchanged `test_frontend_build_timeout_keeps_current_backend_and_dist`
  hit macOS
  `EPERM` while starting its process-group fixture. The same test passed alone
  (`1 passed`) but reproduced when its 21-test module ran as a group
  (`20 passed, 1 failed`), so this is recorded as an existing environment/
  isolation issue rather than treated as coverage for this frontend change.
