# Minimalist UI Audit — Prioritized Findings

**Date:** 2026-07-10
**Branch:** `docs/minimalist-ui-audit`
**Scope:** `frontend/src/` against the landed `--ch-*` design-token scale (`App.vue :root`)
**Audit type:** Read-only analysis. No source files were edited in producing this document.

---

## 1. Baseline: The Token System

`frontend/src/App.vue` :root (lines 457–580) defines a tight, minimalist design scale:

| Token tier | Values |
|---|---|
| Spacing | `--ch-space-1..6` = 4 / 8 / 12 / 16 / 24 / 32 px (4px base) |
| Type size | `--ch-font-xs/sm/md/lg/xl` = 11 / 12 / 13 / 15 / 18 px (no display/hero ramp) |
| Leading | `--ch-leading-tight: 1.25` (headings); `--ch-leading-normal: 1.5` (body) |
| Weight | `--ch-weight-regular/medium/semibold` = 400 / 500 / 600 (no bold/700/800 by design) |
| Radius | `--ch-radius-sm/md/lg` = 5 / 7 / 10 px |
| Motion | `--ch-motion-fast: 120ms ease`; `--ch-motion-standard: 180ms ease` |
| Shadow | `--ch-shadow-popover/dialog/soft` (reserved for popovers/modals/elevated menus only) |
| Color | A curated palette of `--ch-color-*` tokens covering canvas/surface/border/text/accent/semantic states |

## 2. Minimalist Principles Audited Against

1. **4px spacing rhythm** — padding/gap/margin snap to the `--ch-space-*` 4px grid.
2. **Three-weight type** — 400/500/600 only; no `bold`/700 shouting.
3. **Three-step radius** — 5/7/10 px; no custom per-component radii.
4. **Shadows as elevators, not decor** — shadows only on popovers/dialogs/menus; flat hairline borders (`1px solid var(--ch-color-border*)`) elsewhere.
5. **Tokens over ad-hoc hex/rgba** — every color is read off the palette; no one-off brand or status hex.
6. **Calm motion** — 120ms for hover/feedback; 180ms for layout; no transform lifts, no glow-pulse.
7. **Focus everywhere** — every interactive element has a visible `:focus-visible` ring in accent color.
8. **Flatness** — gradients, multiple layered backgrounds, and decorative lifts are noise; remove.

## 3. Deferred Files (in-flight tasks)

Per task instructions, the following files are currently owned by other in-flight work and their findings are LISTED HERE BUT NOT RECOMMENDED FOR IMMEDIATE EDITING. Follow-up tasks must wait for those to land:

- **`frontend/src/App.vue`** — owned by a prefetch task.
- **`frontend/src/components/AgentWorkspaceView.vue`** + **`frontend/src/stores/workspaceStore.ts`** + backend — owned by a board-payload task.

## 4. Prioritized Findings

Sorted by impact (high → med → low), then effort (S → M → L). Each finding names one owning component so follow-up tasks can be dispatched file-disjointly.

| # | Finding | Owning file(s) | Evidence (file:line) | Impact | Effort | Recommended change |
|---|---|---|---|---|---|---|
| 1 | **`font-weight: 700` breaks the calm three-weight cap** — three instances in the app-shell chrome (mode-button label, theme-switch thumb, auth-error retry) use 700 where the token scale intentionally caps at `--ch-weight-semibold`=600. | `App.vue` | `App.vue:745, 806, 843` | **high** (DEFERRED) | S | Replace with `var(--ch-weight-semibold)`. Bundle into the App-shell pass when the prefetch task lands. |
| 2 | **Hardcoded brand hex palette on avatars** — Claude `#d97757/#f1eee5`, Codex `#000/#fff`, Cursor grays, terminal green `#7ee787` are ad-hoc hex literals in `AgentAvatar.vue`; no token source of truth. | `components/AgentAvatar.vue` | `AgentAvatar.vue:123-139` | **high** | M | Introduce a small set of `--ch-agent-{claude,codex,cursor,terminal}-{bg,fg}` tokens in `App.vue :root` (or reuse `--ch-terminal-*` where appropriate); use them in AgentAvatar. Master/origin/autonomy badge colors are owned by `AgentWorkspaceView.vue` — see finding #2b. |
| 2b | **Hardcoded brand hex on AWV status pills duplicates AgentAvatar palette** — status chips repeat Claude `#d97757`, Codex `#10a37f` (green, not the `#000` used on the avatar), terminal `#7ee787`, master/origin violet `#c4b5fd`/`rgba(139,92,246,…)`, autonomy teal `#5eead4`/`rgba(20,184,166,…)`. Same brand family as #2 but in a different file. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6658, 6662-6663, 6673, 6922-6923, 7751-7778, 7786-7789` | **high** (DEFERRED) | M | Wire these chips to the same `--ch-agent-*-{bg,fg}` tokens introduced in finding #2. Do when the board-payload task lands. |
| 3 | **Login CTA hover lift + glow shadow violates flat-minimalist aesthetic** — `transform: translateY(-2px)` + `box-shadow: 0 8px 20px <accent>` added decorative elevation contrary to the flat aesthetic used by every other button. **RESOLVED by `ce5b139`** — lift and glow removed; hover is now `background: var(--ch-color-accent-hover)` only (LoginView.vue:118-120). | `views/LoginView.vue` | Originally `views/LoginView.vue:118-122`; fixed in `ce5b139`. | **high** (RESOLVED) | S | Landed. |
| 4 | **Login CTA uses `transition: all 0.2s ease`** — `transition: all` was over-broad and 200ms off the motion scale; no `:focus-visible` ring. **RESOLVED by `ce5b139`** — transition tightened to `background-color var(--ch-motion-standard)` (LoginView.vue:115); `:focus-visible` ring added at :122-125. | `views/LoginView.vue` | Originally `views/LoginView.vue:115, 102-116`; fixed in `ce5b139`. | **high** (RESOLVED) | S | Landed. |
| 5 | **`LoadingButton` spinner uses `700ms linear infinite`** — the only spinner in the codebase; linear easing looks mechanical against the otherwise eased motion system, and the duration is untethered from any token. | `components/LoadingButton.vue` | `LoadingButton.vue:71` | med | S | Leave as-is if mechanical spinner look is intentional; otherwise consider `cubic-bezier(0.4, 0, 0.2, 1)` or accept that a spinner's motion is exempt from the 120/180 token (spinner rate is not a feedback transition). Flagging for awareness. |
| 6 | **AgentAvatar hardcoded radii off the 5/7/10 scale** — default `border-radius: 8px` (between md=7 and lg=10), sm `border-radius: 6px` (between sm=5 and md=7). | `components/AgentAvatar.vue` | `AgentAvatar.vue:92, 104` | med | S | md avatar → `var(--ch-radius-md)` (7px); sm avatar → `var(--ch-radius-sm)` (5px). Roll into Task 1 (AgentAvatar). |
| 7 | **AgentAvatar default bg/fg hardcoded, not on palette** — `color: #fff`, `background: #4b4b4b` duplicates `--ch-color-text-inverse` / `--ch-color-surface-control` semantics. | `components/AgentAvatar.vue` | `AgentAvatar.vue:95-96` | med | S | Replace with `var(--ch-color-text-inverse)` and `var(--ch-color-surface-control)`. Roll into Task 1. |
| 8 | **Cursor/terminal avatars use linear-gradient backgrounds** — `linear-gradient(135deg, #f5f5f5 0%, #d4d4d4 100%)` and `linear-gradient(135deg, #2a2a2a 0%, #1a1a1a 100%)` add decorative gradient noise to 28px glyphs; gradient stops are hardcoded grays not on the palette. | `components/AgentAvatar.vue` | `AgentAvatar.vue:132-139` | med | M | Drop gradients; use flat surface tokens (cursor → `--ch-color-surface-control`, terminal → `--ch-color-surface-sunken`, terminal fg → `--ch-terminal-green`). Roll into Task 1. |
| 9 | **`font-weight: normal` in EnvPresetManager form labels** — explicit `normal` (400) where `var(--ch-weight-regular)` would document intent and match system usage. Low priority since value is the same. | `components/EnvPresetManager.vue` | `EnvPresetManager.vue:454` | low | S | Replace `font-weight: normal` with `var(--ch-weight-regular)`. Pure consistency; no visual change. Roll into Task 3. |
| 10 | **Injected xterm padding `6px` off the 4px rhythm** — JS-injected CSS sets `.xterm { padding: 6px !important }`, between `space-1=4px` and `space-2=8px`. | `components/TerminalView.vue` | `TerminalView.vue:947` | low | S | Snap to `var(--ch-space-2)=8px` (aligns with TerminalGridView gutter) or `var(--ch-space-1)=4px` (tighter canvas-to-chrome ratio). Standalone one-line fix; not part of any suggested task because terminal chrome is sensitive and worth its own careful review. |
| 11 | **Hardcoded hairline shadows in App.vue shell not on palette** — `.theme-switch-thumb` uses `box-shadow: 0 1px 2px rgba(15,23,42,0.08)` (819) and `.auth-error .retry-btn` uses `0 1px 3px rgba(15,23,42,0.16)` (860); unthemed (breaks dark/light parity) and not mapped to any shadow token. | `App.vue` | `App.vue:819, 860` | med (DEFERRED) | S | Remove (rely on `border-color` change) or add a `--ch-shadow-hairline` token. Bundle into App-shell deferred pass. |
| 12 | **`box-shadow: 0 12px 48px rgba(0,0,0,0.45)` hardcoded on AWV lightbox** — close to `--ch-shadow-dialog` (0 24px 80px / 0.45) but a hand-tuned variant. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:9162` | med (DEFERRED) | S | Use `var(--ch-shadow-dialog)`; visually very similar and unifies elevation across overlays. Bundle into AWV deferred pass. |
| 13 | **Card-hover large elevation adds visual noise on the board** — `box-shadow: 0 8px 24px var(--ch-shadow-color-soft)` on task-card hover is a big elevation; minimalist aesthetic prefers `border-color` change only. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6552` | med (DEFERRED) | S | Drop the hover shadow; rely on `border-color: var(--ch-color-border-hover)` and/or a subtle background tint. Bundle into AWV deferred pass. |
| 14 | **MobileControls scattered motion durations off the 120/180ms scale** — bottom-sheet open uses `140ms ease` (344); button press states use `80ms` (389) and `100ms` (470) with hardcoded decimals and include unused `box-shadow`/`transform` transition-properties on plain background swaps. (The sheet-drawer `180ms cubic-bezier` at line 323 is already correct.) | `components/MobileControls.vue` | `MobileControls.vue:344, 389, 470` | med | S | Consolidate onto `var(--ch-motion-fast)` (120ms) for press/feedback and `var(--ch-motion-standard)` (180ms) for the sheet open/close; drop `box-shadow`/`transform` from transition-property where no such animation exists. Task 2. |
| 14b | **App-shell mode-bar uses hand-tuned `200ms cubic-bezier + 160ms ease` transition pair** — quintuple-property `200ms cubic-bezier(0.2,0,0,1)` on max-height/padding/border-color/transform plus `160ms ease` on opacity (783); duplicates the 180ms cubic-bezier entrance used by TerminalPane/TabBar/LayoutSelector. | `App.vue` | `App.vue:783` | med (DEFERRED) | S | Add a `--ch-motion-drawer: 180ms cubic-bezier(0.2, 0, 0, 1)` custom property and replace the hand-tuned pair. Bundle into App-shell deferred pass (this absorbs former finding #24). |
| 14c | **AgentWorkspaceView cards/chips use scattered off-token motion durations** — `120ms` hover / `80ms` press (6871,6891,6937,7978), `150ms` (7130), `100ms` (8073), one `240ms ease` opacity (7396); micro-jank as different items ease at different rates. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6871, 6891, 6937, 7130, 7396, 7978, 8073` | med (DEFERRED) | M | Consolidate onto `var(--ch-motion-fast)`/`--ch-motion-standard`. Bundle into AWV deferred pass. |
| 15 | **AgentWorkspaceView uses `font-size: 10px` for pills/badges** — 1px below the type-scale floor (`--ch-font-xs=11px`); inconsistent with 11px chips elsewhere. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6301,6323,6638,6648,6696,6741,6762,7681,7725,7737,7755,7790` | med (DEFERRED) | S | Lift to `var(--ch-font-xs)`; tighten chip padding slightly if needed to keep footprint. Bundle into AWV deferred pass. |
| 16 | **AgentWorkspaceView `gap: 6px` chip rhythm off-scale** — 5+ sites using `gap: 6px` between chips/meta elements, between the 4px/8px token steps. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6344,6456,6621,6730,6838` | med (DEFERRED) | S | Snap to `var(--ch-space-1)=4px` (tight) or `var(--ch-space-2)=8px` (airy) consistently. Bundle into AWV deferred pass. |
| 17 | **AgentWorkspaceView scattered off-scale radii (2/3/6/8px)** — 2px tight indicators, 3px chips, 6px cards, 8px segmented caps, inconsistent with the 5/7/10 scale. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:7231,8527,8633,9085,9096,9629,10800,10892` | low (DEFERRED) | S | Cards → `var(--ch-radius-md)`=7px; chips/pills → `var(--ch-radius-sm)`=5px; rectangular 2px affordances can remain. Bundle into AWV deferred pass. |
| 18 | **`TerminalPane.vue` `font-weight: 500` left literal with a comment** — matches `--ch-weight-medium=500` exactly but a "nothing speculative" comment left it literal; the comment itself documents the exact match. | `components/TerminalPane.vue` | `TerminalPane.vue:257` | low | S | Swap to `var(--ch-weight-medium)`; removes the comment and one more numeric weight. Standalone one-line fix (smaller than a full task); not included in the next-3 because it's a single-line consistency tweak with no visual change. |
| 19 | **`EnvPresetManager.vue` form-input `border-radius: 4px` (four instances) and segment `border-radius: 6px` (two instances)** — off the 5/7/10 scale. | `components/EnvPresetManager.vue` | `EnvPresetManager.vue:334,407,437,483,516,538` | low | S | Inputs → `var(--ch-radius-sm)`=5px; segmented caps → `var(--ch-radius-md)`=7px. Roll into Task 3. |
| 20 | **`MarkdownContent.vue` inline-code `border-radius: 4px` and block `border-radius: 6px`** — correctly marked with off-scale comments; same shape as EnvPresetManager #19. | `components/MarkdownContent.vue` | `MarkdownContent.vue:193,204` | low | S | Snap inline-code → `var(--ch-radius-sm)`=5px, block → `var(--ch-radius-md)`=7px (or accept off-scale as a deliberate prose-code choice). Standalone two-line fix. |
| 21 | **`LayoutSelector.vue` preview-tile `border-radius: 2px`** — inline mini-tile radius; a sharp 2px corner reads as an intentional "chip inside a button" affordance. | `components/LayoutSelector.vue` | `LayoutSelector.vue:245` | low | S | Leave as-is (2px reads as sharp). Awareness-only; no change recommended. |
| 22 | **Cursor/terminal avatar gloss gradients are the last remaining decorative gradients in the product** — once finding #8 lands, the entire UI resolves to flat surfaces + hairline borders + elevation shadows reserved for popovers/dialogs/menus. (Login CTA glow was removed in ce5b139, so that half is already resolved.) Meta-observation for the "visual noise" category. | `components/AgentAvatar.vue` | `AgentAvatar.vue:133,138` | low | M | Covered by finding #8; roll into Task 1. |
| 23 | **`LoadingButton` relies on parent for focus ring** — it used `v-bind="$attrs"` + `inheritAttrs: false` with no intrinsic `:focus-visible` rule; when used standalone, focus outline was invisible if the consumer did not supply it. **RESOLVED by `0f02b39`** — `.loading-button:focus-visible` rule added at LoadingButton.vue:55-59. | `components/LoadingButton.vue` | Originally `LoadingButton.vue:2-10, 51-53`; fixed in `0f02b39` (ring at :55-59). | med (RESOLVED) | S | Landed. |

**Findings counts by category:** spacing rhythm (2: #10, #16), type scale (4: #1, #9, #15, #18), radius (5: #6, #17, #19, #20, #21), shadow/elevation (4: #3, #11, #12, #13; #3 RESOLVED), color (4: #2, #2b, #7, #8), motion (5: #4, #5, #14, #14b, #14c; #4 RESOLVED; former #24 merged into #14b), focus/hover (2: #4, #23; both RESOLVED — note #4 is cross-listed under both motion and focus because it describes both issues), visual noise (1: #22; Login half RESOLVED by ce5b139). **Total: 26 unique findings** (originally 24; +1 for the #2 → #2/#2b split, +2 for the #14 → #14/#14b/#14c three-way split, −1 for #24 merged into #14b; category-label sum is 27 because finding #4 is cross-listed under both motion and focus). Every finding has exactly one owning file. 3 findings fully resolved (#3, #4, #23); 1 meta-finding half-resolved (#22 Login half); remaining: 3 dispatchable next-3 tasks (#2/#6/#7/#8/#22 → Task 1 AgentAvatar; #14 → Task 2 MobileControls; #9/#19 → Task 3 EnvPresetManager); small standalone low-priority tweaks (#5 spinner awareness, #10 xterm padding, #18 TerminalPane weight, #20 MarkdownContent radii, #21 LayoutSelector tile — all single-line or awareness, not worth a dedicated bounded task); deferred bundles cover App.vue (#1/#11/#14b) and AWV (#2b/#12/#13/#14c/#15/#16/#17).

## 5. Suggested Next 3 Bounded Tasks

These are the **three highest-value, smallest-blast-radius, file-disjoint** follow-ups available right now (not deferred behind in-flight work). Each is scoped to a single component file so a worker can finish in one small diff, and all three can run concurrently without merge conflicts. Tasks A (LoginView) and C (LoadingButton) from the original audit shipped as `ce5b139` and `0f02b39` during review and are no longer listed here.

### Task 1 — `components/AgentAvatar.vue` — tokenize palette, flatten gradient noise, snap radii
**File:** `frontend/src/components/AgentAvatar.vue`
**Scope:** `<style scoped>` block (~15 lines). (1) Snap default radius 8px → `var(--ch-radius-md)` and sm radius 6px → `var(--ch-radius-sm)` (finding #6); (2) replace default `color:#fff` / `background:#4b4b4b` with `var(--ch-color-text-inverse)` / `var(--ch-color-surface-control)` (finding #7); (3) drop the `linear-gradient(135deg, …)` gloss on `.agent-avatar--cursor` and `.agent-avatar--terminal` in favor of flat surface tokens (cursor → `--ch-color-surface-control`, terminal → `--ch-color-surface-sunken`, terminal fg → `--ch-terminal-green`) (findings #8, #22); (4) leave Claude/Codex brand colors as hex for now (they are legitimate brand marks) but normalize the claude/codex rules to declare flat `background`/`color` without gradient (finding #2 avatar half). Introducing new `--ch-agent-*-bg/fg` tokens in `App.vue :root` is a follow-up for the App-shell pass — this task uses existing tokens where possible and leaves brand hex literal.
**Why first:** avatars appear everywhere a session is listed (tabs, board, status). Cleaning them propagates polish instantly across dozens of surfaces; gradients on 28px glyphs read as noise in a minimalist system. Estimated diff: ~15 lines; cosmetic only. Covers findings #2/#6/#7/#8/#22 in this file.

### Task 2 — `components/MobileControls.vue` — snap off-token motion durations to 120/180ms
**File:** `frontend/src/components/MobileControls.vue`
**Scope:** `<style scoped>` block (~4 lines at 344, 389, 470). Replace the hardcoded `140ms ease` bottom-sheet transition (line 344) with `var(--ch-motion-standard)`; replace `0.08s` / `0.1s` button-press transitions (lines 389, 470) with `var(--ch-motion-fast)` (120ms) and trim `box-shadow` / `transform` from the `transition-property` list where the button never animates those properties (most press states swap only `background-color`/`border-color`). The existing `180ms cubic-bezier` drawer transition at line 323 is already correct and needs no change.
**Why second:** smallest blast radius of any non-trivial fix (~4 lines), mobile-only so lower visible weight than Task 1, but resolves the only remaining multi-property motion-timing drift outside deferred files. Estimated diff: ~4 lines; cosmetic motion tuning. Covers finding #14.

### Task 3 — `components/EnvPresetManager.vue` — snap form weight + radii to tokens
**File:** `frontend/src/components/EnvPresetManager.vue`
**Scope:** `<style scoped>` block (~10 lines across 7 sites). Replace `font-weight: normal` (line 454) with `var(--ch-weight-regular)` (finding #9); snap the four form-input `border-radius: 4px` (lines 407, 437, 483, 538) to `var(--ch-radius-sm)=5px` and the two segmented-control `border-radius: 6px` (lines 334, 516) to `var(--ch-radius-md)=7px` (finding #19).
**Why third:** shared form primitive used in every environment preset dialog; mechanical token mapping with zero design judgment; resolves two low-priority consistency gaps in one file. Estimated diff: ~7 one-line substitutions; purely cosmetic; covers findings #9 and #19.

## 5b. Deferred Follow-up Work (gated on in-flight tasks)

Not part of the "next 3" — these are blocked until the named in-flight tasks land. Listed for completeness so the resident can plan.

- **App.vue shell bundle** (gated on prefetch task): three-weight cap (#1: 745/806/843 fw700 → `--ch-weight-semibold`); hairline shadows (#11: 819, 860); mode-bar motion consolidation (#14b: 783).
- **AgentWorkspaceView board bundle** (gated on board-payload task): brand-hex status pills wired to new tokens (#2b); lightbox shadow → `--ch-shadow-dialog` (#12: 9162); card-hover elevation drop (#13: 6552); motion durations consolidated (#14c); `font-size:10px` pills lifted to `--ch-font-xs` (#15); `gap:6px` chip rhythm snapped (#16); scattered radii snapped to 5/7/10 (#17).
- **Small standalone low-priority tweaks** (not deferred, not worth a dedicated bounded task — any worker can grab these opportunistically): LoadingButton spinner easing awareness (#5); xterm padding 6px → 4/8px (#10); TerminalPane `font-weight:500` → `var(--ch-weight-medium)` (#18); MarkdownContent radii 4/6px → 5/7px (#20); LayoutSelector 2px tile radius left as-is (#21, no-op).

## 6. What This Audit Did NOT Cover

- **Terminal canvas/xterm.js internals** — `TerminalView.vue` JS-injected xterm CSS is partially audited (finding 10) but the xterm vendor theme is out of scope.
- **Backend templates / server-rendered markup** — none exist in this frontend.
- **Contrast / WCAG compliance** — this is a minimalist-aesthetic audit, not an a11y contrast audit (focus-ring presence is covered but text-contrast ratios are not measured).
- **Mobile / viewport / on-screen-keyboard behavior** — covered by existing lesson `on-ios-safari-…`; no new findings in that area.
- **Performance / bundle size** — out of scope; the board-payload perf task covers the major win here.

## 7. Lessons Consulted

- **`auto-review-decisions-were-being-made-by-whichever-reaper-or-reviewer-loop-won-t`** — read? no. Applies to orchestration/reviewer routing policy, not to a read-only UI audit.
- **`on-ios-safari-ios-chrome-and-android-chrome-focusing-the-terminal-input-opens-th`** — read? no. Mobile viewport/keyboard behavior is out of scope for this audit (no source edits; mobile layout issues would require code changes outside the audit's read-only mandate).
- **`the-workspace-state-machine-and-auto-review-dispatch-write-to-the-same-task-conc`** — read? no. State-machine/race issue; not UI.
- **`when-the-cursor-agent-type-was-added-every-downstream-consumer-that-parsed-sessi`** — read? no. Agent-type parser issue; not relevant to a CSS/token audit.
- **`with-more-than-one-terminal-tab-open-page-refresh-or-websocket-reconnect-races-w`** — read? no. Multi-tab reconnect issue; not CSS.

**Conclusion:** no workspace lessons applied directly to this read-only aesthetic audit.
