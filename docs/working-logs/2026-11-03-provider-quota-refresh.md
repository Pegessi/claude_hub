# Provider quota refresh latency and Antigravity timeouts

## Scope and baseline

- User report: clicking Refresh takes too long to show each provider's quota;
  Antigravity often reports unavailable.
- Base: `5a14598`, feature branch `fix/provider-quota-refresh`.
- Worktree: `~/claude_hub_worktree/provider-quota-refresh`.
- Hub Task: `5ce2effe-67fd-4718-9653-0a2c39c3f52a`, initiator-controlled.
- No shared Hub restart, proxy reconfiguration, login change, merge or push.

## Evidence and implementation boundary

The old endpoint synchronously scanned Codex files, then awaited both MASO and
Antigravity before returning anything. Its full-response cache could not expose
a completed provider while another provider was still running.

Four bounded, sequential `agyx -p /usage --output-format json` probes against an
already-listening proxy succeeded in 8.461, 12.001, 9.291 and 7.379 seconds. Each
returned exit 0, `status=SUCCESS` and two groups. The old 10-second Hub timeout
would kill the 12.001-second successful call. This small sample proves the
budget mismatch, not a long-term failure rate. The slow sample's CLI log shows
silent authentication from 12:23:23.487404 to 12:23:31.426453 (7.939049 seconds).
No credentials or upstream payloads are retained in this report.

`agy --help` has no public quota-only command that skips initialization. Sharing
an active Chat process would interfere with its input ordering; a dedicated
persistent quota process would add lifecycle ownership. Direct internal Google
quota APIs would add authentication, proxy and schema maintenance. This fix
continues to use the existing `agyx /usage` path and does not introduce any of
those alternatives.

## Changes

- Optional validated `provider` on `GET /api/quota` returns the same
  `QuotaResponse` envelope, containing only the requested provider. Omitting it
  preserves the ordered aggregate API/CLI contract.
- The panel makes three independent requests and paints each as soon as it
  completes. Fixed provider cards, per-provider loading/error/retry, retained
  old readings and retired-request identities preserve UI state.
- Replace the full-response cache with three process-local provider entries.
  Aggregate and individual requests share cached results and active work.
  Disconnecting one requester does not cancel a query shared by others. A MASO
  force-refresh waits for an existing passive read and then coalesces into one
  forced read; Codex and Antigravity already read their source on every miss.
- Run Codex filesystem work in a thread instead of blocking the event loop.
- Give Antigravity its own 30-second budget, explicitly report a timeout, and
  reap the query subprocess after cancellation/timeout. The aggregate CLI HTTP
  deadline is 35 seconds. The shared proxy remains outside query ownership.
- On transient failure, retain the original timestamp and values with
  `available=false`, `state=unavailable`, `stale=true` and an explicit message.
  Clear values whose reset passed. An unconfigured provider clears old values.

## Feature verification before cross-review

- `./scripts/verify.sh all`: backend default suite **5986 passed, 10 skipped**;
  frontend **1157 passed**; backend types (614 source files), frontend types,
  frontend lint, build, docs and checkout safety passed. The aggregate command
  exited 1 only because the browser scenario needed Black formatting. After
  formatting with `backend/pyproject.toml`, `backend-format` passed (630 files).
  No passing behavior suite was rerun for this formatting-only correction.
- Focused backend/API/CLI independent review run: **149 passed**. Frontend quota
  unit tests: **55 passed**. Review found no P0–P2 runtime correctness defects;
  its only formatting finding was fixed.
- `terminal-hmr`: **12 passed**, default timing scale, private runtime removed.
  Initial setup failed because private HOME made Corepack try an offline
  download. A task-local PATH entry pointing to the already-installed, pinned
  pnpm 9.15.9 resolved it; no dependencies or shared configuration changed.
- Live candidate service-function probe (one parallel sample): Codex returned
  in **0.034 s**, MASO in **0.099 s**, Antigravity in **22.081 s**, all available.
  Cached aggregate returned in **0.000161 s**. This loaded the feature worktree's
  actual module, used real read-only sources and refused to run unless the
  shared Antigravity proxy was already listening. It was not an HTTP/browser
  measurement. Again, a successful Antigravity call exceeded the former 10 s
  limit, while other providers completed without waiting for it.
- `browser-mocks`: **16/16 scenarios passed**. The quota scenario holds
  Antigravity's mocked response until Codex and MASO are visible, then checks
  refresh, retained readings, provider-scoped retry, and 1280/720/390 px layouts.
  Mock Codex/MASO were visible at 146/149 ms; these are UI checks, not live
  upstream timing. All contexts/browser/driver closed, Vite exited normally,
  port 5297 was released and temporary runtime was removed. Early attempts
  encountered another task's fixed refusal-port usage and incorrect test
  assumptions about toolbar remounting/labels across breakpoints. The test
  now opens the actual desktop/mobile menu separately; product code was not
  changed to satisfy those assumptions. Final report:
  `claude-hub-browser-mocks.2dgv4qc7/run/summary.json`, with quota screenshots
  and assertions in `run/runs/browser_service_restart/`.

Evidence root: `~/claude_hub_workspace/provider-quota-refresh/`:
`verify-all.log`, `live-readings.json`, standard backend run
`claude-hub-verify.2h0yu5g8`, format rerun `claude-hub-verify.1k9zunqc`, and
terminal run `claude-hub-verify.5nwmyrim`. All test providers are mocked except
for the explicitly identified live read-only probes.

## Delivery boundary

The candidate is not deployed. Browser checks use mocked APIs; they do not
prove live provider availability. The CLI still needs upstream authentication
and network initialization, so Antigravity itself can take several seconds.
The optimization removes that wait from other providers and prevents the
observed premature timeout; it does not promise an instantaneous upstream read.

## Cross-review follow-up before integration

The user requested a second independent cross-review and local merge. Backend,
API and CLI review passed. Frontend review identified a reachable P2 gap in
`f8e4c75`: real provider failures return HTTP 200 with `state=unavailable`, while
the new Retry control only handled HTTP/schema errors. With another provider
still loading, even retrying a completed failed provider required waiting for
the global Refresh button.

The follow-up adds independent Retry for unavailable readings (excluding
`not_configured`) and always sets `refresh=true` to bypass the cached failure.
HTTP/schema errors still repeat the failed request's original cache intent.
Three added unit cases cover that distinction and another pending provider;
quota unit tests now pass **58/58**. The browser scenario now exercises the real
HTTP 200 unavailable envelope as well as the existing HTTP 503 path. Final
integration/browser results are recorded with the merge verification evidence.

## Final cross-review and local integration

- Backend/API/CLI cross-review found no quota correctness blocker. Frontend
  cross-review closed the P2 after independently reviewing `0c95e2e` and rerunning
  all 58 quota unit tests, types, lint and scenario formatting.
- While review ran, another task advanced `main` to `d0977f6` with Chat Goal/inbox
  recovery guidance. Merge `7eb3b6a915dc35e2be2f5e3a12505b9ec36103b7` combines
  that parent with `0c95e2ebddb5b9528c83ae19bfe845079d0f6bc0`. Its source tree is
  `193a236a1f0e465f5dde947ae1f3880b7aa9a4c5`.
- The only conflict was two new `CHANGELOG.md` entries. Both are preserved in
  full; no source conflict required a manual edit. The adjacent
  `2026-11-03-provider-quota-merge-decisions.json` binds this resolution to the
  immutable merge, parents and blobs.
- On that exact staged tree, frontend **1163/1163**, browser-mocks **16/16**, both
  type checks, lint, formatting and build passed. Browser assertions include
  real-contract HTTP 200 unavailable, retry with `refresh=true` while Antigravity
  remains pending, old-reading preservation, HTTP-error retry identity and
  desktop/narrow/mobile layouts. The staged tree was unchanged after verification
  and equals the merge tree. All browser/Vite processes exited normally.

### Backend validation limits and reused evidence

**This integration's default backend full-suite attempts did not finish green.**
They exposed two unchanged, independently diagnosed timing problems:

1. The first final attempt reached 5574 passing tests, then
   `test_suppressed_count_flushes_after_window` hung. Python 3.11
   `asyncio.wait_for` can swallow external cancellation when its inner future
   has already finished; the monitor catches the resulting inner exception and
   continues. A deterministic probe reproduced this cancellation race. The
   existing helper cancels once and waits without a deadline. Production monitor
   stopping has a related pre-existing risk; it is not changed by quota work.
2. A bounded full rerun reached 2156 passing tests, then
   `test_continuous_message_burst_is_dispatched_at_the_max_wait` observed two
   batches rather than one. It assumes three real 35 ms sleeps fit 50 ms quiet /
   120 ms maximum windows. Scheduler delay can legitimately cross either window;
   the production coalescer uses actual arrival times correctly.

The final staged `backend/` Git tree and verification script/interpreter pins
are identical to the previously reviewed `f8e4c75` backend candidate carrying the
5986-pass result above. The integration reviewer also ran the failing Feishu
case plus the complete monitor test file through the isolated verify entry with
an OS deadline: **13 passed**, exit 0, in `/tmp/claude-hub-verify.paoiqu7k`.
These focused results do not convert the failed full runs into a full-suite pass.
The reviewer accepted quota integration with this explicit baseline limitation;
unrelated monitor/test-stability fixes remain separate follow-up work.

Final evidence root: `~/claude_hub_workspace/provider-quota-integration/`.
`candidate.json` records the tested tree; `verify-all-final.log` preserves the
interrupted backend run and passing frontend targets; `backend-bounded-rerun.log`
preserves the second baseline failure; `verification-equivalence.json` records
Git object equality. Browser evidence is
`claude-hub-browser-mocks.hipzsj_p/run/summary.json`, with the new assertion
`quota_200_unavailable_retry_while_pending=true` in the quota scenario report.
The earlier pre-fix attempt was interrupted when P2 was found, is not counted as
a pass, and retains its disposable runtime `/tmp/ch-v-q1zit6u9` for diagnostics.

User authorization covers local `main` integration only. No push, deployment,
proxy/login change or shared-service restart is part of this delivery.
