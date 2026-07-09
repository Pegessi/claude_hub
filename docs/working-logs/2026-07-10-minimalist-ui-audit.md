# Minimalist UI Audit — Prioritized Findings

**Date:** 2026-07-10
**Branch:** `docs/minimalist-ui-audit`
**Scope:** `frontend/src/` against the landed `--ch-*` design-token scale (`App.vue :root`)
**Audit type:** Read-only analysis. No source files were edited in producing this document.
**Baseline:** develop @ `6c79f45`.

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

## 3. Status: Most Findings Shipped During Review

The in-flight tasks that originally gated edits to `App.vue` and `AgentWorkspaceView.vue` (prefetch and board-payload-slim) have both landed. In addition, eight of the originally-audited cosmetic fixes were picked up by parallel workers during the review cycle and are now on develop:

| Commit | Scope | Findings resolved |
|---|---|---|
| `ce5b139` | LoginView CTA flatten + focus ring | #3, #4 |
| `0f02b39` | LoadingButton intrinsic focus ring | #23 |
| `601c3cf` | MobileControls motion snap to 120/180ms | #14 |
| `cde4352` | AgentAvatar palette tokenize + gradient flatten + radii snap | #6, #7, #8, #22 (partial on #2 — brand hex untouched) |
| `d79afe2` | TerminalPane pane-tab font-weight → `--ch-weight-medium` | #18 |
| `5eef835` | EnvPresetManager form weight + radii snap | #9, #19 |
| `430d277` | App.vue font-weight cap at semibold (700→600) | #1 |
| `6c79f45` | MarkdownContent prose-code radii → `--ch-radius-sm/md` | #20 |

One item has an explicit "differs; keep literal" comment placed by a prior token-pass worker and is treated as a **documented intentional deviation**, not an action item:

- #21 (LayoutSelector 2px mini-tile radius — sharp affordance, awareness/no-op)

No source files are currently deferred. The remaining actionable items are consolidated into three file-disjoint bounded tasks in Section 5; Task 1 and Task 2 have **no cross-task dependency** and can be dispatched concurrently; Task 3 is sequenced after Task 1 because it consumes tokens defined there.

## 4. Prioritized Findings

Sorted by impact (high → med → low), then effort (S → M → L). Each finding names one owning component so follow-up tasks can be dispatched file-disjointly. RESOLVED rows are retained in the table for traceability.

| # | Finding | Owning file(s) | Evidence (file:line) | Impact | Effort | Recommended change |
|---|---|---|---|---|---|---|
| 1 | **`font-weight: 700` breaks the calm three-weight cap.** **RESOLVED by `430d277`** — all three chrome sites (`.mode-button`, `.theme-switch label`, `.auth-error-banner__retry`) now use `var(--ch-weight-semibold)` (App.vue:745, 806, 843). | `App.vue` | Originally `App.vue:745, 806, 843`; fixed in `430d277`. | **high** (RESOLVED) | S | Landed. |
| 2 | **Brand hex on Claude/Codex avatars needs `--ch-agent-*` tokens in `App.vue :root` plus a consumer edit** — commit `cde4352` resolved the cursor/terminal half (cursor → `--ch-color-surface-raised` + `--ch-color-text`, terminal → `--ch-color-surface-sunken` + `--ch-color-success`) but Claude cream/orange (`#f1eee5`/`#d97757`) and Codex black/white (`#000`/`#fff`) remain as ad-hoc hex literals in `AgentAvatar.vue:150-157`; no token source of truth for these legitimate brand marks. | `App.vue` (token def) / `AgentAvatar.vue` (consumer) | `App.vue:457-580` (:root); `AgentAvatar.vue:150-157` (consumer) | **high** | S | Add `--ch-agent-claude-bg/fg`, `--ch-agent-codex-bg/fg` (plus optional `--ch-agent-origin-bg/fg` violet and `--ch-agent-autonomy-bg/fg` teal for AWV #2b) in `App.vue :root` (Task 1) and then wire AgentAvatar to consume them (Task 3). |
| 2b | **Hardcoded brand hex on AWV status pills duplicates agent palette** — status chips repeat Claude `#d97757` (6658), Codex `#10a37f` (6663), terminal `#7ee787` (6673), master/origin violet `#c4b5fd` + `rgba(139,92,246,…)` chip backgrounds (6922-6923, 7788-7789) and autonomy teal `#5eead4` + `rgba(20,184,166,…)` fills (7751/7753/7754/7769/7778). 12 brand-hex sites total. Same brand family as #2; once #2 introduces `--ch-agent-*-bg/fg` tokens these pills can consume them. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6658, 6663, 6673, 6922-6923, 7751, 7753, 7754, 7769, 7778, 7788-7789` | **high** | M | Wire chips to tokens after Task 1 lands. Listed in Section 5b as a follow-up (not in the bounded next-3) to keep Task 2 free of cross-task dependencies. |
| 3 | **Login CTA hover lift + glow shadow violates flat-minimalist aesthetic.** **RESOLVED by `ce5b139`** — lift and glow removed; hover is `background: var(--ch-color-accent-hover)` only (LoginView.vue:118-120). | `views/LoginView.vue` | Originally `views/LoginView.vue:118-122`; fixed in `ce5b139`. | **high** (RESOLVED) | S | Landed. |
| 4 | **Login CTA uses `transition: all 0.2s ease` + missing focus ring.** **RESOLVED by `ce5b139`** — transition tightened to `background-color var(--ch-motion-standard)` (LoginView.vue:115); `:focus-visible` ring added at :122-125. Cross-listed under motion+focus (historical). | `views/LoginView.vue` | Originally `views/LoginView.vue:115, 102-116`; fixed in `ce5b139`. | **high** (RESOLVED) | S | Landed. |
| 5 | **`LoadingButton` spinner uses `700ms linear infinite`** — the only spinner in the codebase; linear easing looks mechanical against the otherwise eased motion system, duration untethered from any token. Awareness; spinners are arguably exempt from feedback-motion rules. | `components/LoadingButton.vue` | `LoadingButton.vue:71` | med | S | Leave as-is unless/until a dedicated motion polish pass decides on spinner easing. Awareness only (Section 5b). |
| 6 | **AgentAvatar hardcoded radii off the 5/7/10 scale.** **RESOLVED by `cde4352`** — default `border-radius: 8px` → `var(--ch-radius-md)` (AgentAvatar.vue:120), sm `border-radius: 6px` → `var(--ch-radius-sm)` (AgentAvatar.vue:132). | `components/AgentAvatar.vue` | Originally `AgentAvatar.vue:92, 104`; fixed in `cde4352`. | **high** (RESOLVED) | S | Landed. |
| 7 | **AgentAvatar default bg/fg hardcoded, not on palette.** **RESOLVED by `cde4352`** — `color: #fff` → `var(--ch-color-text-inverse)` (AgentAvatar.vue:122), `background: #4b4b4b` → `var(--ch-color-surface-control)` (AgentAvatar.vue:123). | `components/AgentAvatar.vue` | Originally `AgentAvatar.vue:95-96`; fixed in `cde4352` (122-123). | med (RESOLVED) | S | Landed. |
| 8 | **Cursor/terminal avatars use linear-gradient backgrounds.** **RESOLVED by `cde4352`** — `.agent-avatar--cursor` flat `var(--ch-color-surface-raised)`/`var(--ch-color-text)` (AgentAvatar.vue:160-163); `.agent-avatar--terminal` flat `var(--ch-color-surface-sunken)`/`var(--ch-color-success)` (165-168); gradients removed. | `components/AgentAvatar.vue` | Originally `AgentAvatar.vue:132-139`; fixed in `cde4352`. | med (RESOLVED) | M | Landed. |
| 9 | **`font-weight: normal` in EnvPresetManager form labels.** **RESOLVED by `5eef835`** — `.form-hint-inline` now `font-weight: var(--ch-weight-regular)` (EnvPresetManager.vue:454). | `components/EnvPresetManager.vue` | Originally `EnvPresetManager.vue:454`; fixed in `5eef835`. | low (RESOLVED) | S | Landed. Note: line 541 `transition: background-color 0.2s` on `.btn` remains 200ms off the motion scale (Section 5b awareness). |
| 10 | **Injected xterm padding `6px` off the 4px rhythm** — JS-injected CSS sets `.xterm { padding: 6px !important }` (TerminalView.vue:947), between `space-1=4px` and `space-2=8px`. | `components/TerminalView.vue` | `TerminalView.vue:947` | low | S | Snap to `padding: 8px !important` (`var(--ch-space-2)`). Listed in Section 5b as a follow-up (not in next-3) to keep the three bounded tasks to three single-owner scopes. |
| 11 | **Hardcoded hairline shadows in App.vue shell not on palette** — `.mode-button.active` `box-shadow: 0 1px 2px rgba(15,23,42,0.08)` (819); floating FAB `0 1px 3px rgba(15,23,42,0.16)` (860). Hardcoded slate shadows, not themed for dark/light, not mapped to any `--ch-shadow-*` token. | `App.vue` | `App.vue:819, 860` | med | S | Remove (rely on `border-color` change) or add a themed `--ch-shadow-hairline` token. Task 1. |
| 12 | **`box-shadow: 0 12px 48px rgba(0,0,0,0.45)` hardcoded on AWV lightbox** — close to `--ch-shadow-dialog` (0 24px 80px / 0.45) but hand-tuned and unthemed. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:9162` | med | S | Use `var(--ch-shadow-dialog)`; unifies elevation across overlays. Task 2 (uses existing token; no cross-task dependency). |
| 13 | **Card-hover large elevation adds visual noise on the board** — `box-shadow: 0 8px 24px var(--ch-shadow-color-soft)` plus `transform: translateY(-1px)` (6552-6553) on `.agent-status-card:hover`; minimalist aesthetic prefers `border-color`/`background` change only. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6552-6553` | med | S | Drop hover shadow and `translateY(-1px)`; rely on `border-color: var(--ch-color-border-hover)` and `background: var(--ch-color-surface-raised)` only. Task 2. |
| 14 | **MobileControls scattered motion durations off the 120/180ms scale.** **RESOLVED by `601c3cf`** — bottom-sheet transition (344) uses `var(--ch-motion-standard)`; button-press transitions (389, 470) use `var(--ch-motion-fast)`. Sheet-drawer `180ms cubic-bezier` was already correct. FAB `box-shadow: 0 4px 12px var(--ch-shadow-color-soft)` (469) is a legitimate elevated-button shadow per principle #4, not a regression. | `components/MobileControls.vue` | Originally `MobileControls.vue:344, 389, 470`; fixed in `601c3cf`. | med (RESOLVED) | S | Landed. |
| 14b | **App-shell mode-bar uses hand-tuned `200ms cubic-bezier + 160ms ease` transition pair** — `.app-mode-bar` quintuple-property `200ms cubic-bezier(0.2,0,0,1)` on max-height/padding/border-color/transform plus `160ms ease` on opacity (783); duplicates 180ms cubic-bezier entrance used elsewhere but is off-token. | `App.vue` | `App.vue:783` | med | S | Add `--ch-motion-drawer: 180ms cubic-bezier(0.2, 0, 0, 1)` and replace the hand-tuned pair; collapse `opacity` onto the same timing for consistency. Task 1. |
| 14c | **AgentWorkspaceView cards/chips use scattered off-token motion durations** — hovers/presses use `0.12s`/`0.08s` decimal shorthand (6871, 6891, 6937, 7978, 8022), `opacity 0.15s, background 0.15s` (7130), `background 0.1s ease, border-color 0.1s ease` (8073), `opacity 240ms ease` (7396); multi-select chip also has a stray `transform 0.08s` inside a `var(--ch-motion-fast)` declaration (6958-6962). The two `700ms linear infinite` spinners (6791, 7046) and `ws-toast-in 180ms cubic-bezier` (10837) are exempt/correct. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:6871, 6891, 6937, 6958-6962, 7130, 7396, 7978, 8022, 8073` | med | M | Consolidate hover/press/select onto `var(--ch-motion-fast)`; layout/entrance onto `var(--ch-motion-standard)`; replace decimal `0.12s`/`0.08s`/`0.15s`/`0.1s` with tokens. Task 2 (uses existing tokens; no cross-task dependency). |
| 15 | **AgentWorkspaceView uses `font-size: 10px` for pills/badges** — 1px below `--ch-font-xs=11px`; 25 off-scale sites. | `components/AgentWorkspaceView.vue` | 25 sites (e.g. 6301, 6323, 6638, 6648, 6696, 6741, 6762, 7681, 7725, 7737, 7755, 7790) | med | S | Lift to `var(--ch-font-xs)`; tighten chip padding slightly if needed. Task 2 (uses existing token). |
| 16 | **AgentWorkspaceView `gap: 6px` chip rhythm off-scale** — 26 sites using `gap: 6px` between chips/meta elements, between 4px/8px token steps. | `components/AgentWorkspaceView.vue` | 26 sites (e.g. 6344, 6456, 6621, 6730, 6838, 7189, 7542, 7645, 7918, 7951) | med | S | Snap to `var(--ch-space-1)=4px` (tight) or `var(--ch-space-2)=8px` (airy) per use. Task 2 (uses existing tokens). |
| 17 | **AgentWorkspaceView scattered off-scale radii (2/3/6/8px)** — 2px indicators (9629), 3px chips (10800,10892), 6px cards/pills (7231,8527,8633,10628), 8px lightbox/overlay caps (9085,9096,9103); ~10 sites off the 5/7/10 scale. | `components/AgentWorkspaceView.vue` | `AgentWorkspaceView.vue:7231,8527,8633,9085,9096,9103,9629,10628,10800,10892` | low | S | Cards/pills/caps → `var(--ch-radius-md)`=7px; tiny chip indicators → `var(--ch-radius-sm)`=5px; sharp 2px affordances (9629) stay. Task 2 (uses existing tokens). |
| 18 | **`TerminalPane.vue` `font-weight: 500` left literal.** **RESOLVED by `d79afe2`** — `.pane-tab-name` now `font-weight: var(--ch-weight-medium)` (TerminalPane.vue:256); stale "nothing speculative" comment reworded. | `components/TerminalPane.vue` | Originally `TerminalPane.vue:201,257`; fixed in `d79afe2` (line 256). | low (RESOLVED) | — | Landed. |
| 19 | **`EnvPresetManager.vue` form-input `border-radius: 4px` / segment `border-radius: 6px`.** **RESOLVED by `5eef835`** — four 4px sites use `var(--ch-radius-sm)`=5px; two 6px sites use `var(--ch-radius-md)`=7px. | `components/EnvPresetManager.vue` | Originally `EnvPresetManager.vue:334,407,437,483,516,538`; fixed in `5eef835`. | low (RESOLVED) | S | Landed. Note: line 541 `transition: background-color 0.2s` is 200ms off the motion scale (Section 5b). |
| 20 | **`MarkdownContent.vue` inline-code `border-radius: 4px` and block `border-radius: 6px`.** **RESOLVED by `6c79f45`** — inline `4px` → `var(--ch-radius-sm)`=5px at line 191; block `6px` → `var(--ch-radius-md)`=7px at line 202; the prior "differs; keep literal" comments were removed by the same commit. | `components/MarkdownContent.vue` | Originally `MarkdownContent.vue:124-125, 193, 204`; fixed in `6c79f45` (191, 202). | low (RESOLVED) | — | Landed. |
| 21 | **`LayoutSelector.vue` preview-tile `border-radius: 2px`** — inline mini-tile radius; a sharp 2px corner reads as an intentional "chip inside a button" affordance. Awareness/no-op. | `components/LayoutSelector.vue` | `LayoutSelector.vue:245` | low (no-op) | — | Leave as-is. |
| 22 | **Cursor/terminal avatar gloss gradients were the last decorative gradients.** **RESOLVED by `cde4352`** — cursor flat `--ch-color-surface-raised`/`--ch-color-text`; terminal flat `--ch-color-surface-sunken`/`--ch-color-success`. Login CTA glow removed earlier in `ce5b139`. The only remaining gradients are functional content-fades (TabBar 1501/1506 left/right fades, AWV skeleton-shimmer 7364) — not decorative gloss. | `components/AgentAvatar.vue` | Originally `AgentAvatar.vue:133,138`; fixed in `cde4352`. | low (RESOLVED) | M | Landed. |
| 23 | **`LoadingButton` relies on parent for focus ring.** **RESOLVED by `0f02b39`** — `.loading-button:focus-visible` rule added at LoadingButton.vue:55-59. | `components/LoadingButton.vue` | Originally `LoadingButton.vue:2-10, 51-53`; fixed in `0f02b39` (ring at :55-59). | med (RESOLVED) | S | Landed. |

**Counts (develop @ `6c79f45`):**

- **RESOLVED / shipped:** 13 findings (#1, #3, #4, #6, #7, #8, #9, #14, #18, #19, #20, #22, #23; #4 cross-listed motion+focus for history) via 8 commits.
- **INTENTIONAL / no-op:** 1 finding (#21).
- **Awareness (no fix scheduled):** 1 finding (#5 LoadingButton spinner easing).
- **Actionable in next-3 (Section 5):** 9 findings across 3 single-file tasks. Finding #2 spans two files (App-token def + AgentAvatar consumer) and is split across Task 1 and Task 3; the other 8 findings are fully contained in a single task.
  - Task 1 (App.vue shell, defines new tokens): #2 token-def half, #11 hairline shadows, #14b mode-bar motion (covers findings #2, #11, #14b)
  - Task 2 (AWV board, uses existing tokens — **no dependency on Task 1**): #12, #13, #14c, #15, #16, #17 (6 findings)
  - Task 3 (AgentAvatar brand consumer, **sequenced after Task 1**): #2 consumer half (completes finding #2)
- **Section 5b follow-ups (dispatch after Task 1):** #2b AWV brand pills (consumes T1 tokens), #10 TerminalView xterm padding — 2 findings.

Total unique rows: 26. Cross-list note: #4 appears under both motion and focus categories (historical), so category-label sum is 27.

## 5. Suggested Next 3 Bounded Tasks

These are the **three highest-value, smallest-blast-radius, single-owner** follow-ups available right now against develop @ `6c79f45`. Task 1 and Task 2 have **zero cross-task dependency** and can be dispatched to two workers concurrently without merge conflicts. Task 3 is sequenced after Task 1 because it consumes tokens defined there; it is a tiny (~4-line) mechanical substitution that can ship in the same PR as Task 1 or immediately after. All originally-recommended tasks (AgentAvatar palette/radii/gradients, MobileControls motion, EnvPresetManager weight/radii, TerminalPane fw500, App fw700 cap, MarkdownContent code radii) shipped in real time during review via parallel workers (`cde4352`, `601c3cf`, `5eef835`, `d79afe2`, `430d277`, `6c79f45`); the three tasks below are rebuilt from the remaining actionable work and restructured per reviewer feedback (v7) so each task touches exactly one owning file.

### Task 1 — `App.vue` shell bundle — brand-token DEFINITIONS + hairline shadows + mode-bar motion
**File:** `frontend/src/App.vue`
**Scope:** `<style>` block (~15 modified lines across 3 sites plus 8 new token declarations). NO consumer edits in other files.
1. **Brand tokens (finding #2 — def half):** add custom properties in `:root` between the existing terminal palette at line 579 and the closing `}` at line 580:
   - `--ch-agent-claude-bg: #f1eee5; --ch-agent-claude-fg: #d97757;`
   - `--ch-agent-codex-bg: #000; --ch-agent-codex-fg: #fff;`
   - `--ch-agent-origin-bg: #c4b5fd; --ch-agent-origin-fg: #8b5cf6;` (violet — for AWV #2b follow-up)
   - `--ch-agent-autonomy-bg: #5eead4; --ch-agent-autonomy-fg: #14b8a6;` (teal — for AWV #2b follow-up)
2. **Hairline shadows (finding #11):** either remove `.mode-button.active` `box-shadow: 0 1px 2px rgba(15,23,42,0.08)` (819) in favor of the existing `border-color`/`background` selected state, or introduce a themed `--ch-shadow-hairline` token and use it for both that site and the floating FAB `0 1px 3px rgba(15,23,42,0.16)` (860).
3. **Mode-bar motion (finding #14b):** add `--ch-motion-drawer: 180ms cubic-bezier(0.2, 0, 0, 1);` to `:root`; in the `.app-mode-bar` transition at line 783, replace the hand-tuned `200ms cubic-bezier(...)` with `var(--ch-motion-drawer)` for max-height/padding/border-color/transform and collapse the separate `160ms ease` for `opacity` onto the same timing for consistency.

**Why first:** App.vue is the design-token source of truth. Defining `--ch-agent-*` unblocks Task 3 and the #2b/#10 follow-ups; #11 and #14b remove the last App-shell token drifts. All three edits are cosmetic `<style>` changes with zero behavioral impact; #1 (fw700) already shipped. Estimated diff: ~15 modified lines + 8 new token declarations; purely cosmetic. **No edits outside App.vue** — AgentAvatar consumer wiring is Task 3, AWV brand-pill wiring is a Section 5b follow-up.

### Task 2 — `AgentWorkspaceView.vue` board bundle — shadows + motion + type + rhythm + radii (EXISTING tokens only — no dependency on Task 1)
**File:** `frontend/src/components/AgentWorkspaceView.vue`
**Scope:** `<style>` block (~50-70 lines; bulk mechanical substitution using only tokens already in `:root`). **Deliberately excludes #2b brand-pill wiring** so this task has zero dependency on Task 1 and can start immediately without waiting for tokens.
1. **Lightbox shadow (finding #12):** replace hardcoded `box-shadow: 0 12px 48px rgba(0,0,0,0.45)` (9162) with `var(--ch-shadow-dialog)`.
2. **Card hover (finding #13):** drop `.agent-status-card:hover` `box-shadow: 0 8px 24px …` + `transform: translateY(-1px)` (6552-6553), leaving `border-color`/`background` changes only.
3. **Motion (finding #14c):** snap all scattered off-token motion durations to existing tokens — decimal `0.12s`/`0.08s`/`0.15s`/`0.1s` shorthand at 6871/6891/6937/6958-6962/7130/7978/8022/8073 → `var(--ch-motion-fast)` (120ms); `opacity 240ms ease` at 7396 → `var(--ch-motion-standard)` (180ms). The two `700ms linear infinite` spinners (6791, 7046) and correct `180ms cubic-bezier` toast-in (10837) stay as-is.
4. **Type (finding #15):** bulk-replace 25 `font-size: 10px` pills → `var(--ch-font-xs)` (11px); tighten chip padding slightly if visual weight shifts.
5. **Rhythm (finding #16):** bulk-snap 26 `gap: 6px` chip/meta rhythms to `var(--ch-space-1)=4px` (tight chips) or `var(--ch-space-2)=8px` (airy meta rows) per visual context.
6. **Radii (finding #17):** snap scattered off-scale radii — 6px cards/pills (7231/8527/8633/10628) → `var(--ch-radius-md)`=7px; 3px chips (10800/10892) → `var(--ch-radius-sm)`=5px; 8px lightbox caps (9085/9096/9103) → `var(--ch-radius-md)`=7px; genuinely sharp 2px indicators (9629) stay.

**Why second (concurrent with Task 1):** AWV is the largest single surface (board/cards/modals). This task is entirely mechanical substitution using tokens that already exist in `:root` today; brand-hex pills are deliberately left for the Section 5b follow-up so there is no cross-task wait. Biggest visual "all at once" polish win remaining; consolidates six residual findings in one file. Estimated diff: ~50-70 lines of one-line substitutions spread across the file; purely cosmetic; zero design judgment required.

### Task 3 — `components/AgentAvatar.vue` — wire claude/codex brand hex to new tokens (sequenced after Task 1)
**File:** `frontend/src/components/AgentAvatar.vue`
**Scope:** `<style scoped>` block (4 lines at 150-157). Once Task 1 lands the new `--ch-agent-*` tokens, replace the hardcoded brand hex on the Claude/Codex avatar classes:
- `.agent-avatar--claude { background: #f1eee5; color: #d97757; }` (150-152) → `{ background: var(--ch-agent-claude-bg); color: var(--ch-agent-claude-fg); }`
- `.agent-avatar--codex { background: #000; color: #fff; }` (154-156) → `{ background: var(--ch-agent-codex-bg); color: var(--ch-agent-codex-fg); }`

**Why third (sequenced):** this is the consumer half of finding #2 and is the only task that depends on Task 1 tokens landing. It is a tiny (~4-line) mechanical substitution with no design judgment; it can be folded into the same PR as Task 1 or shipped immediately after. The cursor/terminal rules (160-168) already use palette tokens from `cde4352` and need no change. Covers finding #2 (consumer half).

## 5b. Follow-ups and Awareness (not in the bounded next-3)

Items that are real fix candidates but are either (a) dependent on Task 1 tokens and small enough to fold into whichever PR takes #2b, or (b) single-line awareness items that do not warrant a dedicated bounded task:

**Follow-ups (dispatch after Task 1 lands):**
- **#2b — AWV brand-pill wiring (AgentWorkspaceView.vue):** 12 brand-hex sites at 6658/6663/6673/6922-6923/7751/7753/7754/7769/7778/7788-7789; mechanically swap Claude `#d97757`→`var(--ch-agent-claude-fg)`, Codex `#10a37f`→`var(--ch-agent-codex-fg)`, terminal `#7ee787`→`var(--ch-color-success)`, violet `#c4b5fd`/`rgba(139,92,246,…)`→`var(--ch-agent-origin-bg/fg)`, teal `#5eead4`/`rgba(20,184,166,…)`→`var(--ch-agent-autonomy-bg/fg)`. Estimated ~12 one-line substitutions; can ride with Task 2 or ship standalone right after Task 1.
- **#10 — TerminalView xterm padding (TerminalView.vue):** JS-injected `.xterm { padding: 6px !important }` (947) → `padding: 8px !important` (`var(--ch-space-2)`). ~1 character in a JS string; test by launching a terminal and verifying no canvas clipping/scrollbar overlap. Smallest remaining fix; kept out of the bounded next-3 to keep the three tasks on the three highest-impact files (App, AWV, AgentAvatar).

**Awareness (no dedicated fix task):**
- **#5 — LoadingButton.vue:71 spinner `700ms linear infinite`:** mechanical linear spinner is arguably intentional; leave unless/until a future motion polish pass decides on spinner easing.
- **EnvPresetManager.vue:541 `.btn` `transition: background-color 0.2s`:** 200ms vs `--ch-motion-standard`=180ms; ships adjacent to the already-merged #9/#19 fix (`5eef835`); any worker touching EnvPresetManager for other reasons can snap this to `var(--ch-motion-standard)` as a drive-by.
- **#21 — LayoutSelector 2px tile radius (LayoutSelector.vue:245):** documented intentional deviation; sharp "chip inside a button" affordance. No action.

## 6. What This Audit Did NOT Cover

- **Terminal canvas/xterm.js internals** — `TerminalView.vue` JS-injected xterm CSS is partially audited (finding #10) but the xterm vendor theme is out of scope.
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

**Conclusion:** no workspace lessons applied directly to this read-only aesthetic audit. Thirteen of the originally 26 findings shipped during the eight-cycle review via eight parallel-worker commits (`ce5b139`, `0f02b39`, `601c3cf`, `cde4352`, `d79afe2`, `5eef835`, `430d277`, `6c79f45`); the remaining 9 actionable fix-items (#2 spans App def + AgentAvatar consumer; #11/#14b in App; #12/#13/#14c/#15/#16/#17 in AWV) are consolidated into three single-file bounded tasks above with zero cross-file leakage inside any task. Task 1 (App.vue) and Task 2 (AgentWorkspaceView) are concurrently dispatchable with no cross-task dependency; Task 3 (AgentAvatar brand consumer) is sequenced after Task 1 and is a 4-line mechanical substitution. The AWV brand-pill (#2b) and TerminalView xterm-padding (#10) follow-ups are listed in Section 5b to keep Section 5 at exactly three tasks.
