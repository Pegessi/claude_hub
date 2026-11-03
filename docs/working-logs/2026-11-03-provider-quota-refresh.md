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

## Verification

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
