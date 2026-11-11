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

## Independent review and closure

The first fixed-SHA review (`e3f1d8e`) found two substantive design issues and one
mobile reachability gap; passing the first browser matrix did not catch them.

1. Filled approval hover consumers used the pale accent intended for text/rings.
   Both Chat approval Submit and Goal plan Approve now use the existing dedicated
   `--ch-color-accent-fill-hover` role. A rendered follow-up also exposed Goal's
   primary selector losing to its generic button selector; the primary rule now
   wins in normal/hover states. Tests distinguish the primary from secondary
   buttons and measure actual foreground/background, not only a token table.
   The full-SPA Goal/approval scenario passed 48 rest/hover samples (minimum
   hovered contrast 4.615:1) in `tasks/theme-browser/review-hover-02/`. Restoring
   the old hover CSS in the browser makes both controls' negative checks fail
   at 3.43:1. The shared helper is included in format and type verification.
2. Removed `viewport-fit=cover`: this feature must not opt the entire app and
   standalone readers into edge-to-edge iPhone layout without an all-edge policy.
   Existing safe-area-aware sheet spacing remains, within the browser's safe
   viewport. Physical notch/Safari acceptance is still not claimed.
3. Aligned the Workspace mobile header with the shell's 768px breakpoint, closing
   the 761–768px band where neither Appearance entry was reachable. The browser
   gate now checks 760, 761 and 768px, not just a conventional phone width.

Added a real terminal palette regression to the existing HMR suite. It changes
Classic → Forest dark → Sand dark → Sand light through the real store and checks
received `terminal-theme` payloads and live xterm options. The iframe, document,
xterm instance and buffer remain identical; no iframe load occurs and seeded
terminal content survives. The full expanded HMR target passed **13/13**, evidence
`tasks/theme-evidence/claude-hub-verify.9hzrqr6h/`.

The complete standard gate on the initial candidate passed **6,556 backend tests
(9 explicit skips)** and **1,459 frontend tests**, plus types, format, lint and
build; all **24 mocked browser scenarios** also passed. Final post-review gates
and the delta review are retained under `tasks/theme-evidence/` and attached to
the same Task, rather than counting the earlier candidate as final evidence.

No real provider, OAuth/Bot, physical mobile device or Safari acceptance is
claimed. The appearance matrix covers token/reader rendering with mocked data;
real rendered Goal/approval controls and the private ttyd terminal are checked
separately. This is not an audit of every existing application's text contrast.
No task server is retained for delivery.


## Visual-feedback revision: studio and game-inspired themes

The user accepted Classic and Contrast, rejected the four tinted alternatives,
then explicitly requested integrating the three neutral studies into this branch
and adding Zenless Zone Zero, Red Dead Redemption and GTA V inspired designs.
The final set for this revision is Classic, Contrast, Mono, Studio, Carbon,
Zenless, Frontier and Los Santos. The existing preference/store/terminal lifecycle
is retained; old unshipped palette IDs fall back to Classic while preserving mode.

**Change boundary:** `palettes.css` owns paired colors; `theme-treatments.css` owns
scoped radii/materials/display headings and small chrome motifs. The normal text
and code fonts stay shared. The eight-card picker uses four desktop columns and
two phone columns with its existing scroll/focus handling. Classic and Contrast
receive no new treatments. Bright fills use the existing `on-accent` token across
all actual accent consumers; user bubbles get a separate `message-user-text`
role. Danger/success foregrounds are intentionally not mechanically changed.

**References:** neutral studies follow the Geist/Linear/Raycast sources above.
Game references are the official [Zenless Zone Zero](https://zenless.hoyoverse.com/),
[Red Dead Redemption 2](https://www.rockstargames.com/reddeadredemption2) and
[GTA V](https://www.rockstargames.com/gta-v) sites. Hex values and CSS motifs are
our own interpretation, not claimed to be official game design tokens. No logos,
artwork, game fonts, audio, animations or full-canvas textures are imported.

**Acceptance:** all 16 variants must preserve core text/code and on-fill contrast,
show their material/font/edge differences, return to an unchanged Classic, keep
retired-ID mode preferences, work at narrow/short mobile and 768px boundaries,
and repaint an existing terminal without reconnecting. Actual rendered primary
controls are checked at rest/hover, rather than accepting CSS token values alone.
Final source SHA, full gates and independent review are recorded in the same Task
`6e8a377f-403d-4738-9108-f24a6ec258f9` and revision evidence. The user explicitly
requested a feature-branch push and retained `theme-preview-ipv6` on port 5358;
this does not authorize a merge to main or a shared backend restart.


Revision evidence before fixed-SHA review:

- Full frontend unit suite: **1,460 passed**; types, lint and bundle passed.
- Appearance scenario: all **16 variants**, both Classic switch-back snapshots,
  retired-ID recovery, keyboard/storage/reader and mobile-boundary contracts pass.
  Parent spotted a preview-only inheritance leak: Contrast's optional message
  primitives inherited the selected root's values. Defaults now resolve locally
  in each scope via `:where(:root, .theme-preview)`. The strengthened gate checks
  **32 foreign-root tiles**, including their painted bubble and paired ink/fill,
  rather than only the selected tile's canvas (`eight-palettes-fidelity`).
- Actual Goal/approval primary controls: **64 rest/hover samples**, minimum hovered
  contrast **4.615:1**; full game Chat/Goal desktop and phone captures inspected
  (`tasks/theme-browser/eight-chat-01/`). No unknown API calls or page errors.
- Full real terminal HMR gate: **13/13 passed**, including Mono dark → Zenless dark
  → Zenless light on the same iframe/document/xterm/buffer, preserving output.
  Evidence: `tasks/theme-game-evidence/claude-hub-verify.4kzjsj9b/`.
- Final complete checks run only after the candidate is frozen. They run serially
  (`all`, then `browser-mocks`) because the existing browser owner reserves a
  fixed refusal port. Do not edit `verify.sh` or source while these checks run.
  Independent review and final source/evidence identities stay in the same Task.


The revision review found two interaction regressions before the requested push.
Generic `.primary-button:active` rules in the TaskCreate dialog and shared
Workspace cards retained dark on-accent ink while replacing a bright fill with a
neutral dark pressed surface. Those rules now keep the accent fill/ink pair at
rest, hover and press. The new material override also removed the active-pane
border/halo; it was deleted so the original semantic selected-pane indicator
survives every theme, rather than trading keyboard ownership clarity for styling.

Real pointer-down coverage now samples the Goal/approval buttons in all three
states, plus the enabled Workspace header and valid TaskCreate submit controls;
release moves outside the action so the probe cannot submit. The Workspace
harness loads the actual production CSS and proves bright fills/dark ink before
sampling, rather than validating its old fallback colors. A real two-Chat split
view checks 32 ordinary pane handoffs across all 16 variants and contrast of the
selected border. Browser-local negative controls and exact final results are
retained in the revision evidence/Task.

The source treatment selectors were also flattened for the development preview;
the existing Vite production build already flattened them. Base per-tile font,
radius and shadow resets were verified, so no redundant reset was introduced.
The pre-fix aggregate check was intentionally cancelled after review; its retained
runtime identified a child that was confirmed exited. Only final frozen-source
checks count toward push acceptance. No shared service or user preview was stopped.


Focused closure evidence: `workspace-press-01` passed 64 real rest/pressed samples
across Add Task and Create Task, while `goal-approval-press-01` passed 96 real
rest/hover/pressed samples across Goal approve and approval Submit. Minimum
pressed contrast is 4.615:1. `split-handoff-01` passed 32 ordinary two-Chat pane
handoffs, minimum selected-border contrast 3.763:1. Parent reconciled the reports
and lifecycle closure; the Workspace/Goal scenarios completed in about 30/36s.
Browser-local negatives restore the reviewed defects and fail at 1.9719:1 on
Mono's pressed Create Task or equal active/inactive gray pane borders. Source
was not edited for negatives. All are in `tasks/theme-browser/`; the user preview
on 5358 remains running by explicit request, while test Vite5318 is stopped.

The frozen `9d056b3` passed strict `all` (6556 backend tests, 9 skips; 1460
frontend tests) and 13 terminal-HMR cases. Its browser owner passed 18 scenarios,
then stopped Appearance at the normal 120s deadline, leaving five scenarios
unrun. Earlier focused Appearance passes had allowed 180s and actually exceeded
138s, so they did not establish the CI-sized budget. That failed gate remains
in `tasks/theme-game-evidence/final-9d056b3/` and is not relabeled as a full pass.

The test now returns input to the main page after opening its separate reader,
using `bring_to_front()` while keeping the reader open and every assertion.
Small monotonic phase records make the scenario's cost inspectable. Under a
real 120s outer deadline, `foreground-budget-01` completes in 16.014s (16.291s
outer wall), retaining all 16 variants, 32 foreign-root preview tile checks,
reader DOM/one-read continuity and all phone/Workspace widths. Both visibility
observations say `visible` in headless Chromium; the evidence supports restoring
the interaction target, not a claim that `document.hidden` caused the delay.
No product code, coverage, runner/CI limits or animation behavior was changed.

On `cc74766`, strict `all` passed again and the browser owner passed 23/24,
including Appearance. The unchanged image-quality scenario instead timed out
waiting for the authored HTML external-link popup; an unchanged focused run
reproduced it. Two task-local diagnostic runs preserved the native click and
opened the link successfully, recording a shared native window and the retained
iframe's header-height/scroll transition. They did not prove the original gate
fixed or capture a failing event target; no sandbox/security change was justified.

The test now explicitly scrolls the external link into view and asserts viewport
visibility after source/preview restoration, before the original single native
click and popup expectation. It adds no sleep, retry or timeout relaxation.
`popup-readiness-01` runs the original uninstrumented scenario under 120s and
passes in 42.447s, including one external navigation, noopener, retained frame
DOM/scroll, sanitization and mobile checks, with complete cleanup. This establishes
the visible input target, not statistically proven elimination of every Chromium
hit-testing fluctuation. Final frozen gates run browser-first, then `all` and
terminal-HMR serially, to fail fast at the affected boundary.
