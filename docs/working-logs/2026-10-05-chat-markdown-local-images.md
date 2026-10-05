# Local Markdown images in Chat

## Symptom and cause

The extension-menu follow-up ended with a Markdown screenshot at
`/tmp/hub-menu-icons-desktop.jpg`. The file existed, but MarkdownContent left
the source as a web path, so the browser requested it from the Hub frontend.
The existing agent-image endpoint only admitted files inside the tab cwd.

## Design

- Extend the existing quoted-image postprocessor after Markdown sanitization.
  Local PNG/JPEG/GIF/WebP paths route to the owning tab's agent-image endpoint;
  ordinary web/data/blob/API/static asset URLs keep their existing behavior.
  Consume whole HTML attributes, decode source entities/URI escaping once,
  and encode the resulting URL safely even in single-quoted attributes.
- Keep the normal cwd-confined reader. Outside cwd, authorize only an exact
  absolute temporary file explicitly referenced by an inline Markdown image
  in this session's canonical assistant text or completion summary. Handle
  streamed chunks and completed-history snapshots; do not join separate
  messages or accept user prompts, tool output, or another tab's history.
- Permit OS temp-root aliases, but reject traversal and symlinks underneath
  them. Continue content sniffing, size limits, remote-session denial, opaque
  errors, and authentication. File/log reads run off the async request loop.
- Failed local/provider images retain their alt text in a small placeholder.
  Successfully loaded images scale to the available chat width.

## Validation and deployment

Regression tests cover local URL rewriting through block/list caches,
escaping, ordinary URL preservation, temporary-file grants and denials,
streamed references, and the existing image-reader boundaries. Browser
validation uses the real MarkdownContent component and image route with a
fixture session in an isolated runtime: the original screenshot and a cwd
relative image both load; missing files show the placeholder; 390px mobile
layout has no horizontal overflow.

Validation results: 51 image endpoint regression tests, frontend unit suite,
frontend type check/build and ESLint, Black/isort, and mypy for both changed
backend modules passed. Preview frontend/backend were stopped after review.

This change requires the updated backend for temporary images outside cwd.
The running Hub is intentionally not restarted during the Chat task; merge
and push do not by themselves activate that backend change.
