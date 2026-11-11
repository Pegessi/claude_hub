# Appearance themes — 2026-11-11

## Goal and scope

Replace the binary theme switch with a small, coherent multi-palette system that
works in both application modes and on phones. Worktree: `theme-system`, branch
`codex/theme-system`, baseline `745b6a9`. No backend preference, provider, shared
service, deployment, merge or push changes.

## Design decision card

- **Goal/boundary:** six curated palettes × light/dark, optional system following;
  keep the current Classic palette and existing saved light/dark preference.
- **Nearest implementations:** existing `theme.css` semantic tokens and terminal
  payload, native `<dialog>` in `ServiceRestartDialog.vue`, mobile app menus.
- **Reuse/add/remove:** keep `ColorScheme` and `data-theme` binary; add independent
  `data-palette`, a small typed preference registry, primitive palette overrides
  and one Appearance dialog. Remove binary switch UI and three root-level theme
  writers; `appStore` owns application/reader state and listeners.
- **Validation contract:** real store tests cover migration, normalization,
  persistence failures, system changes, storage events and listener disposal;
  mocked Chromium SPA tests cover ordinary clicks/keyboard, 12 rendered variants,
  reader inheritance, mobile reachability/overflow, and contrast. Run repository
  `all`, `browser-mocks` and `terminal-hmr` gates; mock coverage is not live provider
  or real-device Safari/Android acceptance.
- **Undecided:** none. Custom arbitrary colors, per-workspace themes, font/density
  settings and a command-palette overhaul are deliberately out of scope.

## Research and rationale

- [Radix functional color scales](https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale):
  separate background, control state, border, solid accent and readable-text roles.
  Adopt the role separation, not an extra dependency or raw color-class API.
- [Linear UI redesign](https://linear.app/blog/how-we-redesigned-the-linear-ui):
  low-chroma structural planes and a small primitive vocabulary make themes
  cohesive without a new per-component palette. Here the presets are curated
  rather than exposing an unvalidated arbitrary-color generator.
- [GitHub appearance settings](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-personal-account-settings/managing-your-theme-settings):
  separate mode/system-following from selected colors.
- [Catppuccin style guide](https://github.com/catppuccin/catppuccin/blob/main/docs/style-guide.md):
  use related but distinguishable chrome, canvas and raised-surface planes.

Classic retains carbon/blue dark and neutral/clay light. Slate is cool blue;
Sand is warm paper/terracotta; Forest is green/teal; Dusk is dimmed violet;
Contrast emphasizes text and borders. Each has both modes. Status and code token
roles remain mode-specific, not recolored to the accent hue.

## Implementation

`main.ts` initializes the existing app store before selecting the dynamic root.
The store resolves an atomic `{mode, palette}` browser preference, listens to the
OS only for System mode, updates same-origin open pages from storage events, and
writes resolved root attributes and the mobile browser theme-color. Invalid
records recover to Classic; legacy preferences are read but never eagerly
rewritten. Failed storage writes keep the in-memory choice and show a limitation.

`palettes.css` adds 13 primitive colors per variant and maps those into the
existing `--ch-color-*`/terminal contract. The picker previews use the identical
CSS scopes; they are not approximate duplicated swatches. Semantic status and
syntax colors continue to come from the light/dark base. Terminal views observe
palette as well as resolved mode, repainting without reconnecting or replacing
the terminal.

Desktop opens a native dialog from the top-bar Appearance control. Terminal and
Workspace mobile menus open the same dialog. At <=640px it becomes a bottom
sheet with two-column previews, safe-area padding, independently scrollable
content, and 44px mode/Done controls. Native radio groups provide keyboard
selection; native modal focus containment, Escape, Done and backdrop dismissal
retain the saved choice and restore focus to the visible opener.

## Validation evidence

- Focused real-store tests: 6/6; existing frontend unit suite: 1,453/1,453 before
  adding those cases. Frontend types, lint and production build passed. The build
  retains the existing large-chunk warning (no new runtime dependency).
- `browser_appearance.py`: passed on owned Vite `:5318` with Python 3.11.16,
  Playwright 1.58.0 and Chromium 1208. All 12 variants, native keyboard/dismissal,
  system/explicit mode, reload, blocked persistence, legacy recovery and cross-tab
  reader updates passed; no unknown API calls or page errors. One document read
  and retained article DOM across palette changes. Phone sizes: 390×844,
  320×568 and 568×320. Evidence: `tasks/theme-browser/run-05/`.
- Negative control: injecting foreground equal to canvas was rejected at 1:1;
  this confirms the contrast gate measures rendered colors rather than merely
  finding expected source strings (`tasks/theme-browser/negative-01/`).
- Parent independently inspected Sand/light, Dusk/dark and portrait/landscape
  phone captures, and recomputed new core text contrast on canvas/chrome/surface/
  raised planes: minimum 4.85:1; white on new accent fills: minimum 5.97:1.
- `terminal-hmr`: 12/12 passed, with real private backend/ttyd/tmux and owned Vite;
  runtime removed. Evidence: `tasks/theme-evidence/claude-hub-verify.0guwzuzq/`.
  Initial attempt hit a Corepack cache miss under private HOME; rerun used the
  already installed pinned pnpm 9.15.9 directly via a task-local PATH shim.
- Whole-repository `all` and `browser-mocks` run separately; final status and
  independent fixed-SHA review are recorded in Task
  `6e8a377f-403d-4738-9108-f24a6ec258f9`. The first `all` attempt reached backend
  suite completion but was terminated at the 500-second foreground deadline
  before reporting, so it is **not** counted as passing. The owned runtime was
  removed; the retry runs with a lifetime longer than the suite. The first
  browser owner refused occupied `:5317`; that unrelated listener was left
  untouched and the retry uses checked-free `:5357`.

No real provider, OAuth/Bot, physical mobile device or Safari acceptance is
claimed. The new browser scenario validates token/reader rendering with empty
mocked Chat/Workspace data, not every existing application's text contrast or
live terminal theme transport. The existing terminal HMR gate verifies the
terminal lifecycle independently. No task server is retained for delivery.
