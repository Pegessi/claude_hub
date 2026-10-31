# Antigravity authentication recovery — 2026-10-31

## Scope

Base: `ec40d57` (Antigravity provider merged into main). The user reported
`authentication failed or timed out` and `wait HTTP 409` after deployment.
This branch improves Hub's error presentation. The host-local `agyx` wrapper
and its proxy are separate deployment dependencies, not Hub package files.
No Hub restart, account removal, repository merge or push is part of this work.

## Direct evidence

The affected `ch gemini` Chat launched three CLI processes at 15:42:16,
15:42:31 and 15:43:50 CST. Each loaded the same token with an expiry of
15:41:14 and `expired=true`, then reached `keyringAuth: timed out after 10s`.
Headless login had no controlling terminal, so the CLI exited with code 1.
A user-authorized Retry at 15:58:49 reproduced the same error after 10.2 s.
The existing user task was not resent; the probe only requested a fixed reply
without tools.

An isolated copy of the deployed proxy was then run on its own port. Its four
configured warm pools were allowed to fill completely: 12 cloudcode connections
and six each for `www.googleapis.com`, `play.googleapis.com` and
`aicode.googleapis.com`. The first actual CLI authentication request was
`POST oauth2.googleapis.com/token`. This hostname was absent from the warm
pool list. The proxy was still establishing that connection when the CLI
exited with its authentication timeout. Only host, query-free path and timing
were recorded; no request headers, credentials or token responses were logged.

This distinguishes a missing refresh-endpoint connection from an invalid
account. Unauthenticated GET probes returning quickly do not validate token
refresh, and the historical successful tests with an unexpired token did not
cover this failure.

## Hub error and recovery behavior

- CLI `result.status=ERROR` becomes an error event and failed completion. A
  subsequent fatal process EOF preserves the redacted exit diagnostic and marks
  the transport as failed.
- `/stream/wait` returns HTTP 409 for that failed transport. This is distinct
  from Hub web authentication, which returns HTTP 401.
- A normal refresh does not replace the failed provider. Explicit Retry creates
  a replacement process, preserving persisted conversation identity and history.
  Retry does not send a message or prove the replacement CLI has authenticated.
- History remains readable through `/stream/events?persisted_only=true` without
  launching a new provider.

Previously the frontend discarded the 409 detail and displayed only its status.
It also recommended raw `agy` for every Antigravity failure, although this host
uses `agyx` to supply the proxy and TLS configuration. The branch retains the
status code and existing retry decisions, adds only string JSON error details,
and renders the result through Vue text interpolation. The banner explains the
network/sign-in checks and no-resend behavior. Wrapping prevents long details
from hiding Retry in narrow panes.

## Host-local proxy recovery

The deployment candidate adds `oauth2.googleapis.com` to the warm targets,
uses a nonblocking SSL read to distinguish TLS control records from EOF, and
checks idle sockets before maintenance counts them as available. It requires
all configured targets before `listen()`: 12 cloudcode tunnels and six each
for OAuth, userinfo, telemetry and config hosts. Initial preparation has a
240 s deadline; the wrapper waits up to 250 s before it starts the CLI. This
keeps proxy preparation outside the CLI's fixed authentication deadline.
Request forwarding, token contents, eligibility-response handling and the
existing request-replay policy were not changed.

In the isolated live candidate, the same expired token was automatically
refreshed by the official CLI in 1.20 s. Silent authentication succeeded after
5.67 s, and the real model returned `AUTH-PROBE-OK`. The OAuth request acquired
a prepared connection immediately. No credentials were deleted or manually
rewritten.

At 16:23 CST the reviewed proxy and wrapper were atomically installed. No
Antigravity CLI was active during the old proxy's replacement. The shared Hub
was not restarted. By then the original failing Chat had been removed outside
this task, so final validation used a new task-owned Chat on the main Hub:

| Check | Result | Time |
| --- | --- | ---: |
| First request, including a cold proxy start and full prewarm | completed; `AUTH-RECOVERY-OK`; no tools | 144.331 s |
| Second request, same Chat | completed; recalled that marker; no tools | 5.993 s |

The first duration includes one-time proxy initialization and must not be
reported as steady-state model latency. The temporary Chat and its CLI were
removed afterward. The fixed proxy remains listening on 18123; Hub's health
instance ID stayed unchanged throughout the operation.

Deployment source hashes:

- `proxy.py`: `17d48db58b1b6bb9399e12dd4ffc66dd3bd868481f41c1162edfb289460e2763`
- `agyx`: `e9006ba0dcb95dc105d27b593fa9e78d655335044795aaa259bab1597be398ee`

## Verification and independent review

- Standard checks: backend format/types, checkout safety, docs, frontend lint,
  types, **1151 unit tests** and production build passed. The default backend
  suite passed **5942 tests, 10 skipped** with the pinned Python 3.11.16.
- The initial `verify.sh all` backend run encountered an existing test's
  `ERROR:` prefix parser under inherited ANSI color output. With `FORCE_COLOR`
  and `PY_COLORS` unset, the focused 11-test CI-health suite and the full default
  backend suite passed. The failed evidence was retained; no unrelated source
  change or test exclusion was used.
- Official mocked browser runner: **16 scenarios passed**, including full
  64 KiB error text, keyboard scrolling to its end, ordinary Retry clicks,
  retained history and reachable composer at desktop and 390 px widths.
  Browser APIs were mocked; this does not replace the live provider checks.
- Explicit terminal HMR gate: **12 passed**. Its first attempt could not find
  pnpm in the private HOME; explicitly reusing the already-installed Corepack
  cache fixed the environment without downloads or source changes.
- External proxy offline suite: **22 passed**, including a real loopback TLS
  1.3 session-ticket exchange, stale-idle replacement, all-target readiness,
  startup failure without a listener, three concurrent warm OAuth acquisitions,
  and unchanged forwarding/replay function bodies.
- Independent review found and closed two issues: readiness initially allowed
  only two connections rather than the full configured targets; a complete
  64 KiB error could push Retry off-screen. The final code uses full-target
  preparation and a bounded, keyboard-accessible scrolling error region.
  No remaining code-review blockers were identified.
- Task-owned browser/Vite/proxy probes were stopped and their ports closed.
  Browser reports prove owned lifecycle closure; their generic
  `process_tree_exit=not_verified` is not presented as proof of every possible
  descendant's absence. Terminal replay was not run.

Local evidence is retained in the operator workspace under
`runtime/antigravity-auth-recovery/` (baseline/candidate proxy sources, offline
regressions, live probe logs, deployment record and Chat verification) and
`antigravity-auth-recovery-ui/browser-large/` (browser reports/screenshots).
The Hub UI branch is prepared and verified but is not merged or deployed by
this task. Its improved error display is separate from the already-active
host-local authentication fix.

## Remaining limits

Warm pools cover their configured capacity, not arbitrary parallelism. A burst
larger than the pools, simultaneous upstream connection loss or slow upstream
responses can still exceed the CLI's 10 s budget. Long idle/one-hour expiry
soaks were not run; stale maintenance has offline regression coverage and an
actual expired-token refresh was verified. Cold proxy startup may take minutes
on this egress path, and it now fails explicitly if full preparation exceeds
240 s. The wrapper's existing TCP probe does not identify an unrelated service
occupying the port. These limitations are not claims that the observed token
refresh regression remains unresolved.
