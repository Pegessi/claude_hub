# Unified Chat attachment previews

## Scope and design

The previous Chat UI recognized local Markdown links and opened a separate
reader. The Markdown reader API rejected code and data files by extension.

The new default interaction stays in Chat. One native modal hosts Markdown,
source text and images. At up to 600px it fills the viewport; desktop uses a
bounded window. The dialog leaves the Chat mounted, makes the background inert,
and owns one browser-history entry. Close, Escape and browser Back dismiss the
preview. A fresh history marker per opening prevents older Forward entries from
trapping a later preview. Nested file/image links share the same modal and have
an explicit Back control. Focus returns to the original trigger without scrolling.

The boundaries are:

| Owner | Responsibility |
| --- | --- |
| `markdownPreview.ts` | Recognize local file URLs, preserve paths/fragments, keep the owning Chat scope; never hijack external links |
| `provideAttachmentPreview` | Per-Chat target stack and focus restoration; no content cache or global cross-Chat state |
| `AttachmentPreviewDialog` | Native modality, responsive bounds and browser-history lifetime |
| `AttachmentViewer` | Request cancellation, errors/retry, sanitized Markdown, literal source, image fit/actual size and copy |
| `local_attachments.py` | Authoritative text-format allowlist and kind/language metadata |
| Existing bounded text reader | Directory/temp-reference authorization, no-follow file reads, regular-file and UTF-8 checks |

### Formats and controls

- Markdown uses the existing sanitized renderer and offers a source toggle.
- Python and other common code files, JSON/JSONL, YAML, TOML, CSV/TSV, logs and
  named build/text files display source. HTML and notebooks are source-only.
- Source can wrap and be copied exactly; JSON is not reparsed or reformatted,
  so large integers and original whitespace are preserved. Highlighting is
  disabled above 64 Ki characters to limit synchronous work on mobile devices.
- Images retain fit, actual-size, dimensions and an explicit original link.
- Known binary/document/archive links show an unsupported explanation without
  asking the backend to read them. This does not add arbitrary file download,
  binary/PDF/Office viewers or non-image upload support.
- Modified clicks and Open separately retain standalone navigation. Historical
  `?markdown=...&scope=...` links and the original Markdown API are compatible;
  `?attachment=...&scope=...` uses the shared viewer without loading the full App.

### Access and failures

The new `GET /api/workspaces/{tabs|sessions}/{id}/stream/attachment?path=...`
returns the existing preview fields plus `kind` and `language`. It uses the
same authentication and before/after session-incarnation checks. Local files
are restricted to the Chat working directory or an exact assistant-linked
OS-temporary path. Remote sessions cannot read Hub-host files. Symlinks, FIFOs,
unsupported extensions and unauthorized files fail closed. A maximum of
512 KiB is returned with an explicit truncation warning; copy labels distinguish
partial content. UTF-8 errors return 415, access failures an opaque 404, and
responses use `no-store` and `nosniff`.

Each new target aborts the previous fetch. Response, error and completion updates
check request identity so a late result cannot replace a new file. Closing the
viewer cancels outstanding work. No source file is executed or inserted as HTML;
only sanitized syntax-highlighter spans and the existing sanitized Markdown
renderer produce markup.

## Validation and delivery

Implementation base: local main `44e68ed`.

This session is restricted to `/home/tiger/claude_hub_workspace`; the candidate
therefore uses an independent task-owned Git checkout at
`unified-attachments/repo`, on `feat/unified-attachment-preview`, instead of
creating or editing a linked worktree outside the permitted directory. The
primary checkout and shared Hub services are unchanged.

Validation results and final review evidence are added below after execution.
No merge, push, installation or shared-service restart is included in this task.

### Final verified results

- Canonical `scripts/verify.sh all`: PASS. Backend default scope: **4585 passed,
  10 skipped**; frontend: **1044 passed**. Documentation, checkout safety,
  backend format/types, frontend lint/types and production bundle all passed.
  The default backend skips include opt-in browser cases, missing zsh and the
  clean-repository provenance condition; they are not counted as passes.
- Focused legacy Markdown + new attachment backend tests: **177 passed**.
- Canonical `scripts/verify.sh browser-mocks`: **14/14 scenarios passed**,
  every runner exited zero, owned Vite shut down and its port stopped accepting
  connections. Evidence: `../claude-hub-browser-mocks.uoPDRM/run/summary.json`.
- Independent browser-test author ran the updated Chat image/attachment scenario
  in the pinned Python 3.11.16 / Playwright 1.58.0 environment: PASS, no page
  errors or unknown API requests. Evidence: `../browser-attachment-run4/`.
  The owner separately checked its report and 320px screenshot.
- Independent read-only review: PASS. Findings fixed and regression-tested before
  the final verdict: history Forward/reopen close failure; nested reader scroll
  restoration; focus after the last nested Back removes its own button; and the
  unsupported BMP image classification. Frontend and backend permission/call
  paths were checked independently.
- Both task preview ports (5298, 5299) were closed after testing. The primary Hub
  was not changed or restarted. Browser mocks use real Vue DOM with simulated
  APIs; they do not prove live provider, OAuth, production deployment or physical
  iOS/Safari behavior. Browser lifecycle evidence does not claim exhaustive
  descendant-process exit.

Not run: terminal HMR, terminal replay, performance/manual-device suites, or a
merge-decision gate (no merge was performed). The existing App bundle-size
warning remains. No merge, push or deployment has been performed.
