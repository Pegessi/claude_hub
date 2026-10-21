# Focused fixes from the capability-cache cross-review

Base: `7c5e73b9f231899380017e9e29563296e2aad00a`.

The user authorized fixes for the four P2 findings from the read-only cross-review.
This follow-up is limited to those findings; no merge, push, deployment or shared
service restart is authorized.

## Required corrections

1. Pending/failed Retry preserves the already-read transcript for the same source,
   including when capabilities are null/unavailable. Changing sources must not
   transfer that transcript to another Chat.
2. The background queued-chat observer uses the same configuration identity as the
   active pane, and reacts to source/config changes rather than using an empty
   cache signature. Presentation-only changes do not restart observation.
3. `StreamCapabilities.metadata_cacheable` distinguishes usable fallback UI data
   from metadata suitable for long-lived caching. Both backend preparation and
   frontend hints respect it. Failed/incomplete discovery retains its retry
   behavior; successful discovery can still use the requested cache.
4. Managed `persisted_only` reads do not acquire the expensive capability
   preparation's exclusive guard. Exact incarnation checks remain before and
   after the durable read; default observation and action paths are unchanged.

## Implementation and review

- Retry snapshots flush pending event batches and retain the current history,
  including details loaded while Retry waits. Source changes do not borrow the
  old transcript. Sending and settings remain disabled until readiness returns.
- Queued observers share the active pane's configuration signature, including
  the tab incarnation and remote settings, and restart on relevant changes.
- Model catalogs explicitly report whether discovery completed. Cursor keeps its
  existing short negative TTL; Codex/TraeX retry incomplete discovery. MASO remains
  excluded from long-lived readiness caching. A previously eligible backend
  cache entry is discarded if current metadata becomes uncacheable.
- Managed persisted-only history skips the runtime preparation lock and rejects
  a replaced session before or after reading. Other interaction guards remain.

The owner reviewed both implementation scopes and corrected two Retry snapshot
edge cases and a Codex empty-catalog eligibility edge case before the final run.
The final backend and frontend scopes were frozen before full validation.

## Verification

Strict toolchain: Node 24.21.0, pnpm 9.15.9, Python 3.11.16 and Playwright 1.58.0.

| Check | Result |
| --- | --- |
| Backend focused suites | 402 passed |
| Frontend focused stream/cache/observer tests | 45 passed |
| `verify.sh all` | Passed; backend 4,781 passed / 10 skipped, frontend 1,112 passed |
| Documentation, checkout safety, format, lint, types and build | Passed in `all` |
| `verify.sh browser-mocks` | All 14 scenarios passed, including pending/failed/successful Retry visibility |
| `verify.sh terminal-hmr` | 12 passed |
| Original managed-lock reproduction | History completed while capability preparation still held its guard |
| Original Cursor reproduction | Fallback at first read; recovered model catalog at simulated 61 seconds, probe count 2 |
| Original Retry reproduction | History ready and event count 1 before, during and after failed Retry |

Evidence root:
`/home/tiger/claude_hub_workspace/chat-capabilities-cache/review-fixes/`.
Primary records are `verify-all.log`, `verify-browser.log`, `verify-terminal.log`,
`managed-probe.log`, `cursor-probe.log` and `retry-probe.log`. Browser details are
under `claude-hub-browser-mocks.ho7EKh/run/summary.json`; its owned Vite process
exited normally and its port was closed. The harness does not certify full
process-tree exit. The original read-only probes remain unchanged.

## Scope limits

The browser uses mocked APIs; provider discovery and managed lock probes use
controlled providers/stores, without real inference or network discovery.
The original user-reported top-level loading failure was not reproduced in the
initial diagnosis. This correction verifies the four reproduced review findings.
No shared-service restart, deployment, merge or push was performed.
