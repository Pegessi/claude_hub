# Assistant image previews and retention

The user could not open a local screenshot link, and supplied a second Chat
showing `Image unavailable` for `dot-mobile-work.png` and `dot-mobile-home.png`.
This is one image-delivery task, implemented in `codex/chat-image-previews`
from clean local main `a89dd78`. Origin was fetched; the existing main/origin
divergence was preserved. The quota-color change remains a separate branch.

## Evidence and resulting behavior

The supplied events URL returned 404. The current
`/api/workspaces/tabs/{tab_id}/stream/events` route returned all 2165 events
in one page (`has_more=false`). They contain canonical Claude `Read` calls
for `/tmp/dotbm/dot-mobile-work.png` and `/tmp/dotbm/dot-mobile-home.png`.
Both files still existed, but the original image reader returned 404: the UI
recognized Read images while the backend only granted temporary files named
in Markdown image references.

- Parsed Markdown local raster links become inline image previews. Code
  examples and external links keep their meaning. The existing restricted
  URL rewrite, HTML sanitizer and owning tab remain in the rendering path.
- Previews open the shared image viewer. Fit/actual-size controls change CSS
  geometry; Open original uses the same image URL. Escape restores focus,
  Tab remains inside the dialog, and mobile controls stay reachable.
- Canonical assistant Read/view_image events grant only the exact temporary
  file. User text, generic shell/tool output, other sessions, siblings and
  symlink escapes remain denied. Cwd-contained images retain existing access.
- Agent images use the existing attachment store, manifest and quotas. The
  manifest keeps source/content hashes, not source paths; uploaded images
  and agent outputs participate in the same session/global byte and count
  limits and LRU touch/eviction rules. No second retention pool is added.
- Live image output is captured after canonical persistence and before UI
  fanout. Historical output is imported on its first successful image fetch.
  A retained copy remains available after source deletion until LRU/TTL eviction.
- Images are never re-encoded. The existing 10 MiB source-read bound remains;
  files above the cache's configured per-image byte/dimension bounds (default
  8 MiB/8192px) can still display from source without being cached or compressed.
  A previously deleted source that was never captured cannot be recovered.
- A path URL refers to its latest retained contents. Same-source read/save
  is serialized with weakly held per-source locks; cache lookup/read/touch is
  atomic under the existing global lock. Overwritten or uncacheable contents
  invalidate older versions instead of resurrecting them after cleanup.
  Image cache/cleanup I/O failures cannot hide a valid original or turn a
  denied image into an HTTP 500. Retention cannot be guaranteed during a
  storage failure.

## Validation and boundaries

- The supplied Chat's original events were replayed against an isolated state
  root. Both screenshots returned PNG bytes identical to their source files:
  28,352 and 42,571 bytes. Neither the main event log nor source files changed.
- Focused API/store checks cover exact tool grants, source deletion, concurrent
  deduplication and version updates, source-only replacements, storage failure,
  cross-session isolation, and mutual upload/output LRU eviction: 101 passed
  including the non-image publishing regression.
- Standard verification passed backend formatting/types and 3412 default-scope
  tests (7 skipped); frontend lint/types, 836 unit tests and bundle build passed.
  Node 24.21.0, pnpm 9.15.9, Python 3.11.16, uv 0.12.15. The existing bundle-size
  warning remains. Staged safety and documentation invariants passed.
- The real SPA browser image-quality scenario covers local-link previews,
  temporary Read screenshots, missing-image fallback, 1920px original pixels,
  actual-size/fit, Escape/focus, Tab containment and a 390px mobile viewport.
  All nine mocked browser scenarios passed with lifecycle cleanup. Summary:
  `/tmp/claude-hub-browser-mocks.oMwTob/run/summary.json`.
- The new local-link unit test fails when link promotion is removed in a
  transient in-memory candidate. The actual source was not modified for this
  negative control.
- Terminal HMR: 12 passed, no skips. JUnit:
  `/tmp/claude-hub-verify.LcI1Ok/junit.xml`.
- Independent read-only review found four cache update/failure issues; all
  were fixed and independently rechecked. Full verification and staged safety
  results are recorded with the local handoff evidence.

Local evidence is retained in
`/home/tiger/claude_hub_workspace/chat-image-previews/`. Browser APIs were
mocked; the historical service replay used actual supplied event data and
original local files against isolated storage. Neither is a production deploy
or live provider-connectivity test. The browser owner does not claim complete
Chromium process-tree exit; its owned server and browser lifecycle cleanup passed.
No primary Hub restart, main merge, or deployment is part of branch delivery.
