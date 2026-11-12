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

The user confirmed iPhone Safari at 22:14. Chromium touch emulation
verifies the font/interaction prerequisite, not iOS's native software keyboard
or automatic visual-viewport zoom. A physical iPhone Safari check remains
unobserved; the installed iPhone Safari version is not known.
No production model calls or primary Hub restart were performed.

## iPhone follow-up: cross-review and WebKit

The user requested several cross-review rounds and simulated testing. Two
read-only reviewers challenged the implementation and then checked the fixes
and evidence. The follow-up found and fixed real omissions:

- Related Chat inputs (composer, question answer, edit/resend, sidebar and diff
  comments) used smaller type. They now use `max(original size, touch floor)`;
  larger theme fonts and desktop type remain available. No global `!important`
  override or disabled user zoom was introduced.
- The mobile shell retained a stable terminal height even with an active Chat.
  A visual-viewport-only reduction left the search at y=551 with only 430px
  visible. Active Chat now follows visual height; active PTY retains stable
  height. Mixed panes still share the app height according to the active pane.
- An anchored menu cannot fit when a landscape keyboard leaves 160–180px.
  Those short touch views use a manual native popover in the top layer and
  scroll the whole menu. Ordinary views remain anchored. The DOM stays owned
  by the same picker, preserving outside-click handling. The top-layer path
  uses the browser Popover API; older Safari builds are not certified here.
- A disabled model trigger lost focus during a request. Focus is restored
  after re-enabling only for the same active tab and absent later user
  pointer/keyboard/focus interaction. Capability refresh can still briefly
  disable and blur the composer; the regression asserts that the picker does
  not steal focus back and that the later draft survives.
- Cached panes close their model menu on deactivation. A post-render popover
  watcher opens the top layer before the controller focuses search.

### Evidence and repeatable commands

The existing composer scenario has an opt-in `--iphone-webkit` mode that reuses
its strict API fixture and lifecycle owner, with Playwright's iPhone 13 device
profile. The default Chromium scenario is unchanged. Run it on a dedicated
Vite port, with a **new** evidence directory:

```sh
backend/.venv/bin/python frontend/tests/browser_chat_composer.py \
  http://127.0.0.1:5299 .artifacts/safari-zoom/NEW-RUN \
  /Users/Apple/Library/Caches/ms-playwright/webkit-2248/pw_run.sh --iphone-webkit
```

- WebKit 26.0 / Playwright 1.58.0: `webkit-final-dismiss/` PASS, exit 0,
  context/browser/driver closure complete. No unknown API or unexpected external
  request. `iphone-chromium-regression/` also PASS.
- Layouts: 320x568, 375x667, 390x844, 430x932, 844x390, with rotation while open.
  Keyboard stubs retain layout dimensions while visual height becomes 430,
  350/offsetTop 50, 180, or 160; restoration is also checked. Stubs exercise
  actual App resize handling, not the native iOS keyboard.
- Real tap/focus, bounds and hit testing; long-list scrolling and last-model
  request payload; custom-model submission; Escape/outside-tap dismissal and
  immediate reopen/autofocus in the short popover; delayed request plus later
  draft input; session name/directory/select and file-browser path navigation.
- `final-lifecycle-unit.log`: 17/17 passing, including KeepAlive leave/return.
  Browser lifecycle owner tests: 24/24 passing.
- Final frontend lint/types/unit/build all pass (`final-frontend.log`, 1485/1485
  unit tests). Final targeted Black and docs checks pass.
- `iphone-verify-all.log`: frontend checks/build pass; the same backend typing,
  process-group test and runtime-cleanup limitations remain. Its format check
  ran before the final formatting pass; final targeted Black check is separate.
  A temporary component test failure exposed an absent matchMedia in the mock
  runtime; the optional feature check was fixed and the owned failed runner
  stopped. No production process was stopped.

No iOS simulator runtimes are installed. Playwright WebKit is browser-engine
emulation, not native Safari acceptance; see
[Playwright browser scope](https://playwright.dev/python/docs/browsers) and
[device emulation](https://playwright.dev/python/docs/emulation).
Original failed evidence is retained alongside passing runs. The branch is
not merged, pushed or deployed by this follow-up.

The existing initiator Task was already marked done for the first code
delivery. Attempts to reopen it returned `task_progress_conflict`, a 500 from
status update, and `task_is_initiator_controlled` from followup. No duplicate
Task or Workspace dispatch was created; this work retains the same Task ID.
