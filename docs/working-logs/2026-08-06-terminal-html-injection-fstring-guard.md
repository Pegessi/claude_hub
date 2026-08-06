# Terminal HTML injection f-string guard

## System overview

Claude Hub serves each ttyd terminal through
`/api/terminal/proxy/{tab_id}/`. For HTML responses, the backend injects one
large script that owns tmux history preload/replay, manual history refresh,
scroll-to-bottom, resize/fit recovery, and terminal viewport protections. The
Vue parent iframe adds input and theme integration, but it cannot replace the
backend history/fit script.

## Failure and diagnosis

The desktop input-row fix in commit `5ff2e4e` added an explanatory JavaScript
comment inside the Python f-string used to build the injected script:

```text
html/body {width:100%;height:100%}
```

Python interpreted the braces as an f-string expression and raised
`NameError: name 'width' is not defined` for every terminal HTML request. The
proxy's defensive injection exception handler then returned the original ttyd
HTML. This made the failure deceptively broad: history preload/replay, manual
refresh handling, activation scroll, ResizeObserver, and delayed fit were all
missing even though the source contained each mechanism.

The live baseline was collected with Playwright against the existing UI at
`http://localhost:5173/`, using a 1038x1320 CSS-pixel viewport and a fresh
browser context. The active iframe rendered xterm and connected its WebSocket,
but its document did not contain `__claudeHubHistoryHooked` or
`__claudeHubRequestFit`; corresponding xterm properties stayed unset. Direct
proxy inspection agreed, and the backend log recorded the exact NameError on
each load.

## Fix and regression boundary

Escape the literal braces as `{{` and `}}`. The regression test calls the full
FastAPI proxy function with a fake ttyd HTML response and consumes the returned
`StreamingResponse`. It asserts that the generated document contains the
history hook, fit hook, refresh message handler, ResizeObserver, and corrected
content length. Testing the endpoint construction path is essential: isolated
JavaScript syntax tests cannot detect Python f-string interpolation failures.

Keep the existing terminal invariants intact:

- Do not restore automatic full-history replay on ordinary agent activation;
  cursor-driven Claude/Codex/Cursor TUIs can be corrupted by mid-redraw
  snapshots.
- Do not resize hidden cached iframes in bulk.
- Preserve user scroll intent and the mobile visualViewport/WebGL paths.
- Keep injection failure visible in logs, but verify the successful output at
  the proxy boundary so future generated-script edits cannot silently disable
  the whole feature set.

## Validation evidence

- Before the fix, Playwright loaded the live app but the active iframe had no
  history/fit markers; the same idle terminal exposed only 84 buffer lines.
- After the fix, the matching `l20 nvshmem debug` Codex session loaded with
  5,164 buffer lines. Real wheel input moved `viewportY` from 5096 to 4885 and
  back to 5096, proving both history and the latest screen were reachable.
- At the bottom, the 68-row xterm screen ended at 1297px inside a 1320px
  viewport; the hidden input helper also ended at 1297px, so the input row was
  not clipped.
- Single-pane, two-column, single-pane restoration, another-tab selection,
  managed-agent reopening, and page reload all retained the injected hooks,
  correct rows/columns, and bottom reachability without using the manual
  refresh button as a recovery step.
- Targeted proxy/unit tests, seven terminal replay E2E cases, the input-latency
  E2E, frontend unit tests, lint, type-check/build, Black, isort, and mypy all
  passed against the feature worktree. The live main backend intentionally was
  not restarted or modified during read-only baseline collection.
