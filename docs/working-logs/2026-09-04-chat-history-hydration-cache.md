# 2026-09-04 — Long structured Chat history hydration

## System Overview

Opening a top-level Chat mounts `StructuredPane`, which hydrates the durable
JSONL event stream before enabling live reconciliation. Switching away
unmounts that pane. Before this change, switching back discarded all browser
history and fetched the entire stream again.

The observed Chat contained 54,745 events (about 22.8 MB), dominated by
thinking and text deltas. A frontend-shaped hydration against the previous
backend, using its maximum 1000-event page size, took 19.055 seconds across 55
serial requests.

The delay had two independent causes:

1. `useAgentStream` always reset its cursor to `-1`, so every remount repeated
   full network transfer, JSON parsing, and timeline reduction.
2. Every API page constructed a new `AgentStreamStore`; `read_since` opened the
   JSONL file at byte zero and parsed all earlier rows before returning the
   requested suffix. Paging the whole history was therefore quadratic in the
   event count.

## Module Design

- `agentStreamHistoryCache.ts` owns a process-local, three-entry LRU. A
  snapshot keeps capabilities, the immutable-by-convention event array, and
  the contiguous cursor. Event arrays are retained by reference rather than
  duplicated.
- `useAgentStream.ts` restores a cached snapshot synchronously and enters a
  `reconciling` state. The timeline is visible immediately, while capabilities
  and events after the cached cursor are fetched in the background. The
  composer remains disabled until the stream reaches `live`.
- Cold hydration uses bounded 5000-event pages instead of 200-event pages.
  Long-poll remains the correctness path and keeps its existing small bound.
- `AgentStreamStore` records sparse sequence-to-text-offset checkpoints every
  256 events and exact checkpoints at returned page boundaries. A session
  read lock protects the process-local index. File replacement, truncation,
  explicit clear, and `replace_all` invalidate it.
- `TailerManager.get_store` returns the active tailer's store, allowing
  sequential API requests to share the index. Calls without an active matching
  tailer retain the prior behavior and receive an independent store.

## Key Issues / Pitfalls

- The cache is intentionally small because one long event history can occupy
  substantially more browser memory after JSON parsing than its on-disk size.
- Cached events are only presentation state. Sequence buffering still admits
  events contiguously, and long-poll plus SSE retain their generation guards
  and deduplication behavior.
- A cached timeline must not enable Send before missed events and current
  capabilities reconcile; `reconciling` is visible but not writable.
- File offsets are Python text-stream seek cookies. They are only reused with
  the same UTF-8 read mode and are discarded when the backing inode changes or
  shrinks.
- Direct read-only validation against the observed 54,745-event log took 421
  ms with 1000-event pages (55 pages) and 394 ms with 5000-event pages (11
  pages). These measurements cover the new store implementation, not HTTP,
  browser JSON parsing, Vue rendering, or network latency.

## Validation

- Agent-stream backend suite, including offset reuse, append-after-EOF,
  replacement invalidation, and active-store reuse regressions.
- Backend code-level suite excluding the three real ttyd/tmux/Playwright files:
  1257 passed. The unfiltered run reached 1283 passed and 1 skipped; its 14
  failures were confined to those environment E2E files (isolated tmux startup
  and one terminal replay deadline), outside the changed Chat stream paths.
- Frontend cache LRU/reference tests, hydration/reconciliation contract tests,
  type checking, lint, production build, and the 266-test frontend unit suite.
- Dedicated Vite review server on port 5275, stopped after HTTP smoke checks.
  The live 5173/8173 services were not stopped or restarted. The repository's
  unfiltered terminal E2E fixture reused the configured 8173 backend; its
  temporary tabs completed fixture cleanup and no matching test tabs remained.
