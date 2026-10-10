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
- Dark uses near-black neutral surfaces with a warm coral highlight. Light uses
  warm paper surfaces with a deeper terracotta accent. Semantic status colors
  remain unchanged, while terminal surfaces and text selection follow the new
  neutrals. Primary button colors retain at least 4.5:1 contrast against their
  inverse text.

## Validation

- Focused interaction, store, theme, and semantic-color tests: `27 passed`.
- Full frontend unit suite after final review: `706 passed`; the added tests
  cover delete success/failure, stale-pin cleanup, menu removal, palette tokens,
  and accent contrast.
- `pnpm lint:check`, `pnpm typecheck`, and `pnpm build` passed.
- Playwright reviewed the worktree frontend on `127.0.0.1:5184` in Dark and
  Light at 1440x980. It verified the menu stays inside the viewport, Move
  actions are absent, Delete opens the confirmation with initial Cancel focus,
  Escape closes it, pointer movement enters drag state, and the console has no
  errors or warnings. No session was deleted. The one test reorder write was
  restored and verified against the backend before continuing.
- Independent diff review found and fixed insufficient Dark primary-button
  contrast, then added a regression assertion for both themes.
- Final repository-wide `./scripts/verify.sh all` (pinned Node 24.21.0, uv
  0.12.15, Python 3.11.16, and pnpm 9.15.9): docs, backend formatting and
  types, frontend lint and types, all 706 frontend tests, and the production
  build passed. The backend suite stopped at `1803 passed, 1 skipped` because
  the unchanged `test_frontend_build_timeout_keeps_current_backend_and_dist`
  hit macOS
  `EPERM` while starting its process-group fixture. The same test passed alone
  (`1 passed`) but reproduced when its 21-test module ran as a group
  (`20 passed, 1 failed`), so this is recorded as an existing environment/
  isolation issue rather than treated as coverage for this frontend change.
