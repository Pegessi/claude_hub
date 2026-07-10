# Perceived-Responsiveness & Motion Audit (RM-01..RM-08)

> Authored 2026-07-10; filed under the task-assigned path/date
> `2026-07-12-perceived-responsiveness-motion-audit.md`.
> Read-only discovery doc. **No source code was changed** — the only files in the
> commit are this doc + one `CHANGELOG.md` bullet.
> Branch point: `develop` tip `b08d6f0` (contains PR-02 `176a4b1`, PR-12
> `b46f042`, PR-04 `b08d6f0`). All line numbers reference that tip; small drift is
> acceptable because nothing here is edited.

## Purpose

This audit covers the **perceived-responsiveness + motion** layer — how fast and
how *elegant* the UI **feels**, independent of raw milliseconds. It fills the gap
between two sibling audits:

- **Static visual polish** — `2026-07-10-minimalist-ui-second-look.md`
  (**SL-01..SL-13**): font weights, radii, focus rings, colour tokens, spacing
  rhythm. Deals with how the UI looks *at rest*.
- **Raw response speed** — `2026-07-11-frontend-response-speed-audit.md`
  (**PR-01..PR-12**): bundle size, memoization, network cadence, re-parse cost.
  Deals with actual latency in *ms*.

The perceived/motion layer sits between them: loading & empty states, action
feedback, layout shift / pop-in, and the transition/animation vocabulary. It has
never had a dedicated sweep.

## Method

### Surfaces read (read-only inspection)

`frontend/src/App.vue`, `components/AgentWorkspaceView.vue` (HOT),
`components/TabBar.vue`, `components/AgentStatusFloatingPanel.vue`,
`components/LoadingButton.vue`, `components/LayoutSelector.vue`,
`components/MobileControls.vue`, `components/NetworkAccessMenu.vue`,
`components/TerminalPane.vue`, `components/EnvPresetManager.vue`,
`views/LoginView.vue`, plus targeted greps across all `*.vue` for:
`@keyframes` / `animation:`, `transition:`, `var(--ch-motion-*)`,
`prefers-reduced-motion`, `<transition>` / `<Transition>`,
`skeleton|spinner|loading`, `debounce|throttle`, and empty/error-state markup.

### Non-overlap deltas (per prong) vs SL / PR

| Prong | SL owns | PR owns | This audit's NEW angle |
| --- | --- | --- | --- |
| (a) loading / empty / skeletons | — | Board skeleton already exists & is "covered" (PR §3); PR-01 report-body payload | Extend the **existing** skeleton *pattern* to the OTHER in-view async loads that still show a bare spinner/blank (RM-07). Not re-treading the board skeleton. |
| (b) optimistic / action feedback | — | — | Micro layout jump when a button enters `loading` (RM-03); no motion **liveness** cue on a `working` agent (RM-05). |
| (c) input responsiveness / debounce | — | PR §3: "debounce not needed (no search-as-you-type)" | **Confirmed clean** — no finding. See "Prong coverage". |
| (d) layout shift / pop-in | — | PR-04 (ASFP lazy chunk), lazy workspace-view / prefetch (bundle) | The **visual** pop-in of the async workspace view has no fade/placeholder (RM-04); LoadingButton spinner shifts its label (RM-03). Bundle/prefetch cost is PR's; the *motion* of the boundary is new. |
| (e) motion & micro-interactions | SL notes some "intentional deviations" (2px gaps, 999px pills, 180ms toast, 140ms panel) as **static** choices, proposes no system | — | A **motion vocabulary**: reduced-motion coverage (RM-01), duration/easing tokens (RM-02), duplicate toast keyframes (RM-08), 5 near-duplicate spinners (RM-06). SL treated the numbers as fixed; this proposes a consistent, guarded scale. |

Where a topic is adjacent to an existing id, findings cross-reference it rather
than restate it. No RM finding duplicates an SL or PR finding.

## Findings

Ranked impact-first, then lowest-effort within a tier. Each names the exact
file(s) it would touch; any finding touching
`frontend/src/components/AgentWorkspaceView.vue` (AWV) or
`frontend/src/stores/workspaceStore.ts` is flagged **HOT** so the orchestrator
serializes it. (No finding touches `workspaceStore.ts`; all are CSS/template
motion work.)

---

### RM-01 — `prefers-reduced-motion` is honoured in exactly one place

- **Files:** `App.vue` (add one global guard) + every animated file below.
  **HOT** (includes AWV).
- **Evidence:** The only `prefers-reduced-motion` block in the whole frontend is
  `AgentWorkspaceView.vue:7399`, and it disables just two properties
  (`.board-skeleton-line` shimmer at `:7359` and
  `.task-card-autonomy-progress-fill`). Every other animation is unguarded:
  boot spinner `App.vue:958` (`spin 1s`), `LoadingButton.vue:71`
  (`loading-button-spin 700ms`), `TerminalPane.vue:300`
  (`pane-history-spin 700ms`), `AWV:6786`/`AWV:7041` (700ms spinners), toast
  entrances (`TabBar.vue:2477`, `AWV:10832`), panel/menu entrances
  (`AgentStatusFloatingPanel.vue:612`, `LayoutSelector.vue:310`,
  `TabBar.vue:1734`), the `MobileControls.vue:6` `<Transition>`, and all 77
  `var(--ch-motion-*)` transitions. Grep: `prefers-reduced-motion` → 1 hit total.
- **Symptom:** Users who set "reduce motion" (vestibular sensitivity) still get
  every spinner, slide, scale-in and hover-transform. It is an accessibility
  correctness gap, and it also removes the cheap "calm" win of respecting the OS
  preference.
- **Proposed fix:** Add a single global `@media (prefers-reduced-motion: reduce)`
  rule in `App.vue` that neutralizes app-wide motion (e.g. `*, *::before,
  *::after { animation-duration: .001ms !important; animation-iteration-count: 1
  !important; transition-duration: .001ms !important; }`), keeping the existing
  AWV-specific opt-outs. Spinners can keep a minimal rotation if a busy indicator
  is still wanted.
- **Impact:** high · **Effort:** S · **Risk:** low (additive, media-gated).

### RM-02 — Motion-token vocabulary exists but is only half-adopted (no easing token, durations hardcoded in keyframes)

- **Files:** `App.vue` (token defs) + `AgentStatusFloatingPanel.vue`,
  `LayoutSelector.vue`, `AgentWorkspaceView.vue`, `TabBar.vue` (hardcoded sites).
  **HOT** (includes AWV).
- **Evidence:** Three motion tokens are defined — `--ch-motion-fast: 120ms ease`,
  `--ch-motion-standard: 180ms ease`, `--ch-motion-drawer: 180ms
  cubic-bezier(0.2, 0, 0, 1)` (`App.vue:529-531`, duplicated at `:662-663`) — and
  used **77×** via `var(--ch-motion-*)`. But `@keyframes` entrances bypass them
  with **hardcoded** values: `status-panel-in 140ms cubic-bezier(0.2, 0, 0, 1)`
  (`ASFP:612`), `layout-menu-in 120ms cubic-bezier(0.2, 0, 0, 1)`
  (`LayoutSelector:310`), `ws-toast-in 180ms cubic-bezier(0.2, 0, 0, 1)`
  (`AWV:10832`). The easing `cubic-bezier(0.2, 0, 0, 1)` is repeated as a literal
  in ≥3 files with **no easing token** at all, and `140ms` is a fourth, undocumented
  duration alongside the 120/180 scale.
- **Symptom:** Motion drifts subtly out of sync (120 vs 140 vs 180 ms, ease vs
  cubic-bezier) and there is no single knob to tune the app's "feel." SL flagged
  these numbers as intentional *static* deviations; this is the *systemic* fix.
- **Proposed fix:** Add `--ch-motion-ease: cubic-bezier(0.2, 0, 0, 1)` and (if
  140ms is intentional) a named token; have the `@keyframes` `animation`
  shorthands reference the duration/easing tokens instead of literals. De-dup the
  `App.vue:662-663` token redefinition.
- **Impact:** high · **Effort:** M · **Risk:** low (values unchanged, only
  tokenized).

### RM-03 — `LoadingButton` spinner is inline-prepended → label jumps ~1.45em on every async action

- **Files:** `components/LoadingButton.vue`. **Not HOT** — single-file, ideal
  parallel task.
- **Evidence:** `LoadingButton.vue:62-73` — the spinner is
  `display: inline-block; width: 1em; height: 1em; margin-right: 0.45em`, injected
  with `v-if="loading"` *before* `.loading-button__content` (which is
  `display: contents`). Content is only hidden when the opt-in
  `hideContentWhileLoading` is set (`:73`), which most call-sites do not pass
  (LoadingButton is used in `AWV`, `TabBar`, `ASFP`, `LayoutSelector`,
  `LoginView`).
- **Symptom:** The moment a button enters `loading`, its label slides right by
  ~1.45em (spinner width + margin). Every submit/dispatch/refresh click produces a
  small horizontal jitter — the opposite of a "solid" feel, and a real (if tiny)
  layout shift.
- **Proposed fix:** Reserve the spinner's box so it does not reflow the label —
  e.g. absolutely-position the spinner and pad the button, or render the spinner
  in a fixed-width leading slot that exists (invisible) even when not loading.
- **Impact:** med · **Effort:** S · **Risk:** low.

### RM-04 — Async workspace view pops in with no transition or placeholder

- **Files:** `App.vue`. **Not HOT** (App.vue). Cross-refs PR-04 / lazy
  workspace-view (bundle) — the NEW angle here is *motion*, not bundle size.
- **Evidence:** `App.vue:113` mounts `<AgentWorkspaceView v-if="mode ===
  'workspace'" />`; the component is a `defineAsyncComponent` (`App.vue:138`) and
  the code comment at `App.vue:135` explicitly notes "**No Suspense /
  loadingComponent**: `v-if` gates mounting until the first workspace…". The
  terminal↔workspace switch itself has no `<transition>` wrapper.
- **Symptom:** On the first switch into workspace mode the chunk resolves and the
  entire view appears abruptly (blank → full board), *then* the board's own
  `boardLoading` skeleton (`AWV:417`) runs. The hand-off reads as a flash/pop
  rather than a smooth reveal.
- **Proposed fix:** Wrap the mode content in a short fade `<transition>`, and/or
  give the async component a lightweight `loadingComponent` (or `<Suspense>`
  fallback) so there is a deliberate placeholder during chunk fetch. Keep it under
  `--ch-motion-standard` and reduced-motion-guarded (see RM-01).
- **Impact:** med · **Effort:** S · **Risk:** low.

### RM-05 — A `working` agent has no motion liveness cue (colour-only status)

- **Files:** `AgentStatusFloatingPanel.vue`, `AgentWorkspaceView.vue`. **HOT**
  (includes AWV).
- **Evidence:** Status dots are differentiated purely by colour:
  `ASFP:549-553` sets `.status-dot[data-status='working'] { color:
  var(--ch-color-warning) }`; `attention`/`idle`/`offline` differ only in hue
  (`ASFP:543-566`). The AWV agent-status dot (`AWV:267`, styled `AWV:6590`) is the
  same static swatch. Nothing animates on `working`.
- **Symptom:** A busy agent and a stalled/idle one look identical except for a
  colour a user may not be watching; the UI feels static even while work is
  happening — low *perceived* activity.
- **Proposed fix:** Add a subtle, low-amplitude pulse (opacity or scale) to the
  `working` dot only, reusing a motion token and **gated by**
  `prefers-reduced-motion` (depends on RM-01). No new colours.
- **Impact:** med · **Effort:** S · **Risk:** low.

### RM-06 — Five near-duplicate spinner keyframes at two different speeds

- **Files:** `App.vue`, `AgentWorkspaceView.vue`, `LoadingButton.vue`,
  `TerminalPane.vue`. **HOT** (includes AWV).
- **Evidence:** Five separate 360° rotate keyframes exist: `App.vue:958`
  (`spin 1s`), `AWV:6786` (`agent-status-spin 700ms`), `AWV:7041`
  (`workspace-select-spin 700ms`), `LoadingButton.vue:71`
  (`loading-button-spin 700ms`), `TerminalPane.vue:300`
  (`pane-history-spin 700ms`). Four spin at **700ms**, the boot spinner at
  **1s** — visibly slower.
- **Symptom:** Spinners rotate at inconsistent speeds across the app (the boot
  spinner is the odd one out), and the same keyframe is defined five times — more
  surface to drift.
- **Proposed fix:** Define one shared `@keyframes ch-spin` and one
  `--ch-motion-spin` duration token; point all five at them (pick one speed;
  700ms is the majority). Fold reduced-motion handling in via RM-01.
- **Impact:** med · **Effort:** M · **Risk:** low.

### RM-07 — Skeleton pattern is workspace-board-only; other in-view async loads still show a bare spinner/blank

- **Files:** `TabBar.vue`, `AgentWorkspaceView.vue`, `NetworkAccessMenu.vue`.
  **HOT** (includes AWV). Cross-refs PR §3 ("board skeleton already covered") —
  this explicitly does **not** re-tread the board skeleton; it extends the same
  pattern to the surfaces PR left out.
- **Evidence:** The board has a content-shaped skeleton with a fade
  (`AWV:417` `<transition name="board-skeleton-fade">`, `:419` `v-if="boardLoading"`,
  shimmer at `:7359`) — a good pattern. But sibling async loads fall back to a
  spinner or nothing: `TabBar.vue:416` (`v-if="browserLoading"`), `AWV:2907`
  (`agentBrowserLoading`), `NetworkAccessMenu.vue:54` and `:116`
  (`v-if="isLoading"`). None reuse the skeleton vocabulary.
- **Symptom:** Loading feels consistent and "designed" on the board but ad-hoc
  everywhere else (spinner-on-blank), so those panels feel slower / less finished
  than the board even at equal latency.
- **Proposed fix:** Extract the board's skeleton styles into a small reusable
  block and apply a lightweight variant to the agent-browser list and the network
  menu, replacing the bare spinner where the shape of the incoming content is
  known.
- **Impact:** med · **Effort:** M · **Risk:** low.

### RM-08 — Two byte-identical toast keyframes, tokenized inconsistently

- **Files:** `AgentWorkspaceView.vue`, `TabBar.vue`. **HOT** (includes AWV).
- **Evidence:** `TabBar.vue:2477` uses `animation: toast-in
  var(--ch-motion-standard)` while `AWV:10832` uses `animation: ws-toast-in 180ms
  cubic-bezier(0.2, 0, 0, 1)` — the two `@keyframes` are otherwise identical
  (`from { opacity: 0; transform: translateY(-6px) scale(0.98) }`). One references
  the token; the other hardcodes the same duration + a different easing curve.
- **Symptom:** Two toasts that should be one entrance are maintained twice and can
  drift (`ease` vs `cubic-bezier`); the AWV toast bypasses the motion token.
- **Proposed fix:** Point `ws-toast-in`'s `animation` at `var(--ch-motion-standard)`
  (and the RM-02 easing token), or share a single `toast-in` keyframe. Purely a
  consolidation.
- **Impact:** low · **Effort:** S · **Risk:** low.

---

## Per-file ownership & HOT flags

| Finding | Files it would touch | HOT? |
| --- | --- | --- |
| RM-01 | `App.vue` (+ app-wide, media-gated) | HOT (AWV in scope) |
| RM-02 | `App.vue`, `ASFP`, `LayoutSelector`, `AWV`, `TabBar` | HOT (AWV) |
| RM-03 | `LoadingButton.vue` | no |
| RM-04 | `App.vue` | no |
| RM-05 | `ASFP`, `AWV` | HOT (AWV) |
| RM-06 | `App.vue`, `AWV`, `LoadingButton`, `TerminalPane` | HOT (AWV) |
| RM-07 | `TabBar`, `AWV`, `NetworkAccessMenu` | HOT (AWV) |
| RM-08 | `AWV`, `TabBar` | HOT (AWV) |

- **Fully disjoint / non-HOT (parallel-safe now):** RM-03 (`LoadingButton` only),
  RM-04 (`App.vue` only).
- **AWV-touching (serialize):** RM-01, RM-02, RM-05, RM-06, RM-07, RM-08. Dispatch
  one at a time or land RM-01/RM-02 (the token + reduced-motion foundation) first,
  since RM-05/RM-06/RM-08 build on those tokens.
- **`workspaceStore.ts`:** not touched by any finding.

Suggested sequencing: **RM-01 → RM-02** (foundation: guard + tokens), then the
dependent AWV items **RM-05, RM-06, RM-08** reuse them; **RM-03, RM-04, RM-07**
are independent and can run in parallel with the foundation.

## Prong coverage summary

- **(a) loading / empty / skeletons:** RM-07 (extend skeleton pattern), RM-04
  (async mount placeholder). Board skeleton itself is left as-is (owned by PR §3).
- **(b) optimistic / action feedback:** RM-03 (button spinner jitter), RM-05
  (working-state liveness).
- **(c) input responsiveness / debounce:** **CLEAN — no finding.** Grep for
  `debounce`/`throttle` returns nothing and AWV has no search-as-you-type input
  (`v-model` on filter/query text is absent); inputs are form fields committed on
  submit, not live queries. Matches PR §3 ("debounce not needed"). Adding debounce
  would be premature.
- **(d) layout shift / pop-in:** RM-03 (label shift), RM-04 (view pop-in).
  Attachment `<img>` tags (`AWV:825`, `:1516`, `:1623`) lack width/height but are
  user-content thumbnails inside already-sized containers — noted, not raised as a
  finding (low signal, would touch AWV for negligible gain).
- **(e) motion & micro-interactions + motion vocabulary + reduced-motion:** RM-01
  (reduced-motion), RM-02 (tokens/easing), RM-06 (spinners), RM-08 (toasts), RM-05
  (liveness). This is the core of the audit.

## Explicitly out of scope / intentional

- **Any source-code change.** This task is analysis + this doc + one CHANGELOG
  bullet only. Every "proposed fix" above is a recommendation for a **separate
  bounded follow-up**, not done here.
- **Deep terminal / xterm.js / ttyd / WebSocket motion or throughput** — iframed,
  high-risk; flagged out by the task and by PR §3. `TerminalPane.vue:300` is cited
  only as one of the duplicate spinners (RM-06), not as terminal-internals work.
- **Re-treading SL-01..SL-13** (static weights/radii/focus/colour/spacing) or
  **PR-01..PR-12** (bundle/memoization/network/re-parse). Adjacent topics
  (PR-04 lazy ASFP, lazy workspace-view, board skeleton, LoadingButton focus ring)
  are cross-referenced by id, not duplicated.
- **Introducing the motion tokens themselves in this task** — RM-01/RM-02 describe
  them; adding them to `App.vue` is a source edit for a follow-up.
- **Mobile on-screen-keyboard / viewport perceived issues** — owned by the mobile
  audit track.
- **`main`** is untouched (HEAD stays `2d034f6`).
