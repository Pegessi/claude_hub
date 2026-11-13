# Integrate the accepted theme system into current main

## Intent and fixed inputs

The user authorized main integration after accepting the six-theme branch.
The isolated integration checkout is `theme-system-merge`, on
`codex/theme-system-merge`; the shared primary checkout is not a development
workspace. First parent is `c2a97baad057659450a207ef5ae59f188040af29`, and the
accepted theme parent is `fbc1491bb86813905b2a533e8059aa5fe4450bb8`.

Keep Classic, Contrast, Mono, Zenless, Frontier and Los Santos, the existing
preference lifecycle and all six compact/panoramic assets. Keep current main's
Safari viewport/touch-input and attachment behavior, Plan-first default-off,
rebuilt Goal dialog, waiting-question status, quota naming and backend fixes.
This integration does not authorize a shared-service restart or deployment.

## Resolution and semantic integration

- **CHANGELOG:** retain both parents' additions, with the newly integrated theme
  entries followed by main's existing entries. No historical entry is discarded.
- **GoalSetupDialog:** keep main's complete rebuilt component; carry the theme
  change to its new `.goal-dialog-button--primary` selector using
  `--ch-color-on-accent`, not the obsolete broad `button.primary` rule.
- **Goal acceptance:** main added a light/dark dialog check using the removed
  `.theme-switch`. Use the existing real `select_appearance` path, preserving
  the current palette and the main dialog/Plan-first/keyboard assertions.
- **Waiting status:** the game active-tab outline would override main's new
  waiting ring. Retain the active contrast outline and layer the waiting ring
  outside it, only for active game tabs with `data-awaiting=true`. No status,
  label, completed color or geometry is changed. The real Sidebar Status runner
  now compares actual waiting and idle active tabs in all three games and both
  modes; it does not fake their data attributes.
- **Negative-probe cleanup:** injecting the old thick composer border moves
  transcript scrolling. Restore the probe's recorded scroll after its inline
  styles are restored, only in that negative control. Check the same DOM owner,
  unchanged viewport extents and full original CSS/geometry equality. Preserve
  the quiet-surface failure, not just the final restoration. No product scroll
  behavior or acceptance threshold is changed.

## Preserved attempts and evidence

Evidence is under `tasks/theme-main-integration/`.

- `qa-01` failed on the obsolete theme-switch locator (39.265s).
- `qa-02` and diagnostic `qa-03` failed the old-frame restoration comparison.
  The diagnostic shows only 33 vertical-coordinate fields moving by -20px;
  all 13 surface sizes/styles match. Subsequent recorded viewport evidence is
  scrollTop292→312 after style restoration→292 after scroll restoration, with
  scroll extent1190×928 and client extent1190×616 unchanged. This establishes
  the displaced view state, not which browser/application anchoring mechanism
  caused it.
- `qa-04` passed Goal104.672s, Appearance39.420s and Composer39.007s.
- `status-red-01` reproduced identical waiting/idle active rings in Zenless/dark
  before the CSS repair; no state, label or color discrepancy was invented.
- Final scoped `qa-05` passes Goal105.060s, Appearance39.309s,
  Composer38.983s and Sidebar Status11.753s. All original entry points run
  unfiltered with the original120s per-runner deadlines. The last includes the
  six game/mode waiting-versus-idle comparisons. Source snapshots match during
  each run and known owned Vite/browser resources close after each attempt.

Dependencies were freshly installed from unchanged lockfiles in this checkout;
no environment was copied. The official format, frontend lint and type checks
passed before browser iteration; changed Python files use the same explicit
Black/isort configuration as the official gate. Independent integration review,
immutable merge-decision coverage and fixed-final-SHA all/browser/HMR/replay
checks follow these scoped results rather than inheriting old branch passes.

The scoped browser evidence is Chromium with mocked APIs. WebKit's optional
`--iphone-webkit` path and physical iPhone behavior are not certified here;
Safari source/contract preservation is reviewed separately. Existing warning,
performance, lifecycle-inspection and live-provider boundaries remain.
