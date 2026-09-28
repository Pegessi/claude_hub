# Nested sub-agent thread attribution (TraeX / Codex collab)

Date: 2026-09-27/28
Branch: `fix/nested-subagent-thread-attribution` (worktree `subagent-thread-attribution`)
Commits: `5e1b397` (initial backend+frontend), `1f10c05` (unique keys + spawn-card
semantics), `8b9c083` (multi-spawn host attribution)

## Symptom

In a structured Chat turn where the main TraeX/Codex agent delegates to nested
workers via the collab protocol (`spawnAgent`, follow-up `sendInput`), the child
workers' reasoning, reports, and command executions were rendered as the main
agent's own bubble/process rows. With multiple parallel workers everything was
flattened together and the main thread read like it "had become" the sub-agent.

## Main vs sub-thread: deciding field (from real jsonl, not guessed)

Source of truth: real persisted stream
`~/.claude_hub/workspaces/terminal-tabs/agent_streams/terminal-tab-4ed6b70c-….jsonl`
(5,413 normalized events; 13 turns; 10 collab calls; 596 `tool_call_*` events
whose call id starts `code-mode-nested:`).

Raw app-server JSON-RPC notification shapes (consumed by
`codex_jsonl.py::_normalize_notification`):

- Main agent collab calls: `item/started|completed` with
  `params.item.type = "collabAgentToolCall"`, `item.tool` = `spawnAgent` /
  `sendInput`, `item.receiverThreadIds: [<uuid>…]`, and `params.threadId` = the
  **main** thread id. These are issued BY the main agent and stay main-stream.
- Child text/thinking: `item/agentMessage/delta` /
  `item/reasoning/textDelta` with `params.threadId` = the **child** thread id
  (≠ main) and `params.itemId`.
- Child command executions: `item/started|completed`,
  `item.type = "commandExecution"`, item id
  `code-mode-nested:<depth>:<host_call_id>:exec-<uuid>`. `<depth>` is a constant
  namespace token (`29` in all 596 real events) and is **not** a thread id.
  `<host_call_id>` identifies the nested code-mode host: one host per spawned
  worker, stable across all that worker's exec items (real turn 9ffdd6dd: 62
  distinct tokens, each scoped to that one turn). Some records additionally
  carry the owner in `params.threadId`; item-level fields seen in the protocol
  binary/schema are `threadId` / `senderThreadId` / `agentThreadId`.

Main-thread identity for comparison comes from the verified `thread/start` id
(`native.py::ProviderSession.active_thread_id` →
`NormalizeContext.main_thread_id`). One-shot/transcript paths pass `None` and
fail closed to main-stream attribution.

## Backend attribution

`backend/claude_hub/services/agent_stream/codex_jsonl.py`,
`backend/claude_hub/services/agent_stream/base.py`, `native.py`, `tailer.py`:

- `NormalizeContext.event(..., sub_thread_id=)` stamps `subagent_thread` into
  the payload and gives deltas a thread-scoped `message_id`
  (`{turn}:sub:{thread}:assistant|thinking`) so coalescers never merge a child
  stream into the main `{turn}:assistant`.
- Collab registry (per `session_id`): spawned threads from `spawnAgent`,
  addressed threads from `sendInput`.
- `_resolve_sub_thread` precedence:
  1. explicit owner (`params.threadId`, else item-level `threadId` /
     `senderThreadId` / `agentThreadId`) ≠ main → that child; for a
     `code-mode-nested` item it also **learns host-call-token → child**;
  2. `code-mode-nested:` id → learned host owner, else unique-spawned thread,
     else unique addressed thread, else stable synthetic `code-mode-<depth>`
     group (off-main, but not merged into a guessed receiver).
- Multi-spawn bug fixed: the old `_code_mode_thread` only used the spawn
  registry when `len(spawned) == 1`; with 4 spawns all ~600 nested tools fell
  into the synthetic bucket. The learned host-token map attributes each
  worker's later thread-less records (e.g. a bare `item/completed`) correctly.

## Frontend grouping and keys

`frontend/src/utils/agentStreamTimeline.ts`, `subagentTool.ts`,
`components/StructuredPane.vue`:

- Events with `payload.subagent_thread` are routed by
  `applySubthreadEvent` into one lazily-mounted `subthread` part per thread
  (`ensureSubthread`); the child has its own tool map, so call ids can never
  collide with main tools. Main bubble, main tool bucket, copy text, and
  process-step counts exclude child rows.
- `parseSubagent` marks `sendInput` as `directive: true`: it renders as an
  inbound instruction row inside each addressed child card (a multi-receiver
  call is filed into every addressed group). `spawnAgent` keeps its standalone
  Codex-style launch card on the turn — the prompt is shown exactly once.
- **Key fix (reviewer MUST-FIX)**: the sub-thread `v-for` used
  `:key="sub.kind"`; kinds repeat (`text`, `thinking`, `tool_group` appear many
  times), causing Vue vnode reuse and dropped/serialized rows. It now uses the
  reducer-provided unique `sub.key` (`sub-text-<seq>`,
  `sub-tool-group-<seq>`, `sub-instruction-<callid>-<tid>`, …). All other
  nested loops key on stable unique ids (`tool.key`, `question.id`).
- Blank/whitespace `subagent_thread` degrades to the main stream.

## Regression surface / validation

- Backend: `backend/tests/test_codex_subthread_attribution.py` (10 tests) —
  spawn stays main, child deltas stamped, unique-spawn nested fallback,
  explicit-thread distinction, **multi-spawn host-owner learning**
  (threaded delta teaches host; bare started/completed follow; unknown host →
  synthetic bucket), item-level `senderThreadId`, per-session registry
  isolation, no-main-context fail-closed, blank thread id.
- Frontend: `tests/nestedSubthreadAttribution.test.mjs` (8 tests; unique-key
  regression with repeated kinds, four-spawn grouping),
  `tests/structuredPaneSubagentCard.test.mjs`,
  `tests/chatProcessFold.test.mjs`, `tests/subagentTool.test.mjs`,
  `tests/agentStreamTimelineReducer.test.mjs`.
- Real-data evidence: tab 4ed6b70c — F6 spawn
  `01a0e2f6-…`, FEC peer `01a0e2ec-…`, and turn 9ffdd6dd's three
  `01a0e38b-…` spawns; 596 nested items carry the constant depth `29` but 465+
  distinct host tokens, proving the host token (not depth) is the per-worker
  discriminator. The live tab a50d8522 reproduced the same multi-spawn shape
  (three `01a0dc4…` spawns in one turn), confirming recurrence.

## Known non-blocking limits

- Attribution of a nested host requires at least one of its records to carry
  an explicit owner id; a host whose every record is bare and shares the turn
  with multiple spawns can only be placed in the off-main synthetic bucket.
  Real captures always show an explicit owner before the bare completions.
- Persisted normalized streams predate these fields; re-normalization/replay
  of a historical tab does not retroactively stamp old events.

## Backfill / old-session reopen — verified verdict (2026-09-28)

The commander question: after deploy, does reopening an OLD multi-spawn tab
(e.g. `a50d8522`) auto-move child content from the main timeline into sub-agent
cards? **No — not without a one-time migration.** Evidence:

- Chat is a **native** session. On reopen the SSE history endpoint replays the
  flat `AgentStreamStore` JSONL (`api/agent_stream.py read_since`); the native
  poll loop returns at `tailer.py` before `_tail_file`, so there is **no
  raw-rollout re-normalization** for a chat tab. `TraexJsonlAdapter.discover_source`
  returns `None` outright (no rollout discovery), and only non-chat transcript
  sessions reach `_tail_file`.
- The frontend groups **only** on `payload.subagent_thread`
  (`agentStreamTimeline.ts subThreadIdOf`); there is no `code-mode-nested:`
  call-id or spawn-registry fallback. Old flat events lack the field, so they
  stay on the main bubble. Reopen / hard refresh / clearing browser cache all
  re-read the same flat store and change nothing.
- Old events are not merely *missing a tag*: pre-merge child text/thinking
  shared the main `{turn}:assistant` / `{turn}:thinking` `message_id`, so store
  history compaction already **coalesced child text into the main bubble
  durably**. The child/main split cannot be reconstructed from the flat stream.

Recovery path per provider (net-new work, NOT shipped):

- **TraeX** (incl. `a50d8522`): no raw rollout is discovered, so there is
  currently nothing to rebuild from — old tabs remain flat.
- **Codex**: a rebuild is *theoretically* possible from
  `~/.codex/sessions/rollout-*.jsonl`, but requires (1) a one-time migration
  that re-normalizes a rollout and replaces a tab's store rows, and (2)
  sub-thread attribution in the rollout normalizers — today
  `_normalize_event_msg` / `_normalize_response_item` emit tool/text/thinking
  without any `_resolve_sub_thread`/collab attribution (the merge wired
  attribution only into the live JSON-RPC `_normalize_notification` path).

**New turns are fine:** live attribution stamps `subagent_thread`, it is
persisted verbatim into the flat stream, survives history compaction (the
thread-scoped `message_id` keeps children off the main row), and regroups after
a cold reopen. Pinned by
`test_subthread_attribution_survives_persistence_and_cold_reopen` and the
frontend `nestedSubthreadAttribution.test.mjs`. Only pre-deploy turns stay flat.
