# Minimalist UI Audit — Prioritized Findings

**Date:** 2026-07-10
**Branch:** `docs/minimalist-ui-audit`
**Scope:** `frontend/src/` against the landed `--ch-*` design-token scale (`App.vue :root`)
**Audit type:** Read-only analysis. No source files were edited in producing this document.

---

## 1. Baseline: The Token System

`frontend/src/App.vue` :root (lines 405–528) defines a tight, minimalist design scale:

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
| 1 | **`font-weight: 700` breaks the calm three-weight cap** — three instances in the app-shell chrome (theme switch, mode button, auth-error retry) use 700 where the token scale intentionally caps at `--ch-weight-semibold`=600. | `App.vue` | `App.vue:693, 754, 791` | **high** (DEFERRED) | S | Replace with `var(--ch-weight-semibold)`. Do when the prefetch task lands. |
| 2 | **Hardcoded brand hex palette duplicated across avatars and status pills** — Claude `#d97757/#f1eee5`, Codex `#000/#fff`, Cursor gradient grays, terminal green `#7ee787`, master/origin violet, autonomy teal are all ad-hoc hex scattered between `AgentAvatar.vue` and `AgentWorkspaceView.vue`. Same colors re-declared twice, no token source of truth. | `AgentAvatar.vue` (+ `AgentWorkspaceView.vue` DEFERRED) | `AgentAvatar.vue:123-140`; `AgentWorkspaceView.vue:6657-6673, 6921-6923, 7751-7778` | **high** | M | Introduce a small set of `--ch-agent-{claude,codex,cursor,terminal}-{bg,fg}` tokens in `App.vue :root`, use them in `AgentAvatar.vue`; wire the status pills in `AgentWorkspaceView.vue` to the same tokens when that file is free. |
| 3 | **Login CTA hover lift + glow shadow violates flat-minimalist aesthetic** — `transform: translateY(-2px)` + `box-shadow: 0 8px 20px <accent>` adds decorative elevation contrary to the flat aesthetic used by every other button (TabBar, LayoutSelector logout, EnvPreset buttons all use flat background-color-only hover). | `views/LoginView.vue` | `views/LoginView.vue:118-122` | **high** | S | Remove `translateY(-2px)` lift and `box-shadow` hover; rely on the existing `background: var(--ch-color-accent-hover)` color change only. Tightens consistency with the rest of the UI. |
| 4 | **Login CTA uses `transition: all 0.2s ease`** — `transition: all` is over-broad (perf + noisy property list) and 200ms is off the motion scale (standard is 180ms). Also no `:focus-visible` ring on the CTA (accessibility + polish gap). | `views/LoginView.vue` | `views/LoginView.vue:115, 102-116` | **high** | S | Replace `transition: all 0.2s ease` with `transition: background-color var(--ch-motion-standard), transform var(--ch-motion-standard)` (or just background-color if lift is removed per finding 3); add `outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px;` on `.feishu-login-btn:focus-visible`. |
| 5 | **`LoadingButton` spinner uses `700ms linear infinite`** — the only spinner in the codebase; linear easing looks mechanical against the otherwise eased motion system, and the duration is untethered from any token. | `components/LoadingButton.vue` | `components/LoadingButton.vue:65` | med | S | Leave as-is if mechanical spinner look is intentional; otherwise consider a `cubic-bezier(0.4, 0, 0.2, 1)` or accept that a spinner's motion is exempt from the 120/180 token (spinner rate is not a feedback transition). Flagging for awareness. |
| 6 | **AgentAvatar hardcoded radii off the 5/7/10 scale** — default `border-radius: 8px` (between md=7 and lg=10), sm `border-radius: 6px` (between sm=5 and md=7). | `components/AgentAvatar.vue` | `AgentAvatar.vue:92, 104` | med | S | md avatar → `var(--ch-radius-md)` (7px, 1px tighter — acceptable for an avatar); sm avatar → `var(--ch-radius-sm)` (5px). Or add md to lg for a softer default. Choose one direction for visual consistency. |
| 7 | **AgentAvatar default bg/fg hardcoded, not on palette** — `color: #fff`, `background: #4b4b4b` duplicates `--ch-color-text-inverse` / `--ch-color-surface-control` semantics. | `components/AgentAvatar.vue` | `AgentAvatar.vue:95-96` | med | S | Replace with `color: var(--ch-color-text-inverse)` and `background: var(--ch-color-surface-control)` (or a dedicated avatar-fallback token). |
| 8 | **Cursor/terminal avatars use linear-gradient backgrounds** — `linear-gradient(135deg, #f5f5f5 0%, #d4d4d4 100%)` and `linear-gradient(135deg, #2a2a2a 0%, #1a1a1a 100%)` add decorative gradient noise to 28px glyphs; gradient stops are hardcoded grays not on the palette. | `components/AgentAvatar.vue` | `AgentAvatar.vue:132-139` | med | M | Drop gradients; use a flat surface token (`--ch-color-surface-control` for cursor, `--ch-color-surface-sunken` for terminal) and a mapped fg (terminal fg → `--ch-terminal-green` or `--ch-color-success`). Curved glyphs already read as 'branded' without gradient gloss. |
| 9 | **`font-weight: normal` in EnvPresetManager form labels** — explicit `normal` (400) where `var(--ch-weight-regular)` would document intent and match system usage. Low priority since value is the same. | `components/EnvPresetManager.vue` | `components/EnvPresetManager.vue:454` | low | S | Replace `font-weight: normal` with `font-weight: var(--ch-weight-regular)`. Pure consistency; no visual change. |
| 10 | **Injected xterm padding `6px` off the 4px rhythm** — JS-injected CSS string sets `.xterm { padding: 6px !important }`. Padding sits between `space-1=4` and `space-2=8`, slightly denser than the outer grid (which uses `--ch-space-1=4px` gap/padding). | `components/TerminalView.vue` | `TerminalView.vue:947` | low | S | Snap to `var(--ch-space-2)=8px` to match `TerminalGridView` gutter OR `var(--ch-space-1)=4px` for a tighter canvas-to-chrome ratio. Pick one; current 6px is an orphan. |
| 11 | **Hardcoded hairline shadows in App.vue shell not on palette** — `.mode-button.active` and `.theme-switch-thumb` use `box-shadow: 0 1px 2px rgba(15,23,42,0.08)` / `0 1px 3px rgba(15,23,42,0.16)` — tiny, but unthemed (breaks dark/light parity) and not mapped to any shadow token. | `App.vue` | `App.vue:767, 808` | med (DEFERRED) | S | Remove (minimalist — rely on `border-color` change) or add a `--ch-shadow-hairline` token used consistently for pressed-thumb states. Do when the prefetch task lands. |
| 12 | **`box-shadow: 0 12px 48px rgba(0,0,0,0.45)` hardcoded on lightbox in AgentWorkspaceView** — close to `--ch-shadow-dialog` (0 24px 80px / 0.45) but a hand-tuned variant; would be covered by the dialog token or a new popover-alt. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:9162` | med (DEFERRED) | S | Use `var(--ch-shadow-dialog)`; visually very similar and unifies elevation across overlays. Do when board-payload task lands. |
| 13 | **Card-hover large elevation adds visual noise on the board** — `box-shadow: 0 8px 24px var(--ch-shadow-color-soft)` on task-card hover is a big elevation (matches Login CTA finding 3); minimalist aesthetic prefers `border-color` change only for hover. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6552` | med (DEFERRED) | S | Drop the hover shadow; rely on `border-color: var(--ch-color-border-hover)` and/or a subtle background tint. Do when board-payload task lands. |
| 14 | **Scattered motion durations (80ms, 100ms, 140ms, 150ms, 200ms, 240ms) in App.vue + AgentWorkspaceView + MobileControls** — not snapped to 120/180ms tokens; produces micro-jank as different items ease at different rates. 80ms press jitter is particularly noticeable on MobileControls. | `App.vue:731`, `AgentWorkspaceView.vue:6871,6891,6937,7978,8073,7130,7396`, `MobileControls.vue:344,389,470` | multiple (see left) | med | M | Consolidate onto `var(--ch-motion-fast)` for hover/press feedback and `var(--ch-motion-standard)` for layout/entrance. MobileControls 80ms press → `var(--ch-motion-fast)` is fine (120ms still feels crisp for a press). |
| 15 | **AgentWorkspaceView uses `font-size: 10px` for pills/badges** — 1px below the type scale floor (`--ch-font-xs=11px`); produces visual inconsistency next to chip text at 11px in TabBar/LayoutSelector/NetworkAccessMenu. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6301,6323,6638,6648,6696,6741,6762,7681,7725,7737,7755,7790` | med (DEFERRED) | S | Lift to `var(--ch-font-xs)`; tighten chip padding slightly if needed to keep the pill footprint. Do when board-payload task lands. |
| 16 | **AgentWorkspaceView `gap: 6px` chip rhythm off-scale** — 5 sites using `gap: 6px` between chips/meta elements, between the 4px/8px token steps. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6344,6456,6621,6730,6838` | med (DEFERRED) | S | Snap to `var(--ch-space-1)=4px` (tight pills) or `var(--ch-space-2)=8px` (airy pills); pick one and apply consistently. Do when board-payload task lands. |
| 17 | **AgentWorkspaceView scattered off-scale radii (2/3/6/8px)** — a mix of 2px (tight indicators), 3px (chips), 6px (cards), 8px (segmented caps) inconsistent with the 5/7/10 scale. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:7231,8527,8633,9085,9096,9629,10800,10892` | low (DEFERRED) | S | Snap cards → `var(--ch-radius-md)`=7px, chips/pills → `var(--ch-radius-sm)`=5px; rectangular affordances (2px) can remain if intentional. Do when board-payload task lands. |
| 18 | **`TerminalPane.vue` `font-weight: 500` left literal with a comment** — value matches `--ch-weight-medium=500` exactly but a "nothing speculative" comment left it literal; the comment itself documents it is an exact match. | `components/TerminalPane.vue` | `TerminalPane.vue:257` | low | S | Swap to `var(--ch-weight-medium)` now that the scale is stable; removes the comment and one more numeric weight. Pure consistency. |
| 19 | **`EnvPresetManager.vue` form-input `border-radius: 4px` (four instances) and segment `border-radius: 6px` (two instances)** — off the 5/7/10 scale. | `components/EnvPresetManager.vue` | `EnvPresetManager.vue:334,407,437,483,516,538` | low | S | Inputs → `var(--ch-radius-sm)`=5px (small, tactile); segmented-control caps → `var(--ch-radius-md)`=7px. |
| 20 | **`MarkdownContent.vue` inline-code `border-radius: 4px` and block `border-radius: 6px`** — correctly marked with off-scale comments; same treatment as EnvPresetManager. | `components/MarkdownContent.vue` | `MarkdownContent.vue:193,204` | low | S | Snap inline-code → `var(--ch-radius-sm)`=5px, block → `var(--ch-radius-md)`=7px (or keep and accept off-scale; this is a low-priority consistency polish). |
| 21 | **`LayoutSelector.vue` preview-tile `border-radius: 2px`** — inline mini-tile radius; a rectangular 2px corner is an intentional "chip inside a button" affordance. | `components/LayoutSelector.vue` | `LayoutSelector.vue:245` | low | S | Accept or snap to `var(--ch-radius-sm)`=5px; likely leave as-is (2px reads as sharp). Acknowledge for awareness. |
| 22 | **Gradient/gloss avatars + Login hover shadow are the only decorative gradients/shadows in the product** — once findings 3 and 8 are resolved, the entire UI resolves to flat surfaces + hairline borders + elevation shadows reserved for popovers/dialogs/menus. This is a meta-observation rather than an independent fix. | `AgentAvatar.vue`, `views/LoginView.vue` | `AgentAvatar.vue:133,138`; `LoginView.vue:121` | low | M | Covered by findings 3 and 8; tracked here for the "visual noise" category. |
| 23 | **`LoadingButton` relies on parent for focus ring** — it uses `v-bind="$attrs"` and `inheritAttrs: false` but does not forward `:focus-visible` styling itself. When used as the root of a CTA (e.g. `.feishu-login-btn`), the parent's focus ring works (because `class` is bound through); when used standalone inside other components, focus outline is invisible if the consumer does not apply it. | `components/LoadingButton.vue` | `LoadingButton.vue:2-10, 50-53` | med | S | Add a `.loading-button:focus-visible { outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px; }` rule so focus is always visible regardless of parent styling. (Login CTA gets a ring via finding 4 in either case; this is defense-in-depth.) |
| 24 | **App-shell mode-bar transition uses `200ms cubic-bezier(...) + 160ms ease` opacity** — hand-tuned pair that duplicates the TerminalPane/TabBar/LayoutSelector 180ms cubic-bezier entrance. | `App.vue` | `App.vue:731` | med (DEFERRED) | S | Compose with a shared motion custom property (e.g. add `--ch-motion-drawer: 180ms cubic-bezier(0.2, 0, 0, 1)` to :root); replace all four sites (App.mode-bar, LayoutSelector.menu-in, TabBar.tab-menu-in/toast-in, TerminalPane collapse). Do when prefetch task lands. |

**Findings counts by category:** spacing rhythm (2: #10, #16), type scale (4: #1, #9, #15, #18), radius (5: #6, #17, #19, #20, #21), shadow/elevation (4: #3, #11, #12, #13), color (3: #2, #7, #8), motion (3: #4, #5, #14), focus/hover (2: #4, #23), visual noise (1: #22).

## 5. Suggested Next 3 Bounded Tasks

Ordered to be **high-value**, **smallest blast radius**, and **file-disjoint** (each can run concurrently without merge conflicts). Scoped to a single owning component each so a worker can finish in one small diff.

### Task A — `views/LoginView.vue` — flatten the CTA + add focus ring
**File:** `frontend/src/views/LoginView.vue`
**Scope:** CTA only (~6 lines). (1) Remove the `.feishu-login-btn:hover` `transform: translateY(-2px)` and `box-shadow: 0 8px 20px …` (rely on background-color change, which already exists); (2) replace `transition: all 0.2s ease` with `transition: background-color var(--ch-motion-standard)` (plus border-color if any); (3) add `.feishu-login-btn:focus-visible { outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px; }`.
**Why first:** single highest-fix/ lowest-risk win on the user's first brand touchpoint; removes the last non-popover elevation-shadow in the login flow and brings the CTA in line with every other button in the product. Estimated diff: ~6 lines; no behavioral change.

### Task B — `components/AgentAvatar.vue` — tokenize palette, flatten gradient noise, snap radii
**File:** `frontend/src/components/AgentAvatar.vue`
**Scope:** `<style scoped>` block (~20 lines). (1) Snap default radius 8px → `var(--ch-radius-md)` and sm radius 6px → `var(--ch-radius-sm)`; (2) replace default `color:#fff` / `background:#4b4b4b` with `var(--ch-color-text-inverse)` / `var(--ch-color-surface-control)`; (3) drop the `linear-gradient(135deg, …)` gloss on `.agent-avatar--cursor` and `.agent-avatar--terminal` in favor of flat surface tokens (cursor `--ch-color-surface-raised`, terminal `--ch-color-surface-sunken`, terminal fg → `--ch-terminal-green`); (4) leave Claude/Codex brand colors as hex for now (they are legitimate brand marks and don't yet have tokens) BUT normalize the claude/codex rules to declare `background`/`color` without gradient.
**Why second:** avatars appear everywhere a session is listed (tabs, board, status). Cleaning them propagates polish instantly across dozens of surfaces; gradients on 28px glyphs read as noise in a minimalist system. Estimated diff: ~12 lines; cosmetic only. Note: full brand-color tokenization (finding 2) is a follow-up that adds tokens in `App.vue :root` and is slightly larger blast radius — this task is the safe cosmetic pre-step.

### Task C — `components/LoadingButton.vue` — add intrinsic focus ring
**File:** `frontend/src/components/LoadingButton.vue`
**Scope:** `<style scoped>` block (~3 lines). Add a `.loading-button:focus-visible { outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px; border-radius: inherit; }` rule. Because the component uses `inheritAttrs: false` and renders its own `<button>`, focus styling currently depends on the parent providing it (which is why the Login CTA also needed its own ring — finding 4). Giving the component an intrinsic focus ring closes an accessibility/polish gap for every LoadingButton consumer (logout, login, env-save, etc.).
**Why third:** smallest possible change (3 lines), zero visual impact outside keyboard navigation, but closes a systemic focus gap in a shared primitive so future button consumers get the ring automatically.

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
