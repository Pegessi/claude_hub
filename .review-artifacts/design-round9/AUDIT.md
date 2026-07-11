# Round 9 — Minimalist Design Audit

- **Baseline:** `5c869c4` (tip of `style/ui-wave3-d8-08`, contains D8-01…D8-08 fixes)
- **Branch:** `docs/design-round9-audit`
- **Scope (read-only):** `TerminalGridView.vue`, `TabBar.vue` (inline modals, menus, toasts), `EnvPresetManager.vue` (inline modal), `LayoutSelector.vue` (beyond the D8-09 documented dense dialect), cross-view typography / spacing-token consistency.
- **Hard exclusions honoured:** no D8-01…D8-10 re-report (D8-09's `5/6/7px` LayoutSelector dialect intentionally untouched); no deep audit of `AgentWorkspaceView.vue` or `LoginView.vue`; no JS / behavior / template / dependency changes; no fixes implemented in this commit.
- **Scope note (reviewer follow-up):** two surfaces the reviewer asked about explicitly:
  - *"Workspace select"* lives in `AgentWorkspaceView.vue`, which is under the AWV deep-audit exclusion (round 8 already covered AWV in depth as D8-01…D8-08, and round 9's mandate was surfaces round 8 under-covered).
  - *"Agent config"* = `AgentConfigFields.vue`, rendered inside the TabBar-owned Create-Tab modal (`TabBar.vue:292-299`) and inside AWV. ACF is lazy-loaded into the shared `agent-config` chunk (TabBar L669-680) and its chrome inherits the surrounding modal's `.form-group`/`input`/`.select-input` styles — I audited those inherited form styles as part of the TabBar modal coverage (R9-01 modal chrome, R9-02 button scale cover the visible surfaces ACF sits inside). Component-internal styling of ACF (field labels, model selector, solo-mode toggle) is not covered by this round; flag it for round 10 if ACF grows bespoke chrome rather than inheriting form tokens.
- **Method:** each anchor was spot-grepped against `5c869c4` before being recorded; cross-view diff done by diffing duplicated class blocks (`.modal`, `.modal-overlay`, `.btn`, `.btn-small`, `.form-group label`, `.form-hint`) across the in-scope SFCs and against the global token scale in `App.vue :root` (`--ch-space-1..6`, `--ch-radius-sm/md/lg`, `--ch-weight-regular/medium/semibold`, `--ch-font-xs/sm/md/lg/xl/2xl`, `--ch-motion-fast/standard/panel/drawer/ease`).

---

## Overall judgment

The shell is now largely on-token after rounds 6–8. This round finds **one cluster of medium-severity cross-file dialect divergence** (the duplicated modal chrome and `.btn` scale in `TabBar.vue` vs `EnvPresetManager.vue`) plus a handful of low-severity tokenization gaps and a subjective call around press-lift on buttons (post-D8-01 flatness). No high-severity issues.

The majority of `TabBar.vue` is well-documented — the 65-line block comment at L1358–1393 explains which px values are retained as intentional functional constants (toolbar icon sizes, indicator dots, chip strokes, functional widths, glyph offsets). Those are accepted as a deliberate dense-toolbar dialect, parallel to D8-09 on `LayoutSelector.vue`.

`TerminalGridView.vue` is a 75-line SFC with a single off-token transition; otherwise clean.

---

## Severity × dimension summary

| Severity | Spacing | Chrome | Hierarchy | Motion | Typography |
|---|---|---|---|---|---|
| High | 0 | 0 | 0 | 0 | 0 |
| Med | 0 | R9-01 | R9-02 | 0 | 0 |
| Low | R9-03, R9-04, R9-09 | R9-07 | 0 | R9-05, R9-06, R9-08 | 0 |
| Subjective | 0 | R9-10 | 0 | R9-05 | 0 |

---

## Findings

### R9-01 — Two divergent `.modal` + `.modal-overlay` implementations (med / chrome)

- **Surface:** `EnvPresetManager.vue` vs `TabBar.vue` (cross-file)
- **Anchor:**
  - `EnvPresetManager.vue:281-309` — `.modal-overlay` uses `--ch-color-overlay`, `.modal` uses `border-radius: var(--ch-radius-lg)` (10px), `padding: var(--ch-space-5)` (24px), `box-shadow: var(--ch-shadow-dialog)`, `overflow: hidden`.
  - `TabBar.vue:1922-1957` — `.modal-overlay` uses `--ch-color-overlay-soft`, `.modal` uses `border-radius: var(--ch-radius-md)` (7px), `padding: var(--ch-space-6)` (32px), no `overflow: hidden` (uses `overflow-y: auto` directly).
  - `TabBar.vue:1939-1941` further sets a distinct `z-index: 1100` for `.file-browser-overlay` vs the base overlay's `z-index: 1000`; EPM overlay uses `z-index: 1100`.
- **Current state:** every modal looks subtly different depending on which SFC declares it. The EPM "Manage Presets" modal has a higher-contrast dimmer, larger corner radius, tighter interior padding, and a heavier shadow than the four TabBar-owned modals (Create Tab / Close Confirm / File Browser / Switch Env). Overlay z-index also disagrees (1000 vs 1100) — EPM is always on top by 100 because it hard-codes 1100 while the base overlay is 1000.
- **Minimal fix:** pick one canonical modal chrome. Recommendation (lowest-energy direction): converge **on the TabBar values** since four of the five modal surfaces already use them and EPM is the outlier.
  1. In `EnvPresetManager.vue`, set `.modal-overlay` background to `var(--ch-color-overlay-soft)`, z-index to `1000` (TabBar's `.file-browser-overlay` can remain at 1100 as a stacking-context override for the nested modal case — which is exactly why EPM is opened on top of Switch Env today).
  2. Set `.modal` to `border-radius: var(--ch-radius-md)`, `padding: var(--ch-space-6)`, drop the per-file z-index/overflow drift.
  3. (Optional, nicer): extract the base `.modal-overlay` / `.modal` rules into `App.vue` as unscoped global classes so the dialect cannot silently re-diverge. Keep per-modal sizing overrides (`.switch-env-modal`, `.file-browser-modal`, `.env-manage-modal`) scoped.
- **Why more minimal:** user learns exactly one modal weight; one z-index ladder; one corner radius; one interior rhythm. Removes "which SFC wrote this modal?" as a visual variable.

---

### R9-02 — Divergent `.btn` / `.btn-small` scale between TabBar and EPM (med / hierarchy)

- **Surface:** `EnvPresetManager.vue` vs `TabBar.vue` (cross-file)
- **Anchor:**
  - `TabBar.vue:2356-2387` — `.btn { padding: var(--ch-space-3) var(--ch-space-5); font-weight: var(--ch-weight-medium); line-height: var(--ch-leading-tight); display: inline-flex; align-items: center; justify-content: center; gap: var(--ch-space-2); transition: … }`; `.btn-small { padding: var(--ch-space-2) var(--ch-space-3); font-size: var(--ch-font-sm); }`. Effective size: 40 px tall (13 + 12×2 cap).
  - `EnvPresetManager.vue:543-560` — `.btn { padding: var(--ch-space-2) var(--ch-space-4); font-size: var(--ch-font-md); transition: background-color 0.2s; }` (no weight, no flex, no gap, no line-height); `.btn-small { padding: var(--ch-space-1) var(--ch-space-2); font-size: var(--ch-font-sm); }`. Effective size: 29 px tall (13 + 8×2 cap).
- **Current state:** primary / secondary / danger buttons sharing the same class names (`.btn .btn-primary .btn-secondary .btn-danger .btn-small`) are ~11 px shorter in EPM than in the four TabBar modals. EPM's `.btn` also has no `display: inline-flex`, which means the `.btn-icon` "+" glyph in `.env-new-btn` (the "New" preset button at the sidebar header, `EnvPresetManager.vue:24-32`) is not centered with its label — visible as a slight vertical misalignment. Buttons across the app should read as one family.
- **Minimal fix:** adopt TabBar's larger (12×24 pad) `.btn` block as canonical (used by 4 modals vs EPM's 1). In `EnvPresetManager.vue`:
  1. Replace the `.btn` rule with TabBar's `.btn` rule verbatim (padding 12×24, weight medium, inline-flex + gap + center alignment, line-height tight).
  2. Replace `.btn-small` padding with `var(--ch-space-2) var(--ch-space-3)` to match TabBar.
  3. `.btn:disabled`, `.btn-secondary`, `.btn-primary`, `.btn-danger` color blocks are already the same tokens in both files — they can stay as-is (or be deduped by the global extraction in R9-01 step 3).
- **Why more minimal:** a single button scale across modal surfaces; the Save / Cancel / Delete buttons in EPM stop feeling like a shrunken legacy variant. Collaterally fixes the "+" icon alignment in the "New" preset button.

---

### R9-03 — Off-token 6 px label / hint rhythm in EPM forms (low / spacing)

- **Surface:** `EnvPresetManager.vue`
- **Anchor:**
  - L433 `.form-group label { margin-bottom: 6px; … }`
  - L464 `.form-hint-inline { … margin-left: 6px; }`
  - L470 `.form-hint { … margin: 6px 0 0 0; }`
- **Current state:** three separate 6 px gaps (between label and control, between label text and inline "(new preset)" hint, between control and block hint). 6 px sits between `--ch-space-1` (4) and `--ch-space-2` (8), i.e. it is an off-scale half-step. TabBar's parallel form conventions are on-scale: `.form-group label { margin-bottom: var(--ch-space-2) }` at L2132 and `.form-hint { margin: var(--ch-space-2) 0 0 }` at L2168.
- **Minimal fix:** snap all three to `var(--ch-space-2)` (8 px), matching TabBar's form rhythm:
  - `.form-group label { margin-bottom: var(--ch-space-2); }`
  - `.form-hint-inline { margin-left: var(--ch-space-2); }`
  - `.form-hint { margin: var(--ch-space-2) 0 0 0; }`
- **Why more minimal:** puts form rhythm on the 8/12/16 scale across both modal families and removes the orphan 6 px half-step. Label-to-control distance will grow 2 px, which is the cheapest possible change that re-establishes the grid.

---

### R9-04 — Off-token `2px 6px` chip padding on `.env-preset-item-badge` (low / spacing)

- **Surface:** `EnvPresetManager.vue`
- **Anchor:** L412-413 `.env-preset-item-badge { … padding: 2px 6px; … }`
- **Current state:** the "built-in" chip uses 2 px vertical (half `--ch-space-1`, an optical constant — fine to keep literal) and 6 px horizontal (off-scale, between 4 and 8). Other chips in the shell are on-scale:
  - `TabBar.vue:1506` `.mobile-app-menu-item--mode strong` (the "Current" chip) uses `padding: 2px var(--ch-space-2)` (2 × 8).
  - `TabBar.vue:1673` `.pane-indicator` uses `padding: 1px var(--ch-space-1)` (1 × 4 — an even smaller numeric badge).
- **Minimal fix:** snap to the mobile-mode-chip convention since this is a word chip ("built-in") not a single-digit badge:
  - `padding: 2px var(--ch-space-2);` (vertical 2 px stays literal; horizontal 8 px on-scale).
- **Why more minimal:** chip horizontal padding lands on-scale; "built-in" aligns with the existing word-chip pattern ("Current") rather than inventing a third chip density.

---

### R9-05 — Press-lift pattern persists on buttons after D8-01 flattened cards (low / motion — subjective Option A)

- **Surface:** `LayoutSelector.vue`, `TabBar.vue`
- **Anchor:**
  - `LayoutSelector.vue:222` `.layout-btn:active { transform: translateY(1px); }`
  - `LayoutSelector.vue:288` `.layout-menu-trigger:active { transform: translateY(1px); }`
  - `LayoutSelector.vue:346` `.layout-menu-item:active { transform: translateY(1px); }`
  - `LayoutSelector.vue:424` `.logout-btn:active:not(:disabled) { transform: translateY(1px); }`
  - `LayoutSelector.vue:213` transition lists `transform var(--ch-motion-fast)` and `box-shadow var(--ch-motion-fast)` for `.layout-btn`, and L278 / L414 do the same for `.layout-menu-trigger` / `.logout-btn`.
  - `TabBar.vue:2013-2015` `.path-nav-btn:active:not(:disabled) { transform: scale(0.94); }`
  - `TabBar.vue:2372` `.btn:hover:not(:disabled) { transform: translateY(-0.5px); }`
  - `TabBar.vue:2376` `.btn:active:not(:disabled) { transform: translateY(1px); }`
  - `TabBar.vue:2368` `.btn` transition carries a raw `transform 80ms ease` off-token duration.
  - `TabBar.vue:1594` `.tab.active { box-shadow: 0 1px 3px var(--ch-shadow-color-soft) }` and `LayoutSelector.vue:228` `.layout-btn.active { box-shadow: 0 1px 3px var(--ch-shadow-color-soft) }` — an embossed active tile.
- **Current state:** D8-01 removed hover lift / float shadow from cards (`.task-card`). Buttons across toolbars and modals still carry a physical-press metaphor: tiles depress 1 px on active, modal action buttons lift 0.5 px on hover and depress 1 px on press, path-nav scale-presses to 0.94, active tiles carry a soft drop shadow. The TabBar `.btn` transition also contains the only remaining raw millisecond duration (`80ms`) in the in-scope surfaces.
- **Minimal fix (Option A — minimalist-consistent, recommended):**
  1. Remove `translateY` / `scale(0.94)` from every `:hover` / `:active` rule above.
  2. Remove `transform` from every `transition` declaration listed (keep background / border-color / color / opacity).
  3. Replace the raw `80ms` — it will disappear with the transform transition.
  4. (Optional, goes one step further): replace the two `0 1px 3px var(--ch-shadow-color-soft)` active-tile shadows with a flat ring, e.g. on `.layout-btn.active` / `.tab.active`, match the `.task-card.selected` treatment introduced by D8-01 wave-1 (`box-shadow: 0 0 0 1px var(--ch-color-accent-ring)`); this would make the selected tile state a single ring rather than a soft emboss.
- **Minimal fix (Option B — conservative, respects existing muscle memory):** keep press-lift only on primary / destructive modal action buttons (`.btn-primary`, `.btn-danger`), drop it from all toolbar / menu / tile buttons (`.layout-btn`, `.layout-menu-trigger`, `.layout-menu-item`, `.logout-btn`, `.path-nav-btn`, `.tab`, `.add-tab`). Trim the `80ms` to `var(--ch-motion-fast)` regardless.
- **Why more minimal:** extends D8-01's "flat on hover / selected" rule from cards to the full toolbar+button system; removes the last physical-button metaphor; eliminates the raw `80ms` orphan. Severity kept at **low/subjective** because buttons have a longer historical expectation of press feedback than cards do, and Option B is a reasonable compromise.

---

### R9-06 — Raw `180ms cubic-bezier(…)` transition in `TerminalGridView.vue` (low / motion)

- **Surface:** `TerminalGridView.vue`
- **Anchor:** L38 `transition: padding 180ms cubic-bezier(0.2, 0, 0, 1), gap 180ms cubic-bezier(0.2, 0, 0, 1);`
- **Current state:** duration is `--ch-motion-drawer` (180 ms) and easing is `--ch-motion-ease`, but both are spelled as literals. `TabBar.vue:1404` already uses `var(--ch-motion-drawer)` for the same (padding / gap) transition in `.tab-bar`.
- **Minimal fix:**
  ```css
  transition: padding var(--ch-motion-drawer), gap var(--ch-motion-drawer);
  ```
  (`--ch-motion-drawer: 180ms var(--ch-motion-ease)` is defined at `App.vue:543`, so the easing is bundled — no need to repeat it.)
- **Why more minimal:** motion tokens centralize easing/duration tuning; this is the only raw motion literal left in the in-scope surfaces after R9-05's cleanup.

---

### R9-07 — Triple-stroke divider stack in EPM sidebar (low / chrome)

- **Surface:** `EnvPresetManager.vue`
- **Anchor:**
  - L339 `.env-manage-sidebar { border: 1px solid var(--ch-color-border); … }` (outer)
  - L355 `.env-manage-sidebar-header { … border-bottom: 1px solid var(--ch-color-border); … }` (header)
  - L383 `.env-preset-item { … border-bottom: 1px solid var(--ch-color-border-muted); … }` with L387-389 `:last-child { border-bottom: none }` (between-item separators)
- **Current state:** the 220 px sidebar contains three simultaneous divider strokes: outer border, a same-weight border under the "Presets" header (which is already distinguished by a `surface-sunken` background change), and muted hairline separators between every preset item. Other menu panels in the app are flatter:
  - `LayoutSelector.vue:297-312` `.layout-menu-panel` uses a single outer `border-strong`, no header divider, no per-item divider — items are separated by hover / active background only.
  - `TabBar.vue:1449-1466` `.mobile-app-menu-panel` same pattern (one outer border, no item separators).
  - `TabBar.vue:1742-1754` `.tab-menu-panel` same (outer border only; `gap: 2px` lets panel bg show through as a hairline — see R9-09).
- **Minimal fix (Option A — matches the LayoutSelector / mobile-menu pattern, recommended):**
  1. Delete `.env-manage-sidebar-header { border-bottom: … }` at L355 (header is already distinguished by `background: var(--ch-color-surface-sunken)`).
  2. Delete `.env-preset-item { border-bottom: … }` at L383 and the `:last-child` override at L387-389. Rely on `:hover` background (`--ch-color-surface-control-hover`, L391) and `.active` background (`--ch-color-accent-soft`, L395) to delimit items — exactly like LayoutSelector's menu items.
- **Minimal fix (Option B — keeps item separators, drops only the header stroke):** drop only L355; keep per-item borders. Removes one redundant stroke (header vs outer) but leaves the between-item hairlines.
- **Why more minimal:** the rest of the app's menu panels use exactly one stroke (the panel border); EPM's sidebar is the only panel with three. Option A brings it into alignment with the established convention and removes the need for the `:last-child` override.

---

### R9-08 — Raw `0.2s` btn transition duration in EPM (low / motion)

- **Surface:** `EnvPresetManager.vue`
- **Anchor:** L549 `.btn { … transition: background-color 0.2s; }`
- **Current state:** `0.2s` is ~`--ch-motion-standard` (180 ms) but spelled as a literal; also the transition only animates `background-color` while TabBar's `.btn` animates `background-color` + `transform` + `opacity`. After R9-02 converges `.btn` padding/weight and R9-05 (if Option A is chosen) drops transform, the TabBar transition will reduce to `background-color var(--ch-motion-standard), opacity var(--ch-motion-standard)` and this rule can be replaced wholesale.
- **Minimal fix:** replace `0.2s` with `var(--ch-motion-standard)`:
  ```css
  transition: background-color var(--ch-motion-standard);
  ```
  (If R9-02 copies the TabBar `.btn` block verbatim, this line disappears automatically.)
- **Why more minimal:** on-token duration; when R9-02 lands, this is just a one-line tokenization until the blocks are unified.

---

### R9-09 — Orphan `gap: 2px` gutter in `.tab-menu-panel` (low / spacing)

- **Surface:** `TabBar.vue`
- **Anchor:** L1751 `.tab-menu-panel { … gap: 2px; … }`
- **Current state:** the ⋯ tab-actions popover uses 2 px vertical gutters between menu items (so the panel background shows through as a hairline stripe between "Rename" / "Duplicate" / "Switch Env"). Every other popover/menu panel in the in-scope set uses `gap: 0` — items butt directly against each other and the hover background forms a continuous column:
  - `LayoutSelector.vue:297-312` `.layout-menu-panel` — no gap.
  - `TabBar.vue:1449-1466` `.mobile-app-menu-panel` — no gap.
  - `TabBar.vue:2480-2496` `.toast` stack uses `gap: var(--ch-space-2)` between toasts (8 px, between separate toasts, not items).
  2 px is also off-scale (between 0 and `--ch-space-1` = 4 px) — functionally a hairline but implemented as gap rather than as a divider.
- **Minimal fix:** change `gap: 2px` to `gap: 0` (items butt up, hover rectangles form a continuous column like every other menu). If a hairline separator between items is genuinely wanted, use a 1 px border on the items rather than a 2 px gap, for parity with the rest of the stroke system.
- **Why more minimal:** removes the only 2 px gap in the popover system; tab-menu-panel matches the other two popovers.

---

### R9-10 — `.file-browser-modal` uses `width: 80%` while other modals use token-based max-width (subjective / chrome)

- **Surface:** `TabBar.vue`
- **Anchor:** L1959-1967
  ```css
  .file-browser-modal {
    min-width: 500px;
    width: 80%;
    max-width: 600px;
    height: 70vh;
    max-height: 600px;
    padding: var(--ch-space-4);
    overflow: hidden;
  }
  ```
- **Current state:** create-tab / close-confirm / switch-env modals use the fixed-min pattern `width: min(520px / 480px, 100%)`; EPM uses `width: min(800px, 100%)`. Only the file browser uses `width: 80%`, which produces a viewport-tracked width between the 625 px and 750 px breakpoints (e.g. at 700 px vw it is 560 px wide, a value no other modal will hit). It also overrides padding to `--ch-space-4` (16 px) vs the `.modal` default of `--ch-space-6` (32 px) — a tighter rhythm consistent with it being a dense browser but worth noting as a secondary dialect.
- **Minimal fix:** replace `width: 80%` with the same fixed-min pattern used by the other sized modals:
  ```css
  .file-browser-modal {
    min-width: 480px;
    width: min(600px, 100%);
    max-width: 600px;
    /* height, max-height, padding, overflow unchanged */
  }
  ```
  The tighter `padding: var(--ch-space-4)` is justified by the list density — keep it (it is a deliberate density override, parallel to D8-09).
- **Why more minimal:** predictable modal geometry across all five modal surfaces; the file browser stops widening/narrowing with the viewport between the min and max breakpoints. Severity kept at **subjective** because the current 80 % width is a defensible choice (file pickers often scale with the window), and the min/max already bound it tightly.

---

## Clean bill of health

The following surfaces and patterns were reviewed and found to be on-token / consistent / already documented; no findings filed:

| Surface / Pattern | Verdict |
|---|---|
| `TerminalGridView.vue` base rules (`.terminal-grid`, all `.layout-*` grid templates, `gap`/`padding` tokens) | Clean, one motion-token finding (R9-06) only. |
| `TabBar.vue` `.tab-bar` shell (L1395-1405) — padding, gap, bg, border, transition tokens | Clean; comment block at L1358-1393 documents the retained 30 px toolbar convention. |
| `TabBar.vue` `.tab`, `.tab:hover`, `.tab.dragging`, `.tab.drag-over-*` drag affordances | Clean; 2 px drag-indicator borders are documented stroke constants. |
| `TabBar.vue` `.tab-name`, `.tab-name-input`, `.tab-indicator`, `.pane-indicator` typography | Clean; indicator dot 7 × 7 + 1.5 px ring documented as optical constant. |
| `TabBar.vue` `.tab-menu-trigger`, `.tab-close`, `.add-tab`, `.mobile-app-menu-trigger` | Clean; 24 px hit-box, 30 px toolbar-icon convention documented. |
| `TabBar.vue` `.tab-menu-item`, `.mobile-app-menu-item` typography/padding | Clean; `var(--ch-space-2/3)` padding, font-sm/md, weight-semibold consistent. |
| `TabBar.vue` `.switch-env-*` block (header, icon, callout, callout 3 px accent stripe) | Clean; 36 px icon is a documented glyph size; 3 px stripe matches `.toast::before` stripe. |
| `TabBar.vue` form controls (`.segmented-control`, `.segment-button`, `.select-input`, `.cwd-dropdown-btn`, `.checkbox-*`, `.env-preset-row`) | Clean; padding/gap on-scale; input focus ring `box-shadow: 0 0 0 2px accent-ring` consistent. |
| `TabBar.vue` file-browser list (`.file-browser-list`, `.file-item`, `.file-icon`, `.file-name`, path input) | Clean. |
| `TabBar.vue` toast stack (`.toast`, `.toast::before`, `.toast__icon/message/close/timer`, type variants, mobile media query) | Clean; entrance animation + toast-timer are functional constants, on-token otherwise. |
| `TabBar.vue` `:focus-visible` outline block (L2613-2626) | Clean; unified 2 px `accent-ring-strong` at 2 px offset for every interactive element. |
| `EnvPresetManager.vue` `.modal-overlay` structural rules (`position: fixed; inset: 0; flex; padding: var(--ch-space-4); overscroll-behavior; z-index`) | Padding/structural rules clean; chrome value drift (overlay bg, radius, padding) captured in R9-01. |
| `EnvPresetManager.vue` `.env-manage-*` body/editor layout (`.env-manage-body`, `.env-manage-sidebar`, `.env-manage-editor`, `.form-group`, input/textarea focus rings, empty state, footer) | Spacing on-scale; input/textarea focus-ring treatment matches TabBar. Sidebar border stack captured in R9-07. |
| `EnvPresetManager.vue` `.btn` color variants (`.btn-secondary`, `.btn-primary`, `.btn-danger`, `:hover` / `:disabled`) | Color tokens consistent with TabBar; only `.btn`/`.btn-small` padding/transition drift, captured in R9-02 and R9-08. |
| `EnvPresetManager.vue` mobile `@media (max-width: 720px)` overrides | Clean; sensible column collapse, tighter padding consistent with TabBar mobile overrides. |
| `LayoutSelector.vue` `5/6/7/10/11px` dense dialect | Explicitly excluded by D8-09 (commented "off-scale … density-tuned"); not re-flagged. |
| `LayoutSelector.vue` icon/cell geometry (`.layout-icon`, `.layout-cell`, avatar 24 px circle, fallback glyph) | Clean; 24 px + 50% radius is the standard avatar convention; 2 px cell radius + 4 px min-size documented as density dialect. |
| `LayoutSelector.vue` `@media (max-width: 768px)` variant switch | Clean; mobile menu variant, row variant hidden. |
| Cross-view button color tokens (`.btn-primary`/`.btn-secondary`/`.btn-danger` color map) | Identical tokens (`accent`/`surface-control-hover`/`danger-strong`) across TabBar and EPM; no drift in color semantics. |
| Cross-view form focus ring (`:focus-visible` outline 2 px `accent-ring-strong` at 2 px offset, input `box-shadow: 0 0 0 2px accent-ring`) | Consistent across all in-scope surfaces. |
| Cross-view menu/popover chrome (`border: 1px border-strong/border`, `radius-md`, `shadow-soft/popover`, `surface-glass/raised`) | All on-token; the only chrome drift is modal chrome (R9-01). |

---

## Suggested implementation order

Findings are grouped by whether they touch a shared rule block (coordinate those within a single edit) vs being independent.

**Wave A — single-file, independent, low-risk (one PR):**

1. **R9-06** (TerminalGridView motion tokens) — one-line change in a 75-line SFC, zero design risk.
2. **R9-09** (TabBar `.tab-menu-panel` gap 2px → 0) — one-line change, aligns tab-menu-panel with other two popovers in the same file.
3. **R9-03** (EPM form 6 px → `var(--ch-space-2)`) — three one-line substitutions in EPM form rules; no file-crossing.
4. **R9-04** (EPM `.env-preset-item-badge` horizontal padding 6 px → `var(--ch-space-2)`) — one-line change.
5. **R9-08** (EPM `.btn` transition `0.2s` → `var(--ch-motion-standard)`) — one-line change; becomes a no-op after R9-02 lands so ordering matters (do R9-08 first OR let R9-02 overwrite it).
6. **R9-10** (subjective, TabBar `.file-browser-modal` width 80% → `min(600px,100%)`) — one-line change; keep tighter padding.

**Wave B — coordinated EPM + TabBar convergence (one PR, touches both files):**

7. **R9-01 + R9-02 + R9-07** — these all touch the duplicated modal/button chrome and should land together to avoid a half-converged intermediate state:
   - Converge `.modal-overlay` / `.modal` base chrome on TabBar values (R9-01) — edits EPM.
   - Converge `.btn` / `.btn-small` on TabBar's larger button scale (R9-02) — edits EPM.
   - Flatten EPM sidebar divider stack (R9-07) — edits EPM.
   - Consider the global-class extraction (step 3 of R9-01) as a follow-up once the values are aligned; not required for correctness.

**Wave C — press-lift exorcism across toolbars (one PR, subjective; pick Option A or B first):**

8. **R9-05** — drops translateY/scale transforms and raw 80 ms duration from button/menu/tile active states. Optionally flattens `.tab.active` / `.layout-btn.active` shadows to rings to match `.task-card.selected`. If Option B is chosen, keep lift on `.btn-primary`/`.btn-danger` only. Coordinate across `LayoutSelector.vue` and `TabBar.vue` in a single pass so the feel changes consistently.

Waves A and B are CSS/token-only and carry zero behavioral risk; Wave C is a feel change and should be reviewed as a design-taste question (it extends D8-01's flatness rule to buttons — explicitly called out as a subjective extension rather than a bug fix).
