# Chat history during provider failure and JSONL append safety

Task: `13874efd-686e-4a9d-99d5-844405887974`.
Base: `d8316e7`; branch: `codex/chat-history-fallback`.
Worktree: `/home/tiger/claude_hub_worktree/chat-history-fallback`.

## Behavior and ownership

A native provider handshake is live execution readiness, not permission to read
an existing conversation. Native Chat now retains structured capabilities with
an uncached unavailable reason when initialization/preparation or runtime fails.
Terminal sources also retain structured access when persisted events exist.
The history endpoint checks durable history before capabilities/startup and
returns raw, compact, conversation and fixed-turn pages through existing Store
readers. Exhausted pages retain their requested cursor. Empty native history
also remains readable on startup failure. Live subscription/send admission
continues to use the existing provider and runtime error paths.

Store mutation locks are shared weakly by canonical path across instances.
Append uses a stable `.append.lock` advisory file across sequence recovery and
binary writes. File identity/size/mtime detect intervening writers so sequence
caches recover before allocation. Short writes finish while holding the lock;
an exception truncates the partial record before the lock is released.
Cancellation still joins the disk worker. Advisory locking requires cooperating
writers; deployed old processes do not acquire the new lock. Cross-process
clear/replace operations are not newly coordinated by the append sidecar.

## Runtime repair

The approved file was repaired without restarting the service. An exclusive
Linux write lease fenced existing and newly opening readers/writers throughout
backup, same-inode repair, fsync and full-file validation. Lease acquisition
would fail if another process already held an open descriptor.

File: `~/.claude_hub/workspaces/terminal-tabs/agent_streams/terminal-tab-f6dc3483-d19e-4b89-ab6f-3eeb5f75c024.jsonl`.
Backup suffix: `.backup-20261110T133550Z` (verified byte-identical before repair).
Original SHA-256: `290acce531eafbe682fda2add4da94e9929444ca06fd5267c7d83f8e79186077`.
Repaired SHA-256: `a79ffae87aae7b1e3905202133b7e4eb65a8f44bf4b1f6a6d739eabc82fe9a7d`.

Contrary to the initial description, line 1760 contained a 296-byte incomplete
`tool_call_completed` prefix with a second, complete `tool_call_started` object
embedded in it. The complete object and every other line were retained exactly.
The incomplete prefix cannot be parsed or reconstructed without inventing data;
its bytes remain preserved in the backup. No sequences were renumbered.
The repaired file has 1,871 lines and zero JSON parse errors; size changed from
1,825,063 to 1,824,767 bytes.

## Verification

Focused runtime/history/lifecycle/retirement checks initially passed 236 tests.
Final focused validation passed 239 tests, including empty native and persisted
Terminal failure behavior.
Regression coverage includes multiple Store instances, three concurrent writer
processes, successful short writes, failure rollback and cancellation lifecycle.
The full default backend scope passed 6,225 tests with 10 expected skips.
Three additional failure-state test cases added after aggregate collection are
covered by the final 239-test focused pass. Backend formatting and complete
types (619 source files) passed. All 18 mocked
browser scenarios passed, including the full Chat composer/history cases.
The owned Vite process exited normally and port 60873 was closed. Standard `./scripts/verify.sh all` passed, including frontend lint, types,
1,279 unit tests and build. Evidence: `/tmp/claude-hub-verify.x3vvyx5i` (backend),
`/tmp/claude-hub-verify.73yg1xez` (final focused), and
`/tmp/claude-hub-browser-mocks.tbe25dpg/run/summary.json` (browser).
The first browser launch found port 5397 occupied; no unrelated process was
stopped. Retried with task-owned port 60873. Browser checks use mocked APIs,
not a real provider or the deployed runtime.

## Review and remaining boundaries

Review the full diff against all acceptance criteria, including runtime repair
evidence rather than only the initial file list. Relevant lesson applied:
`a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`.
An independent managed review and human acceptance are required. No main merge,
push, deployment, shared-service restart, or SQLite migration is included.
