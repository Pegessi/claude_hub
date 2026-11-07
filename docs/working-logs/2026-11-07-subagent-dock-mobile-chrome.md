# Agents dock: one pill in the pane chrome, on phones too (2026-11-07)

Branch `codex/subagent-dock-mobile`. Follow-up to `feat(chat): replace the
bottom sub-agent list with an Agents dock` (`610bec5`).

## User report

> 感觉这个移动端呀，它这个 subagent 的悬浮窗还是放到右上角，和那几个现在悬浮的
> tag 并列比较好。然后样式也可以和原来的统一一下。

So: on phones the sub-agents control should join the existing top-right floating
pills (session name, Feishu) instead of living above the composer, and it should
look like them.

## What was wrong

- Two controls for one idea. Desktop had `ChatSubagentDockPill` in the pane
  chrome; phones had a separate `ChatSubagentMobileBar` row above the composer
  with its own pill (28px height, accent-ring border, 12px text) that matched
  nothing. The bar pushed the composer down and duplicated the list code path.
- The mobile pill was running-only (`counts.running > 0`), so a finished
  sub-agent had no entry point on a phone — a gap already recorded in the
  previous handoff.

## Changed

- `ChatSubagentDockPill` is rendered by `TerminalPane` for every Chat pane at
  every width. It keeps the chrome pill geometry it already shared with its
  neighbours (`--pane-chrome-control-size` 24px, `--ch-color-surface-raised`,
  hairline `--ch-color-border-muted`, `--ch-shadow-color-soft`, 11px muted
  text), so the row now reads as one cluster of identical pills.
- Only the reveal differs by width. Desktop keeps the gutter rail or the
  anchored popover; a phone opens a bottom sheet, the same native pattern as
  the other phone-sized overlays. `railFits` also requires `!isMobile`
  (a phone can never leave a 230px right gutter).
- New `ChatSubagentSheet.vue` owns that sheet: dialog semantics, safe-area inset,
  grip, Done, Esc, backdrop tap, focus moved to Done on open, Tab trapped inside
  the dialog (counted over rendered buttons only, since the "Earlier" rows are
  `v-show`-hidden while something runs), and the same body scroll lock the other
  phone overlays use (`html, body, #app` are already `overflow: hidden` app-wide,
  so this matches the convention rather than curing a reachable scroll-through).
  It is teleported to `body`, so the pane's `overflow: hidden` cannot clip it,
  and `aria-controls` follows the reveal actually rendered (`revealId`).
- A reveal's own Esc handler only sees keys pressed while focus is inside it;
  tapping static content (sheet header, popover body) moves focus to `<body>`.
  A document-level `keydown` (capture) Escape now closes the reveal wherever
  focus is, for the popover and the sheet alike, following
  `QuotaMenu`/`GoalStatusBar`.
- Dismissal plumbing: an outside-pointer listener runs only while the anchored
  popover is open, the document Escape whenever a reveal is open, and both are
  removed on close/unmount. A breakpoint change dismisses the reveal (restoring
  focus to the pill) instead of morphing one into the other.
- Deleted `ChatSubagentMobileBar.vue` and its `StructuredPane` row;
  `StructuredPane` no longer injects the dock at all.
- The pill now shows history on phones too ("N done" when idle), which gives
  finished sub-agents the entry point they were missing.

## Validation

- `pnpm typecheck`, `pnpm lint:check`, `pnpm test:unit` (1235 pass, 0 fail),
  `pnpm build:bundle` — all clean in the worktree.
- `frontend/tests/subagentJumpCard.test.mjs` asserts the single pill, the
  `!isMobile` popover/rail guards, the sheet's dialog semantics, its focus trap
  and body scroll lock, the document-level Escape and that the old bar component
  is gone.
- `frontend/tests/browser_subagent_jump_card.py` (mocked APIs, own Vite server
  on 5291, Playwright 1.58.0 on Python 3.11.16) now also resizes to 390x844 and
  checks: exactly one sub-agent control exists and the pill sits on the
  session-name pill's row inside `.pane-chrome`; tapping opens the sheet (not the
  popover); a full Tab and Shift+Tab cycle stays inside it and the body lock is
  applied then released; the
  sheet closes via Esc (both with focus inside and after a tap dropped focus to
  `<body>`), Done (focus returns to the pill), a backdrop tap, and a
  breakpoint change (with no popover left behind); history-only reads "2 done"
  and still opens the list. Screenshots and `report.json` in
  `~/claude_hub_workspace/ui-review-shots-sdm/mobile-dock-final3/`.
- Two new gates were seen failing once: with the sheet's `v-if` forced off it
  fails at `Locator expected to be visible` (`ui-review-shots-sdm/gate-must-fail/`),
  and with `@keydown.tab` removed it fails at `Tab must stay inside the sheet`
  (`gate-trap-fails/`). Neither gate is vacuous.

## Risks / not done

- The chrome cluster is still 24px tall on phones; the pill is therefore a
  small touch target, exactly like the session-name and Feishu pills beside it.
  Enlarging hit areas is app-wide chrome work, not part of this change.
- At 390px the chrome row can still overlap the first conversation line (the
  pre-existing "session-name pill covers the first line" issue); one more
  compact pill makes it slightly wider.
- Deferred from the earlier handoff and still open: unified dialog close
  button/header/footer, dialog leave animations, overlay z-index scale.
