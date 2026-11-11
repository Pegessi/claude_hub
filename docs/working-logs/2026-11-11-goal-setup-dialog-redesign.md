# 2026-11-11 Goal setup dialog redesign

## Symptom

The "Set a Goal" form looked bolted on next to the rest of the Hub. The shared
overlay rules already gave its backdrop a scrim/blur and the panel a radius and
shadow, but the form itself was one flat block: unscoped `h2`/`p`/`label`/
`button` selectors styling every child, a single flat `button.primary`, and
three loose sentences of guidance stacked under it. The user's words: it feels
like something patched on (`有点像临时糊上去的`).

## What changed

Three files, no props, events, payloads or focus/trap behaviour moved:

- `frontend/src/components/GoalSetupDialog.vue` — the frame, header, fields,
  option card, notes and footer.
- `frontend/src/features/chat/components/ChatGoalSetup.vue` — keeps ownership of
  the composer attachment row (caption, picker trigger, chips), because the
  dialog only frames it and must not restyle the chip preview or remove badge.
- `frontend/src/features/app-shell/styles/overlays.css` — the shared bottom-sheet
  rule now lifts a phone sheet by `--keyboard-height` (the app already computes
  it in `App.vue` from the visual viewport, zoom- and scroll-safe) and bounds the
  generic backdrop dialogs' height by `--visual-viewport-height`; the two native
  `<dialog>` sheets are lifted the same way and bounded too, because a lifted
  full-height sheet otherwise leaves its header and close button above the top
  (the Feishu Bot settings dialog is full of text inputs, so the keyboard state
  is its normal state). The Goal dialog is also out of the duplicate safe-area
  padding rule, because its footer pads itself.

- **Frame**: the overlay uses `--ch-color-overlay`, the panel
  `--ch-radius-overlay` + `--ch-shadow-dialog` with a scrolling body between a
  header and a footer, so the actions stay put while the form scrolls.
- **Header**: an eyebrow (`New goal` / `Goal settings`), the title that names the
  dialog for `aria-labelledby`, and a one-sentence subtitle that says what a Goal
  is or what editing does.
- **Objective**: a real label bound to the textarea (`for`/`id`, so its
  accessible name is just "Objective"), a live `n/4000` counter that turns amber
  near the limit, a `surface-sunken` field with a focus ring, and a hint that
  replaces the old "Describe the outcome and how to verify it." paragraph.
- **Plan-first**: an option *card* rather than a floating checkbox — accent tint
  and a `Review first`/`Off` badge follow the checkbox, and the note keeps both
  the opt-in promise and the new default ("starts working immediately"). The
  badge sits beside the label, not inside it, so the checkbox's accessible name
  stays exactly "Plan first".
- **Notes**: two factual bullets instead of a paragraph block.
- **Footer**: separated actions with the primary in the contrast-safe
  `--ch-color-accent-strong` fill (the repo's `themePalette` rule) and its
  `--ch-color-accent-fill-hover` hover.
- **Phones**: the same form becomes a bottom sheet (`place-items: end`), full
  width, rounded top, safe-area padding and 44px actions; the body owns the
  scroll.
- The attachment picker and the recovery slot stay with their owners; the dialog
  only frames them (`:deep()` for the picker's paragraph/button).

## Cross-review (Argon) fixes

- The attachment row rule became `.goal-dialog-images :deep(.goal-image-picker > button)`:
  a bare `button` selector also matched the chip's preview and remove badge
  (specificity beat the parent's scoped rules) and turned the 18px `×` into a
  32px grey rectangle.
- The phone block no longer duplicates `overlays.css`'s sheet geometry (its
  `:root:root` rules always won, so the duplicated `max-height` was dead code
  and its `padding-bottom` left an empty strip below the footer). The shared
  rule now measures `--visual-viewport-height` so a keyboard cannot cover the
  footer, and the Goal dialog is excluded from the second safe-area pad.
- The field hint is linked with `aria-describedby`, the counter (not the hint)
  is what turns amber near the limit and is now asserted at 3700/4000, and the
  contrast probe composites translucent layers and also measures the tinted
  option card and the notes.
- `.goal-image-picker` is laid out by its owner (`ChatGoalSetup.vue`) as
  caption + trigger + chips instead of stacking at zero gap.

## Verification

- `browser_chat_goal_steer.py` screenshots the dark pass, then closes the dialog,
  flips the theme, reopens it and screenshots the light pass, and measures real
  computed contrast in both (`title`/`label`/`hint`/`note`/`tips` ≥ 4.5 — the 12
  and 13px copy is normal text, so AA applies — alpha-composited) by walking up
  for the effective background — a restyled surface that only works in one
  palette fails the gate. It also pins the attachment chip/remove-badge geometry
  and their owner's layout (caption above, chips on the trigger's line), the
  counter's near-limit state, the field hint's `aria-describedby`, and the phone
  sheet under a shrunk visual viewport (it sets `--visual-viewport-height: 480px`
  and requires the sheet and its actions to end above it, which is what fails
  when the shared rule drops the keyboard lift). `browser_service_restart.py`
  covers the native `<dialog>` family instead: with the keyboard height set it
  requires the restart sheet's header to stay inside the shrunk viewport and its
  bottom to end above the keyboard, which is what fails when the shared rule
  lifts a sheet without bounding it. (`browser_feishu_bot_components.py` mounts
  that dialog through its own harness, which loads no app shell CSS, so the
  `bot-pool` height bound shares the restart sheet's proven rule rather than a
  browser assertion of its own.)
- `browser_chat_goal_mobile.py` (Edit Goal / Save Goal from the phone sheet) and
  `browser_chat_native_goal.py` (the disabled provider-native option) pass.
- Whole frontend unit suite, `vue-tsc`, `eslint`, `black`/`isort`, `docs`, and
  the full mocked browser target (23/23 PASS on this branch, which does not
  carry the separate session-status scenario).

## Known limits

- `themePalette.test.mjs` pinned the dialog's old `button.primary` selector; the
  assertion now names `.goal-dialog-button--primary` and still requires the
  contrast-safe fill role.
- The paragraph wording inside the attachment picker still belongs to
  `ChatGoalSetup.vue`; this change frames it but does not rewrite its copy.
