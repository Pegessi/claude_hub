# Terminal display fixes: selection alignment, mobile selection, tab-switch scroll

Date: 2026-07-24
Branch: `fix/terminal-display-selection-scroll`
Task: "terminal显示优化" (3c4b8920)

## Problems (reported)

1. **Desktop selection drift** — dragging to select terminal text: the mouse
   position and the displayed/selected position "串行" (drift apart), worse the
   further down you go.
2. **Mobile cannot long-press to select** terminal text.
3. **Tab switch shows history** — occasionally, switching to a terminal shows
   scrollback history instead of the bottom prompt.

## Reproduction & root cause (evidence-based)

Reproduced against a running instance with Playwright (`sync_api`), driving the
ttyd proxy iframe directly and inspecting the xterm.js internals.

### #1 Desktop selection

- The selection **model** is always correct: `term.getSelection()` returns
  exactly the text under the pointer with **0 drift** across
  `devicePixelRatio` 1 / 1.25 / 1.5 / 2, both at-bottom and scrolled, with
  `.xterm-screen` `pointer-events` `none` or `auto`. So the *copied text is
  correct* (confirmed with the user).
- The visual drift is the **xterm.js v4 WebGL renderer selection-highlight
  bug** (xterm.js issue #5198). In WebGL mode there is **no `.xterm-selection`
  DOM node** — the highlight is painted on the WebGL canvas — so the common
  `.xterm-selection { overflow: hidden }` CSS workaround does not apply, and it
  cannot be fixed from the injection layer.
- ttyd 1.7.7 bundles xterm.js v4 and cannot be upgraded here. The only lever is
  the renderer. The user approved switching WebGL → canvas for correct
  selection (perf acceptable).

### #2 Mobile selection

- `terminal.py` injected `.xterm-screen { pointer-events: none !important }`
  **unconditionally** (intended for mobile inertial-scroll passthrough), and
  `.xterm { user-select: none }` (xterm default). Together they disable touch
  selection. xterm.js v4 also has **no native touch text-selection** (touch
  only scrolls).

### #3 Tab-switch scroll

- `TerminalView.vue` posts `terminal-scroll-bottom` at fixed delays
  (rAF/120/360 ms). The iframe `scrollBottomWhenReady` scrolled once the term
  existed and returned — it did **not** verify the view actually reached the
  bottom, so if the term was still replaying/settling the scroll was undone and
  the view stuck mid-history.

## Fixes

### #1 — `rendererType` webgl → canvas (`ttyd_manager.py`)

- Canvas renderer paints selection on its own aligned `xterm-selection-layer`
  (2D). Verified: highlight visible and aligned with text; `getSelection` drift
  0.
- SAB input fast path is renderer-independent → input latency unchanged.
- `.xterm-screen { pointer-events: none }` scoped to `@media (pointer: coarse)`
  so desktop mouse keeps interacting with the screen element.

### #2 — mobile "select text" mode (`terminal.py` + `MobileControls.vue` + `TerminalView.vue` + `types`)

- MobileControls "选择" toggle → `window.__claudeHub.setTerminalSelectMode` →
  posts `terminal-select-mode` to the active iframe.
- Injected handler: while active, capture-phase non-passive touch listeners on
  `.xterm` translate single-finger touches to a selection via the selection
  **model** (`_selectionService._model.selectionStart/End` + `refresh()`),
  because **xterm.js v4 ignores synthetic `MouseEvent`s** (verified: real CDP
  mouse selects, `new MouseEvent`+`dispatchEvent` does not, on any element).
  Endpoints are ordered so upward drags work; the focused column is inclusive.
- On lift: copy `getSelection()` via `navigator.clipboard` (fallback
  `execCommand('copy')`), post `terminal-select-copied` → MobileControls toast
  ("已复制"/"无选择"/"复制失败"). Turning the mode off clears the selection and
  restores the coarse-pointer passthrough scroll.
- `html[data-select-mode='on']` flips `.xterm-screen` back to
  `pointer-events: auto` while selecting.

### #3 — robust scroll-to-bottom (`terminal.py`)

- `scrollBottomWhenReady(attemptsLeft, settleTries, lastScrollHeight)`:
  waits while `__claudeHubReplayBuffering`, scrolls, then treats "settled" as
  *visually at bottom* (`viewportY >= baseY` and viewport within 2px of bottom)
  **and** `scrollHeight` stable across two ticks, retrying up to ~12×60 ms
  otherwise. Scroll-only; never reintroduces history replay.

## Verification

- Isolated second backend (`HOME=/tmp/ch_test_home PORT=8174
  TTYD_BASE_PORT=11000`) so the live workspace `~/.claude_hub` state was never
  touched. Playwright drove the real modified injection + canvas ttyd.
  - #1: canvas `xterm-selection-layer` present; highlight aligned + visible
    (screenshot); selection drift 0.
  - #2: mobile (`is_mobile`+`has_touch`) touch-drag in select mode produced a
    correct multi-row selection + visible highlight + copy; off-mode did not
    select.
  - #3: 5/5 tab switches landed at the bottom; settled at bottom while output
    streamed.
- Perf A/B (standalone ttyd, SwiftShader): canvas ≈ webgl, median 20 ms vs
  19 ms for a 4000-line write+render burst. (Headless SwiftShader does not show
  WebGL's real-GPU advantage under sustained load, but absolute canvas cost is
  small and ample for agent TUIs.)
- `black`/`isort`/`mypy` clean; frontend `lint` + `build` (vue-tsc) pass;
  backend `pytest` 571 passed (CI-style, E2E replay/latency excluded as CI
  does).

## Caveats / follow-ups

- Playwright **headless cannot simulate inertial touch scroll**
  (`docs/terminal-debugging.md`), so mobile off-mode scroll and inertia should
  be confirmed on a real device.
- Mobile select mode does not yet auto-scroll when dragging past the top/bottom
  edge; selection is bounded to the visible viewport per gesture (turn off the
  mode to scroll, then re-select). Reasonable MVP; can be extended later.
- If real-GPU WebGL throughput ever proves necessary for a specific
  high-output workload, revisit whether the selection bug is fixable another
  way (e.g., an xterm.js bump) rather than reverting the renderer.
