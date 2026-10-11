# Chat session menu and Maso-inspired themes

## Goal

Make the Chat sidebar's session actions easier to understand and give both
application themes a coherent developer-tool character. The sidebar should
expose permanent deletion, avoid redundant menu-based ordering, retain direct
drag reordering, and make genuinely active work easy to scan.

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
- Dark uses Maso's compact carbon hierarchy and the blue family already present
  on `main`: `#7cb7ff` for foreground emphasis and focus, `#5a9cf0` for selected
  borders and live-session light, and a darker `#3276ba` for white-text actions.
  Light keeps neutral surfaces with a low-saturation clay-orange family; both
  hue and saturation stay separated from the brighter warning and danger roles.
- White-text controls use a separate contrast-safe fill pair: the brighter
  Maso hover token remains available for non-text emphasis, while `#3478bb`
  keeps hovered Dark buttons above the WCAG AA contrast threshold.
- User messages use a theme role instead of a fixed blue: Maso deep blue in
  Dark and the contrast-safe clay-orange fill in Light.
- The Chat layout now gives the two primary planes their own roles, copied from
  Maso's base/nav split: Dark main `#181818` with sidebar `#1c1c1c`, and Light
  main white with sidebar `#f5f5f5`. Shared application surfaces stay on their
  prior tokens so Workspace and dialogs do not change as a side effect.
- An unread completed turn keeps its semantic status color and receives a small
  highlight moving inside the dot. It no longer combines a green idle dot with
  a large blue ripple; reduced-motion users receive the static dot.
- Only sessions whose runtime status is `working` receive the travelling edge
  light. The yellow status dot remains the semantic working indicator, idle and
  attention colors are unchanged, and reduced-motion users receive a static
  edge instead of an animation.
- Active mode, layout, sidebar item, pane, and assistant avatar now share the
  selected-surface language. Empty panes use a theme-aware line icon and
  brighter guidance instead of a dim system emoji.
- Sidebar rows now use 7px vertical padding with 3px row gaps and 8px between
  workspace groups. Dragging a workspace header moves all manual sessions in
  that cwd as one persisted block without changing any session cwd. Terminal
  Status follows the Chat workspace sequence and appends terminal-only groups.

## Validation

- Focused interaction, store, theme, and semantic-color tests passed.
- Full frontend unit suite after the latest sidebar-order revision: `716 passed`; the added tests
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
- The final 5184 visual pass rendered 11 real Chat sessions with no page errors,
  observed two genuine `working` sessions in Dark, and confirmed the computed
  Maso tokens (`#7cb7ff`, `#5a9cf0`, and `#3276ba`) plus the Light clay-orange
  selected state.
- A follow-up Playwright pass at 1440x1050 measured 31px Chat rows, 29px
  workspace headers, 3px row gaps, and 8px group spacing. It dragged
  `codex_workspace` after `claude_hub`, observed one complete 35-tab order
  write, and confirmed Terminal Status rendered the same four workspace groups
  in the same order with no page errors. The order request was intercepted, so
  the live backend order remained unchanged.
- Independent diff review found and fixed insufficient Dark primary-button
  contrast. The researched revision review then caught foreground accents reused
  as white-text fills and translucent focus rings; filled controls now use the
  contrast-safe strong role, keyboard focus uses a solid contrast-safe ring, and
  decorative halos retain their separate translucent role.
- Final repository-wide `./scripts/verify.sh all` (pinned Node 24.21.0, uv
  0.12.15, Python 3.11.16, and pnpm 9.15.9) passed docs, backend formatting
  and types, frontend lint and types, all 711 frontend tests, and the production
  build. The backend suite stopped at `1803 passed, 1 skipped` because
  the unchanged `test_frontend_build_timeout_keeps_current_backend_and_dist`
  hit macOS
  `EPERM` while starting its process-group fixture. The same test passed alone
  (`1 passed`) but reproduced when its 21-test module ran as a group
  (`20 passed, 1 failed`), so this is recorded as an existing environment/
  isolation issue rather than treated as coverage for this frontend change.
