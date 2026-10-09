# Feishu header controls

## Problem

The Chat session-name pill belongs to `TerminalPane` and was positioned at the
pane's upper-right edge. The Feishu status trigger belonged to the nested
`StructuredPane` and independently claimed the same corner. Because the two
controls had separate absolute-positioning contexts, the Feishu icon could
stack over the name pill. Their 32 px versus approximately 21 px geometries
also made the overlap look like an unrelated floating control.

## Change

`TerminalPane` now owns both pieces of Chat pane chrome. It renders the Feishu
binding component immediately after the session-name pill in the existing flex
row, while Terminal sessions continue to render their reconnect action there.
`StructuredPane` no longer positions a second control over that row.

The pane chrome defines one 24 px control size. The session label, reconnect
action, and Feishu action use that height; the Feishu action also shares the
name pill's fully rounded raised surface, muted border, shadow, and five-pixel
row gap. The status dot, tooltip, accessible status label, pairing behavior,
and popover remain intact. Changing the pane's Chat closes any open popover
before the binding composable reconciles the new tab.

## Verification

- The complete frontend unit suite passed: 701 tests.
- `pnpm lint:check`, `pnpm exec vue-tsc --noEmit`, and `pnpm build` passed. The
  build retained the repository's existing warning for a chunk above 500 kB.
- The real Vue settings and binding components ran in Chrome against an owned
  Vite server on port 5297, with all HTTP and WebSocket traffic mocked. The
  full Bot/pairing flow completed with ten expected API calls, no blocked or
  unknown requests, and no page errors.
- Desktop (1280 px) and mobile (390 px) checks assert that the name and Feishu
  controls are parallel rather than overlapping: both are 24 px high, share
  the same top edge, use a five-pixel gap, and keep the Feishu action at the
  right edge. Screenshots and the machine-readable report are under
  `/tmp/feishu-header-controls-browser`.

The first browser pass caught a line-box baseline offset around the otherwise
24 px Feishu trigger. Giving the component root an explicit flex height removed
the offset; the exact geometry assertion remains as regression coverage. This
is frontend-only and does not validate live Feishu credentials or callbacks.
