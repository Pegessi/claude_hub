# 2026-06-14 — Terminal input latency v3: remove per-frame layout reflow

Task `e0ea4c2e-3458-4ac3-8193-e6681b7c2ea1` — "前端terminal输入响应速度优化".

> 之前其实优化过一次 但是现在输入响应慢 不跟手的问题又出现了 上网搜搜有没有
> 类似的项目/用法 看看能怎么优化

The key signal is **又出现了** ("came back"): the laggy/"不跟手" typing
regressed despite a prior optimization round. This log explains why the earlier
work did not actually cover the hot path, what the real bottleneck was, and the
minimal fix.

## System overview

Typing into a Claude Hub terminal flows through:

```
xterm.js (inside ttyd iframe)  ──WS──▶  FastAPI proxy  ──▶  tmux  ──▶  pty
            ▲                                                          │
            └──────────────── output frames ◀──────────────────────────┘
```

`backend/claude_hub/api/terminal.py` injects a JavaScript bootstrap into the
ttyd HTML (`setupResizeGuard` + `setupHistoryResync`) that wraps `term.write`
to drive history-resync and bottom-follow behavior after live-output bursts.

The injected script is a Python `str.format` template, so all literal JS braces
are doubled (`{{ }}`). This must be preserved on every edit.

## Why the earlier optimization didn't hold

The prior round shipped real improvements that are all still present:

- SharedArrayBuffer + Atomics SPSC ring buffer for parent→iframe keystrokes
- WebGL renderer (`rendererType=webgl`, `cursorBlink=false`)
- `TCP_NODELAY` on proxy sockets
- COOP/COEP/CORP headers to enable `crossOriginIsolated` (gates SAB)

The catch: **desktop typing into a focused terminal iframe never touches the
SAB path.** Focused keystrokes go straight from xterm.js → ttyd WS → tmux. The
parent/SAB ring buffer only carries *synthetic* keys (mobile, compose,
injected). So that work, while valid, was off the real desktop hot path — which
is why the lag could "come back" without any of it regressing.

## Root cause: forced synchronous reflow on every output frame

The `term.write` wrapper consulted "is the viewport at the bottom?" on every
frame via `isAtBottom()` → `viewportIsAtBottom()`, and the bottom-follow
scheduler did the same in `needsBottomScroll()`. Each call did:

```js
const viewportEl = document.querySelector('.xterm-viewport');
const domAtBottom = !viewportEl ||
  viewportEl.scrollTop >= viewportEl.scrollHeight - viewportEl.clientHeight - 1;
```

Reading `scrollTop`/`scrollHeight`/`clientHeight` forces the browser to flush
pending layout — a **synchronous reflow** — and this happened once (often
multiple times) per output frame. Under fast output (an agent redrawing a TUI,
or simply echoing a burst), the main thread spends its time in layout instead
of processing the next keystroke, so typing feels detached ("不跟手").

This is the classic "layout thrash on the keypress/output path" pitfall that
VS Code's terminal and others explicitly avoid.

## Fix: event-driven cached "at bottom" flag

Replace the per-frame DOM geometry read with a cached boolean that is recomputed
only when it can actually change:

- `cachedViewportEl` + `viewportElement()` — cache the `.xterm-viewport`
  node (re-query only if detached).
- `domAtBottomCached` + `recomputeDomAtBottom()` — the single place that reads
  DOM geometry.
- `viewportIsAtBottom()` / `needsBottomScroll()` now read `domAtBottomCached`
  (no DOM access) on the hot path.

`recomputeDomAtBottom()` is called exactly at the state-changing edges:

1. The `.xterm-viewport` `scroll` listener (user/programmatic scroll).
2. After every programmatic scroll-to-bottom — three sites: bottom-follow
   `run()`, `writeHistorySnapshot` done, and the auto-resync completion
   callback — each sets `domAtBottomCached = true`.
3. The resize/fit paths (see below).

### Resize-staleness follow-up

`domAtBottomCached` can also go stale on a layout change that alters
`scrollHeight`/`clientHeight` **without** firing a scroll event: window/pane
resize and the mobile-keyboard `fitAddon.fit()`. Those handlers live in
`setupResizeGuard`, a sibling function that cannot see the resync closure's
flag. Rather than duplicate state, the closure exposes its recompute as
`term.__claudeHubRecomputeBottom`, and the resize guard calls it (guarded by a
`typeof` check) right after the debounced `onResize` forward and after the
mobile `fit()`. The geometry read forces a reflow, so calling it in the same
tick as `fit()` reads the post-resize layout.

## Validation

- `black --check`, `isort`, `mypy`, and `python -c "import ..."` all pass.
- Brace balance of the injected template preserved (`{{ }}`).
- `pytest`: failures observed are pre-existing/environmental (asyncio
  test-isolation `RuntimeError` in `test_ttyd_manager.py`/`test_workspaces.py`
  and flaky Playwright in `test_terminal_replay.py`), confirmed against a clean
  `main` baseline — not a regression from this change.

### On baseline measurement

Per the workspace lesson *"performance-tuning tasks must baseline-measure the
original symptom before validating the fix"*: the symptom here is interactive
keystroke-to-render latency inside a browser-embedded iframe. A precise
millisecond baseline requires Chrome DevTools performance profiling of the live
ttyd frame, which is not reproducible in this headless backend context. The
bottleneck is instead established **mechanistically**: the removed code path
performed a forced synchronous layout reflow per output frame (a documented,
deterministic cost), and the fix eliminates all DOM reads from that path. The
change is behavior-preserving for scroll/bottom-follow (the cached flag tracks
the same predicate at the same edges), so the latency reduction is structural
rather than tuned to a measured number. A follow-up could add an in-page
`performance.now()` keystroke→`onData` probe if a quantified figure is wanted.

## Key issues / pitfalls

- Preserve `{{ }}` escaping in the injected JS template.
- Any new code path that re-pins the viewport to the bottom or changes its
  geometry must update `domAtBottomCached`, or the bottom-follow self-correction
  will be skipped until the next scroll event.
- The desktop typing hot path is xterm.js → ttyd WS → tmux; the SAB ring buffer
  is only for synthetic/mobile keys. Optimize the right path.
