# Attachment preview review fixes

Base: `7732df09035b95a5ea5abeca29f7141958191bd2`.

The second independent review found two functional regressions and a smaller
mobile action-size inconsistency. This follow-up fixes those findings without
changing file access, supported formats, payloads, or shared-service deployment.

## History ownership

The initial implementation used same-URL browser history entries for preview
closure. App's existing `popstate` handler also consumed those events and
reactivated the `?tab=` entry link after the user had manually selected another
Chat.

The fix identifies preview-owned events explicitly:

- The dialog marks same-URL departures in the capture phase, before App routes
  the event. A traversal to a different URL is still ordinary navigation.
- Explicit Close and component cleanup install a one-shot capture callback
  before `history.back()`, so classification survives component unmount.
  Cleanup does not queue another traversal while a Close is already pending;
  real Vue lifecycle tests reproduce both deactivation and unmount interleavings.
- A Forward traversal into a preview entry is recognized only when the stored
  preview URL matches the actual destination.
- App bypasses deep-link routing only for classified preview events. The event
  is not stopped, so unrelated listeners still receive it. A `WeakSet` ties the
  classification to that exact event and does not retain content or Chat state.

An intermediate query-only de-duplication was rejected during review because it
could suppress genuine Back navigation after another `pushState`. It is not in
the final implementation. Real A/B history navigation has a browser regression.

## Short-screen layout and controls

The viewer title is clamped to two lines. Its complete filename remains in the
accessible heading/title and the full path remains keyboard-scrollable. The
header is bounded to half the viewer height and can scroll, reserving a usable
content region even when title/actions grow. The modal itself does not overflow
the viewport; controls remain operable by normal scrolling/clicking.

Slotted actions use the same 44px minimum height and keyboard focus style as
viewer-owned actions. This covers `Back to Hub` in the standalone code reader.

## Regression evidence

The test author extended the existing Chat image/attachment browser scenario;
no parallel browser-test framework or new provider integration was introduced.
The three new groups were independently run against an immutable frontend
snapshot of `7732df0` on a task-owned port:

- Routing assertion failed: closing B's preview selected stale entry Chat A.
- Long-filename assertion failed: header reached 447.30px in a 390px viewport,
  leaving no visible source region.
- Standalone action assertion failed: Back to Hub was 32px instead of 44px.

All three baseline runs exited nonzero at their corresponding assertions and
closed the browser resources. The same tests plus all retained scenario checks
passed on the final fixed source. At 320×390 with a legal 224-character filename,
visible source height is now **163px** in both modal and standalone views; the
standalone return action is **44px**. Tests exercise Close, Escape, browser Back,
programmatic Chat changes, genuine differing-tab Back/Forward, keyboard source
scrolling, exact copy, and normal header-control scrolling/clicking.

Task-owned evidence, relative to the checkout: `../review-fixes/`.

- `baseline-routing/`, `baseline-long-name/`, `baseline-standalone/`: expected
  failure reports against `7732df0`.
- `fixed-browser-full-run1/`: passing combined browser report and lifecycle.
- `review/`: independent fix-review evidence.

The full repository and browser-gate results are recorded below after execution.
No merge, push, installation, or shared Hub restart is part of this follow-up.

## Final validation

- Canonical `scripts/verify.sh all`: PASS; backend **4585 passed, 10 skipped**;
  frontend **1051 passed**. Documentation, checkout safety, types, formatting,
  lint and production bundle passed. Default-scope skips and the existing App
  bundle-size warning remain; no skipped test is counted as a pass.
- Canonical `scripts/verify.sh browser-mocks` on the frozen final patch:
  **14/14 passed**, zero failed runners, owned port closed. Summary:
  `../review-fixes/claude-hub-browser-mocks.am2j2A/run/summary.json`.
- Final combined attachment browser rerun: PASS with no page errors or unknown
  API requests; `../review-fixes/fixed-browser-final/` is the relied-on focused
  result after the pending-Close guard.
- Before the additional guard, both actual component Close→deactivation and
  Close→unmount regressions failed with two history traversals instead of one.
  After the guard, the helper/component group passed **14/14**. Evidence:
  `pending-close-before-guard.log` and `pending-close-after-guard.log`.
- Independent reviewer rechecked the final helper, App/Dialog routing, layout,
  and component tests: PASS, no remaining actionable findings in this scope.
- All task-owned Vite ports (5310/5311/5312) were closed and checked after use.
  No shared Hub operation was performed.

Browser tests exercise real Vue/Chromium with mocked APIs and clipboard. They do
not prove physical iOS/Safari behavior, live provider connectivity or production
installation. Terminal HMR/replay and performance/manual-device gates were not
run. No merge or push was performed.
