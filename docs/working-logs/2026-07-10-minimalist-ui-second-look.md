# Minimalist UI + Response-Speed Sweep — Second Look (Round 2)

Date: 2026-07-10
Baseline: `develop @ 3ee1ef3` (immediately after the TerminalView xterm-padding
snap for audit finding #10 landed).
Auditor: single-agent linear read-only pass (no source edits in this task).

---

## 1. Methodology

### 1.1 Scope

This is a second-pass sweep over the `frontend/` tree after the first
minimalist-audit doc (`2026-07-10-minimalist-ui-audit.md`, v7 at c32a071) had
the majority of its findings shipped across 9 sequential commits (ce5b139 →
3ee1ef3). The goal is to refresh the backlog rather than re-audit what is
already done. Every claim in §2 is anchored to a `file:line` on
`develop@3ee1ef3` and verified against code; nothing is speculative.

### 1.2 What was inspected

| Area | Files | Verdict |
| --- | --- | --- |
| App shell chrome | `frontend/src/App.vue` | **Findings** — see SL-04, SL-06, SL-10 |
| Workspace board | `frontend/src/components/AgentWorkspaceView.vue` (AWV) | **Findings** — see SL-01, SL-03, SL-07, SL-08, SL-09, SL-12 |
| Floating status panel | `frontend/src/components/AgentStatusFloatingPanel.vue` (ASFP) | **Findings** — see SL-02, SL-08 |
| Tab strip | `frontend/src/components/TabBar.vue` | **Findings** — see SL-05, SL-08 |
| Mobile controls | `frontend/src/components/MobileControls.vue` | **Finding** — see SL-08 |
| Env preset manager | `frontend/src/components/EnvPresetManager.vue` | **Finding** — see SL-05 |
| Agent config fields | `frontend/src/components/AgentConfigFields.vue` | Inspected, no new issues (focus-ring treatment already uses `box-shadow: 0 0 0 2px var(--ch-color-accent-ring)` at L287) |
| Agent avatar | `frontend/src/components/AgentAvatar.vue` | Inspected, no new issues (brand-wiring from 30e10b4 is complete for claude/codex; cursor/terminal were already on palette tokens) |
| Terminal route view | `frontend/src/components/TerminalView.vue` | Inspected, no new issues — finding #10 landed at 3ee1ef3; the SAB keystroke fast path and iframe lifecycle are sound; the `<style scoped>` block is ~25 lines of absolute-position/opacity toggling with no stray hex or off-scale literals |
| Login, LayoutSelector, LoadingButton, MarkdownContent, NetworkAccessMenu, TerminalPane, TerminalGridView | previously shipped; spot-checked | Inspected, no regressions |
| Stores | `src/stores/{app,auth,terminal,workspace}Store.ts` | Inspected (SL-12 on terminalStore polling-diff; workspaceStore ETag/304 + isTextEntryFocused + incremental-reports already in place from b788fd4; no new perf hot spots) |
| Composables | `src/composables/*` (2 files) | Inspected, no issues |
| Main entry | `src/main.ts` (17 lines) | Inspected, no issues — pure bootstrap (createApp + Pinia + mount); the single `window.__claudeHub` namespace shim is per F8 and correctly typed |
| Views | `src/views/LoginView.vue` | Already shipped; no regressions |

### 1.3 Explicitly excluded

- **All round-1 RESOLVED findings** — do not re-report. The 9 shipped commits
  cover LoginView CTA, LoadingButton focus ring, MobileControls motion,
  AgentAvatar palette, TerminalPane fw500, EnvPresetManager weight/radii,
  App.vue fw700 cap, MarkdownContent radii, App.vue shell bundle (defs/hairline/
  drawer-motion), AWV board bundle (shadows/motion/type/gap/radii), Avatar
  consumer wiring, TerminalView xterm padding.
- **Intentional deviations documented in round 1** (MarkdownContent 2px tile
  gap; 999px pill radii; the 2px status-indicator in TabBar; the 700ms spinner
  and 180ms cubic-bezier toast entrance in AWV) — none of these have new
  evidence to justify reopening.
- **`TerminalView.vue.bak` / other backup files** — a reviewer note asked that
  `.bak` be flagged as skipped. Sweep confirms zero `.bak`/`.orig` files exist
  anywhere under `frontend/src`; nothing to skip.
- **`router/`** — confirmed empty at this SHA; perf sweep covers `main.ts`
  instead per reviewer note.

### 1.4 Cross-cutting observations

- **Brand-token rollout is 2/4 wired.** `--ch-agent-claude/codex-{bg,fg}` are
  now consumed by `AgentAvatar.vue` (30e10b4); the origin/autonomy pairs
  (`--ch-agent-origin-{bg,fg}`, `--ch-agent-autonomy-{bg,fg}`) are defined in
  App.vue `:root` but still un-consumed. SL-03 covers this.
- **Codex "teal" (#10a37f) is NOT the codex brand color** — it is a status
  color (CLI-active chip) used in ASFP and AWV alongside claude terracotta,
  terminal green, and cursor gray. The Simple-Icons codex mark wired in
  `AgentAvatar` at black/white is the *brand*; the CLI teal is a separate
  semantic. Treating them as the same token would be a regression; SL-02 keeps
  them distinct.
- **`:focus-visible` coverage is inconsistent across shell chrome.** App.vue
  shell buttons (.mode-button, .theme-switch thumb/labels, auth-error-banner
  retry/close) and TabBar/MobileControls buttons rely entirely on `:hover` /
  `.active` with no `:focus-visible` outline or ring. Keyboard users see no
  focus indicator on these surfaces. ACF/AWV form modals already use
  `box-shadow: 0 0 0 2px var(--ch-color-accent-ring)`, so a precedent exists.
- **Input focus treatment is two-schools.** EPM/TabBar small form fields use
  `outline:none; border-color:var(--ch-color-accent);` (border-only, no ring);
  ACF/AWV modal inputs add the 2px accent-ring box-shadow. SL-05 proposes
  unifying to the ring treatment via `:focus-visible` (so click does not show
  a ring but keyboard does).
- **Polling is already well-optimized.** workspaceStore has ETag/304,
  isTextEntryFocused guard, statusesEqual diff, incremental report fetch.
  terminalStore has a reference-counted consumer model + statusesEqual diff
  (SL-12 is a narrow tightening, not a new 5s-poll problem). The SharedArrayBuffer
  keystroke fast path in TerminalView is present and sound. No speculative
  "add v-memo" or "lazy-load X" claims without evidence of render cost.
- **All `v-for` have `:key`.** Earlier grep appeared to show missing keys, but
  deeper inspection confirmed every `v-for` has a `:key` on the immediately
  following element line (ASFP rowGroups→`group.key`, group.rows→`row.tab.id`,
  LayoutSelector iterations→`i`/`layout.type`, TabBar all lists, EnvPresetManager/ACF
  `preset.id`, AWV all lists). No finding here.

---

## 2. Findings

Count: **13 new findings** — 12 UI, 1 perf.
Severity: 0 high, 6 medium, 7 low.
Effort: 12 small, 1 medium, 0 large.
Single-file-disjoint: 12 true, 1 false (SL-05 spans 3 files but is a bounded set).

| ID | Title | File:line | Cat | Sev | Eff | 1-file? | Description + fix |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SL-01 | AWV residual `font-weight: 700` literals | `AgentWorkspaceView.vue:6487,6503,6627,6647,6749,6778,6912,6922,7076,7214,7877,7964,8205,8471,8642,8693,8827,8894,9206,9442,9501,9594,10177,10630,10868` (25 sites) | UI | medium | small | **yes** | The App.vue fw700→semibold cap (430d277) covered shell chrome but did not reach AWV. All 25 sites are headings/eyebrows/pills/button-labels/badge-marks that already live on the smallest font sizes (xs=11px, sm=12px) where 700 is unnecessarily heavy; `var(--ch-weight-semibold)` (=600) is the documented preferred weight per token-scale comments. Bulk `replace_all` CSS-only swap, zero template/script change, postcss trap: no `*/` inside `/* */`. |
| SL-02 | Wire ASFP agent-CLI chips to brand/semantic tokens | `AgentStatusFloatingPanel.vue:900-914` | UI | low | small | **yes** | `.agent-cli[data-kind='claude']` hardcodes `rgba(217,119,87,.18)/#d97757` (already matched byte-for-byte by `--ch-agent-claude-bg`/`--ch-agent-claude-fg` after 0f036c6). `.agent-cli[data-kind='codex']` uses `#10a37f` teal — this is a CLI-ACTIVE semantic (NOT the codex brand, which is black/white per AgentAvatar); leave as-is or introduce a dedicated `--ch-color-cli-active` token instead of forcing it to codex-brand. `.agent-cli[data-kind='terminal']` uses `#7ee787` which matches the `--ch-color-success` family already used by `.tab-indicator`. Suggest: claude → `var(--ch-agent-claude-fg)` + bg `rgba(from var(--ch-agent-claude-fg) r g b / 0.18)` (or a pre-dimmed bg token); terminal → `var(--ch-color-success)`; cursor already on surface tokens. Codex teal stays until a dedicated semantic token exists. 4 one-line CSS edits. |
| SL-03 | Wire AWV brand-tinted pills to `--ch-agent-*` tokens | `AgentWorkspaceView.vue:6656-6671` (agent-status-cli chips, duplicate of ASFP), `6921-6927` (.agent-status-master-badge origin pill: `#c4b5fd`/`rgba(139,92,246,.16)`/`rgba(167,139,250,.45)`), `7752-7762` (.autonomy-badge: `#5eead4`/`rgba(20,184,166,.12)`/`rgba(20,184,166,.34)`), `7787-7799` (.origin-badge: `#c4b5fd`/`rgba(139,92,246,.14)`/`rgba(167,139,250,.38)`) | UI | low | small | **yes** | Claude/terminal chips match SL-02 pattern; the origin/autonomy badge fg values are byte-identical to `--ch-agent-origin-bg` (`#c4b5fd`) and `--ch-agent-autonomy-bg` (`#5eead4`) defined at App.vue:588-589. Bg/border use the same hue at low alpha; either reference `rgba(from var(--ch-agent-origin-fg) …)` or keep the rgba literals alongside a fg-token swap (minimal risk). Master/origin badges duplicate the same purple palette at two opacities — a follow-up could consolidate but the immediate win is fg-token substitution for dark/light parity. CSS-only, ~12 one-line edits. |
| SL-04 | Add `:focus-visible` ring to App.vue shell chrome buttons | `App.vue:752` (.auth-error-banner__retry), `768` (__close), `811` (.mode-button), `859-865` (.theme-switch-label, .theme-switch-thumb) | UI | medium | small | **yes** | 6 interactive surfaces have `:hover`/`.active` but zero `:focus-visible` rule; keyboard Tab navigation lands on these invisibly. Add a `:focus-visible` rule per control (or a shared grouped selector) using `outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px;` — matches the AWV precedent at L6570/9625 and avoids showing a ring on pointer-press. Do NOT remove existing `:hover`/`.active`. CSS-only; ~6-10 lines; postcss trap. |
| SL-05 | Input focus-ring consistency (EPM/TabBar small forms) | `EnvPresetManager.vue:444-445,493-494` (.form-group input:focus, .env-textarea:focus: outline:none + border-color:accent), `TabBar.vue:2001-2002,2116-2117` (.current-path-input:focus, .form-group input/select:focus: same pattern) — contrast with ACF:285-287 and AWV:9561-9563 which add `box-shadow: 0 0 0 2px var(--ch-color-accent-ring)` | UI | medium | small | **no** (3 files, but bounded disjoint set) | Border-only focus is low-contrast in both themes (accent border on surface-control is ~1.3:1 vs control-bg in dark, hard to see on light). Switch to the ACF/AWV precedent: `outline: none; border-color: var(--ch-color-accent-ring-strong); box-shadow: 0 0 0 2px var(--ch-color-accent-ring);` and change the pseudo-class from `:focus` to `:focus-visible` so the ring appears for keyboard but not pointer. TabBar's `.tab-name-input` at L1600 already uses `outline: 1px solid var(--ch-color-accent)` — bump that to `outline: 2px solid var(--ch-color-accent-ring-strong)` for parity. 3 files × ~2-4 lines each = ~12 lines; CSS-only; postcss trap on every file. |
| SL-06 | `.auth-error-banner__retry` color #1a1a1a hardcoded against warning bg | `App.vue:752-766` | UI | medium | small | **yes** | The retry button uses `color: #1a1a1a` against `background: var(--ch-color-warning)`. In dark theme `--ch-color-warning = #facc15` (yellow) → #1a1a1a has strong contrast (ok). In light theme `--ch-color-warning = #906018` (warm brown) → #1a1a1a on #906018 is ~4.5:1 but the banner is a call-to-action and the dark-on-brown pairing reads as muted/inactive. Either introduce a themed `--ch-color-on-warning` (dark:#1a1a1a / light:#fdfdfc) or swap to `var(--ch-color-text-strong)` (which already adapts). Verify by toggling themes. CSS-only; 1-2 lines; postcss trap. |
| SL-07 | AWV `.agent-status-cli` font-weight 700 should follow SL-01 semibold cap | `AgentWorkspaceView.vue:6647` | UI | low | small | **yes** | Already covered by the SL-01 bulk replacement; called out separately because this specific site is the duplicated ASFP chip and its weight should stay in lock-step with ASFP L893 which is already on `var(--ch-weight-medium)` (500, uppercase xs letterspacing 0.04em — medium is the precedent here, not even semibold). When SL-01 ships, audit this one rule against the ASFP twin and consider `var(--ch-weight-medium)` instead of semibold for cross-component consistency. Note: this is a 1-line judgment call inside SL-01's scope; file-level ownership is still AWV. |
| SL-08 | `:focus-visible` on interactive TabBar / MobileControls / ASFP surfaces | `TabBar.vue` — 17 buttons (tab-strip buttons, profile/browser/env items, notification close, remote-browser actions), `MobileControls.vue` — 17 buttons (keyboard, nav, close), `AgentStatusFloatingPanel.vue` — agent-row buttons (~5-10) | UI | medium | small | **no** (3 files, disjoint) | Same root issue as SL-04: `:hover` present, `:focus-visible` absent. Keyboard users cannot see which tab/mobile-control/agent-row is focused. Fix is per-file, parallelizable: add a shared `:focus-visible { outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px; }` rule for the primary button class in each file. ~3-5 lines per file; CSS-only; postcss trap on each. |
| SL-09 | AWV residual `font-size: 11px` literals (33 sites) | `AgentWorkspaceView.vue` (33 matches — see grep listing: 6495, 6502, 6678, 6777, 7222, 7246, 7663, 7865, 7928, 7963, 8204, 8238, 8431, 8470, 8602, 8641, 8692, 8826, 8914, 8932, 9017, 9022, 9055, 9086, 9132, 9185, 9269, 9279, 9593, 9877, 10059, 10194, 10286) | UI | low | small | **yes** | Round-1 finding #15 snap-replaced `font-size: 10px` → `var(--ch-font-xs)` (=11px) in 25 pill sites, but ~33 other sites already written as `font-size: 11px` were left as literals. These are semantically xs (eyebrows, meta lines, pill labels, small badges) and should use `var(--ch-font-xs)` for consistency. Bulk `replace_all` is not safe (some 11px may live on body-like elements); audit-and-replace is ~20-30 minutes. CSS-only, zero template/script change; postcss trap. |
| SL-10 | AWV residual `margin-top: 3px/4px/6px` and small-padding literals | `AgentWorkspaceView.vue` — 3px at 6676/6685/6695/7256; 4px at 6289/6314/8038; 6px at 6685; paddings like 4px 7px, 3px 7px, 2px 7px, 5px 8px, 1px 7px, 1px 8px scattered 6303-7792 | UI | low | medium | **yes** | Round-1 finding #16 snapped `gap: 6px` → space tokens, but vertical rhythm on detail/meta rows still uses 3/4/6px literals for `margin-top` and small mixed px values for `padding` that don't align with `--ch-space-1=4/--ch-space-2=8px`. Recommend bucketing: meta-row margins → `var(--ch-space-1)` (4px) for tight pairs, `var(--ch-space-2)` for section spacing; pill/pill-like paddings → `var(--ch-space-1) calc(var(--ch-space-1) + 3px)` style calc expressions are ugly, so prefer a new 5-6px "chip-padding" custom property if desired, else leave as intentional deviation. Lower priority than SL-01/SL-04/SL-05; defer if a reviewer judges the noise-to-value ratio poor. |
| SL-11 | AWV off-scale `font-size` on `.agent-status-detail` | `AgentWorkspaceView.vue:6678` `font-size: 11px` → already counted in SL-09; `.agent-status-meta` L6685+ uses 11px too; no separate finding. | — | — | — | — | Duplicate of SL-09; rolled up. |
| SL-12 | terminalStore: add ETag/304 to `fetchAgentStatuses` | `src/stores/terminalStore.ts:229-244` | perf | low | small | **yes** | workspaceStore's board poll already uses ETag + If-None-Match + 304 + `isTextEntryFocused` guard + incremental reports (b788fd4). terminalStore's 5s `fetchAgentStatuses` has `statusesEqual()` to skip reactive writes (already good) but does NOT send If-None-Match, so every 5s tick transfers the full status array even when unchanged. For a deployment with many tabs this is unnecessary bytes over the loopback. Pattern is identical to workspaceStore: store `lastETag`, send `headers: { 'If-None-Match': lastETag || '' }`, on 304 return last value, on 200 update ETag from `response.headers.get('etag')`. ~15 lines TS; no behavior change; no UI. |
| SL-13 | Codex CLI teal (#10a37f) and terminal green (#7ee787) — promote to semantic CLI-status tokens | `AgentStatusFloatingPanel.vue:903,913`; `AgentWorkspaceView.vue:6661,6671` | UI | low | small | **no** (2 files, bounded) | The four CLI chips (claude/codex/cursor/terminal) appear in BOTH ASFP and AWV with identical color literals duplicated. Once SL-02/SL-03 wire claude to brand tokens, the remaining three (codex-teal, cursor-gray, terminal-green) are duplicated across files. Promote to three semantic tokens in App.vue `:root` (e.g. `--ch-cli-codex-active: #10a37f`, `--ch-cli-cursor-active: #787878`, `--ch-cli-terminal-active: #7ee787` — or reuse `--ch-color-success` for terminal since #7ee787 is already in the success family; #787878 is close to `--ch-color-text-muted`). Then both ASFP and AWV reference the tokens. This prevents future drift. 3 App.vue defs + ~8 consumer lines across 2 files; CSS/defs only; postcss trap. |

### Findings not filed (considered and rejected)

- **"v-for without :key"** — grep initially showed 20+ candidates; every one has
  a `:key` on the very next element line. Vue's single-root-component template
  idiom places `:key` on the child, not on the same line as `v-for`. No issue.
- **"Lazy-load AWV / route-split"** — AWV is already loaded via `defineAsyncComponent`
  (per dfb26f3 in the round-1 shipped set). No additional chunk-split wins without
  concrete render-cost evidence.
- **"Add v-memo / v-once to board lists"** — board lists are virtualized at the
  data layer (incremental report fetch, statusesEqual diff); adding v-memo
  without a measured hot path is speculative. Deferred.
- **"All polling intervals too fast"** — STATUS_POLL_INTERVAL_MS=5000 and
  boardPollTimer=2500ms are both on reference-counted consumer models with
  diff guards; 2.5-5s is appropriate for a terminal/agent dashboard. No change.
- **"TerminalView iframe opacity toggling is slow"** — the iframe is
  absolute-positioned with opacity/visibility toggles between active/cached
  tabs; no per-frame JS, no layout thrash. Sound.

---

## 3. Suggested Next 3 Bounded Tasks

Each task is mechanically dispatchable, single-file (or a tight disjoint set
with clear per-file boundaries), and has zero dependency on the others.
Reviewer can dispatch all three in parallel to three workers without ordering
concerns.

> **Postcss guard reminder (applies to every CSS-touching task below):** never
> place `*/` inside a `/* */` comment. The postcss `discard-comments` pass in
> Vite's production build mis-parses nested `*/` and has previously caused
> silent rule-dropping (see round-1 doc §3 pitfall). Keep any added comments
> single-line and avoid the `*/` byte sequence inside them.

### Task A — SL-01: AWV font-weight 700 → semibold cap (single file)

- **Owning file:** `frontend/src/components/AgentWorkspaceView.vue`
- **Concrete edits:** bulk replace the 25 `font-weight: 700;` matches with
  `font-weight: var(--ch-weight-semibold);`. Exception (SL-07 note):
  `.agent-status-cli[data-kind=…]` (L6647) is an uppercase xs chip whose ASFP
  twin at `AgentStatusFloatingPanel.vue:893` already uses `var(--ch-weight-medium)`
  (500) — use `var(--ch-weight-medium)` here instead of semibold for cross-component
  parity. Total: 24 semibold + 1 medium substitutions.
- **Validation:** `pnpm lint`, `pnpm build`, dev smoke on workspace route;
  visual diff should be a modest softening of headings/chips/button labels
  with zero weight jumps above 600.
- **Scope guard:** no template/script changes; no new tokens; do NOT touch
  App.vue (already capped) or other components; do NOT roll in SL-09
  (font-size:11px) — separate task.
- **Disjoint?** yes — this task is a pure CSS substitution in a single file
  with no producer-side dependency (the `--ch-weight-semibold` token already
  exists from the initial token scale).

### Task B — SL-04: App.vue shell chrome focus-visible (single file)

- **Owning file:** `frontend/src/App.vue`
- **Concrete edits:** add `:focus-visible` ring rules for the 6 shell-chrome
  interactive surfaces:
  - `.auth-error-banner__retry:focus-visible`, `.auth-error-banner__close:focus-visible`
  - `.mode-button:focus-visible`
  - `.theme-switch-label:focus-visible`, `.theme-switch-thumb:focus-visible` (thumb
    is focusable via the label; ensure the label carries the ring when the
    hidden checkbox is focused)
  Use `outline: 2px solid var(--ch-color-accent-ring-strong); outline-offset: 2px;`
  matching AWV's existing treatment. Do NOT remove existing `:hover` / `.active`
  rules; keyboard focus must be *additive*.
- **Validation:** `pnpm lint`, `pnpm build`, dev smoke with keyboard Tab to
  walk mode buttons, theme switch, and auth-banner buttons; ring should appear
  only on keyboard focus (not on pointer click if implemented as `:focus-visible`).
- **Scope guard:** CSS-only; do NOT change colors/sizes/spacing; do not add
  shadows; do not touch AWV/TabBar/MobileControls (those are SL-08, separate).
- **Disjoint?** yes — single file, additive rules only.

### Task C — SL-02+SL-03: Wire claude brand + terminal-success chips in ASFP and AWV (tight 2-file set)

- **Owning files:**
  - `frontend/src/components/AgentStatusFloatingPanel.vue`
  - `frontend/src/components/AgentWorkspaceView.vue`
- **Concrete edits (per file, symmetric):**
  1. `.agent-cli[data-kind='claude']` / `.agent-status-cli[data-kind='claude']`:
     - `color: var(--ch-agent-claude-fg);` (replaces `#d97757`)
     - `background: color-mix(in srgb, var(--ch-agent-claude-fg) 18%, transparent);`
       — if the build target does not support `color-mix`, keep the existing
       `rgba(217,119,87,0.18)` literal alongside the fg token swap (low risk,
       just note as a follow-up); DO NOT introduce new build dependencies.
  2. `.agent-cli[data-kind='terminal']` / `.agent-status-cli[data-kind='terminal']`:
     - `color: var(--ch-color-success);` (replaces `#7ee787`, matches tab-indicator)
     - bg either `color-mix(… 16%, transparent)` or keep the `rgba(126,231,135,.16)` literal.
  3. In AWV only, also wire the two brand pills:
     - `.agent-status-master-badge` (L6921) and `.origin-badge` (L7787):
       `color: var(--ch-agent-origin-bg);` (byte-identical to `#c4b5fd`).
     - `.autonomy-badge` (L7752): `color: var(--ch-agent-autonomy-bg);`
       (byte-identical to `#5eead4`).
     - Leave the bg/border rgba literals as-is (no `color-mix` dependency)
       unless the reviewer prefers otherwise.
  4. **Do NOT touch codex teal (#10a37f) or cursor gray (#787878) in this task**
     — those are CLI-semantic colors distinct from the codex brand; SL-13
     covers promoting them to semantic tokens in a later pass. The cursor
     variant is already on surface/text tokens and stays.
- **Validation:** `pnpm lint`, `pnpm build`, dev smoke on both terminal route
  (ASFP visible) and workspace route (AWV visible) in both dark and light
  themes; claude chips should look identical (the token values are
  byte-for-byte the previous hex); terminal chips shift to the success
  family (tiny hue shift acceptable); origin/autonomy pills visually
  unchanged (token value = old hex).
- **Scope guard:** CSS-only; no new token defs (tokens already exist from
  0f036c6); no template/script changes; do NOT bundle SL-01 or SL-09 into
  this task (separate single-file passes).
- **Disjoint?** yes — two files but same edit pattern applied symmetrically;
  no cross-file runtime coupling (both files already have the colors as
  independent hardcoded literals; tokenizing them does not change the
  contract). One worker; one commit.

> **Why these three, in this order?**
> SL-01 is the biggest visual softening (25 weight drops) and the most mechanical
> (single `replace_all` with one exception). SL-04 closes an accessibility gap
> that currently hides shell-chrome focus from keyboard users. SL-02+SL-03 finishes
> the brand-token consumer wiring that was half-done at 30e10b4 (Avatar only) and
> restores the ASFP/AWV chip symmetry that SL-07 calls out. Together they consume
> SL-01, SL-02, SL-03, SL-04, SL-07. The remaining findings (SL-05 input ring
> unification across 3 files, SL-06 auth-banner themed on-warning color, SL-08
> focus-visible long tail across TabBar/MobileControls/ASFP, SL-09 font-size:11px
> bulk, SL-10 small-px rhythm literals, SL-12 ETag on terminal poll, SL-13 CLI
> semantic tokens) are deliberately deferred to §4 so §3 stays at exactly three
> tasks.

---

## 4. Follow-ups (out of scope for Next 3)

These are real but either (a) span >3 files, (b) need a design decision
(e.g. new token introductions, theme decisions), or (c) are lower priority than
§3. Dispatch after §3 lands.

- **SL-05 (input focus ring unification)** — spans EnvPresetManager, TabBar, plus
  TabBar's `.tab-name-input`; 3 files. Switch `:focus` → `:focus-visible` and
  add 2px accent-ring box-shadow. Requires per-file visual check because some
  inputs (e.g. `.tab-name-input` inline rename) use a transparent background
  where a ring changes chromatic weight.
- **SL-06 (auth-banner on-warning color)** — needs a light-theme contrast
  decision; introduce `--ch-color-on-warning` or reference `--ch-color-text-strong`.
  Single file but gated on a design choice rather than mechanical.
- **SL-08 (focus-visible long tail)** — TabBar (17 buttons), MobileControls
  (17 buttons), ASFP agent-rows (~5-10 buttons). 3 files, low difficulty,
  tedious per-control selector crafting; dispatch in parallel if 3 workers
  available.
- **SL-09 (AWV font-size:11px → `var(--ch-font-xs)` bulk)** — 33 sites, requires
  per-line audit to avoid snapping non-xs text; mechanical but high line count.
- **SL-10 (AWV small-px rhythm literals)** — margins/paddings at 3/4/6px and
  mixed `2px 7px` etc.; may warrant a new `--ch-space-chip` token or be left as
  an intentional deviation. Design decision needed.
- **SL-12 (terminalStore ETag/304)** — small TS change, no UI, low risk; good
  first backend/frontend-interface task for a worker comfortable in stores.
- **SL-13 (CLI semantic tokens)** — introduces up to 3 new tokens in App.vue
  `:root` and wires 8 lines across ASFP+AWV. Small but producer-side (adds
  tokens), so sequence after SL-02+SL-03 to avoid token churn.
- **Codex CLI teal semantic decision** — #10a37f is an OpenAI-green that
  historically signals "CLI is alive"; confirm whether to keep it, switch to
  a neutral `--ch-color-cli-active` shared across agent types, or align with a
  future codex brand-palette extension. Out of scope for this doc.
