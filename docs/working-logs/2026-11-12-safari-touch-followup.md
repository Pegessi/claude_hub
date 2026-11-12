# Safari Chat touch and keyboard follow-up

Task: `d7b4caab-2c5b-4694-b520-80e549660323`, reopened in the same
initiator-controlled task after the user's 23:02 report. Base: `1ff1ca4d`.
Worktree: `~/claude_hub_worktree/fix-safari-input-zoom`.

## Failure and cause

The previous font-size fix was insufficient. The user reported that focusing
an input still moved the screen upward and Add attachment did not work.
The running 8173 server already served the same built asset as the merged
branch, so a missing server build was not assumed to explain the report.

Real Playwright WebKit/iPhone-profile taps reproduced the attachment failure:
textarea → Add to chat → Add attachment produced pointerdown/up followed by
focusout with no relatedTarget. The menu unmounted before click; no file chooser
opened despite the option being enabled. An independent reviewer reproduced
the same sequence separately. Ignoring an unknown focus destination lets the
existing synchronous file-input click work; outside pointers, known external
focus, Tab and Escape still dismiss. Disabled reasons are visible on touch.

The viewport reproduction retained an 844px layout viewport while reporting a
350px visual viewport at offsetTop=50. The old app occupied 0..350, placing its
top 50px above the visible area. The active mobile Chat shell now uses fixed
positioning at the visual viewport origin and its existing visual height.
It uses no transform and does not change PTY grid sizing. Pinch zoom pauses
layout/keyboard-baseline updates until scale returns to 1; native zoom remains
enabled. Programmatic menu focus uses preventScroll.

This coordinates layout with reported viewport geometry. It does not prove that
every native Safari keyboard animation or OS-specific viewport bug is covered.
[WebKit's root viewport issue](https://bugs.webkit.org/show_bug.cgi?id=292603)
also explains why overflow:hidden alone is not a reliable mobile keyboard fix.

## Reproduction and validation

All browser API routes are mocked, unknown routes fail, and sockets/external
requests are blocked. Vite uses task-owned port 5299, never the primary service.
Evidence directories are under `.artifacts/safari-zoom/`:

- `real-paths-regression-before`: FAIL waiting for a chooser after an ordinary
  WebKit touch. No forced click or direct hidden-input selection.
- `real-paths-viewport-before`: with only the menu fix, chooser/preview/cancel
  passed, then FAIL waiting for the whole shell to match visual origin 50.
- `real-paths-after`: first repaired WebKit run PASS.
- `real-paths-expanded-v3`: expanded WebKit run PASS. Actual taps open the chooser;
  a fixture image is selected through it, previewed, and sent with exact image
  bytes/text to the mocked endpoint. Cancellation/reopening retain the draft.
  Outside tap, external focus, Tab/Escape and visible disabled reasons are checked.
  Shell/header/composer bounds cover offsets 0/50/150, portrait/844px landscape,
  scale-2 pinch metrics, restoration and the previous model-picker scenarios.
- Earlier expanded/v2 failures were test taps on an area covered by the open
  menu. The final test taps an unobscured textarea corner normally; it does not
  force a click through the menu.

WebKit is the installed Playwright engine with iPhone 13 emulation, not a real
iPhone. Native keyboard animation and Photos/Files selection UI remain untested.
The chooser event and Vue interaction are real; selected image bytes and visual
viewport changes are fixtures. API closure evidence does not certify all possible
escaped browser descendants.

- `real-paths-chromium`: the complete existing desktop/mobile composer scenario
  PASS, including image-only turns, queue/failure recovery and model controls.
- Independent reviewer `safari_real_paths_review` separately reproduced the
  original attachment fault, then reviewed the final diff and both final browser
  report/lifecycle files: PASS, no P0/P1/P2 blockers. Both browsers exited 0 with
  context/browser/driver closure complete and no unknown API requests.
- The pan screenshot caught a sidebar exit animation following rotation and
  is not presented as a stable user-facing preview. Assertions check the
  shell/header/composer geometry independently.

## Standard verification

`./scripts/verify.sh all` completed with exit 1. Frontend lint, type checking,
all 1,485 unit tests and production build passed; docs and checkout-safety passed.
Backend results retain the existing host limitations: `os.waitid` typing errors
at `check_verify_runtime.py:138/141`, and one process-group termination test
failed with 4,167 passed / 1 skipped. Runtime cleanup also reports PermissionError
and retains its owned temporary directories. No unrelated process was killed.

The first format target overlapped a test-file edit and flagged that file. After
formatting with the repository's 100-column configuration, the repeated format
check reported all 650 files unchanged (isort also passed), but its wrapper still
exited 1 on the same cleanup PermissionError. Direct final Black/isort checks of
the changed scenario passed. This is not an all-green or CI-equivalent run:
Node 24.13.0 and uv 0.11.2 differ from declared 24.21.0 / 0.12.15.

Evidence: `real-paths-verify-all.log`, `real-paths-final-format.log` in the same
artifact directory. `check-staged` passed. No backend code or runtime state was
changed by this patch; production activation only needs the static frontend.
