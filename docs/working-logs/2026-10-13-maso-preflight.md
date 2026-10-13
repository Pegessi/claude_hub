# MASO availability preflight

Task `78d3de83-f631-46a4-9c2d-a3fff32064e1` is initiator-controlled with the
configured review policy retained. User requested warning and refusal when MASO
is selected without a usable service. Work belongs to the root writer in
`~/claude_hub_worktree/maso-preflight`, branch `codex/maso-preflight`, based on
the already validated preview candidate `d2bb915cbcbd647cb98a401a67ba1226e2cd9170`.
Existing preview on 5297 and primary Hub 8173/5173 remained untouched during
implementation. An independent native reviewer performed read-only source and
mocked checks without Task credentials or shared service mutations.

Fetched origin before creating the feature worktree: local main was nine ahead
and four behind with its protected unrelated dirty start.sh. The four fetched
commits concern other Chat/directory-picker changes; this feature preserves the
accepted preview base and does not merge/reset/rebase/push primary main.

## Behavior and decisions

- POST `/api/maso/preflight` checks MASO from the Hub host rather than the user's
  browser. Default is local 9796; an explicit preset MASO_BASE_URL is supported.
- GET model catalog/default verifies advertised usable configuration, with a
  three-second total budget and two-second per-request timeout, no proxy-env
  inheritance, no session creation or model invocation. Failures return a safe
  explanatory result; invalid URL/port is rejected before HTTP.
- Choosing MASO blocks Create while checking/unavailable and offers Retry check.
  Provider/preset changes and closure invalidate old requests synchronously.
  Drafts are retained while retrying or selecting a working preset.
- Creation checks again before allocating tab/tmux resources and returns 503 if
  the previously healthy service became unavailable. A preflight response is
  not an admission token.
- MASO capabilities carry a safe unavailable_reason through initial hydration
  and SSE hello. Existing Chat shows the actual service explanation and Retry,
  without suggesting the unsupported MASO Terminal surface.

Independent review reproduced an invalid-port/control-character URL escaping
HTTPX client construction as 500. The shared URL validator now checks parsed
port bounds and HTTPX URL validity and returns a generic 400 without echoing
credentials or malformed input. Regression covers credentials and invalid ports.
The total-deadline test reaches both 1.6-second responses, each individually
below the request deadline but cumulatively above the three-second budget.
Reviewer approved after independently passing 32 focused backend tests, four
real Vue composable tests, URL/control-character checks and the cumulative
deadline reproduction. Its focused evidence is `/tmp/claude-hub-verify.CD7fXC`.

## Validation and handoff

Focused backend API, protocol and lifecycle checks, real Vue composable checks,
type checks and browser acceptance are retained under
`.agent_office/maso-preflight/`. The browser composer gate covers unavailable
warning/disabled Create, draft retention, retry, custom endpoint selection,
mobile geometry, provider switching and existing Chat recovery. A first browser
attempt retained a desktop modal across the responsive breakpoint and failed on
the resulting detached DOM; the final gate opens a fresh mobile dialog, matching
the app's existing top-bar remount behavior. Failed evidence is retained at
`/tmp/claude-hub-browser-mocks.4YOOvy/run`.

The second browser attempt reached the existing-Chat Retry action and correctly
flagged its newly exercised endpoint as unmocked; the fixture now returns the
real capabilities shape for POST `/stream/retry`. That failed evidence is
retained at `/tmp/claude-hub-browser-mocks.VvZUi4/run`. Nine final runners passed
at `/tmp/claude-hub-browser-mocks.YLSuC9/run`. Visual inspection additionally
strengthened mobile acceptance to click Retry normally and verify the full
control can scroll above the sticky footer; the latest browser/format results
are retained in `browser-mocks-mobile.log`/`backend-format-accepted.log`.

Strict `./scripts/verify.sh all` exited zero: 3285 backend tests, seven existing
skips, 779 frontend tests and every standard format/type/lint/build/document/
checkout target passed. Final evidence is `verify-all-final.log`. An initial
run's format target rejected two files formatted from the root without the
backend Black configuration; canonical formatting fixed them and the initial
`verify-all.log` was retained. Staged/document checks are run before commit.

The real Vue/Vite page used backend 18198, Vite 5298, private Hub home/state,
unique tmux and ttyd range 18800. `.agent_office/maso-preflight/live-ui-1` contains
the passing browser lifecycle, screenshots and summary. APIs were not mocked:
the default unreachable loopback endpoint produced warning/disabled Create and
API 503 with unchanged tab state; a preset selected actual MASO9796 and created
an empty Chat with eight groups. A separately owned discovery-only HTTP stub
then exited to verify the existing-Chat warning and disabled Send. It received
only GET catalog/default. Mobile warning geometry and zero page errors passed;
no model was invoked. All test-owned servers/browser/tmux were closed, and
primary/user-preview health identities remained unchanged during this run.

Final checks, live host-side probe evidence, independent-review decision and the
updated user preview identity are recorded before delivery. Only the requested
user preview is retained running. Test-owned services/browser resources are
cleaned, preserving all unrelated state and the shared MASO service.

The check establishes service response and advertised configuration. It does
not verify upstream credentials, quota or model inference. No code installs or
launches MASO. No merge or primary service restart is included.
