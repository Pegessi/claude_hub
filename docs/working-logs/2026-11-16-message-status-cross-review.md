# Message status recovery: independent cross-review

## Fixed scope and verdict

- Task: `3c06a9b0-001f-40e2-9cc1-3030f82257d7`.
- Original candidate: `b5c103d959cdf36349b94a55c1e881539bc65492`,
  `codex/fix-message-status-lockout`.
- Target and merge base: `114f28a4df0ee6fa67ed086b7827ae4f636688cf`.
- Reviewed the complete eleven-path candidate diff, not just the brief's file
  examples. This log adds a twelfth path. Applied lesson:
  `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`.
- Verdict after fixes: **MERGE**. Independent Astra frontend re-review found no
  remaining P0/P1/P2 blocker; the owner checked backend contracts and reproduced
  the two original frontend findings before fixing them. Integration is local;
  no push, deployment, or shared-service restart is part of this task.

## Findings and fixes

1. **P2: status recovery preempted successful automatic hydration.** A capability
   503 scheduled auto-recovery. While its next capability request was pending,
   the state still said `failed`; inbox recovery then invoked provider `retry`,
   aborting that observation generation. If the retry POST failed, even a
   successful original capability response could not reopen the composer.
   `reconnect()` now uses the existing observation `start()` path, coalesces an
   existing backoff, and never calls the provider-replacement endpoint. Active
   hydration reports `hydrating`/`reconciling`. Same-source readable history is
   retained even when capabilities have never succeeded. Explicit reset and
   different-source history isolation remain intact.
2. **P2: repeated malformed HTTP 200 envelopes hid errors forever.** Resetting
   `consecutiveFailures` before parsing made every invalid envelope look like
   the first failure. Reset now follows successful parsing. Regression coverage
   verifies repeated errors, the retry control, and successful recovery.

The race test uses real `StructuredPane`, `ChatMessageInbox`, and
`useAgentStream`, with HTTP/timers mocked. Both regression tests failed against
b5c103d before production edits and passed afterwards. Additional tests cover
backoff coalescing, history retention after the retry budget, and cleanup through
stop, unmount, and explicit provider retry.

## Backend and lifecycle review

- `ChatMessageInbox.list()` does not await between snapshot, filtering, sorting,
  and receipt serialization. Mutations/persistence are synchronous on the same
  event loop; it cannot interleave halfway through one synchronous state change.
  This avoids waiting on the pump lock, not synchronous event-loop work itself.
- Indexed evidence reads constrain `name`, `turn_id`, and `sequence`, in sequence
  order, using the existing `(name, turn_id, sequence)` index and WAL read
  snapshot. The completed/cancelled/failed, synthetic-rejection and legacy-error
  interpretation is unchanged. Cancellation joins the worker I/O before return.
- Session refresh compares the four fields it actually derives, but still writes
  on task fallback, status, runtime status, or activity change. Sampling time alone
  no longer causes a state write. Existing live-session identity and spawn-grace
  checks remain ahead of mutation.
- Stream stop cancels retry timers and fetches; generation checks retire late
  responses. Inbox deactivation stops its timer and aborts its request; active
  pane checks prevent hidden observation recovery.

## Validation and retained evidence

All tests ran in foreground with task-owned runtime state. Tools matched the
repository pins (Python 3.11.16, Node 24.21.0, uv 0.12.15, pnpm 9.15.9).

| Check | Result | Evidence |
| --- | --- | --- |
| Focused backend contracts (8 modules) | 164 passed | `/tmp/claude-hub-verify.o2h8g3vs` |
| Full default backend suite | 6633 passed, 10 skipped, 17 warnings; 466s | `/tmp/claude-hub-verify.dp94uz47` |
| Backend Black/isort and complete mypy | passed | `/tmp/message-status-review-all-2.log` |
| Frontend lint and vue-tsc | passed | `/tmp/message-status-review-all-2.log` |
| Complete frontend unit suite | 1511 passed, no skipped/cancelled | `/tmp/message-status-review-frontend-3.log` |
| Frontend production bundle | passed; existing large-chunk warning | `/tmp/message-status-review-build-3.log` |
| Independent Astra targeted review suite | 86 passed across 5 suites | Task cross-review report |
| Mocked browser registry, entries 1–13 | 13 passed | `/tmp/claude-hub-browser-mocks.2liyv982` |
| Mocked browser registry, entries 14–25 | 11 passed; Markdown timing flake | `/tmp/claude-hub-browser-mocks.c4bt25kv` |
| Focused Markdown browser rerun | passed without code/test change | `/tmp/claude-hub-browser-mocks.vcchip94` |
| Isolated real terminal HMR | 13 passed, JUnit verified | `/tmp/claude-hub-verify.f7izo59g` |

`verify.sh all` was invoked twice. The first invocation exposed injected
`FORCE_COLOR=3`: an existing subprocess diagnostic parser expects `ERROR:` at
column zero and received ANSI prefixes. Removing only this environment injection
made the full backend suite pass. The second invocation exceeded the tool's
500-second bound during frontend tests, after backend/formats/types/lint passed;
only its unfinished frontend tests/build were rerun to completion. These are
per-target passes, not a claim that one uninterrupted `all` command exited zero.

The broad browser command also exceeded 500 seconds, so the unmodified registered
scenarios were executed as two foreground shards through the existing runner and
runtime owner (registry slices `[:13]` and `[13:]`). Both use the same source,
locked browser, and checks. A refusal-port collision aborted one setup before any
scenario; retry used owned ports 5299/59998 instead of 5298/59999. No unrelated
process was stopped. The second shard's Markdown failure was an existing
rounded-opacity wait followed by exact equality (`0.998585` versus `1` at
`browser_markdown_reader.py:354`); a single focused rerun passed. Thus all 25
scenarios have passing evidence, but the flaky shard was not relabeled green.

Terminal HMR initially could not find pnpm in its isolated HOME with network
disabled. Supplying the existing `COREPACK_HOME` cache fixed setup without
changing dependencies. Runtime-owner records show test children exited and
private runtimes were removed; Vite ports stopped accepting connections.

## Evidence limits and handoff

- Browser checks render real Vue DOM against mocked APIs; HMR uses an isolated
  backend/tmux. Neither proves live provider connectivity or deploys these fixes.
- The default backend suite's ten skips remain disclosed; no coverage claim is
  made for its excluded replay/performance suites.
- Existing Markdown timing flakiness remains outside this behavior fix.
- The managed final report records the committed fixup, local merge SHA, staged
  and fixed-merge checks, clean-main verification, and exact worktree cleanup.
  Human Task acceptance stays with the controller/user.
