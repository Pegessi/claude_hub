# Chat history-first loading and capability reuse

## Scope and evidence

The user approved showing existing history before runtime capability preparation
and requested roughly 30-minute capability caching. A prior read-only diagnosis
measured a 4.93-second capabilities request followed by an 85ms history request.
The slow request overlapped Codex network selection (direct probe timeout, then
proxy selection). The user's original failure banner was not reproduced; this
change addresses the demonstrated coupling and tests failure behavior explicitly.

Base: `34dc7c8d1c1c05540af2cceb2012e71c1471670f`.

This session restricts writes to its working directory. Work therefore uses a
new, independent feature checkout inside that directory, not a linked worktree
outside it. The primary checkout, external worktree configuration and shared
services are unchanged. No deployment, restart, merge or push is authorized.

## Behavior

- Frontend history hydration and capability preparation run concurrently.
  `historyReady` controls transcript visibility independently of live controls.
  A capability error leaves successfully loaded history readable and shows the
  existing retryable error; it does not enable sending.
- `persisted_only=true` on either history events route reads the durable store
  without capabilities or runtime startup. Authentication, resource resolution,
  pagination, compacting and fixed detail ranges remain in force. The default
  route behavior is unchanged. Live observation and actions keep their checks.
- Browser capability hints are process-local, successful-only, source/config
  scoped, limited to128 entries, and expire30 minutes after the response. Cache
  reads do not extend TTL. There is no localStorage persistence.
- Provider/environment/incarnation changes invalidate the browser hint.
  Explicit capability refresh sends `refresh=true`; mode updates replace hints
  from their response. Late generation/configuration/update responses are
  ignored. Live capability handshakes update current values.
- Backend observation caches expensive successful preparation, not a serialized
  full capability response. Current model, mode and availability fields are
  obtained from the current transport. Action/send paths retain fresh checks.
- MASO's existing preparation combines model metadata and live service readiness.
  It is excluded from long-lived preparation/browser-hint reuse so service loss
  is not hidden for30 minutes. Splitting its catalog and health API is outside
  this focused change.

## Review and validation

Independent frontend and backend review found four actionable boundaries, all
corrected and rechecked: settings must stay locked during reconciliation;
`structured=false` settings responses must fail closed; cancelled forced refresh
must clear a previous successful cache entry; source-location and remote/solo
configuration must participate in the preparation key. Regression tests cover
these cases. Empty first-touch history also gets a compacted catch-up after
preparation, before raw live reconciliation.

`CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 ./scripts/verify.sh all` passed with Python
3.11.16, uv 0.12.15, Node 24.21.0 and pnpm 9.15.9:

- Backend: 4,776 passed, 10 skipped; types passed across 549 files.
- Frontend: 1,108 unit tests passed; ESLint, types and production build passed.
- Repository docs, source safety, Black and isort passed.
- Isolated terminal HMR gate: 12 passed, no skipped cases.
- Full mocked browser gate: all 14 scenarios passed, including manual/managed
  history-first loading, failure readability, readiness gates, native cache reuse
  and fresh MASO readiness. All runner lifecycles passed and the owned Vite port
  was closed without forced termination.

An earlier run used pnpm12 from PATH and the wrong Black configuration for a
browser file. Those setup errors were corrected and the strict standard gate
rerun successfully. The provenance browser fixture's exact query whitelist was
updated for `persisted_only=true`; this did not relax its unknown-route checks.

Browser tests use mocked APIs and task-owned resources. They do not establish
live provider connectivity or deployment acceptance. Evidence is retained in the
task's `verification/` directory, with final browser summary
`claude-hub-browser-mocks.zozN2l/run/summary.json` and terminal receipt
`claude-hub-verify.iMFvdb/junit.xml`. The original user-visible failure was not
directly reproduced. No live-service integration or deployment is claimed.
