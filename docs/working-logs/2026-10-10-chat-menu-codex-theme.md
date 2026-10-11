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
  any server-persisted pin for the deleted session. Existing TabBar and
  archived-session confirmation surfaces also stay open when deletion fails.
- Chat pins now use an authenticated server preference API backed by an atomic,
  locked runtime-home file. Single-tab Pin/Unpin mutations avoid whole-list
  lost updates, preferences are isolated per server user, and all browser
  origins/devices share the same state. Existing valid localStorage pins are
  merged at most once and the legacy key is removed after a successful read.
  `fetchTabs` synchronizes pins after it restores the active tab and first pane,
  so the preference request no longer delays pane initialization.
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
- Only sessions whose runtime status is `working` mount
  `ChatSidebarWorkingEffect`. Like Maso, the row is marked by a light pulse
  alone: it gains no full outline or outer glow, and a selected working row
  keeps only a 20% selected border beside its selected fill. The pulse travels
  clockwise on the border centerline as twelve translucent SVG dashes nested
  around one peak (`createWorkingBeamLayers`), giving an 18-unit fading tail and
  a 3-unit leading fade on a 100-unit perimeter, plus a faint blurred bloom.
  There is no solid head segment. Inside the row, 14 dim stars and two small
  glints, seeded by tab id, brighten as the peak crosses their column on the
  same orbit clock, and a faint light pool follows the peak. The yellow status
  dot remains the semantic working indicator, idle and attention colors are
  unchanged, and reduced-motion users receive no moving light.
- Active mode, layout, sidebar item, pane, and assistant avatar now share the
  selected-surface language. Empty panes use a theme-aware line icon and
  brighter guidance instead of a dim system emoji.
- Sidebar rows now use 7px vertical padding with 3px row gaps and 8px between
  workspace groups. Dragging a workspace header moves all manual sessions in
  that cwd as one persisted block without changing any session cwd. Pinned
  Chats keep their slots, so a workspace drag never rearranges the pinned
  section. Terminal Status follows the Chat workspace sequence and appends
  terminal-only groups.

## Validation

- Focused interaction, store, theme, and semantic-color tests passed.
- Full frontend unit suite after the cross-review fixes: `730 passed`; the added tests
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
- A server-Pin Playwright pass used `127.0.0.1:5184` and `localhost:5184` as
  separate browser origins. Pinning in the first appeared after reloading the
  second, both legacy localStorage values were null, and the server API returned
  the same pinned id with no page errors. The isolated preview state was reset.
- The 5184 review server keeps all live Hub APIs (tabs, Worker sessions, status,
  streams and Workspace data) on 8173 and routes only the new
  `/api/tabs/sidebar-preferences*` family to the isolated 8184 preview store.
  A live comparison returned identical 35-tab id sets and included a newly
  created `乱码2b5复现` session; that new id also passed the two-origin Pin check.
- The working-light visual pass paused every row animation at seven orbit
  phases in Playwright Chromium on 5184, for a selected and an unselected
  working row in Dark and an unselected row in Light. The pulse stayed on the
  border with soft ends, stars brightened behind its peak in the same direction,
  rows showed no full lit outline, and the page reported no errors. An earlier
  revision showed why replacement was needed: its SVG particles used
  `offset-path` and never left the row corner, and its solid white head read as
  a hard segment.
- A cross-review of the whole branch against `main` found two issues, both
  fixed. First, the pulse animated the inherited `stroke-dashoffset` on a shared
  SVG `<g>`. With two working rows in headless Chromium 145, the effect then
  cost about 256-324 ms of main-thread work per second, 168-189 ms of it style
  recalculation; hiding only the beam dropped this to about 2 ms, while the
  composited stars and pool were negligible. Animating each rect directly keeps
  the same picture and measured 111-118 ms per second with 36-39 ms of style
  recalculation; the remainder is per-frame SVG repaint. Second, a workspace
  drag moved pinned Chats with their cwd block and so reordered the pinned
  section; a store test reproduced `[P2, P1]` before the fix. The review also
  merged the branch with `origin/main` in an exported tree: only `CHANGELOG.md`
  conflicted, and all 746 combined frontend tests plus typecheck passed.
- Independent diff review found and fixed insufficient Dark primary-button
  contrast. The researched revision review then caught foreground accents reused
  as white-text fills and translucent focus rings; filled controls now use the
  contrast-safe strong role, keyboard focus uses a solid contrast-safe ring, and
  decorative halos retain their separate translucent role.
- Final repository-wide `./scripts/verify.sh all` (pinned Node 24.21.0, uv
  0.12.15, Python 3.11.16, and pnpm 9.15.9) passed docs, backend formatting
  and types, frontend lint and types, all 730 frontend tests, and the production
  build. The backend suite reported `1808 passed, 1 skipped, 1 failed` because
  the unchanged `test_frontend_build_timeout_keeps_current_backend_and_dist`
  hit macOS
  `EPERM` while starting its process-group fixture. The same test passed alone
  (`1 passed`) but reproduced when its 21-test module ran as a group
  (`20 passed, 1 failed`), so this is recorded as an existing environment/
  isolation issue rather than treated as coverage for this frontend change.
