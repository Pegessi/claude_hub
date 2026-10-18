# Markdown reader cross-validation and UI refinement

## Scope and revisions

User follow-up: independently cross-validate the delivered reader and improve
reading efficiency, visual hierarchy and responsive presentation.

- Reviewed delivery: `5bd8aa8`, relative to original base `5e3da89`.
- Refined implementation continues in the session-local
  `markdown-preview/repo` checkout on `feat/chat-markdown-preview`.
- Read-only integration comparison used main `3ae9132`: affected product files
  apply cleanly across the lazy-history changes; `CHANGELOG.md` requires manual
  textual integration. No merged candidate or shared deployment was executed.

## Cross-validation findings and fixes

The independent review identified two reproducible issues in the prior delivery:

1. An anchor inside HTML `select` content could grant an outside-cwd temporary
   pathname even though browser HTML parsing drops the anchor. Four real HTTP
   regressions returned 200 before the fix and now return the uniform 404. The
   reference collector now treats select content as excluded, including option,
   self-closing and embedded-Markdown forms.
2. Sanitized raw SVG anchors reached the Markdown link rewriter, whose HTML-only
   `.href` and `.target` assignments throw for readonly SVG animated properties.
   A real Chromium probe reproduced `TypeError`; rewriting now uses attributes.
   The new browser scenario renders the SVG anchor, checks its scope/target,
   and verifies the following document content is still present.

The review also found a mobile loading/error layout issue: without the source
button, Refresh occupied the flexible middle column. Its explicit final-column
placement is now checked in both two-action states.

A reported baseline formatting failure was retracted after checking the exact
immutable file with the repository's 100-column Black configuration; it came
from a dirty-tree race and a default-88 stdin probe, not the delivered commit.

## UI choices and efficiency

- Center a 960px document rail with 72ch prose; retain wide table/code/media
  space. Reader-only typography is 16px/1.7, with 28/22/18px heading levels.
  Chat and Workspace Markdown defaults are unchanged.
- Keep one quiet masthead: filename, selectable path and three native controls.
  Source view is the bordered action; Back and Refresh remain secondary.
  Mobile controls are 44px tall with visible keyboard focus.
- Improve error/loading/source spacing and enlarge document images beyond the
  Chat thumbnail cap. Use existing theme tokens, without new fonts, icons,
  gradients, cards around normal prose, sticky chrome or UI dependencies.
- Keep the parsed article mounted with `v-show` across source switches. Source
  is still exact escaped text; toggles make no additional document request.
- Select reader versus full App through conditional imports. The main entry
  eagerly imports the shared theme; App and reader use the same plain CSS module
  identity, preserving direct-component styling without duplicate theme output.
  The final production assets contain the dark/light theme definitions only in
  `index-sNFwUxqG.css` (two expected declarations, no duplicate file).

### Reproducible local measurement

Harness: `ui-refinement/measure_reader.py`; built SPA served by a task-owned
loopback static server, document API mocked, external font stylesheet stubbed
so both runs use system fonts. The fixture is **152,898 bytes**, 80 sections;
**five fresh browser contexts**, five source/preview cycles per context.

| Metric | Prior delivery | Refined reader |
| --- | ---: | ---: |
| JavaScript response bodies, uncompressed | 971,675 B | 342,640 B |
| Same bodies gzip-compressed for comparison | 304,389 B | 120,401 B |
| Source → preview median | 80.1 ms | 43.8 ms |
| Initial load/render median | 278.7 ms | 248.5 ms |
| Same heading DOM node retained | No | Yes |

The uncompressed JS reduction is **64.7%**. These are local synthetic browser
measurements, not production-network or provider latency guarantees. The
same historical transcript scan limitation for outside-cwd temp files remains;
this UI change does not add a persistent grant index.

## Validation and evidence

- `./scripts/verify.sh all`: PASS; backend **4386 passed, 10 skipped**;
  frontend **1034 passed**; mypy **536 files**; lint, formatting, Vue types,
  document/safety checks and production build passed.
- Focused Markdown HTTP/security suite: **85 passed**.
- `./scripts/verify.sh browser-mocks`: **14 scenarios passed**. The new standalone
  reader scenario covers desktop dark/light, 390/320px widths, keyboard focus,
  44px controls, source DOM retention/no extra reads, local table scrolling,
  loading/empty/truncated/error states, SVG and no full App/terminal/Workspace
  module requests. Unknown requests and page errors were empty.
- Large-text coverage uses 640 CSS pixels plus injected 24px article text as a
  reflow exercise; it is explicitly not a claim of native browser zoom testing.
- The new reader gate fails against the unchanged prior production bundle;
  its evidence is retained under `ui-refinement/baseline-ui-negative/`.
- Independent final code review: APPROVE. Independent desktop/mobile/light/dark
  visual review: ACCEPT. A non-blocking optional cap for the wider source button
  at exactly 640px was left unchanged to preserve the verified layout.

Evidence root: `/home/tiger/claude_hub_workspace/markdown-preview/`.
Logs and performance JSON are in `ui-refinement/`. Final browser report and
screenshots are under
`verification/claude-hub-browser-mocks.rCW7cX/run/runs/browser_markdown_reader/`.
Browser owner summary is in that run's `summary.json`; owned Vite port 5376 is
closed. Static benchmark/negative-control servers also closed. Browser
context/driver cleanup is verified; arbitrary descendant-process exit beyond
those owned handles is not claimed by the runner.

No merge, push, shared-service restart, live provider/Feishu check, or dedicated
terminal HMR/replay run was performed. The current Hub deployment is unchanged
by this task.
