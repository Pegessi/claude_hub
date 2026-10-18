# Chat Markdown document preview

## Goal and behavior

Local Markdown links in Chat messages previously navigated to filesystem paths
on the HTTP server and returned Not Found. Chat-scoped links now open
`/?markdown=<path>&scope=<tab-or-managed-stream>` in a separate browser tab.
This uses the existing SPA root, so reload does not require a server catch-all
route. The reader does not initialize Chat streams or terminal sessions.

The page reuses `MarkdownContent` and its sanitizer, code highlighting, math,
and scrollable tables. It provides preview/source switching, refresh, loading,
empty/error states, retry, heading anchors and document-relative links/images.
External web URLs remain unchanged. Existing Workspace report modal behavior
is unaffected because it has no Chat scope.

## File access

- Both native tab and managed-session API routes use the existing authenticated
  stream bindings and recheck session incarnation after the threaded read.
- Normal reads stay under the local session working directory. An outside-cwd
  temporary document must appear as an exact link in this session's persisted
  assistant output. That permission does not extend to sibling documents,
  nested links or an entire temporary directory.
- CommonMark parsing selects actual Markdown/reference-style links and HTML
  anchors. Inline/fenced/indented code and image syntax grant no access. The
  existing temporary-path validator is reused without changing image behavior.
  User messages, unrelated sessions and arbitrary tool output cannot grant
  temporary-file access. Messages above 1 MiB fail closed rather than dropping
  parser context such as an opening code fence. Raw/inert HTML fails closed on
  malformed nesting. The first duplicate href wins, matching browser parsing.
- Temporary permission is intentionally bound to a live exact pathname, not a
  file inode or captured bytes: Refresh reads later edits and regular-file
  replacements at that same pathname. Deletion makes the preview unavailable;
  reusing the path later makes its new content readable from the old link.
  The reader does not retain a historical copy.
- File suffixes are limited to `.md`, `.markdown`, `.mdown`, `.mkd`. Reads use
  directory descriptors with no-follow flags for each component, reject all
  symlinks and non-regular files, and are limited to 512 KiB plus one byte.
- UTF-8 text (with optional BOM) is returned as JSON, never unsanitized HTML.
  Truncated UTF-8 prefixes do not invent replacement characters at the boundary.
  Responses use `no-store` and `nosniff`. Remote session paths are not read on
  the Hub host. Missing and disallowed files share an opaque 404 response.

## Workspace and operational boundaries

Implementation is based on local main `5e3da89`. The session's modification
boundary permits only `/home/tiger/claude_hub_workspace`; therefore an independent
Git copy was initialized there on `feat/chat-markdown-preview`, instead of
creating a linked worktree outside that boundary. The shared main checkout,
its branch, live Hub service, provider sessions and configuration are unchanged.
No deployment, merge, push or restart is part of this task.

## Independent review and remaining limitations

An independent reviewer examined the product changes and the HTTP/UI tests.
Revisions addressed canonical-root ancestor replacement, actual-link parsing,
raw/inert HTML edge cases, owning-scope rebinding, parent-relative image paths,
and heading ids surviving lazy rendering in nested blocks and lists. Final
review approved the implementation, with the live-path semantics above explicit.

Outside-cwd temporary lookup scans the session transcript and parses bounded
assistant messages; its cost is linear in history length. It does not keep a
persistent grant index. Normal in-cwd previews do not scan Chat history. Remote
files, files moved to a different pathname, arbitrary external local directories,
and temporary sibling resources without their own grant remain unavailable.

## Validation

Final stable candidate:

- `./scripts/verify.sh all`: PASS. Backend default scope: **4382 passed,
  10 skipped**. Frontend unit tests: **1034 passed**. Python type checking
  covers **536 files**. Formatting, lint, Vue types, docs, checkout safety and
  production bundle build passed. The existing Vite large-chunk warning remains.
- Focused Markdown HTTP/security suite: **81 passed**.
- `./scripts/verify.sh browser-mocks`: **13 scenarios passed**, including real
  ordinary-click navigation, source fidelity, reload, Unicode/space filenames,
  nested links/headings, parent-relative images, scope rebinding, lazy renderer
  updates, error/retry, sanitization, and a 390px reader layout. Unknown API
  requests and page errors were empty. All scenario processes exited zero;
  context/browser/driver cleanup completed and owned Vite port 5376 closed.
- `./scripts/verify.sh check-staged`, `git diff --check`, and a redacted
  Gitleaks scan of the staged diff: PASS.

Evidence under `/home/tiger/claude_hub_workspace/markdown-preview/`:
`verify-all-final.log`, `backend-markdown-agent-final.log`,
`verification/claude-hub-browser-mocks.rNeX68/run/summary.json`, and the
`runs/browser_chat_image_quality/` report/screenshots in that browser run.

Browser acceptance extends `browser_chat_image_quality.py` and uses mocked APIs
on a task-owned isolated Vite server, not the shared Hub. A negative-control
Vite transform disabled only standalone page selection without editing product
sources; the scenario failed at the missing document heading as expected.
`negative-control/` retains that evidence. Its context/browser/driver closed,
its owned server stopped, and port 5377 was confirmed closed. Browser descendant
process exit beyond these owned handles is not claimed by the runner.

Not run: dedicated terminal HMR/replay targets, live provider/Feishu flows or a
shared production deployment. Those flows were not changed or operated.
