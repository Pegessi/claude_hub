# Static HTML attachment previews (2026-10-19)

## Scope and starting point

- Base: local `main` at `9b8c3ce09d88b2b96a1b1d1b57fb36b855e90e60`.
- Candidate branch: `feat/html-attachment-preview`.
- The user supplied a mounted HTML report: 513,642 bytes, authored CSS and two
  inline PNGs, without JavaScript or external asset dependencies. Its exact
  filename was already listed in the originating initiator Task's artifact refs.
- The existing attachment reader treated HTML as source. Separately, production
  static serving returned 404 for filesystem-shaped URLs outside the bundle;
  the normal Chat reader did not grant that outside-cwd storage directory.
- Work is confined to a task-owned checkout inside this session's writable
  directory. The primary checkout and shared services are not edited or restarted.

## Display and browser isolation

HTML/HTM has a distinct `html` attachment kind (`xml` source highlighting).
The shared viewer renders it by default, with exact source/copy and refresh
controls. Vue/Svelte and notebooks remain source-only. Empty files retain their
empty state; truncated HTML is source-only rather than partially rendered.

The HTML document is sanitized independently of Chat Markdown. Authored styles
and embedded PNG/JPEG/GIF/WebP data images are retained. Scripts, inline handlers,
forms, frames, objects, refresh metadata, base tags and external resource tags or
attributes are removed. Local sibling links are not granted access. External
HTTP(S) links require an explicit click, open separately and have no opener or
referrer. Inline SVG and JavaScript-driven charts are outside this static preview.

The document uses an iframe sandbox **without `allow-same-origin`**. Its content
security policy denies ordinary sources, permits inline styles/data images and
fonts, and allows only a generated nonce-authorized navigation bridge. This does
not enable authored scripts or `eval`. The bridge only scrolls local fragments
without adding joint iframe history entries, and forwards Escape to the parent.
Both message receivers validate the sender window, expected origin and random
channel. Source/preview toggling retains the mounted iframe and its inner scroll.

Browser testing exposed a nonce serialization problem: setting the script's
`nonce` property did not emit its attribute in the detached document used here.
The implementation now sets the nonce and CSP metadata attributes explicitly;
serialized policy and runtime behavior are checked, rather than inferred from
TypeScript properties. An independent review also required explicit parent-origin
validation in the child init listener; negative message tests cover both receivers.

## Single-file authorization and link compatibility

`GET /api/workspaces/artifacts/html-preview?path=...` returns authenticated JSON,
never raw same-origin `text/html`. A grant requires an exact absolute HTML filename
in the current initiator Task's `latest_progress.artifact_refs`, in the same
execution epoch, with still-present canonical local Workspace and Chat records.
If that Chat is managed, its exact local session record must also exist. Supplied
execution-session identity must match the source. Replaced/deleted/remote owners
fail closed. Archived/inactive Chats and stopped managed sessions remain valid
sources of previously published artifacts: runtime inactivity is not revocation.
The same distinction applies when runtime status changes during a read.

This deliberately does not add a directory allowlist, historical-reference search,
artifact cache, anonymous file route or managed-report grant mechanism. Registering
an HTML artifact explicitly makes that individual file available through the
Hub's authenticated artifact reader; neighboring files are not included.

Reads reuse the 512 KiB bound, UTF-8/BOM/NUL handling and descriptor-relative
no-follow regular-file checks. Task, Workspace, Chat/session identities, epoch,
progress revision and reference are checked again after the asynchronous read,
including failure/encoding paths. Successful responses are no-store/nosniff.

The ordinary scoped attachment endpoint keeps its original cwd/temporary-reference
policy. Only a denied HTML read may try the Task grant, and that fallback must have
the same owning Chat as the request. Same-origin full HTML web links explicitly
select the registered reader; other external/code/Markdown links keep their prior
meaning. Missing non-API/non-static HTML paths in production redirect to the
registered reader shell. The redirect itself does not authorize or read a file;
normal static files, other misses and API behavior remain unchanged.

## Task release compatibility

An empty/omitted artifact-ref list on `released` now preserves the preceding
same-epoch list in the existing durable Task snapshot. Releasing execution
ownership should not silently unpublish its output. A nonempty release list
replaces it. All non-release progress updates still replace refs (empty revokes),
and handoff clears the snapshot and advances the epoch. No new persistent grant
store was introduced, and old references already erased by historical releases
are not automatically recovered.

## Browser acceptance harness

The opaque iframe exposed assumptions in the test harness, without requiring
weaker product isolation. Playwright 1.58's built-in service-worker blocking reads
an unavailable getter in every opaque frame and raises a test-injected page error.
The shared runner still defaults to `block`; only this acceptance runner opts into
`allow` and installs a guarded registration blocker before any page is created.
It tests rejection, zero registrations and zero worker-script requests, and keeps
an unfiltered hard failure for page errors. No product sandbox flag is relaxed.

A separate mobile reader page left Chromium's shared native window at 320×390
while the main page's emulated viewport remained 1280×900. Controlled identical
sequences in block/plain-allow/guarded-allow all lost child pointer delivery.
Restoring the resized secondary page to the parent viewport before closing it
restored native 1280×900 bounds and delivered the first trusted click to the child,
scrolling from 0 to 1251 without extra history. The chosen fix is test-page cleanup,
not an extra click, forced click, DOM click or product workaround.

Scroll waits use bounded read-only polling because Playwright's string-based
`wait_for_function` attempts the very `eval` that this document's CSP disallows.
Popup URL checks wait for the intended navigation rather than treating the initial
`about:blank` DOMContentLoaded event as the requested reader. Keyboard, exact URL,
viewport, scroll, history and source-retention assertions remain in place.

## Verification and delivery

- Final `./scripts/verify.sh all`: **PASS**. Backend **4,755 passed / 10 skipped**;
  frontend **1,063 passed**; docs, checkout safety, Black/isort, complete backend
  types, frontend lint/types and build passed. Evidence: `verify-final.log`.
  The backend skip count includes
  `test_read_git_provenance_matches_head_on_clean_delivery_repo`, which explicitly
  skips because this candidate was validated as a dirty delivery worktree.
- Canonical `./scripts/verify.sh browser-mocks`: **14/14 PASS**.
  Evidence: `claude-hub-browser-mocks.SZmgG5/run/summary.json`.
- Full existing attachment plus HTML acceptance using the supplied report:
  **PASS**, `browser/html-run8/`. Both PNGs decoded, source/copy and retained
  iframe scroll matched, initial fragment load/reload passed, and error/unknown/
  automatic-resource request lists were empty. The single service-worker
  registration attempt was the deliberate negative probe; it was rejected with
  no registration or script request.
- Canonical ownership snapshots plus actual mounted-file read: **PASS**,
  `backend-real-file/result.json`. All **513,642 bytes** were read without
  truncation; raw-file and preview SHA-256 both equal
  `52ee72cb63f3223bb9dbbf82d043cd2bed1f5e2a86aa66d5249acbc74838ad42`.
- Independent backend review: **PASS**, including **340 focused tests** and
  full backend types/format. Frozen backend/shared-lifecycle digest:
  `6d4636f772394281ec83622db571b86b05f45a3faef954b0b421f9525f0762a0`.
- Independent frontend review: **PASS**, including **23 focused tests**, types,
  lint and final browser evidence. Frozen frontend digest:
  `1e833f29fbf47a6a5e0205276316120e75ba0c928012f943781873d5336a4b48`.
  Digests use each reviewer's recorded diff/untracked-file serialization and
  were independently reproduced before staging.
- Task-owned ports **5320/5321** were verified closed. Browser context, browser
  and driver cleanup passed; complete descendant-process exit remains marked
  `not_verified` by the harness. Failed development runs are retained separately.
- Delivery is an independent local feature branch. No main integration, remote
  push, production build update or shared-service restart was performed.

Evidence is retained under the session's `html-preview/` directory. The browser
runner accepts an optional `--html-report FILE` for bounded real-report validation;
the supplied private report is not copied into the repository. Browser checks use
real Vue/Chromium with mocked APIs, separate from backend authorization tests.
A canonical metadata snapshot plus the real mounted file is not a deployed API
check. Physical iOS/Safari, provider execution and production deployment are not
claimed. No push, main integration or shared-service deployment is included in
this candidate's delivery.
