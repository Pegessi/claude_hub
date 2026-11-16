# Working Log: Unified Review vs Done Lifecycle across Chats & Workspaces (`2026-11-16`)

## Objective & Cross-Validated Design

Unified the `Review` vs `Done` lifecycle across Chat sessions (`ChatSidebarRow`, `useTabStatus`, `inboxModel`) and Agent Workspace (`task_lifecycle.py`, `triageProModel.ts`) using **Work-type + Natural Terminal Signals**:

1. **SQLite Stream Summary & Honest Sidebar Activity Time (`services/agent_stream/store.py`, `terminal_runtime/manager/status.py`)**:
   - Repaired post-SQLite migration `ReplyExcerpt` reads (which previously checked only legacy `.jsonl` files) with a generation/sequence-keyed `read_stream_summary_sync` projection over `stream_events`.
   - Surfaced `last_activity_at` on `TerminalAgentStatus` so Chat sidebar rows render the newest meaningful turn/reply timestamp instead of `tab.created_at`.
2. **Non-Blocking Delivery MR Cache & Calm `Review` Projection (`services/mr_status_cache.py`, `manager/status.py`, `tmux/board.py`)**:
   - Added a bounded stale-while-refresh `MRStatusCache` (`bytedcli --json codebase mr get <url>` off-request with concurrency cap 3, TTL 180s, error backoff 120s, disabled by default in pytest).
   - Idle sessions with open delivery MRs render a calm non-interactive `MR !<iid>` review badge (`data-review="true"`) alongside `data-status="idle"` without violating `Session status:` accessibility or unread contracts.
   - Confirmed `merged` MRs automatically transition open tasks to `DONE` (`closed_reason="mr_merged"`).
3. **Intentional Chat Archive -> `Done` & Lazy Reopen on Restore (`_task_execution.py`, `task_lifecycle.py`, `api/tabs.py`)**:
   - Archiving an originating Chat settles active initiator-controlled Tasks to `DONE` (`closed_reason="chat_archived"`, recording `TaskArchiveClosure` provenance with `last_stream_sequence` and `execution_epoch`) instead of `ORPHANED` (`Session lost`).
   - Restoring (`unarchive_tab`) to read history leaves the Task `DONE`. Starting a genuinely new turn (`TURN_STARTED` past `last_stream_sequence` on the main thread) or sending authenticated active `task progress` lazily reopens only that archive-closed Task (`DONE -> WORKING`), leaving historical `DONE` Tasks untouched.
4. **Safe Snapshot Preview Mode (`CLAUDE_HUB_SAFE_PREVIEW=1` in `main.py`, `retirement.py`)**:
   - Allows an isolated preview backend over a SQLite-backup snapshot to serve live `/api/tabs/status` and `/api/workspaces/board-overview` (including archive/restore previews) without spawning saved tmux/provider processes, restart continuations, Feishu bots, or workspace monitors.
