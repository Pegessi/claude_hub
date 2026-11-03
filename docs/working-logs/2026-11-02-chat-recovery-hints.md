# Chat recovery guidance — 2026-11-02

## Scope

The user requested actionable hints for intermittent Codex/Goal startup errors,
then reported `Hub message inbox unavailable; retrying…`. Keep Codex versions,
provider configuration, delivery semantics and the shared Hub service unchanged.
This branch is a code-only candidate; deployment is not authorized.

## Evidence

- The earlier Goal failure followed Codex model-cache version mismatch and model
  list timeouts. Hub's Codex RPC deadline is 10 seconds. A concurrent Goal read
  can surface the runtime retirement guard instead of the earlier startup error.
- `ChatMessageInbox.vue` polls the receipt list every three seconds, with a
  ten-second request timeout. Any failed HTTP response, fetch failure or JSON
  read failure previously displayed the same generic warning.
- The receipt GET is separate from message submission. A failed observation
  does not establish either successful delivery or message loss. Keep the last
  observed receipt, identify it as potentially stale, and do not resend.
- At investigation time the main Hub was healthy. Three read-only receipt GETs
  for the current Chat succeeded (about 0.41 seconds each, including CLI startup).
  No matching backend exception was identified. The screenshot's tab/time was
  not supplied, so its specific HTTP/network cause remains unknown.

## Changes

- Goal errors retain original diagnostics under Error details, explain that
  refreshing may retry provider startup, and advise saving unsent text and
  attachments. Retry Goal status continues to perform only the existing read.
- Inbox errors distinguish timeout, transport, HTTP 401/403, other HTTP status
  and unreadable-response failures. Automatic polling is unchanged. Manual
  Retry status cancels the previous observation generation and reads again;
  it does not submit or replay a message.
- Existing receipts remain visible with a stale-status label during a read
  failure. Recovery clears the warning without clearing drafts or receipts.

## Validation

- Backend default scope: 5,972 passed, 10 skipped; evidence at
  `/tmp/claude-hub-verify.96ppzo2s`. Backend types: 614 files passed.
- Frontend unit suite: 1,156 passed; frontend lint, types and build passed.
- The first `verify.sh all` run failed only the browser-scenario formatting
  check. Corrected formatting using the backend's Black configuration, removed
  unrelated formatting changes, and reran `backend-format` successfully. The
  aggregate command's historical exit remains nonzero; individual required
  targets now pass.
- Mocked browser gate: all 16 scenarios passed, including Goal read/start errors,
  HTTP/network recovery, preserved text/images/receipts and no extra submission.
  Evidence: `/tmp/claude-hub-browser-mocks.o1brazlh/run`.
- Terminal HMR: 12 passed using the installed Playwright 1.58 Chromium headless
  shell; evidence `/tmp/claude-hub-verify.oouwt2_9`. The initial attempt could not
  find pnpm in its private HOME; explicitly reusing the existing `COREPACK_HOME`
  resolved that environment issue without downloads or dependency changes.
- Independent read-only review identified missing Goal-start dialog guidance
  and image preservation coverage; both were added and re-reviewed with no
  remaining required fixes. Subsequent visual review found the old 8rem inbox
  height clipped the heading after expanding details. Only the error state now
  allows up to `min(18rem, 40dvh)`. The final browser gate passed all 16 scenarios,
  including full heading and retry-button visibility at 1280/720/390px;
  evidence `/tmp/claude-hub-browser-mocks.u8xk0p53/run`. Its Vite process and
  browser contexts closed successfully; the owned port was no longer listening.

No live provider or browser-to-Merlin integration is claimed. No production
configuration, service, session, provider version or delivery behavior changed.
The specific screenshot's network/HTTP failure remains unconfirmed; the new
error details make the next occurrence distinguishable.

## Cross-review — 2026-11-03

Two independent reviewers inspected the cumulative `5a14598..2c3a210` diff.
The user authorized integration after resolving review findings, but did not
request deployment or a shared-service restart.

- Corrected the absolute claim that refreshing never restarts a provider.
  Native Goal GET calls `transport.start()`: it reuses a healthy Codex process
  but can terminate/relaunch a half-started process. Guidance now says a refresh
  may retry provider startup; no provider version or configuration is changed.
- Added guidance to the existing Goal details surface, including the reachable
  case where Goal creation is persisted before the startup response fails.
- Disabled only Goal submission during status hydration; Cancel stays available.
  This prevents a concurrent Start from leaving a spurious loading error.
- Reject malformed inbox envelopes before replacing receipts. HTTP 200 with
  missing/non-array `messages` now preserves prior receipts and reports an
  unreadable response, just like malformed JSON.
- Browser coverage now waits for successful GET responses, holds Goal hydration
  to inspect submit/Cancel availability, and covers persisted Goal startup
  failure and malformed HTTP 200 inbox responses. A compiled Vue component unit
  test leaves automatic timers frozen while invoking the rendered retry handler
  and checking its own GET and receipt update. Existing row filtering is retained.
- The first revised full run inherited `FORCE_COLOR=3`; an unrelated CI-health
  test expects a plain `ERROR: ` prefix and failed on ANSI output. Its eleven
  tests pass with `FORCE_COLOR` unset and `NO_COLOR=1`, without source changes.
- An initial browser attempt to pause the page clock could allow an in-flight
  poll to clear the error before the click. Removed that test-only clock control;
  browser checks retain normal user clicks, while the deterministic component
  test establishes that automatic polling cannot satisfy the manual-retry check.

Focused and full validation of the revised candidate, followed by both reviewers'
rechecks, are required before integration. Previously recorded backend/HMR
results remain historical evidence, not a claim that the revised candidate has
already passed its final checks.
