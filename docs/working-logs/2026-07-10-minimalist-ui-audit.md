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
| 1 | **`font-weight: 700` breaks the calm three-weight cap** — three instances in the app-shell chrome (mode-button label, theme-switch thumb, auth-error retry) use 700 where the token scale intentionally caps at `--ch-weight-semibold`=600. | `App.vue` | `App.vue:745, 806, 843` | **high** (DEFERRED) | S | Replace with `var(--ch-weight-semibold)`. Do when the prefetch task lands. |
| 2 | **Hardcoded brand hex palette duplicated across avatars and status pills** — Claude `#d97757/#f1eee5`, Codex `#000/#fff` in avatars but `#10a37f` green on the status chip, Cursor gradient grays, terminal green `#7ee787`, master/origin violet `#c4b5fd`/`rgba(139,92,246,…)`, autonomy teal `#5eead4`/`rgba(20,184,166,…)` are all ad-hoc hex/rgba scattered between `AgentAvatar.vue` and `AgentWorkspaceView.vue`. Same brand colors re-declared across components; no token source of truth. | `AgentAvatar.vue` (+ `AgentWorkspaceView.vue` DEFERRED) | `AgentAvatar.vue:123-139`; `AgentWorkspaceView.vue:6658, 6663, 6673, 6922-6923, 7751-7778, 7786-7789` | **high** | M | Introduce a small set of `--ch-agent-{claude,codex,cursor,terminal,master,autonomy}-{bg,fg}` tokens in `App.vue :root`, use them in `AgentAvatar.vue`; wire the status pills in `AgentWorkspaceView.vue` to the same tokens when that file is free. |
| 3 | **Login CTA hover lift + glow shadow violates flat-minimalist aesthetic** — `transform: translateY(-2px)` + `box-shadow: 0 8px 20px <accent>` added decorative elevation contrary to the flat aesthetic used by every other button. **RESOLVED by `ce5b139`** — lift and glow removed; hover is now `background: var(--ch-color-accent-hover)` only (LoginView.vue:118-120). | `views/LoginView.vue` | Originally `views/LoginView.vue:118-122`; fixed in `ce5b139`. | **high** (RESOLVED) | S | Landed. |
| 4 | **Login CTA uses `transition: all 0.2s ease`** — `transition: all` was over-broad and 200ms off the motion scale; no `:focus-visible` ring. **RESOLVED by `ce5b139`** — transition tightened to `background-color var(--ch-motion-standard)` (LoginView.vue:115); `:focus-visible` ring added at :122-125. | `views/LoginView.vue` | Originally `views/LoginView.vue:115, 102-116`; fixed in `ce5b139`. | **high** (RESOLVED) | S | Landed. |
| 5 | **`LoadingButton` spinner uses `700ms linear infinite`** — the only spinner in the codebase; linear easing looks mechanical against the otherwise eased motion system, and the duration is untethered from any token. | `components/LoadingButton.vue` | `LoadingButton.vue:71` | med | S | Leave as-is if mechanical spinner look is intentional; otherwise consider a `cubic-bezier(0.4, 0, 0.2, 1)` or accept that a spinner's motion is exempt from the 120/180 token (spinner rate is not a feedback transition). Flagging for awareness. |
| 6 | **AgentAvatar hardcoded radii off the 5/7/10 scale** — default `border-radius: 8px` (between md=7 and lg=10), sm `border-radius: 6px` (between sm=5 and md=7). | `components/AgentAvatar.vue` | `AgentAvatar.vue:92, 104` | med | S | md avatar → `var(--ch-radius-md)` (7px, 1px tighter — acceptable for an avatar); sm avatar → `var(--ch-radius-sm)` (5px). Or add md to lg for a softer default. Choose one direction for visual consistency. |
| 7 | **AgentAvatar default bg/fg hardcoded, not on palette** — `color: #fff`, `background: #4b4b4b` duplicates `--ch-color-text-inverse` / `--ch-color-surface-control` semantics. | `components/AgentAvatar.vue` | `AgentAvatar.vue:95-96` | med | S | Replace with `color: var(--ch-color-text-inverse)` and `background: var(--ch-color-surface-control)` (or a dedicated avatar-fallback token). |
| 8 | **Cursor/terminal avatars use linear-gradient backgrounds** — `linear-gradient(135deg, #f5f5f5 0%, #d4d4d4 100%)` and `linear-gradient(135deg, #2a2a2a 0%, #1a1a1a 100%)` add decorative gradient noise to 28px glyphs; gradient stops are hardcoded grays not on the palette. | `components/AgentAvatar.vue` | `AgentAvatar.vue:132-139` | med | M | Drop gradients; use a flat surface token (`--ch-color-surface-control` for cursor, `--ch-color-surface-sunken` for terminal) and a mapped fg (terminal fg → `--ch-terminal-green` or `--ch-color-success`). Curved glyphs already read as 'branded' without gradient gloss. |
| 9 | **`font-weight: normal` in EnvPresetManager form labels** — explicit `normal` (400) where `var(--ch-weight-regular)` would document intent and match system usage. Low priority since value is the same. | `components/EnvPresetManager.vue` | `components/EnvPresetManager.vue:454` | low | S | Replace `font-weight: normal` with `font-weight: var(--ch-weight-regular)`. Pure consistency; no visual change. |
| 10 | **Injected xterm padding `6px` off the 4px rhythm** — JS-injected CSS string sets `.xterm { padding: 6px !important }`. Padding sits between `space-1=4` and `space-2=8`, slightly denser than the outer grid (which uses `--ch-space-1=4px` gap/padding). | `components/TerminalView.vue` | `TerminalView.vue:947` | low | S | Snap to `var(--ch-space-2)=8px` to match `TerminalGridView` gutter OR `var(--ch-space-1)=4px` for a tighter canvas-to-chrome ratio. Pick one; current 6px is an orphan. |
| 11 | **Hardcoded hairline shadows in App.vue shell not on palette** — `.theme-switch-thumb` uses `box-shadow: 0 1px 2px rgba(15,23,42,0.08)` and `.auth-error .retry-btn`/`.mode-button` use `0 1px 3px rgba(15,23,42,0.16)` — tiny, but unthemed (breaks dark/light parity) and not mapped to any shadow token. | `App.vue` | `App.vue:819, 860` | med (DEFERRED) | S | Remove (minimalist — rely on `border-color` change) or add a `--ch-shadow-hairline` token used consistently for pressed-thumb states. Do when the prefetch task lands. |
| 12 | **`box-shadow: 0 12px 48px rgba(0,0,0,0.45)` hardcoded on lightbox in AgentWorkspaceView** — close to `--ch-shadow-dialog` (0 24px 80px / 0.45) but a hand-tuned variant; would be covered by the dialog token or a new popover-alt. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:9162` | med (DEFERRED) | S | Use `var(--ch-shadow-dialog)`; visually very similar and unifies elevation across overlays. Do when board-payload task lands. |
| 13 | **Card-hover large elevation adds visual noise on the board** — `box-shadow: 0 8px 24px var(--ch-shadow-color-soft)` on task-card hover is a big elevation (matches Login CTA finding 3); minimalist aesthetic prefers `border-color` change only for hover. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6552` | med (DEFERRED) | S | Drop the hover shadow; rely on `border-color: var(--ch-color-border-hover)` and/or a subtle background tint. Do when board-payload task lands. |
| 14 | **MobileControls scattered motion durations off the 120/180ms scale** — bottom-sheet open uses `140ms ease` (344), button press states use `80ms` (389) and `100ms` (470) with hardcoded decimals (`0.08s`, `0.1s`) and include unused `box-shadow`/`transform` transitions on plain background swaps. | `components/MobileControls.vue` | `MobileControls.vue:344, 389, 470` | med | S | Consolidate onto `var(--ch-motion-fast)` (120ms) for press/feedback and `var(--ch-motion-standard)` (180ms) for the sheet open/close; drop `box-shadow` from the transition-property list when no shadow animation is present. 120ms still feels crisp for a press. |
| 14b | **App-shell mode-bar and board off-token motion durations (150/200/240ms + 80/100/120ms press)** — App.vue mode-bar uses a hand-tuned quintuple `200ms cubic-bezier + 160ms ease` pair (783); AgentWorkspaceView task cards repeat `120ms` hover / `80ms` press (6871,6891,6937,7978) and `150ms` (7130), `100ms` (8073), plus one `240ms ease` opacity (7396). Produces micro-jank as different items ease at different rates across the board and shell. | `App.vue` + `components/AgentWorkspaceView.vue` (DEFERRED) | `App.vue:783`; `AgentWorkspaceView.vue:6871, 6891, 6937, 7130, 7396, 7978, 8073` | med (DEFERRED) | M | Compose with shared motion custom properties (e.g. promote the existing `--ch-motion-fast`/`--ch-motion-standard` and add a drawer/press pair); replace all sites. Do when the prefetch + board-payload tasks land. |
| 15 | **AgentWorkspaceView uses `font-size: 10px` for pills/badges** — 1px below the type scale floor (`--ch-font-xs=11px`); produces visual inconsistency next to chip text at 11px in TabBar/LayoutSelector/NetworkAccessMenu. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6301,6323,6638,6648,6696,6741,6762,7681,7725,7737,7755,7790` | med (DEFERRED) | S | Lift to `var(--ch-font-xs)`; tighten chip padding slightly if needed to keep the pill footprint. Do when board-payload task lands. |
| 16 | **AgentWorkspaceView `gap: 6px` chip rhythm off-scale** — 5 sites using `gap: 6px` between chips/meta elements, between the 4px/8px token steps. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6344,6456,6621,6730,6838` | med (DEFERRED) | S | Snap to `var(--ch-space-1)=4px` (tight pills) or `var(--ch-space-2)=8px` (airy pills); pick one and apply consistently. Do when board-payload task lands. |
| 17 | **AgentWorkspaceView scattered off-scale radii (2/3/6/8px)** — a mix of 2px (tight indicators), 3px (chips), 6px (cards), 8px (segmented caps) inconsistent with the 5/7/10 scale. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:7231,8527,8633,9085,9096,9629,10800,10892` | low (DEFERRED) | S | Snap cards → `var(--ch-radius-md)`=7px, chips/pills → `var(--ch-radius-sm)`=5px; rectangular affordances (2px) can remain if intentional. Do when board-payload task lands. |
| 18 | **`TerminalPane.vue` `font-weight: 500` left literal with a comment** — value matches `--ch-weight-medium=500` exactly but a "nothing speculative" comment left it literal; the comment itself documents it is an exact match. | `components/TerminalPane.vue` | `TerminalPane.vue:257` | low | S | Swap to `var(--ch-weight-medium)` now that the scale is stable; removes the comment and one more numeric weight. Pure consistency. |
| 19 | **`EnvPresetManager.vue` form-input `border-radius: 4px` (four instances) and segment `border-radius: 6px` (two instances)** — off the 5/7/10 scale. | `components/EnvPresetManager.vue` | `EnvPresetManager.vue:334,407,437,483,516,538` | low | S | Inputs → `var(--ch-radius-sm)`=5px (small, tactile); segmented-control caps → `var(--ch-radius-md)`=7px. |
| 20 | **`MarkdownContent.vue` inline-code `border-radius: 4px` and block `border-radius: 6px`** — correctly marked with off-scale comments; same treatment as EnvPresetManager. | `components/MarkdownContent.vue` | `MarkdownContent.vue:193,204` | low | S | Snap inline-code → `var(--ch-radius-sm)`=5px, block → `var(--ch-radius-md)`=7px (or keep and accept off-scale; this is a low-priority consistency polish). |
| 21 | **`LayoutSelector.vue` preview-tile `border-radius: 2px`** — inline mini-tile radius; a rectangular 2px corner is an intentional "chip inside a button" affordance. | `components/LayoutSelector.vue` | `LayoutSelector.vue:245` | low | S | Accept or snap to `var(--ch-radius-sm)`=5px; likely leave as-is (2px reads as sharp). Acknowledge for awareness. |
| 22 | **Gradient/gloss avatars + Login hover shadow are the only decorative gradients/shadows in the product** — once findings 3 (SHIPPED) and 8 land, the only remaining decorative gradient is the cursor/terminal avatar gloss. Meta-observation tracked for the "visual noise" category. | `AgentAvatar.vue` (+ `views/LoginView.vue` RESOLVED) | `AgentAvatar.vue:133,138`; `LoginView.vue:121` (removed in ce5b139) | low | M | Login half resolved by ce5b139; avatar half covered by finding 8 (roll into Task B). |
| 23 | **`LoadingButton` relies on parent for focus ring** — it used `v-bind="$attrs"` + `inheritAttrs: false` with no intrinsic `:focus-visible` rule; when used standalone, focus outline was invisible if the consumer did not supply it. **RESOLVED by `0f02b39`** — `.loading-button:focus-visible` rule added at LoadingButton.vue:55-59. | `components/LoadingButton.vue` | Originally `LoadingButton.vue:2-10, 51-53`; fixed in `0f02b39` (ring at :55-59). | med (RESOLVED) | S | Landed. |
| 24 | **App-shell mode-bar transition uses hand-tuned quintuple `200ms cubic-bezier + 160ms ease` pair** — duplicates the TerminalPane/TabBar/LayoutSelector 180ms cubic-bezier entrance; see finding 14b for the consolidated deferred motion pass. | `App.vue` | `App.vue:783` | med (DEFERRED) | S | Compose with a shared motion custom property (e.g. add `--ch-motion-drawer: 180ms cubic-bezier(0.2, 0, 0, 1)` to :root); replace all four sites (App.mode-bar, LayoutSelector.menu-in, TabBar.tab-menu-in/toast-in, TerminalPane collapse). Roll into finding 14b; do when prefetch task lands. |

**Findings counts by category:** spacing rhythm (2: #10, #16), type scale (4: #1, #9, #15, #18), radius (5: #6, #17, #19, #20, #21), shadow/elevation (4: #3, #11, #12, #13; #3 RESOLVED), color (3: #2, #7, #8), motion (5: #4, #5, #14, #14b, #24; #4 RESOLVED), focus/hover (2: #4, #23; both RESOLVED), visual noise (1: #22; Login half RESOLVED). Total: 25 findings (previously 24; #14 split into dispatchable 14 and deferred 14b). 3 findings fully resolved (#3, #4, #23); 1 half-resolved (#22 Login half); 21 open (3 dispatchable now: #6/7/8 → Task B AgentAvatar, #14 → Task D MobileControls, #5/#9/#10/#18/#19/#20/#21 small standalone; 12 deferred: #1/#11/#24 App, #2 AWV half/#12/#13/#14b/#15/#16/#17 AWV).

## 5. Suggested Bounded Tasks

Ordered to be **high-value**, **smallest blast radius**, and **file-disjoint** (each can run concurrently without merge conflicts). Scoped to a single owning component each so a worker can finish in one small diff. **Update (post-review fix):** Tasks A and C shipped within hours of the initial audit — commits `ce5b139` (LoginView CTA flatten + focus ring) and `0f02b39` (LoadingButton intrinsic focus ring) both landed on develop. Tasks B and D remain dispatchable; Tasks E/F cover deferred work gated on prefetch/board-payload.

### Task A (SHIPPED — `ce5b139`) — `views/LoginView.vue` — flatten the CTA + add focus ring
**File:** `frontend/src/views/LoginView.vue`
**Scope as-shipped:** ~6 lines. Removed `translateY(-2px)` hover lift and `0 8px 20px <accent>` box-shadow; tightened `transition: all 0.2s ease` to `transition: background-color var(--ch-motion-standard)`; added `.feishu-login-btn:focus-visible { outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px; }`.
**Status:** landed on develop; findings #3 and #4 resolved.

### Task B — `components/AgentAvatar.vue` — tokenize palette, flatten gradient noise, snap radii
**File:** `frontend/src/components/AgentAvatar.vue`
**Scope:** `<style scoped>` block (~20 lines). (1) Snap default radius 8px → `var(--ch-radius-md)` and sm radius 6px → `var(--ch-radius-sm)`; (2) replace default `color:#fff` / `background:#4b4b4b` with `var(--ch-color-text-inverse)` / `var(--ch-color-surface-control)`; (3) drop the `linear-gradient(135deg, …)` gloss on `.agent-avatar--cursor` and `.agent-avatar--terminal` in favor of flat surface tokens (cursor `--ch-color-surface-raised`, terminal `--ch-color-surface-sunken`, terminal fg → `--ch-terminal-green`); (4) leave Claude/Codex brand colors as hex for now (they are legitimate brand marks and don't yet have tokens) BUT normalize the claude/codex rules to declare `background`/`color` without gradient.
**Why first remaining:** avatars appear everywhere a session is listed (tabs, board, status). Cleaning them propagates polish instantly across dozens of surfaces; gradients on 28px glyphs read as noise in a minimalist system. Estimated diff: ~12 lines; cosmetic only. Note: full brand-color tokenization (finding 2) is a follow-up that adds tokens in `App.vue :root` and is slightly larger blast radius — this task is the safe cosmetic pre-step.

### Task C (SHIPPED — `0f02b39`) — `components/LoadingButton.vue` — add intrinsic focus ring
**File:** `frontend/src/components/LoadingButton.vue`
**Scope as-shipped:** ~3 lines in `<style scoped>`. Added `.loading-button:focus-visible { outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px; border-radius: inherit; }`.
**Status:** landed on develop; finding #23 resolved. This also removes the parent-dependency bug that made finding #4's Login CTA focus ring a partial defense-in-depth — LoadingButton consumers now all get a ring automatically.

### Task D — `components/MobileControls.vue` — snap off-token motion durations to 120/180ms
**File:** `frontend/src/components/MobileControls.vue`
**Scope:** `<style scoped>` block (~4 lines at 344, 389, 470). Replace the hardcoded `140ms ease` sheet transition (line 344) with `var(--ch-motion-standard)`; replace `0.08s` / `0.1s` button-press transitions (lines 389, 470) with `var(--ch-motion-fast)` (120ms) and trim `box-shadow` / `transform` from the `transition-property` list where the button never animates those properties (most press states swap only `background-color`/`border-color`). The sheet-drawer `180ms cubic-bezier` at line 323 is already correct and needs no change.
**Why next:** small blast radius (~4 lines), mobile-only so lower user-facing weight than the shipped CTAs, but this finding was split off from the former multi-owner #14 precisely because this file is not in-flight and can be cleaned up immediately. Estimated diff: ~4 lines; cosmetic motion tuning; resolves finding #14.

### Task E (DEFERRED — gated on prefetch task landing) — App.vue: three-weight cap + hairline shadows + motion consolidation
**Files:** `frontend/src/App.vue`
**Scope:** replaces font-weight 700 → `--ch-weight-semibold` (745, 806, 843; finding #1), resolves hairline box-shadow literals (819, 860; finding #11), and consolidates the quintuple mode-bar transition (783; findings #14b, #24). Roll into whatever token pass the prefetch task performs.

### Task F (DEFERRED — gated on board-payload task landing) — AgentWorkspaceView: tokens for pills/chips + motion + lightbox shadow
**Files:** `frontend/src/components/AgentWorkspaceView.vue`
**Scope:** lift `font-size: 10px` pills → `--ch-font-xs` (finding #15), snap `gap: 6px` chip rhythm (finding #16), consolidate scattered radii 2/3/6/8px (finding #17), snap off-token 80/100/120/150/240ms transitions to `--ch-motion-fast/standard` (finding #14b), replace `0 12px 48px` lightbox shadow with `var(--ch-shadow-dialog)` (finding #12), drop card-hover `0 8px 24px` elevation (finding #13), and wire brand-hex pills to the tokens introduced in Task B's follow-up (finding #2). Wait for board-payload to land to avoid conflicts.

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
