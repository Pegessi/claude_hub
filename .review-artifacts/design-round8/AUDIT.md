# Round-8 holistic minimalist/elegant design audit (6d3084a)

> Baseline: `6d3084a style(ui): round-6 visual wave 1 — VIS-01/02/03 fixes`
> Chain verified: `ee69173 → c91d918 → 7029a91 → 8ef15aa → 6d3084a`
> Audit date: 2026-07-11
> Method: read-only static reading of source `<template>` + `<style>` blocks and token
> vocabulary at `App.vue:474-696` against all six design-coherence dimensions
> (visual hierarchy / spacing rhythm / information density / chrome redundancy /
> typographic hierarchy / visual-language unity). No live browser, no screenshots,
> no source edits. Severity rubric:
> - **high** — visibly breaks hierarchy or looks broken/unpolished at a glance
> - **med** — clear inconsistency within or across surfaces; a careful eye catches it
> - **low** — refinement / token snap; visible to a trained eye only
> - **subjective/optional** — taste call; flagged but not required
>
> Findings are numbered D8-NN. Every anchor is a `file:line` reference valid at 6d3084a.

---

## Overall judgment

The app is already in strong minimalist shape. Seven prior audit+polish rounds
have converged on a tight, calm visual system:

- A compact token vocabulary covers space (4/8/12/16/24/32), radius
  (5/7/10/pill), weight (400/500/600), font (11/12/13/15/18/24), and
  three elevations (hairline / soft / popover / dialog) — all defined in
  `App.vue:474-696` and consumed consistently across ~80% of the UI.
- Hover language is **flat** across the shell (mode buttons, theme switch,
  layout tiles, TabBar, toolbar buttons, Login CTA): state changes are
  communicated through border-color and background-color only; no lifts.
- Focus rings are uniform: every interactive surface uses the
  `outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px`
  convention introduced in round-3 polish (shell buttons, mode buttons,
  toolbar, card actions, modal items, LayoutSelector, Login CTA, auth-banner).
- Chip/pill language is coherent across summary chips, column-count pills,
  agent-status CLI/kind/paused pills, autonomy/origin/review badges,
  TabBar pane-indicator: `--ch-radius-pill`, `--ch-font-xs`, semibold,
  2px 7px padding is the repeated pattern.
- Modal chrome is unified behind `BaseModal.vue` (dialog shadow,
  border+radius-lg+padding-5), and popovers/dropdowns uniformly use
  `--ch-shadow-popover` + `border` + `--ch-radius-md`.

Remaining issues are concentrated almost entirely inside the
**AgentWorkspaceView board** — where card chrome, horizontal dividers,
and a cluster of off-scale paddings introduce two visual dialects inside
the same panel and break the otherwise-tight spacing rhythm. The rest of
the app (TabBar, shell, LoginView, MobileControls, modals, BaseModal,
EmptyState, NetworkAccessMenu, ASFP) is **clean bill of health** with
only one low-severity missed token (Login h1 weight).

**Severity distribution**: 0 high · 3 med · 5 low · 2 subjective/optional.
All fixes are CSS/token-only, additive <30 lines total if all are accepted;
no redesign, no new tokens, no new dependencies required.

---

## Clean bill of health (no findings)

These surfaces were read end-to-end against all six dimensions and found to be
elegant, consistent, and appropriately minimal. Listed so future audits don't
re-sweep them.

| Surface | Why clean |
|---|---|
| **App shell mode-bar** (`App.vue:818-928`) | Segmented control + theme-pill use the sunken/control/raised idiom consistently; focus rings uniform; no gratuitous shadow (only `--ch-shadow-hairline` on the theme thumb, which is an elevation affordance for a draggable-looking element). Tight 3px inner padding is idiomatic for segmented controls. |
| **Mode/content fade transition** (`App.vue:956-992`) | Reduced-motion guard is correctly global; enter-only opacity prevents two-flex-child stacking. Clean. |
| **TabBar** (`components/TabBar.vue`) | All borders/radii/shadows/paddings consume tokens; popover menus use `--ch-shadow-popover`; tab hover is flat (border-color only); active tab uses a tight hairline shadow (`0 1px 3px`) that reads as selected-state, not a lift; chip/pill language consistent with workspace board. |
| **BaseModal primitive** (`components/BaseModal.vue`) | Uses `--ch-shadow-dialog` (correct modal elevation), single border, `--ch-radius-lg`, `--ch-space-5` padding, fade+scale entrance; all child modals (EnvPresetManager, workspace new/edit, agent modals in AWV) inherit this. Clean. |
| **EmptyState primitive** (`components/EmptyState.vue`) | Reused across no-tab and board empty cases; token-consistent; no decoration. |
| **EnvPresetManager modal** (`components/EnvPresetManager.vue`) | Hangs off BaseModal (dialog chrome is consistent); sidebar+editor uses same surface/border/radius vocabulary as the rest of the app; focus rings uniform; off-scale values (2px 6px badge, 6px 0 0 0 form-hint margin) are isolated and defensible for dense form chrome. |
| **NetworkAccessMenu** (`components/NetworkAccessMenu.vue`) | Fully tokenized (from round-3 minimalist pass); same popover pattern as TabBar menus. |
| **AgentStatusFloatingPanel** (`components/AgentStatusFloatingPanel.vue`) | Tokenized in round-3 (spacing/weights/density polish); uses hairline divider and soft shadow consistently. |
| **MobileControls** (`components/MobileControls.vue`) | Tokenized in round-3; pressed state correctly uses `inset box-shadow` (push-button affordance, which is a different-but-appropriate idiom for a directional D-pad); sheet uses `--ch-shadow-soft` popover; focus rings uniform. D-pad 56px/42px heights are functional (touch targets) and not a rhythm problem at mobile breakpoints. |
| **LoginView card** (`views/LoginView.vue`) | Single border, dialog shadow, generous intentional hero padding (48px, correctly commented as above-scale since the space scale tops out at 32px), flat CTA hover matching the rest of the app. Only finding: **D8-06** (h1 weight), otherwise clean. |
| **Board chip language** (`.summary-chip`, `.summary-chip-button`, `.column-tab-chip`, `.column-count`, `.agent-status-pill`, `.agent-status-cli`, `.agent-status-kind`, agent paused/master/origin/autonomy/review badges, `.status-dot`) | All pill-radius + xs 11px + 2px-7px padding + muted → accent fill language. Coherent. |
| **Focus rings** (global) | Every :focus-visible rule uses the same 2px accent-ring-strong with 2px offset. No missing surfaces found on the high-visibility controls. |

---

## Findings

### D8-01 — Task-card hover "lifts" while every other card/button is flat
- **Surface**: AgentWorkspaceView board, task cards
- **Dimension**: visual-language unity, chrome redundancy
- **Severity**: **med**
- **Anchor**: `frontend/src/components/AgentWorkspaceView.vue:7714-7725`
- **Current state**:
  ```css
  .task-card:hover {
    border-color: var(--ch-color-border-hover);
    background: var(--ch-color-surface-raised);
    box-shadow: 0 8px 20px var(--ch-shadow-color-soft);  /* ad-hoc float shadow */
    transform: translateY(-1px);                          /* physical lift */
  }
  .task-card.selected {
    border-color: var(--ch-color-accent);
    background: var(--ch-color-surface-selected);
    box-shadow: 0 0 0 1px var(--ch-color-accent-ring), 0 8px 20px var(--ch-shadow-color-soft);
  }
  ```
  This is the **only** hover in the entire app that physically raises an element
  off the plane. Every other hover in the product is deliberately flat —
  background-color and/or border-color changes only:
  - `.agent-status-card:hover` (border-color + background only, no shadow, no translate)
  - `.tab:hover` (border-color only)
  - `.tool-button:hover`, `.primary-button:hover`, `.danger-button:hover` (border-color only)
  - `.layout-btn:hover`, `.layout-menu-item:hover` (bg+border-color only)
  - `.mode-button:hover/.active` (bg+color only)
  - `.feishu-login-btn:hover` is explicitly documented "flat" at `LoginView.vue:64-66`
  - `.workspace-mobile-menu-item:hover`, `.summary-chip-button:hover`, etc. — all flat
  The `0 8px 20px` shadow is also an orphan magnitude — it sits between
  `--ch-shadow-soft` (0 4px/0 12px popover) and `--ch-shadow-dialog` (24px 80px/70px),
  not represented in the shadow scale.
- **Minimal fix**: drop the lift and the ad-hoc shadow; use the same flat-hover
  language as peers:
  ```css
  .task-card:hover {
    border-color: var(--ch-color-border-hover);
    background: var(--ch-color-surface-raised);
  }
  .task-card.selected {
    border-color: var(--ch-color-accent);
    background: var(--ch-color-surface-selected);
    box-shadow: 0 0 0 1px var(--ch-color-accent-ring);  /* keep the accent-ring */
  }
  ```
- **Why more minimal**: a single flat interaction language (state change =
  border/bg only, no elevation change on hover) is a defining trait of calm,
  minimalist UIs. Hover lifts are a Material/maximalist idiom; they pull focus
  on mousemove and clash with the rest of the app's "quiet" feel. The
  `:selected` 1px accent ring is already a sufficient selection affordance
  (matching `.mode-button.active` border-color treatment).

---

### D8-02 — Agent-status cards and task cards disagree on resting chrome (hairline shadow vs. none)
- **Surface**: AgentWorkspaceView, two peer "card" types in the same panel
- **Dimension**: visual-language unity, chrome redundancy
- **Severity**: **med**
- **Anchor**: `frontend/src/components/AgentWorkspaceView.vue:6681-6695` (agent-status-card)
  vs. `:7692-7704` (task-card)
- **Current state**:
  ```css
  .agent-status-card {
    border: 1px solid var(--ch-color-border-muted);
    border-radius: var(--ch-radius-lg);
    background: var(--ch-color-surface);
    padding: 10px 11px;
    box-shadow: 0 1px 0 var(--ch-shadow-color-soft);  /* bottom hairline */
  }
  .task-card {
    border: 1px solid var(--ch-color-border-muted);
    border-radius: var(--ch-radius-md);   /* note: md=7 vs lg=10, see D8-02b */
    background: var(--ch-color-surface);
    padding: 9px 10px 9px 12px;
    /* no box-shadow */
  }
  ```
  Both are surface-raised cards with the same border-color/background, sitting
  in the same workspace view, but:
  1. Agent-status cards carry a `0 1px 0` bottom hairline shadow stacked onto a
     `border-bottom: 1px solid border-muted` — effectively double-stroking the
     bottom edge (one dark 1px border + one soft 1px shadow just below it).
     That's chrome redundancy: the bottom edge is drawn twice while the other
     three edges are drawn once, giving the card an uneven visual weight.
  2. Task cards at rest have no shadow, then on hover gain a float shadow
     (D8-01). The two card types therefore speak two different resting dialects.
  3. Radius mismatch: agent-status-card `--ch-radius-lg` (10px) vs. task-card
     `--ch-radius-md` (7px). Both are card-level containers, not buttons or
     chips; a radius difference here reads as "designed by two different people"
     rather than a deliberate hierarchy.
- **Minimal fix**: standardize both cards to border-only at rest (drop the
  agent-status hairline), and either (a) unify radius to `--ch-radius-md` for
  both (cards feel tight/quiet; preferred for minimalist), or (b) unify to
  `--ch-radius-lg` for both (cards feel softer/airier). Keep the 3px left
  status stripe (D8-08) decision separate.
  ```css
  .agent-status-card {
    /* ... */
    border-radius: var(--ch-radius-md);
    /* drop the box-shadow: 0 1px 0 … line */
  }
  ```
- **Why more minimal**: cards that sit on the same background should not
  disagree about how they're separated from it. One clean border beats
  "border + a little extra shadow on one side only." Removing the shadow
  subtracts chrome rather than adding it.

---

### D8-03 — Three stacked horizontal dividers between workspace header and the board
- **Surface**: AgentWorkspaceView (top-of-panel bands)
- **Dimension**: chrome redundancy, spacing rhythm
- **Severity**: **med**
- **Anchor**:
  - `.workspace-header` border-bottom — `frontend/src/components/AgentWorkspaceView.vue:6308`
  - `.workspace-summary-strip` border-bottom — `:6479`
  - `.workspace-agent-status` border-bottom — `:6579`
- **Current state**: the workspace view renders four horizontal bands stacked
  vertically before the board:
  1. `.workspace-header` (surface-raised, border-bottom-muted, padding 16px) —
     title, "Manual task queue…" description, New/Workspace-Select/Manage-Env
  2. `.workspace-summary-strip` (canvas, border-bottom-muted, padding 8px 16px+2) —
     summary chips ("0 agents, 0 working") + column filter tabs
  3. `.workspace-agent-status` (canvas, border-bottom-muted, padding 12px 16px+2) —
     "Agents" eyebrow + refresh button + horizontally scrolling agent cards
  4. `.board` (app-bg, no divider below, padding 16px, gap 12px)
  Three `border-bottom: 1px solid border-muted` dividers in ~160px of vertical
  space, with bands swapping between `surface-raised` and `canvas` backgrounds.
  The summary strip and the agent-status band are both "metadata about the
  current board" (counts/tabs vs. agent list) but are split into two canvas
  bands separated by a hairline divider.
- **Minimal fix** (two options, pick one):
  - **Option A (simpler, recommended):** remove the `border-bottom` between
    `.workspace-summary-strip` and `.workspace-agent-status` by merging them
    into a single metadata band (one flex container wrapping both left/right
    halves, with a single border-bottom). This collapses three dividers to two.
  - **Option B (more aggressive):** drop the `.workspace-summary-strip`
    entirely — fold its column-tabs into the `.workspace-header .workspace-actions`
    row (they're filter chips that behave similarly to action chips) and move
    the summary counters into `.agent-status-header` (already an eyebrow+toolbar
    row). Three dividers collapse to one (header → agent-status band), with
    board directly under the agent-status band.
  Even the smallest change (Option A — just remove the border-bottom between
  strip and status, and give both bands the same background) removes one
  hairline and calms the vertical rhythm.
- **Why more minimal**: minimalist toolbars collapse metadata into as few
  visual bands as possible. Every additional divider is a horizontal line the
  eye has to cross before reaching the content (the board). Linear/Lagrange-style
  dev tools, Notion, and iA Writer all keep the "secondary metadata" in a single
  band under the primary header.

---

### D8-04 — Cluster of off-scale, often-asymmetric paddings inside the board
- **Surface**: AgentWorkspaceView board (task cards, columns, agent cards,
  agent rows, skeleton)
- **Dimension**: spacing rhythm
- **Severity**: **low** (not broken, but prevents the board from feeling
  "locked to a grid")
- **Anchor** (all `frontend/src/components/AgentWorkspaceView.vue`):
  - `.task-card` padding: `9px 10px 9px 12px` — `:7699` (9/10/12px; asymmetric L/R; none are tokens)
  - `.task-list` padding: `10px` — `:7689` (column-card internal padding;
    peers: column-header padding=12px=space-3, board outer padding=16px=space-4,
    inter-card gap=8px=space-2)
  - `.agent-status-card` padding: `10px 11px` — `:6691` (10/11px; asymmetric 1px; none tokens)
  - `.agent-status-grid` gap: `8px` — `:6673` (equals space-2 but written literal)
  - `.board-skeleton-list` gap: `10px`, padding: `12px` — `:7484-7486`
  - `.board-skeleton-card` padding: `12px` — `:7492`
  - `.agent-row` padding: `9px`, gap: `8px` — `:7377-7380` (agent-manager panel inside a modal, not
    board proper, but same AWV file)
  - `.task-card-description` margin: `4px 0 6px` — `:7807` (4=space-1, 6 is off-scale)
  - `.latest-report` margin: `0 0 6px`, padding: `6px 8px` — `:8008-8012` (6px off-scale)
  - `.agent-badge`/`.session-meta span` gap: `5px` — `:7853` (gap 5px between icon and label in badges)
- **Current state**: these are the residual "feel" literals that didn't get
  snapped in round-3's SL-10 spacing sweep (which only addressed
  `margin-top: 3/4/6px`). The 9/10/11/12px cluster on cards is the
  largest offender — every card interior in the board has its own
  slightly-different padding, and several are asymmetric
  (`9px 10px 9px 12px` has a 2px L/R bias with no functional reason; the
  `::before` status stripe sits on the left but is only 3px wide and sits
  inside the card so it doesn't demand extra left padding).
- **Minimal fix**: snap to the nearest scale token, symmetrically:
  - `.task-card` padding: `var(--ch-space-2) var(--ch-space-3)` (8px 12px,
    symmetric — same as `.agent-status-card` after snapping)
  - `.task-list` padding: `var(--ch-space-3)` (12px, matching column-header padding)
  - `.agent-status-card` padding: `var(--ch-space-2) var(--ch-space-3)` (8px 12px, symmetric)
  - `.agent-status-grid` gap: `var(--ch-space-2)`
  - `.board-skeleton-list` gap/padding: snap to `var(--ch-space-2)` / `var(--ch-space-3)`
  - `.agent-row` padding: `var(--ch-space-2)` (8px)
  - `.latest-report` padding: `var(--ch-space-1) var(--ch-space-2)` (4px 8px);
    margin-bottom: `var(--ch-space-1)` (4px)
  - `.task-card-description` margin: `var(--ch-space-1) 0 var(--ch-space-1)`
  - `.agent-badge` gap: `var(--ch-space-1)` (4px)
  Net effect: the board snaps to an 8/12/16 rhythm and cards feel evenly weighted.
- **Why more minimal**: a coherent spacing scale is the quiet backbone of
  minimalist layouts. When paddings are randomly 1px apart between peer
  containers, the layout feels handmade rather than designed — even if a
  user can't say why. Snapping to the existing 4/8/12/16 scale makes the board
  visually "lock" without any visual redesign.

---

### D8-05 — Agent-status-name uses body-text color instead of strong, flattening hierarchy
- **Surface**: AgentWorkspaceView agent-status-card
- **Dimension**: visual hierarchy, typographic consistency
- **Severity**: **low**
- **Anchor**: `frontend/src/components/AgentWorkspaceView.vue:6772-6780`
- **Current state**:
  ```css
  .agent-status-name {
    color: var(--ch-color-text);         /* body text */
    font-size: 13px;                     /* = --ch-font-md, literal */
    font-weight: var(--ch-weight-semibold);
  }
  ```
  Every other card/section title in the workspace uses `--ch-color-text-strong`
  for the title:
  - `.workspace-header h1` → text-strong (`:6317`)
  - `.column-header h2` → text-strong (`:7589`)
  - `.task-card h3` → text-strong (`:7797`)
  The agent card's name is semantically the card's heading, but it sits at
  body-text color. That makes agent-name and `.agent-status-kind`/meta closer
  in contrast than they should be, flattening the card's internal hierarchy
  (name vs. "claude · idle" meta line). The literal `13px` also wants to be
  `var(--ch-font-md)` (same value, consistent with other card titles).
- **Minimal fix**:
  ```css
  .agent-status-name {
    color: var(--ch-color-text-strong);
    font-size: var(--ch-font-md);
    /* keep semibold + truncation */
  }
  ```
- **Why more minimal**: consistent title color (strong) for card titles means
  the user can predict "where to read the name" across the whole board.

---

### D8-06 — LoginView h1 inherits UA bold (700), missing the semibold cap applied to every other heading
- **Surface**: LoginView
- **Dimension**: typographic consistency
- **Severity**: **low**
- **Anchor**: `frontend/src/views/LoginView.vue:92-96`
- **Current state**:
  ```css
  .login-header h1 {
    margin: 0 0 var(--ch-space-2) 0;
    font-size: var(--ch-font-2xl);
    color: var(--ch-color-text-strong);
    /* font-weight not declared → UA default h1 = 700 (bold) */
  }
  ```
  Round-3 (audit SL-01, committed in `9b0c3192`) capped every `font-weight: 700`
  in App.vue shell and AWV to `--ch-weight-semibold` (600) to enforce a
  three-weight scale (400/500/600) and avoid "shouty" bold headings. LoginView
  h1 was missed: it inherits the user-agent default `font-weight: bold` (700),
  making "Claude Hub" on the login card the single heaviest type in the whole
  product — one step bolder than workspace h1, column h2, task h3, etc.
  (The comment block at `LoginView.vue:62-63` says "No font-weight declarations
  exist in this file; weight tokens are a no-op" — that comment is inaccurate
  because the UA applies a weight to h1 by default; the absence of a declaration
  does not mean the heading renders at regular weight.)
- **Minimal fix**: add `font-weight: var(--ch-weight-semibold);` to
  `.login-header h1` (and update the comment to remove the "weight tokens are
  a no-op" claim, or note the exception).
- **Why more minimal**: a single weight scale reads as intentional. Having one
  hero title pop in at 700 while every other heading uses 600 creates an
  unmotivated emphasis peak at the one screen that should feel calmest.

---

### D8-07 — In-card action buttons have a darker resting background than toolbar buttons
- **Surface**: AgentWorkspaceView task-card action buttons
- **Dimension**: visual-language unity
- **Severity**: **low**
- **Anchor**:
  - Toolbar baseline: `frontend/src/components/AgentWorkspaceView.vue:7125-7129`
    (`.workspace-select, .tool-button, .primary-button, .abort-button, .danger-button`
    → `background: var(--ch-color-surface-control)`)
  - Card-button baseline: `:8120-8123`
    (`.task-actions button` → `background: var(--ch-color-surface-control-active)`,
    `border: 1px solid var(--ch-color-border-strong)`)
- **Current state**: task-card action buttons (Start, Abort, Done, Request review,
  Open tab, more-trigger) use `--ch-color-surface-control-active` as the resting
  background — that's the "pressed" surface step (`--ch-surface-pressed` in some
  systems; in this codebase control < control-hover < control-active < pressed).
  Toolbar buttons (".tool-button" in the header, "+ New", Manage Env, workspace
  select) and agent-status action buttons (`.agent-status-pause`,
  `.agent-status-run-now`) use `--ch-color-surface-control` as the resting
  background. In-card buttons therefore look permanently "pressed" compared to
  toolbar buttons that are ostensibly the same kind of secondary button
  (border+bg, 26-30px tall, radius-sm).
  Also note: `.task-actions button` at `:8120` uses `border-color: border-strong`
  while `.tool-button` at `:7125` also uses `border: 1px solid border-strong`,
  so the border is shared — only the bg differs.
- **Minimal fix**:
  ```css
  .task-actions button {
    background: var(--ch-color-surface-control);  /* was surface-control-active */
  }
  ```
  (The active/pressed state should continue to use the control-active/pressed
  step, which would be achieved via an :active rule — currently there's no
  :active background swap, only `transform: translateY(1px)`. That's a separate
  issue but adding `background: var(--ch-color-surface-pressed)` on :active
  would also be a reasonable minimalist addition.)
- **Why more minimal**: same semantic button = same chrome. If every secondary
  button in the app uses surface-control at rest, in-card buttons should too,
  or they'll look like they're in a different mode.

---

### D8-08 — Task cards use a left-edge status stripe while live columns use a top-edge stripe (two idioms, same meaning)
- **Surface**: AgentWorkspaceView board (cards and columns)
- **Dimension**: visual-language unity
- **Severity**: **low** (subjective; some designers prefer left stripes for cards)
- **Anchor**:
  - Card stripe: `.task-card::before` — `frontend/src/components/AgentWorkspaceView.vue:7706-7712`
    (3px vertical bar on left, with status-color per state: 7727-7753)
  - Column stripe: `.task-column--live-working`, `--live-review` — `:7633-7647`
    (2px horizontal bar on top, with status-color per state)
- **Current state**: status (working/review/queued/done) is announced two ways:
  - On cards: 3px **left** stripe (painted by `::before`), layered over/inside
    the card's 1px `border-left: border-muted` (so total left-edge chrome is
    1px grey + 3px color = 4px, vs. 1px grey on top/right/bottom —
    asymmetric edge weight).
  - On columns: 2px **top** stripe (painted by `border-top-color` on the column),
    which sits as part of the column's 1px four-side border (so top edge is
    2px color, other edges 1px muted).
  Both stripes encode the same concept ("this container is in state X"), but
  they use different axes. Because the column stripe sits at the top, the
  card-internal left stripe doesn't visually "line up" with anything at the
  column level; a board with mixed-status cards shows a patchwork of left-edge
  colors with no columnar echo.
- **Minimal fix** (choose one):
  - **Option A (preferred for unity):** convert cards to top stripes too (match
    columns). Change `::before` to a 2px top stripe:
    ```css
    .task-card::before {
      inset: 0 0 auto 0;
      height: 2px;
      width: auto;
    }
    ```
    Remove the `padding-left` compensation (no L/R padding asymmetry needed —
    D8-04 already proposes symmetric padding).
  - **Option B (keep left stripes):** remove the card's `border-left` so the
    3px color stripe reads as the left edge (not a stripe on top of a border).
    That makes the asymmetric edge weight intentional (status IS the edge), but
    leaves the "left stripe vs top stripe" dialect in place.
- **Why more minimal**: one idiom for "status is encoded on the edge of this
  container" is cleaner than two. The top-stripe treatment reads as a
  highlighter mark across both column and card when aligned.

---

### D8-09 — LayoutSelector's "dense" 5/6/7px dialect (subjective)
- **Surface**: LayoutSelector (terminal-mode layout tile bar)
- **Dimension**: spacing rhythm
- **Severity**: **subjective/optional**
- **Anchor**:
  - `.layout-selector` padding `7px 10px` — `frontend/src/components/LayoutSelector.vue:187`
  - `.layout-buttons` gap `5px` — `:199`
  - `.layout-menu-panel` top `calc(100% + 7px)`, padding `6px` — `:299, 305`
  - `.layout-menu-item` padding `5px 7px` — `:331`
  - `.layout-btn.active` ad-hoc shadow `0 1px 3px …` — `:228`
- **Current state**: these values are explicitly commented as "off-scale …
  density-tuned" and represent a deliberate sub-density for a compact graphical
  toolbar (tiles represent grid layouts so they are visually tight by design).
  They are the largest cluster of honest "I know this is off-scale" literals
  in the codebase. The comments are useful.
- **Minimal fix**: no fix required — these are isolated to one compact
  toolbar, don't bleed into other surfaces, and are commented. Flagging only
  for completeness so future audits know this is intentional. If a future
  design-token iteration adds a "compact" or "density: tight" mode, these
  would be the values to elevate.
- **Why more minimal (if accepted)**: if the team prefers strict scale
  adherence, snapping to 4/8/12 would slightly calm this bar, at the cost of
  making the layout tiles feel looser than their geometry warrants. Recommending
  no action.

---

### D8-10 — `font-size: 14px` orphan on `.task-card-more-trigger` (subjective)
- **Surface**: AgentWorkspaceView task-card "⋯" overflow trigger
- **Dimension**: typographic consistency
- **Severity**: **subjective/optional**
- **Anchor**: `frontend/src/components/AgentWorkspaceView.vue:8164`
- **Current state**: `.task-card-more-trigger` (the "⋯" button in card actions)
  uses `font-size: 14px`. The font scale is 11/12/13/15/18/24 (xs/sm/md/lg/xl/2xl)
  — 14px is an orphan between md (13) and lg (15). Other icon-only buttons in
  the same weight class (`.agent-status-refresh`, `.layout-menu-trigger`,
  `.tab-menu-trigger`) inherit their font-size from parent (--ch-font-sm = 12)
  or use 12-13px. Because "⋯" is a glyph, its apparent size is font-specific,
  and 14px is likely a visual tweak to make it match icon weight.
- **Minimal fix**: snap to `var(--ch-font-md)` (13px) or `var(--ch-font-sm)`
  (12px) and verify vertically; or leave it — this is a single-glyph control
  and the off-scale size is invisible to all but typo-sensitive eyes.
- **Why more minimal** (if accepted): removes the only orphan size between 13
  and 15, so no rendered text in the app sits at an unlabeled step.

---

## Suggested implementation order (if accepted)

For a future implementation wave, I'd suggest tackling these in this order,
each being a tiny CSS/token-only PR:

1. **D8-06** (add `font-weight: semibold` to Login h1) — single line, zero risk
2. **D8-01** (drop task-card hover lift/shadow) — two-line removal; instantly calms the board
3. **D8-07** (in-card buttons → surface-control bg) — one-line token swap
4. **D8-05** (agent-status-name → text-strong + font-md token) — two-line fix
5. **D8-02** (agent-status cards lose hairline shadow; radius unified) — remove box-shadow, set both cards to the same radius
6. **D8-04** (snap off-scale board paddings to space tokens) — mechanical token replacement across ~15 lines
7. **D8-03** (collapse summary-strip / agent-status divider) — single CSS line for Option A; more if Option B
8. **D8-08** (align card status-stripe axis with column stripe) — a few lines of `::before` repositioning; design decision needed
9. **D8-09 / D8-10** — leave as-is unless strict scale adherence is desired

Total LOC change if 1–8 are accepted: roughly 25 lines changed, 5 lines removed,
0 new tokens, 0 new dependencies, 0 JS/template changes.

---

## Method note

- No Playwright, no browser screenshots, no dev server (consistent with
  reviewer guidance from round 6 — Playwright is not a frontend dep).
- Evidence is source-level: every finding cites a `file:line` at commit 6d3084a
  that the reviewer can open directly; no pixel claims.
- Token vocabulary was inventoried first (App.vue :root + both theme blocks,
  lines 474–696) so every "inconsistency" claim is cross-referenced against
  the existing vocabulary (space-1..6, radius-sm/md/lg/pill, xs/sm/md/lg/xl/2xl,
  regular/medium/semibold, shadow-hairline/soft/popover/dialog, surface/canvas/
  raised/soft/control hierarchy, accent/success/warning/attention/danger
  semantic colors).
- Findings that would require new tokens, new components, or JS changes were
  rejected out of scope per the minimalist "don't introduce new design
  vocabulary" bar; all suggestions re-use existing tokens.
- Clean surfaces were explicitly enumerated (the table above) so future
  audits don't re-trawl them.
