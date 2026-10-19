# HTML identifier preservation fix (2026-10-19)

## Finding and scope

Fresh review of `778042c7f68eda79357995fe6f1d798145997948` reproduced a P2:
DOMPurify removes IDs that collide with document/form properties, including
`target`, while the fragment href and authored CSS remain unchanged. The retained
heading consequently cannot be found by the fragment bridge or ID selectors.
Independent Chromium and real-HTTP probes confirmed the missing ID, zero scroll
and wrong computed color; a differently named control scrolled normally.

The fix stays on `feat/html-attachment-preview`. It changes only the HTML
sanitizer/bridge and their regression coverage, not file authorization or Task
reference semantics. There is no main integration, push or shared-service operation.

## Implementation and safety constraints

- Use a private DOMPurify instance, not hooks on the shared Markdown sanitizer.
- Record identifier-shaped IDs (`[A-Za-z_$][\w$]*`) during attribute cleanup.
  Leave DOMPurify's clobbering and other checks enabled throughout sanitization.
- After the complete cleanup, restore a recorded ID only if its element survived
  and its ID is now absent. Other rejected attributes and arbitrary rejected ID
  values are not restored. IDs already accepted by DOMPurify are left unchanged.
  Retaining the original ID keeps fragment and CSS behavior aligned without a
  partial CSS selector rewrite or a second application-level ID namespace.
- Restoration can expose document named properties through images with a safe
  `name` and a method-shaped `id`. The bridge therefore uses native Document,
  Element and EventTarget prototypes rather than resolving methods on the document.
  The parent window and channel remain lexical values; message source/origin/channel
  validation, opaque sandbox, restrictive CSP and script nonce remain unchanged.
- Restored nodes remain detached from the Hub document and are serialized only
  into the isolated iframe. Forbidden elements are not reintroduced.

## Verification

- Final `./scripts/verify.sh all`: **PASS**; backend **4,755 passed / 10 skipped**,
  frontend **1,063 passed**, with format, types, docs, lint and build checks passing.
  The skip count includes the deliberately clean-worktree-only provenance test;
  its post-commit result is recorded separately in the delivery manifest.
  Evidence: `id-fix/verify-final.log`. An earlier run caught the browser test file
  before formatting was finished; the complete frozen-file rerun supersedes it.
- Canonical `./scripts/verify.sh browser-mocks`: **14/14 PASS**, including existing
  image/code/data/Markdown/HTML behavior and the new dialog/standalone ID cases.
  The actual `document.getElementById` and `document.addEventListener` properties
  resolve to IMG in the fixture; initial/reload, eight real anchor clicks, empty
  fragment, unchanged URL/history and focused-iframe Escape still pass. No page
  errors or automatic hostile-resource requests occurred.
  Evidence: `id-fix/claude-hub-browser-mocks.zgkbHv/run/summary.json`.
- Focused `--html-ids-only` regression: frozen `778042c` build **fails as expected**
  with no target ID, fallback CSS and zero initial scroll. The fixed build **passes**
  with one target ID, expected red CSS and initial/reload scroll 1935. Both use the
  same permanent assertions, not different expected values per build.
  Evidence: `id-fix/browser/focused-{baseline,fixed}/report.json`.
- Independent production and test review: **PASS**, no actionable findings.
  An independent frozen-build probe covers 19 method/global-name IDs, retained
  selectors, removed dangerous names/nodes, message source/origin/channel rejection,
  actual clicks, empty fragment and Escape. Existing Markdown/URL tests **68/68**
  passed. Reviewed production diff SHA-256:
  `c7550c1c5619c35bdbb53945eb59f5cb90b27d0b6a60d982af59d08184dd345d`.
  Reviewed browser test file SHA-256:
  `033c9217665c4ab303e01eace7eeef888e29259f9a4d2ecc1f60cbddbbab51c6`.
  Evidence: `id-fix/review/id-fix-runtime-probe.json` and reviewer reports.
- Parent-owned real HTTP probe: **PASS**, using actual auth/session lookup, routes,
  file reads and built frontend with isolated fixture identities and state. The
  original 513,642-byte report and two PNGs render; source is exact; 401/404, raw
  path redirect and revoke-then-refresh behavior pass. The target ID/CSS and
  initial/click navigation also pass through this interface, not mocked JSON.
  Evidence: `id-fix/asgi-browser-result.json` and `id-fix/http-browser.log`.

Evidence is retained in the session's `html-preview/id-fix/` directory. Owned
ports 5322/5324/5325/5326 are closed. Browser/context/driver cleanup passed;
complete descendant-process exit remains `not_verified` in the browser harness.
Real login/provider execution, physical iOS/Safari and production deployment are
outside the verification boundary. Delivery remains branch-only.
