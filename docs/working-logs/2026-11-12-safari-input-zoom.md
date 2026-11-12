# Safari editable picker focus zoom

Task: `d7b4caab-2c5b-4694-b520-80e549660323` (initiator controlled).

## Problem and change

The reported symptom is a page-scale jump when tapping a selectable/editable
control in Safari. The model picker focuses its search input when opened. In
the real application the search computed to 13px and the custom-model field
to 12px. The new-directory combobox and session fields also used small text.
WebKit's iOS focused-element reveal path passes the focused element's font
size into its zoom calculation:
[WKContentViewInteraction.mm](https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/ios/WKContentViewInteraction.mm).

The model search/custom inputs and session creation fields now use 16px on
`any-pointer: coarse`, covering landscape and touch tablets as well as narrow
phones. The child-owned Provider/Preset selects have their own rule because
Vue's parent scoped styles cannot reach their internal elements. No viewport
scaling restriction or focus-time zoom-reset JavaScript was added.

The landscape browser check also exposed the model menu under the app toolbar
(844x390: x=288, y=46, width=560). Its touch width/height now leave room for the
toolbar/composer; both editable fields remain reachable through ordinary
clicks. Desktop compact styling is retained.

## Evidence

Initial verification baseline `42e23f139f64c42f30f5da44ca5828f03f4dffc2` combines
the existing local main with fetched origin/main inside this isolated worktree.
The primary checkout was clean and was not modified during investigation.

- Extended the existing full-app mocked Chat composer scenario. Before the
  style change it failed on actual computed font size 13px at 390x844.
- The final scenario passed at 390x844, 844x390 and 1024x768, with real input
  focus, search filtering, custom text, directory selection, nested select
  typography, viewport bounds and desktop typography checks. The viewport
  remains free of maximum-scale/user-scalable restrictions.
- The existing diagnostics keyboard-scroll check used Linux Control+End;
  on macOS it now uses Command+Down. The final end-marker visibility assertion
  remains intact.
- Full browser report/lifecycle: `.artifacts/safari-zoom/after-mac-key/`.
  Exit 0, all context/browser/driver closes complete; process-tree exit remains
  explicitly `not_verified` on this host. API requests are mocked and external
  requests blocked; this is not a live provider test.
- `verify.sh all`: docs, checkout safety, frontend lint/types/unit/build passed.
  Backend types reported existing macOS `os.waitid` typing errors. Backend
  tests: 4167 passed, 1 skipped, 1 failed in process-group termination. The
  runtime owner also reported PermissionError during cleanup and retained its
  temporary evidence. No backend files were changed by this fix.
- The first all-run format check caught use of the wrong Black configuration;
  the file was reformatted with `backend/pyproject.toml`, and its targeted
  Black check passed. Full all-run results are not claimed green.
- Local Node 24.13.0 / uv 0.11.2 differ from declared verification pins;
  Python 3.11.16 and Playwright 1.58.0 match. Results are not CI-equivalent.
- The temporary upstream/local merge passed `merge-decisions` with zero
  decision paths; that is structural evidence, not an unrelated semantic review.
- Independent read-only review found the child select scoping omission; fixed
  and re-reviewed. The reviewer checked the final browser evidence and found
  no remaining blocker in this patch.

## Limits

The user's Safari device/OS has not yet been confirmed. Chromium touch emulation
verifies the font/interaction prerequisite, not iOS's native software keyboard
or automatic visual-viewport zoom. A physical iPhone Safari check remains
unobserved, and a Mac-only Safari symptom would require separate reproduction.
No production model calls or primary Hub restart were performed.
