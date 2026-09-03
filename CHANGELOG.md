# Changelog

> Each entry corresponds to a merge or significant commit on `main`.
> For detailed bug analysis, see `docs/working-logs/` and `WORKLOG.md`.

## Unreleased

### fix: promote Cursor Chat tabs to native transport regardless of cwd

- A Cursor Chat tab could stay on the raw `terminal` transport and fail closed
  in the adapter registry ("no structured adapter for agent_type=CURSOR") via
  two paths: `create_tab` required a truthy cwd to promote `terminal` →
  `native` (a leftover from the retired `terminal_transcript` mode; the native
  `ProviderSession` spawns with `cwd=None` and does not need one), and the
  state-restore path read `cursor_transport` directly from `tabs.json` without
  re-running the promotion.
- Extracted `_promote_cursor_chat_transport()` and applied it at all three
  transport-assignment boundaries: `create_tab` (dropping the stale cwd
  guard), the restore path (heals already-persisted bad rows on next reload),
  and `update_tab` (re-promotes after agent_type switches). The fail-closed
  contract is preserved — only a local Cursor CHAT tab on the raw `terminal`
  transport is promoted; `terminal_transcript` and TERMINAL-kind tabs are
  untouched.

### fix: correct Codex model picker list to real model slugs

- The Codex model picker listed stale placeholders (`gpt-5`, `gpt-4o`, `o3`,
  `o4-mini`). It now lists the actual Codex model slugs matching the real
  Codex app picker: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`,
  `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`. "Default"
  remains the empty-value state shown by the trigger.

### refactor: Codex-style composer layout (textarea above controls)

- The Structured Chat composer previously packed the attach button, mode
  picker, model picker, textarea, and Send button into a single crowded row.
  It now uses a Codex/ChatGPT-style layout: the textarea spans the full width
  on top, and the controls (attach, mode, model) sit in a bottom toolbar row
  with Send/Stop right-aligned. The composer card also shows an accent border
  on focus.

### feat: model switching in Chat composer and Cursor model support

- Add a model picker button to the Structured Chat composer for Claude, Codex,
  and Cursor tabs. Each agent type maps to its own env var (`ANTHROPIC_MODEL`,
  `CODEX_MODEL`, `CURSOR_MODEL`); selecting a model calls `switch-env` which
  updates the persisted env and pushes it to the live native transport.
- Cursor native sessions now forward `CURSOR_MODEL` as the `--model` CLI flag
  (the Cursor agent CLI does not read `CURSOR_MODEL` from the environment).
- `switch_env` for native Chat sessions updates the provider env directly
  without a tmux respawn, since the one-shot subprocess reads env per turn.

### feat: timeline tool call grouping

- Consecutive tool calls within a turn are now grouped into a single
  expandable `tool_group` part, matching Codex/Paseo UX. The group header
  shows a summary ("3 tools: Read, Edit, Bash") and an aggregate status;
  expanding reveals per-tool args and results.

### fix: release native turn guard at TURN_COMPLETED, not subprocess EOF

- For one-shot providers (Claude, Cursor), the turn guard (`_turn_in_flight`)
  was released only when the subprocess's stdout closed (EOF). If a turn's
  final tool call spawned a long-running process (e.g. `pnpm dev`), the
  `claude --print` subprocess stayed alive after the turn had logically
  completed, so the guard never released and every subsequent send returned
  HTTP 409 "a turn is already in flight".
- Every adapter emits `TURN_COMPLETED` as its final record (Claude's top-level
  `result`, Cursor's `turn_ended`, Codex's `turn/completed`), so there are no
  trailing records after it. The tailer now releases the active turn id and
  the turn guard at `TURN_COMPLETED` for all providers. The EOF handler still
  synthesizes a failed completion if the process exits without ever emitting
  `TURN_COMPLETED`.

### fix: handle >64 KiB stdout lines in native agent transports

- `ProviderSession._drain_stdout` (Claude/Cursor) and `CodexNativeSession._drain_stdout`
  previously used `asyncio.StreamReader.readline()`, which enforces a 64 KiB
  line-length limit. A single JSON record larger than that (e.g. a tool result
  carrying a large file's contents) raised `LimitOverrunError`, crashed the
  stdout drain, and made the tailer emit the misleading fallback
  "provider exited without a completion record".
- Replaced `readline()` with `read(65536)` + manual newline splitting so
  arbitrarily long stdout lines are parsed correctly.

### feat: structured Chat composer controls and Cursor AskQuestion cards

- Add `POST /stream/cancel` and `delivery=steer|normal` on `POST /stream/send` so
  Structured Chat can stop an in-flight turn or interrupt-and-send (Codex-style
  steer) without falling back to tmux.
- Composer UX: Stop button, in-memory queue on Enter while a turn is active,
  Cmd/Ctrl+Enter steer, Shift+Enter newline, IME-safe Enter handling, and JS
  textarea autoresize up to 240px.
- Cursor native/stream-json now normalizes assistant `tool_use` blocks; `AskQuestion`
  emits `approval_required` and Structured Chat renders selectable option cards
  with JSON answer submission back through the composer.
- Fix StructuredPane TDZ: declare composer refs before the immediate pending-turn
  watcher and drain the draft queue from a separate watcher after `flushDraftQueue`
  is defined (avoids ReferenceError that left Structured Chat as an empty shell).

### fix: isolate `test_ttyd_manager` from live Hub tab persistence

- Redirect `STATE_FILE`, `ORDER_FILE`, `LAUNCH_ENV_DIR`, and `_RUNTIME_HOME`
  to per-test temp paths so `TTYDManager.__new__` cases cannot overwrite live
  `~/.claude_hub/tabs.json` or `tab_order.json` when pytest runs on the primary
  checkout.
- Add a regression guard asserting unit tests never target live tab state files.

### feat: add provider-aware Chat Plan mode and native activity status

- Chat creation remains a top-level Terminal-area action. Agent Workspace
  orchestrator/reviewer/worker sessions remain Terminal control-plane runners
  and never mount the Chat `StructuredPane`.
- Added per-Chat Default / Plan mode discovery, persistence, and switching.
  Claude and Cursor are gated by their installed CLI capabilities; Codex uses
  `collaborationMode/list` and sends the provider preset's own mode with the
  next `turn/start`. A mode change while a turn is active fails with HTTP 409.
- Chat tab indicators now use native `idle | working | attention | offline`
  runtime state, while Terminal tabs retain the existing tmux status path.
  Desktop indicators expose `data-status`, `aria-label`, and title text.
- The structured composer renders only backend-advertised modes in a compact
  upward-opening picker beside the attachment control, preserves the previous
  selection on failed updates, aborts stale updates on source switch, disables
  mode changes throughout a working turn, and restores them in the same sample
  that authoritative completion returns the tab to idle. The user-facing
  default mode label is `Agent`, while its wire and persistence id remains
  `default`. Compact layouts use 44px mode controls; a 390x844 viewport has no
  horizontal overflow.
- Verified local Claude, Codex, and Cursor capabilities all advertise Default
  and Plan; a live Claude Plan turn transitioned working to idle and left the
  workspace unchanged. Claude Code may still write its internal plan artifact
  under `~/.claude/plans`, which is outside the workspace.
- Mode survives a cold restart for top-level Chat tabs.
  Final merge gate passed: 1,223 backend tests (20 environment-gated skips) and
  253/253 frontend unit tests, frontend lint and production build, plus Black,
  isort, mypy, and the repository-doc parity check.
- Chat activity remains working through both the initial wait and streamed
  output, then becomes idle at authoritative turn terminalization. A one-shot
  provider process reaching EOF is transport input to that lifecycle; EOF by
  itself is not the UI completion authority.
- Paseo-style Implement / Reject Plan approval cards are not implemented in
  this iteration. Error-attention and missing-provider live paths, provider
  failure rollback, per-mode isolation across multiple Chats, inactive-pane
  SSE disconnect, and three-provider Default image turns remain outside the
  recorded live acceptance evidence.

### feat: bounded structured image-message history

- **Bounded preview persistence**: Structured/Paseo sends keep original image
  bytes out of durable history and persist only browser-generated previews
  (1024px maximum edge, 512 KiB maximum each). FIFO limits default to 200
  previews / 64 MiB per session and 2000 previews / 512 MiB globally. Evicted
  previews leave the user message intact and render an explicit expired
  placeholder.
- **Immediate composer and conversation thumbnails**: the composer clears as
  soon as Send is pressed, the optimistic user turn shows its image, and the
  authoritative history resolves the same scoped preview after refresh.
- **Compact click-to-expand previews**: conversation images now render as
  compact 4:3 thumbnails (72–88px wide) instead of expanding the user
  bubble to the image width.
  Clicking a thumbnail opens a viewport-contained lightbox that closes from
  the backdrop, close button, or Escape and restores keyboard focus.
- **TTL live enforcement in `save`** (`attachments.py`): `attachment_max_age_seconds`
  was previously enforced only during startup `gc`, so a long-lived process
  would never age out previews between saves. `save` now runs `_evict_aged`
  before session/global quota eviction. When `max_age_seconds` is `None`
  (the default), `_evict_aged` returns immediately without scanning the
  index, so an under-quota save still incurs exactly one manifest write.
  `gc`'s inline age-eviction was refactored to call the same `_evict_aged`
  helper.
- **Tab deletion forgets the tailer before clearing state** (`ttyd_manager.py`):
  `delete_tab` previously called `AgentStreamAttachmentStore.clear()`
  directly, leaving the in-process terminal-tab stream tailer running. An
  in-flight send could then rewrite previews/events after the clear,
  resurrecting deleted state. `delete_tab` now delegates to
  `discard_session_stream("terminal-tabs", f"terminal-tab-{tab_id}")`,
  which stops the tailer first, then clears the event log and preview
  cache.
- **Codex inflight image cleanup on server death** (`native.py`): if the
  Codex app-server dies mid-turn (EOF on stdout before `turn/completed`),
  the in-flight image temp files were leaked until `stop()`. The
  `_drain_stdout` `finally` block now cleans both `_inflight_images` and
  `_staged_images` before signalling EOF to consumers.
- **Failure and path hardening**: atomic 0600 writes, 0700 cache/temp
  directories, startup orphan GC, symlink-safe cleanup, rollback on
  publication/eviction failures, request count/size/MIME validation, and
  session-scoped preview GET endpoints prevent unbounded or cross-session
  history growth in normal operation.

### fix: structured timeline scroll replay during history hydration

- **Activation gate** (`timelineActivation.ts`): the structured timeline is
  not revealed until authoritative history has been hydrated. While hidden
  (`phase: 'hidden' | 'pinning'`), live updates and resize events do not
  drive scroll behaviour. When the stream goes `live`, the tail is pinned
  synchronously (after Vue's DOM commit, before the browser paints) and only
  then is the timeline revealed — the first painted frame is already at the
  tail, eliminating the visible "history replays from top to bottom" effect.
- **Synchronous tail pinning**: `requestLatestAnchor` now applies
  `scrollTop = scrollHeight` directly inside `nextTick` (no intervening
  `requestAnimationFrame`). A single verification rAF re-sticks if Markdown /
  image / font layout changed the scroll height.
- **Detached viewport preserved**: `detachFromTail` (user scrolls up) sets
  `followOutput = false`; live updates and `ResizeObserver` only re-stick
  while `followOutput` is true, so a deliberately detached viewport is never
  hijacked. `jumpToLatest` / tab switch explicitly `rearmFollow`.
- **`connectionState` watch is `immediate`**: covers the cached-stream case
  where the composable is already `live` when the pane mounts (quick tab
  switch-back), so the pin-and-reveal sequence still runs.

### fix: permanent "Loading structured view" when switching to Codex tab

- **Generation-owned connection state**: `StreamConnectionStateMachine`
  guards `hydrating → live | failed` transitions by generation id. A stale
  `start()` (superseded by a newer one) can no longer flip the current
  generation's `hydrating` state to `live`, nor can its events advance the
  shared sequence cursor and cause the current generation's events to be
  skipped.
- **Bounded hydration abort**: `fetchCapabilities` and `fetchEvents` now take
  an `AbortSignal` owned by the current generation. `stop()` / a newer
  `start()` aborts stale in-flight requests. A 15s hard timeout on each
  hydration fetch fails the stream closed (`failed`) instead of hanging on
  `hydrating` indefinitely.
- **`applyPage` generation guard**: stale pages from a superseded generation
  are dropped before they can pollute the sequence buffer cursor.

### perf: optimize Paseo stream rendering under long Thinking bursts

- **Backend coalescer**: Added `agent_stream/coalescer.py` — a leading-edge
  immediate flush + trailing 60ms timer coalescer that merges same-stream text
  deltas and forces flush on terminal/barrier events. Serialized drain via
  `asyncio.Lock` prevents interleaved writes and guarantees ordered delivery.
  Subscriber queue drops (previously seen under long Thinking bursts) are
  eliminated by reducing event fanout volume.
- **Frontend per-block markdown cache**: `markdownBlocks.ts` splits the source
  into top-level block tokens via `marked.lexer` and caches each completed
  block's rendered HTML. Only the live tail (the still-growing last block) is
  re-parsed on each delta. Cache size is bounded to the number of completed
  blocks, not deltas, so a 5000-delta growing tail keeps the cache at O(1).
  `gfm:true, breaks:true` options are preserved.
- **Thinking rendered as plain text**: Thinking parts use `<pre>{{ part.text }}</pre>`
  with zero `marked.parse` / `DOMPurify.sanitize` calls. This removes the
  dominant rendering cost during multi-kilobyte reasoning streams.
- **Event micro-batching**: `agentStreamBatcher.ts` accumulates delta events
  and flushes on rAF (with a 48ms `setTimeout` fallback for background tabs).
  Barrier events (`turn_completed`, `tool_call_completed`, `approval_required`,
  `approval_resolved`, `error`, `status`) flush immediately with all preceding
  pending deltas in order.
- **Incremental timeline reducer**: `IncrementalTimelineReducer` in
  `agentStreamTimeline.ts` keeps the reducer state alive across calls and only
  processes the unseen suffix. The original `groupEventsIntoTurns` re-scanned
  the entire event list on every batch (O(n) per delta), which dominated the
  long-task budget under ~13.5k historical events. The reducer detects
  non-prefix replacements (session switch, reconnect, reset) via a
  `session_id + tab_id + stream_sequence` key on the last processed event and
  rebuilds from scratch when the prefix diverges. Returns a fresh array
  reference for Vue reactivity.
- **Keyed block DOM promotion**: `MarkdownContent` renders each block as its
  own `<div :key="block:${index}" v-html="block.html" />`. Completed blocks
  have stable index keys so Vue reuses their DOM; only the live-tail block's
  `innerHTML` updates. Cache identity includes the `linkMarkdownPaths` mode.
- **AST list-item splitting for long streamed lists**: A contiguous list is
  one marked token; without splitting, the whole list's `innerHTML` was
  replaced every delta (O(n²) DOM work). `markdownBlocks.ts` now splits the
  list token into its marked-parsed `items` and emits a `RenderedListBlock`
  with one stable `<ul>`/`<ol>` and keyed `<li>` children. Every completed
  item's `<li>` inner HTML is cached (keyed by `item.raw` + the list-wide
  `loose` flag so a tight→loose transition re-renders cached items); only
  the final (still-growing) item is re-rendered per delta. Item-boundary
  deltas cost 2 parser calls (freeze previous + render new final); in-item
  growth costs 1. `joinBlocks` output matches `marked.parse` exactly for
  ordered `start`, nested, loose, task, and emphasis/link fixtures.
- **Removed second-stage text reveal**: The `textReveal` character-by-character
  interpolation in `StructuredPane` was magnifying 98 `text_delta` events into
  375 distinct assistant text DOM states (~3.8×). It has been deleted;
  assistant text now streams directly from the batched event stream (backend
  60ms coalescer + frontend rAF/48ms batcher), one visible update per
  committed batch. `turn_completed` flips `MarkdownContent`'s `complete` prop
  to cache the final block and expose the exact final text synchronously.
- **Per-turn render revision + `v-memo`**: Added `renderRevision` to
  `TimelineTurn`, incremented only when an applied event visibly mutates the
  turn (text/thinking/tool/status/error/completion/user summary). No-op events
  (empty text, exact multi-chunk replay, duplicate tool, unchanged tool
  completion) leave the revision unchanged. `StructuredPane` puts
  `v-memo="[turn.renderRevision]"` on each `.structured-turn`, so Vue skips
  re-rendering completed historical turns whose revision has not changed; only
  the active turn rebuilds. This prevents unchanged historical turn VNode
  reconstruction and is intended to address the 7 long tasks (53–102ms)
  observed after the textReveal removal; the post-`v-memo` E2E measurement is
  pending.
- **Bounded long-poll queue drain (no per-tick `read_since`)**:
  `_wait_stream_events_for` previously called `store.read_since` (a full JSONL
  scan from byte 0, O(rows)) on every loop iteration — both on 1s health-poll
  timeouts and on every queue wake. For a session with ~15.7k rows this cost
  60–76ms per call and could amplify live-stream latency. The waiter now calls
  `read_since` exactly once after subscribe (initial catch-up), then consumes
  live events directly from the subscriber queue. Since `_publish` persists
  before fanout, every queued event is already durable. Contiguous events
  (`seq == since+1`) are drained from the queue in one batch up to the 200
  limit; a gap (`seq > since+1`, e.g. subscriber-queue overflow) triggers a
  single `read_since` reconciliation. Health-poll timeouts only check
  existence/hard-failure/deadline and never re-scan the JSONL. When the drain
  fills the 200-event batch and the queue still holds events, `has_more` is
  set to `True` so consumers know more events are immediately available.
  Stale or duplicate sequences (`seq <= next_seq`) are skipped mid-drain
  rather than treated as a gap.
- **Root-cause note (sustained idle CPU)**: The 40–65% backend CPU observed
  during the post-`v-memo` E2E run was traced to a stale Python E2E polling
  probe (PID 88343) from this worktree that repeatedly requested the legacy
  cursor `?after=999&limit=1000`, which the backend ignored and answered with
  the first 1000 rows every tick. After SIGTERM of the probe, the live backend
  (PID 46248) dropped to 0–0.7% idle. The product long-poll full-scan is a
  **separate, measured per-wake inefficiency** (60–76ms `read_since` on
  15,672 rows) that the bounded queue-drain change above addresses; it was not
  the cause of the sustained idle load. Neither is claimed as the final UI
  long-task root cause until a fresh E2E run after cleanup.
- **Validation**: 193 frontend tests pass (including deterministic
  `renderRevision` tests: active turn revision increments on visible
  mutations, completed historical turn revision stays stable while a later
  turn mutates, empty text and exact multi-chunk replay do not advance
  revision, duplicate tool start and unchanged tool completion do not advance
  revision; plus the one-update-per-batch test: 4 text batches produce exactly
  4 distinct assistant text values), lint clean, build succeeds. Authoritative
  E2E (turn `c01df02c`, real day1 Claude) on the pre-removal build recorded 6
  long tasks of 58/60/50/76/64/90ms; post-removal and post-`v-memo` E2E
  pending re-run.

### fix: recover native agent streams and smooth Cursor structured output

- **Codex recovery**: Restarting an idle-reaped Codex app-server now creates a
  fresh notification queue, so the new process cannot consume the previous
  process generation's EOF sentinel and falsely report `structured observation
  unavailable`. The structured error banner's Retry action now performs an
  explicit backend transport rebuild while preserving the provider thread.
- **Cursor reconciliation**: Timestamped final-message replays are recognized
  from their exact multi-chunk boundaries and no longer appended as fresh
  deltas. The timeline applies the same conservative rule while hydrating, so
  conversations whose duplicate snapshot was already persisted are repaired
  without rewriting the append-only event log. Legitimate repeated
  single-token deltas and multiple assistant messages in one turn remain
  supported.
- **Waiting feedback**: A submitted turn renders an animated agent-side waiting
  card before the first thinking, text, tool, status, or error event arrives;
  normal paced reveal takes over as soon as text begins streaming.
- **Validation**: Real preview sessions recovered the existing Codex thread and
  emitted four native deltas through completion. Cursor emitted 21 deltas for a
  four-marker response, with every marker present exactly once.

### fix: scope native stream reconciliation to provider message id and render ordered parts

- **Root cause**: A Claude tool turn emits two assistant messages in one native
  turn (thinking+tool_use, then, after the tool result, thinking+text). The
  stream accumulator was turn-scoped, so the second message's final thinking
  snapshot failed `startswith(accumulated)` against the first message's
  thinking, producing the `assistant final thinking does not match streamed
  deltas` error. Separately, the frontend flattened thinking, text, and tools
  into fixed buckets and rendered text before tools, discarding the provider's
  correct interleaved event order.
- **Backend**: The text/thinking accumulator is now scoped to the provider
  `message.id`. `message_start` and final-only assistant rows call
  `_begin_provider_message`, which resets per-message buffers only when the id
  changes; consecutive rows sharing an id keep accumulating. Streaming tool
  args are assembled from `content_block_start`/`input_json_delta`/
  `content_block_stop` and `TOOL_CALL_STARTED` is emitted at block stop with
  parsed args when needed. Claude 2.1.x may deliver the authoritative
  assistant tool snapshot either before or after block stop, so both paths
  record and check the same tool id; whichever arrives second is suppressed.
  Malformed streamed args defer to the authoritative final snapshot instead
  of displaying a partial payload.
  `emitted_tool_call_ids` remains turn-scoped. Genuine text/thinking
  mismatches still fail closed.
- **Frontend**: `TimelineTurn` now carries an ordered `parts[]` list
  (`thinking`, `text`, `tool`, `error`, `status`) as the authoritative render
  order; the flat `thinkingText`/`assistantText`/`tools` aggregates are kept
  as derived views for existing callers. `StructuredPane` renders parts in
  order, with paced reveal keyed per text part (not per turn), and protocol
  errors/status appear inline rather than deferred to the turn end.
- **Validation**: Focused agent-stream tests (53 passed) cover streamed tool
  args with real block index, two-assistant-message turns scoped by message
  id, both snapshot/block-stop orderings, malformed-argument fallback, same-id
  consecutive-row accumulation, and genuine mismatch still erroring.
  Frontend timeline tests (19) and full unit suite (140) pass;
  ESLint, vue-tsc, and production build pass; Black, isort, and mypy pass.
  A real day1 Claude/WebSearch browser turn on the isolated 5275/18173 preview
  showed the waiting state and rendered `thinking → tool → thinking → text`;
  the persisted stream contained one tool start, one completion, no error,
  one final marker, and a completed turn.

### feat: separate Paseo Chat sessions from native Terminal sessions

- **What**: The new-session flow now asks for a fixed `Chat` or `Terminal`
  session type. Chat sessions open directly in the Paseo structured timeline
  with text/image composer; Terminal sessions open directly in the native PTY
  UI. There is no per-pane `Terminal | Paseo` switch.
- **Why**: Paseo models chats and terminals as different sessions. Treating
  structured output as a second view of every terminal made the creation
  contract ambiguous, exposed unsupported combinations, and encouraged users
  to expect raw-terminal and structured rendering to be identical in real
  time.
- **How**: A persisted `session_kind` (`chat` or `terminal`) now controls the
  renderer independently of the Claude/Codex/Cursor/runtime profile. Explicit
  user Chat sessions use a provider-native structured transport; Terminal and
  internal managed-task runners retain their native TUI. No hidden raw terminal
  is mounted behind a Chat surface. The retired persisted `agent` value is
  migrated to `chat` only at state-load boundaries. See
  `docs/working-logs/2026-09-01-paseo-agent-terminal-session-separation.md`.
- **Connection lifecycle**: Only visible Chat panes hydrate history and keep
  an SSE accelerator plus the authoritative long poll. Switching away closes
  those browser connections. The backend shares one tailer per Chat and keeps
  it warm for five minutes after the final subscriber leaves; returning later
  rehydrates the append-only event log and resumes the persisted provider
  conversation/thread id.
- **Provider runtime**: Claude and Cursor run one streaming subprocess per
  submitted turn, with no provider process left idle between turns. Codex keeps
  a persistent `codex app-server --stdio` process while its tailer is warm.
- **Current lifecycle limit**: The zero-subscriber reaper currently checks only
  subscriber count and elapsed time. If a provider turn is still running five
  minutes after every pane leaves, that transport can be stopped; an in-flight
  guard remains follow-up work.
- **Streaming**: Claude stream-json, Codex app-server, and Cursor stream-json
  feed stable turn/message IDs into an append-only event stream. SSE accelerates
  long-poll reconciliation; out-of-order events remain buffered until every
  sequence gap is filled. The renderer uses Paseo-style ~60 Hz paced reveal
  with a 150 ms backlog horizon and flushes authoritative text on completion.
- **Codex lifecycle**: Persistent app-server startup is single-flight, so a
  first composer send racing the stream subscriber cannot spawn two readers
  against one overwritten process and break incremental delivery.
- **Images**: Claude and Codex Chat composers accept PNG/JPEG/GIF/WebP. The
  installed Cursor CLI has no image-input contract, so its attachment control
  is explicitly disabled instead of pretending to send an image.
- **Startup gate fix**: A Claude Chat launched in Solo Mode now carries the
  user's explicit bypass acknowledgement as a secret-free command-line
  settings source, so the provider-native transport cannot stall on Claude's
  first-run safety dialog. The Hub also refreshes stable HOME/PATH/shell values
  on its named tmux server before creating panes and clears stale pytest
  markers. This prevents a long-lived worktree tmux server first created by an
  integration test from retaining that test's temporary environment.

### fix: harden image attachment validation and SSE session-deletion handling

- **What**: Image attachments now validate content signatures (magic bytes)
  against the declared MIME type before persisting, and `image/bmp` is now a
  supported attachment type. A live SSE agent-stream connection emits an error
  and terminates promptly when its session is deleted, instead of heartbeating
  forever.
- **Why**: The frontend composer offers BMP, but the backend allowlist omitted
  it. Without signature checks, a client could declare one MIME type while
  uploading bytes of another (or arbitrary non-image data). On the stream side,
  `hard_failed` returns `False` once a deleted session's tailer is forgotten,
  so the SSE loop had no signal to stop.
- **How**: `IMAGE_ATTACHMENT_SIGNATURES` in `_constants.py` plus
  `_validate_image_signature` in `_attachments.py` (WebP checks both the `RIFF`
  prefix and `WEBP` at offset 8). The SSE `event_stream` loop and the long-poll
  `wait` loop now check `workspace_manager.sessions.get(session.id)` and emit a
  `session was deleted` error when the session is gone.

### fix: release idle sessions after a failed subagent run

- **What**: A `failed` task is now terminal for agent-session reuse and
  deletion. Its idle agent can be removed without first forcing the task
  through an invalid abort transition.
- **Why**: Timed-out subagents enter `failed`, while manual abort accepts only
  queued/working/review tasks. Treating `failed` as active in the cleanup
  guard left an otherwise idle agent session permanently consuming a seat.
- **How**: Session binding checks now recognize both `done` and `failed` as
  terminal outcomes; regression coverage exercises direct session deletion.

### feat: Cursor same-pane transcript bridge for Paseo Structured

> Historical implementation stage, superseded by the fixed Chat / Terminal
> session boundary above: this bridge no longer means Agent Workspace runners
> expose a Structured view. Those managed sessions remain Terminal-only.

- **What**: Local Cursor CLI sessions on the verified
  `2026.08.25-3e8eec8` release can now power the same Structured timeline as
  Claude and Codex while retaining the authoritative Raw terminal pane.
  Cursor sessions pin their CLI version, data root, schema, conversation ID,
  canonical cwd-derived transcript path, and child `CURSOR_DATA_DIR`; any
  mismatch remains Raw-only. Snapshot reconciliation appends monotonic stream
  events, supports Cursor's observed replacement of a trailing `turn_ended`
  marker by the next user turn, and otherwise fails closed rather than
  rewriting a connected timeline. Deleting a managed session also purges its
  stream log/cursor, preventing a reused friendly session ID from exposing a
  prior conversation. Image attachment instructions now explicitly require
  native image inspection before an image-dependent answer.
- **Why**: Cursor had the same two-view product contract but no trustworthy
  structured source. A guessed transcript path, non-monotonic replay, or
  attachment path treated as metadata would make Structured misleading.
- **How**: `cursor_cli_transcript.py`, snapshot-aware `SessionTailer`, Cursor
  launch provenance in `ttyd_manager.py` and managed-session creation,
  sequence-zero hydration, and stream cleanup on session delete. See
  `docs/working-logs/2026-08-31-paseo-v2-cursor-transcript-bridge.md`.

### feat: Paseo v2 structured Chat UI + image composer (frontend)

> Historical implementation stage, superseded by the fixed Chat / Terminal
> session boundary above. `StructuredPane` is now owned only by top-level Chat
> tabs; Agent Workspace managed sessions remain Terminal control-plane runners.

- **What**: Structured Chat surface backed by the provider transport. It
  renders a turn-grouped timeline
  (user, assistant text, collapsible thinking, tool calls with status, errors,
  status events) from the backend agent-stream plane, with SSE + long-poll
  reconciliation and a retryable fail-closed Chat surface on stream failure.
  Composer supports text + image attachments (file picker, drag-drop, paste)
  with PNG/JPEG/GIF/WebP + 8 MB validation, preview/remove, image-only
  send, and message/attachment retention on send error. No file paths or
  secrets are rendered in the UI.
- **Why**: First-release product contract for the Paseo v2 structured
  observation layer without altering raw terminal semantics.
- **How**: `StructuredPane.vue`, `agentStreamTimeline.ts`,
  `agentStreamAttachments.ts`, `useAgentStream.ts`; `TerminalPane.vue` selects
  the fixed surface from `session_kind` and keeps the provider transport hidden.
  The initial `sessionForTab` managed-session mapping was removed when Agent
  Workspace became Terminal-only. Stable `client_turn_id` reconciliation keeps
  identical prompts distinct.

### feat: Paseo v2 backend structured observation foundation (Layer B)

- **What**: Provider-neutral structured agent event stream for the Paseo v2
  UI. Adds append-only per-session JSONL event stores with monotonic
  `stream_sequence`, server-side redaction (env value stripping, token
  masking, 4000-char field truncation), and a tailer lifecycle that tails
  Claude JSONL and Codex rollout transcripts into normalized events. Exposes
  REST/SSE/long-poll stream routes under
  `/api/workspaces/sessions/{id}/stream/*` plus a read-only
  `GET /api/workspaces/sessions` tab-to-managed-session mapping. Cursor
  fail-closes (no adapter) until a future ACP/transcript bridge lands.
- **Why**: The raw terminal plane cannot drive a structured agent UI
  (turns, tool calls, thinking). A normalized event log lets the frontend
  render a Paseo-style observation layer without coupling to any single
  provider's transcript format.
- **How**: `services/agent_stream/` (base, redaction, store, registry,
  claude_jsonl, codex_jsonl, tailer) + `api/agent_stream.py`;
  `ManagedSession` gains `stream_capabilities`, `agent_session_id`,
  `cursor_transport`. Lifespan stops tailers on shutdown. Cursor's verified
  transcript bridge is documented in the following Unreleased entry.

### fix: isolate worktree runtime and fail-closed session seat before /clear

- **What**: Linked-worktree backends default to an isolated runtime home and
  tmux socket, and refuse the live `~/.claude_hub` / default tmux server.
  Instance lock and backend logs follow the same isolated home so a
  worktree preview does not collide with the live 8173 process.
  `send_session_message` checks that `tmux_session == claude-hub-{tab_id[:8]}`
  and that no other live tab claims the name. A missing pane fails closed
  instead of `ensure_tab_tmux_session` recreating the same name and sending
  into it. Same reviewer + same task no longer re-sends `/clear` on reaper
  redispath even when `clear_context` is set.
- **Why**: A worktree verification run rewrote live state / renamed tmux, then
  a reviewer `/clear` landed in the main agent seat.
- **How**: `runtime_isolation.py` + `session_seat.py`; tmux argv goes through
  `tmux_command()`; reviewer clear uses `should_clear_reviewer_context`.

### feat: subagent task mode + failed state for agent-to-agent delegation

- **What**: New `subagent` task mode for agent-to-agent delegation (no Goal
  Packet, no bilingual, no AI review — the caller judges results). New
  `failed` task status auto-triggered by worker report, session death, or
  timeout. New `task run` CLI one-step command (ensure agent → create → start)
  defaulting to `--agent-type claude --task-mode subagent`. Per-agent-type
  default env presets via `[default_env_presets]` in config.toml.
- **Why**: External agents delegating via Claude Hub CLI found the reviewed
  flow (Goal Packet, bilingual, AI review) heavy; they judge results
  themselves. Failures (dead session, timeout) previously left tasks stuck
  `working` with no recovery path.
- **How**: `WorkspaceTaskMode.SUBAGENT` + minimal assignment prompt;
  `WorkspaceTaskStatus.FAILED` + `timeout_seconds`/`failure_reason`/`failed_at`
  fields; monitor loop detects session death (`STOPPED`) and timeout
  (`now - updated_at > timeout_seconds`, only when set); session death /
  timeout apply to `subagent` only; `timeout_seconds` defaults to `None` so
  existing WORKING tasks are not auto-failed; frontend keeps `failed` in the
  Working column (no new column); Claude CLI default env preset is `day1`;
  `continue_task` accepts FAILED (reuses alive session or reassigns via
  `start_task`); `task accept` accepts FAILED; `task run --timeout-seconds`
  (default 1800).

### fix: paginate only Done column on Agent Workspace board

- **What**: Board `tasks_limit` now returns all non-Done tasks plus the most recent
  Done window; pagination cursor/remaining counts and Show older apply to Done
  only. Todo/Queued/Working/Review columns always render in full.
- **Why**: The initial recent-15 pagination hid active non-Done tasks when Done
  history was large.
- **How**: Done-only slice in `board_pagination`; frontend remaining/summary
  math uses loaded Done count; E2E fixture with mixed open + 18 Done tasks.

### fix: preserve terminal history across HMR remount and backend reload

- **What**: Initial mount, HMR remount, iframe reload/retry, and backend/ttyd
  restart now restore visible pane content from tmux scrollback before
  `contentReady`, using request/generation-correlated history refresh with
  working-agent TUI deferral unchanged.
- **Why**: Shared-type HMR and uvicorn `--reload` rebuilt TerminalView/iframes
  while tmux sessions persisted, leaving panes blank despite live sessions.
- **How**: `schedulePaneHistoryRecovery` in `TerminalView.vue` +
  `decidePaneRecoveryReplay` policy + per-document recovery correlation in
  `terminalPaneRecovery.ts`; frontend source tests; isolated Playwright E2E
  with real TerminalView harness on Vite at `test/harness/` (no backend test
  route; `test_terminal_hmr_recovery_e2e.py`).

### feat: paginate Agent Workspace board task history (recent-15 lazy load)

- **What**: `GET /api/workspaces/{id}/board` accepts optional `tasks_limit` and
  `tasks_cursor` for stable recent-activity pagination; the UI loads 15 tasks on
  first paint and fetches older history on demand. Unpaginated board responses
  remain the CLI/default full-history contract.
- **Why**: Large workspaces shipped hundreds of tasks in every board poll,
  bloating payload size and slowing first render.
- **How**: `board_pagination` service + `BoardTasksPagination` schema; frontend
  `workspaceStore.fetchBoard({ reset })` / `loadMoreBoardTasks()` with stale
  fetch cancellation; workspace list sorted by latest activity.

### feat: CLI workspace/agent reuse lifecycle and env-preset support

- **What**: `workspace ensure` reuses Hub Workspaces by Git `common-dir` identity;
  `workspace create` fails closed on duplicate identity unless
  `--allow-duplicate`; `agent create` defaults to best-effort compatible idle
  reuse with tri-state `--reuse-existing` / `--no-reuse-existing` and
  task-scoped `--ephemeral`. Reuse is advisory rather than an idempotency key:
  overlapping creates may each produce an Agent for intentional parallelism,
  while CLI/agent-entry help tells callers to inspect status and avoid
  unnecessary sessions. `task cleanup` safely deletes caller-owned ephemeral
  sessions; `--env-preset NAME_OR_ID` resolves any built-in or saved custom
  preset (with `day1` merely an example) and explicit `--env` overrides it.
- **Why**: CLI agents were creating duplicate Hub Workspaces per Git worktree,
  reusing the first orchestrator unconditionally, and launching Claude without
  user env presets (401 bootstrap failures).
- **How**: `workspace_identity` + `env_preset_resolver` services; API
  `POST /workspaces/ensure` and `POST /tasks/{id}/cleanup`; `caller_owned_ephemeral`
  on `ManagedSession`; lifecycle recipe in CLI help and `AGENTS.md`/`CLAUDE.md`.

### feat: optional workspace task agent_tag metadata

- **What**: workspace tasks accept an optional bounded `agent_tag` string on
  create/update, exposed as `claude-hub task create --agent-tag TEXT` and a
  restrained badge on task cards when present.
- **Why**: operators and automation need a stable, human-readable label for
  which agent role owns a task without overloading `agent_type` or building a
  generic tagging system.
- **How**: optional field on `WorkspaceTask` / create / update schemas with
  trim, single-line (no control characters), and max-length validation;
  persistence omits null keys for legacy compatibility; board/tree/API
  round-trip; frontend badge gated on truthy `agent_tag`; pytest/CLI/frontend/E2E
  coverage.

### fix: restore saturated status colors and fix terminal tab-switch history loss / typing lag

- **What**: semantic status colors (success/warning/attention/danger) are
  restored to a saturated, readable palette in both light and dark themes.
  Terminal tab switches no longer reload the iframe (which wiped buffer and
  scroll state), and typing latency after a switch matches the idle baseline.
- **Why**: the previous palette was desaturated (saturation ~0.35–0.55) and
  hard to read. The iframe cache used a single list as both LRU order and
  v-for render order, so switching tabs reordered `<iframe>` DOM nodes;
  Chromium rebuilt the browsing context, reloading xterm and losing history
  and scroll position. Post-switch refreshes also used a blind 200 ms timer
  for resize instead of correlating to the history-refresh completion.
- **How**: split the cache into `cachedTabIds` (stable insertion-order render
  list, never reordered) and `tabRecency` (LRU for eviction only), extracted
  into the pure `computeCacheUpdate` function. History refreshes now carry a
  `requestId` and the done event echoes it, so the post-switch resize fires
  exactly when the snapshot settles. Non-manual refreshes honor
  `preserveUserScroll` so a scrolled-up reader is not yanked to the bottom.
  Agent TUI tabs (claude/codex/cursor) defer history replay while `working`
  to avoid corrupting the live screen.

### fix: filter subagent sessions and cache codex session listing

- **What**: the Codex session picker now shows only user-initiated sessions
  (`thread_source == "user"` or legacy rollouts without the field), hiding
  subagent and automation sessions that cannot receive direct user input.
  The listing is also cached in-memory keyed on the session index and rollout
  directory mtimes, so repeat calls return in <5 ms instead of re-scanning
  all rollout files.
- **Why**: subagent sessions cluttered the picker and selecting one produced
  a "cannot receive input" error. The full scan of ~1100 rollout files took
  ~3-4 s on every call.
- **How**: `_parse_session_meta` now extracts `thread_source`; `ScanEntry`
  carries it; `list_codex_sessions` skips `subagent`/`automation` entries.
  A module-level cache stores the last result; `_codex_sessions_cache_key()`
  stats `session_index.jsonl` and the date directories under `sessions/` and
  `archived_sessions/` to detect changes and invalidate.

### fix: use Codex app conversation titles in session picker

- **What**: the Codex session selector now shows the same conversation titles
  as the Codex app / IDE extension (the `thread_name` stored in
  `~/.codex/session_index.jsonl`), instead of the raw first user message.
- **Why**: previously the picker extracted the title from the first user
  message in the rollout file, which for workspace-managed sessions was the
  task-delivery prompt text — not the short, human-readable title the user
  sees in the Codex app. It also required scanning every rollout file
  (~12 GB total), making the endpoint take ~34 s.
- **How**: `list_codex_sessions()` loads `session_index.jsonl` once and uses
  `thread_name` as the primary title for the ~44% of sessions that have one.
  For sessions absent from the index it falls back to the existing
  first-user-message extractor, capped at the first 100 lines. The scan and
  fallback title extraction are combined into a single file open per session,
  and the rollout body is only read for sessions that lack a `thread_name`.
- **Verified**: 14/14 codex-session tests pass; endpoint response time drops
  from ~34 s to ~4 s on a corpus of 1145 sessions; titles match the Codex
  app's conversation list.

### fix: show meaningful titles in Codex session resume picker

- **What**: the Codex session selector now displays task titles (e.g.
  "codex session选择") or role labels (e.g. "Reviewer (Claude Hub)") instead
  of boilerplate first lines like "New workspace task assigned." or
  "Review workspace task."
- **Why**: workspace-managed Codex sessions receive a system-injected task or
  bootstrap prompt as their first user message; the title extractor only
  filtered Codex's own environment/permission preamble, so the picker showed
  the first line of system prompt text rather than anything a human would
  recognize.
- **How**: `_codex_session_title()` now classifies workspace-injected messages
  — task assignment, review, continue, dispatch-decision, hard-recovery, and
  revision-resume prompts yield their embedded `Task title:` field; idle
  role-bootstrap prompts (agent / reviewer / dispatcher / resident) produce a
  workspace-qualified role label as a fallback that is overridden if a later
  task message exists.
- **Verified**: 11 new unit tests cover each workspace prompt variant,
  bootstrap-to-task override, inline `Task: id (title)` resume format,
  long-title truncation, and codex-boilerplate-plus-task ordering; all
  14 codex-session tests pass.
### fix: per-event AgentReport resolution in Task Graph strict E2E

- Add ``scripts/task-graph-e2e/report_resolution.py`` to resolve seq1/2/3
  TaskEvent ``report_id`` values against the task reports API with matching
  ``task_id``, ``session_id``, and ``state``; emit
  ``target_event_report_resolution`` evidence before and after first cold reload.
- Add ``backend/tests/test_task_graph_e2e_report_resolution.py``.

### fix: Task Graph E2E harness native git provenance for exact-artifact gates

- Add ``scripts/task-graph-e2e/git_provenance.py``: read real
  branch/HEAD/dirty from delivery repo, forbid env spoofing, require clean
  worktree at harness start, assert pre/post SHA unchanged before writing
  ``evidence.json``.
- Wire provenance into ``run_e2e.py`` finally block; export
  ``CLAUDE_HUB_E2E_SOURCE_ROOT`` from ``run.sh``.
- Add ``backend/tests/test_task_graph_e2e_git_provenance.py`` contract tests.

### fix: seal review rounds on cold load and harden Task Graph blockers

- **P1-1:** Leaf-only Task delete with **409** when descendants exist; atomic
  cleanup of TaskMailbox events, call index, compat runs, reports, and session
  bindings with save rollback.
- **P1-2:** Runtime isolation for legacy `resident_root` — skip in
  `recover_pending_runs`; delivery-uncertain events write TaskMailbox only for
  Task-bound sessions (no workspace root AgentRun fallback).
- **P1-3:** Repair persisted `reviewed_cycle=0` when `review_completed_at` is
  set during task normalization so cold reload does not re-trigger the fallback
  reaper (seq4 extras). Regression:
  `tests/test_task_graph_cold_restart_review.py`.
- **P2-1:** Clarify Resident agent contract in `docs/AGENT_TREE.md` — not
  bindable to Tasks via `target_session_id`.
- **P2-2:** Add `claude-hub task create --parent-task-id` (explicit flag wins
  over `--payload-json`).

### feat: unify Agent Tree into Workspace Task Graph

- Make **Task Graph / TaskMailbox** the canonical coordination plane per
  workspace: `parent_task_id` / `root_task_id` / `path`, append-only Task events,
  `task_call_index`, per-Task `consumer_ack_sequence`. TaskMailbox consumers are
  `task:<task_id>` only.
- Add Task-first REST and `claude-hub task` (`tree`, `events`, `wait`, `ack`,
  `followup`, `abort`). These routes do not write AgentRun lifecycle state.
- Keep `/api/agent-tree/*` as a **compat projection** for legacy linked run ids
  (`Task.agent_run_id`). Ordinary Tasks project to `task.id`; runtime resolution
  uses canonical `Task.agent_run_id` only. Legacy run blobs are load-only;
  public APIs fail closed.
- Worker and reviewer are ordinary Agents assigned on Tasks (`session_id`,
  `review_session_id`, `target_session_id` on start). The optional Resident agent
  is an independent long-running Agent — not a Task root, mailbox consumer, or
  Agent Tree supervisor.
- Fail-closed session assignment on report intake; unassigned Tasks cannot be
  claimed by the first report.
- Cold-load migration (`migrate_pre_unification_graph`) for missing parent edges
  and per-Task ACK cursors. Deprecated `workspace:{workspace_id}:resident`
  consumer keys are migration-only and rejected at runtime.
- **Rollback:** restore pre-migration workspace `state.json` + `index.json`
  backups before running an older binary — otherwise TaskMailbox events, cursors,
  and graph fields are dropped on first save.
- UI: board `related_task_id` remains session-affinity/context reuse (not
  `parent_task_id`). Cancelled legacy plan `487c630c` (Resident Root UI) is not
  an active follow-up.
- Docs: rewrite `docs/AGENT_TREE.md` mental model (Workspace → Task Graph →
  session assignment); `claude-hub task` primary, `agent-tree` compat only.

### feat: add agent-friendly Agent Tree CLI

- Add `claude-hub agent-tree` over the existing `/api/agent-tree` REST
  contract (`roots`, `runs`, `events`, `spawn`, `send`, `followup`, `wait`,
  `ack`, `interrupt`). No new server endpoints.
- `--call-id` is accepted only on spawn/send/followup/interrupt (`POST /ack`
  has no `call_id`). Omitted ids are new UUIDs echoed to stderr before HTTP.
- `wait --ack` renders and flushes the event list, then ACKs `max(sequence)`.
  JSON stdout stays one event array; `--json` then writes
  `{"acked_sequence": N}` to stderr. Human mode prints `acked sequence N`
  after the table.
- Wait HTTP timeout is attached on `build_request` (`request.extensions`);
  httpx 0.28 `Client.send` has no `timeout` kwarg, and the client default
  stays 30s.

### docs: make Agent Tree discoverable for agents

- Add `docs/AGENT_TREE.md` as the canonical backend-only agent guide (mental
  model, 422 stubs, copy-paste REST, `executor_config`, `call_id` / 400 / 409 /
  422 / uncertain recovery, migration/rollback).
- Link it from root `README.md`, `backend/README.md`, and the agent entry
  guides. Expand the Resident injected Agent Tree block with the same
  contract. Label stale working-log claims that simulator stubs are public.
  Cursor MUST omit `executor_config.model` (`ManagedTaskAdapter` rejects it).
  Pinned-session spawn omits `executor_config` (Hub derives) or must match
  the selected session; adapter-rejected managed config is HTTP 400, and
  HTTP 422 is only unavailable executors.

### feat: make Agent Tree a reliable multi-CLI subagent control plane

- Make report intake and mailbox ACK advancement one workspace-scoped atomic
  commit: report/session/task state and Agent Tree run/event/cursor state roll
  back together on a pre-commit failure, while wakeups and other side effects
  run only after the durable `state.json` replace succeeds.
- Authorize and replay arbitrary-depth owned subtrees. A root can wait on and
  interrupt grandchildren, `subtree=true` includes descendant-authored events,
  and active subtree waiters are awakened without polling.
- Persist managed executor selection on each run: Claude, Codex, and Cursor
  children carry their concrete CLI, optional model, target, and capability
  contract across restart. Explicit sessions are validated against that
  contract; Codex/Claude models reach the real CLI environment/arguments.
- Expose native-subagent and external-job capability metadata as unavailable
  until a real runtime exists; public spawn rejects those simulator-only
  executors with HTTP 422 instead of advertising a fake successful dispatch.
- Use cycle- and purpose-scoped report call IDs for worker, reviewer,
  recovery, reminder, and Goal Packet prompts so retries stay idempotent
  without colliding across review cycles. Monitor soft reminders now emit
  `monitor-reminder` IDs with durable `attempts+1` before increment;
  reviewer fallback/recovery uses verdict-specific recovery IDs.
- Atomic report intake now leaves the `report:<id>` bridge event durable on
  the first commit, so a post-commit side-effect failure still has exactly
  one report and one bridged event.
- Report-intake rollback snapshots the workspace only after the tab-rename
  await, so a concurrent Agent Tree persist during rename is kept when the
  report rolls back
  (`test_report_rollback_preserves_concurrent_agent_tree_write`).
- Report intake and Agent Tree spawn/send/followup/interrupt/ack share one
  per-workspace mutation lock, so a public tree write cannot persist
  between report snapshot and rollback restore
  (`test_report_rollback_serializes_agent_tree_spawn`).
- Resolve existing or conflicting report call_ids before any tab rename so
  a late retry cannot restore a reused session's assignment
  (`test_reused_session_known_call_id_has_zero_side_effects`).
- Canonicalize leading/trailing `call_id` whitespace before preflight and
  persistence so a padded retry cannot rename a reused session
  (`test_padded_call_id_retry_has_zero_side_effects_after_reassignment`).
- Bound-session matching-call replay runs under the same snapshot/restore
  as new reports (`test_bound_replay_save_failure_rolls_back_ack_and_cold_retry_converges`).
- Replay commit tokens always use the canonical call_id so predecessor
  padded keys cannot restore memory after a durable save
  (`test_predecessor_padded_call_id_post_save_failure_converges`).
- Isolated E2E keeps Claude launch credentials in the backend process
  environment; ttyd writes mode-0600 launch scripts and unlinks them on
  every exit (`remaining_credential_artifacts=[]`).
- **Validation**: listed suites 547 passed in 834.25s
  (`test_agent_tree.py`, `test_workspaces.py`, resident/session,
  report-atomicity including
  `test_reused_session_known_call_id_has_zero_side_effects`,
  `test_padded_call_id_retry_has_zero_side_effects_after_reassignment`,
  `test_bound_replay_save_failure_rolls_back_ack_and_cold_retry_converges`,
  and `test_predecessor_padded_call_id_post_save_failure_converges`, subtree
  reliability, executor selection, `test_ttyd_manager.py`, hard-recovery,
  orchestrator-contract); `mypy claude_hub` checked 67 source files with
  no issues; Black, isort, compileall, and `git diff --check` clean.
  Isolated real-CLI E2E observes only: managed Claude POSTed
  `E2E_CHILD_REPORT` (`0b77c71f-...`, `POST /reports` 201, bridge seq=3);
  wait/ACK/reload kept `ack_sequence=3`. Overlay never written; launch_env
  files were mode 0600 then unlinked; `remaining_credential_artifacts=[]`.
  Round 4 harness injection is stale.
- **Migration/rollback**: new fields are additive on forward load. Before
  rollback, back up workspace `state.json` and drain writers: the first
  old-version save drops Agent Tree and report fingerprint metadata it does
  not know, so tree replay and retry deduplication cannot be recovered without
  the backup.
- **Files**: `models/agent_tree.py`, `services/agent_tree.py`,
  `services/agent_tree_adapters.py`, `services/ttyd_manager.py`,
  `services/workspace_manager/`, `api/agent_tree.py`, Agent Tree/report
  reliability tests, and the Agent Tree working log.

### fix: report/result intake idempotency (call_id + payload fingerprint) — no duplicate report/task-transition/bridged-event on error-after-commit retry

- **What**: `create_report` now requires a stable non-empty `call_id` on every
  report (legacy clients that omit it get a deterministic adapter call_id
  derived from the payload fingerprint). The Hub stores a
  `call_id → report_id` mapping and canonical fingerprint mapping on the
  session (`ManagedSession.report_call_ids` and
  `ManagedSession.report_call_fingerprints`). A retry with the **same** `call_id`
  and an identical payload fingerprint returns the previously-persisted
  report (re-running the idempotent post-commit side effects
  `_after_report_recorded` and `_bridge_report_to_agent_event` against the
  existing report). A retry with the **same** `call_id` but a **different**
  payload raises `ReportCallIdConflict` (a `ValueError` subclass) which the
  API surfaces as **HTTP 409 Conflict**. Concurrent requests with the same
  `call_id` are serialized via a per-`(session_id, call_id)` asyncio lock so
  only one report is created. The canonical fingerprint now includes
  `goal_packet`, so a Goal Packet revision submitted with the same call_id
  is correctly treated as a different payload (409) rather than silently
  dropped as an idempotent retry. This makes report/result intake safe under
  error-after-commit retries: if the Hub durably persists the report (and
  the ACKed call_id's processing→delivered transition) via `_save_state`
  but then raises in `_after_report_recorded` or the agent-tree bridge, the
  client's retry with the same `call_id` is a no-op that returns the
  existing report, task transition, and bridged event — exactly one of each.
- **Why**: `create_report` persists the report via `_save_state` **before**
  running post-commit side effects. If those side effects fail, the report
  is durably committed but the client receives an error; a naive retry
  creates a second report, a second task transition, and a second bridged
  agent-tree event. Without a required `call_id`, production prompts
  emitted no idempotency key, so retries always duplicated. Without
  `goal_packet` in the fingerprint, a GP revision with the same call_id
  was wrongly dropped. Without a lock, two concurrent same-call_id requests
  both passed the existence check and created two reports. The fix mirrors
  the existing message-delivery idempotency pattern
  (`call_payload_fingerprints`).
- **How**:
  - `schemas.py`: `call_id: Optional[str]` on `AgentReportCreate` /
    `AgentReport`; `report_call_ids: Dict[str, str]` and
    `report_call_fingerprints: Dict[str, str]` on `ManagedSession`.
  - `_reports.py`:
    - `_compute_report_fingerprint` now includes `goal_packet` in the
      canonical content fields.
    - `create_report` requires a non-empty `call_id`; if the client omits
      it, a deterministic `legacy:<fingerprint[:32]>` call_id is generated
      (compatibility adapter).
    - A per-`(session_id, call_id)` `asyncio.Lock` (`_report_call_locks`)
      serializes the claim so concurrent same-call_id requests create at
      most one report.
    - On call_id reuse with a different fingerprint, raise
      `ReportCallIdConflict(ValueError)` → HTTP 409.
    - On call_id reuse with a matching fingerprint, return the existing
      report and re-run the idempotent post-commit side effects.
  - `api/workspaces.py`: catch `ReportCallIdConflict` → HTTP 409.
  - `_prompts.py`: every report curl example (worker, reviewer, resident,
    continue, recovery) includes a `call_id` field and instructs the agent
    to reuse the same call_id on retry.
- **Migration/rollback**: `report_call_ids` and
  `report_call_fingerprints` default to `{}` on existing sessions; no forward
  migration is needed. Older code ignores them on load but removes them on its
  next state rewrite, so back up state and drain writers before rollback.
  Legacy clients without `call_id` still work via the deterministic adapter.

### fix: fail-closed durable mailbox (persist-intent-first → processing → ACK → delivered; ambiguous → uncertain) + tmux receipt at-most-once + Resident wait/ack + followup:delivered

- **What**: the mailbox uses a Hub-owned **receiver pump** with
  **persist-intent-before-side-effect** semantics. For each pending call_id
  the pump: (1) persists the intent to deliver by moving the call_id from
  `pending_call_ids` to `processing_call_ids`; (2) sends the message to
  tmux. On a **pre-side-effect** failure (tmux write not attempted) the
  call_id rolls back to `pending_call_ids` for retry. On an **ambiguous**
  failure (tmux write may have succeeded) the call_id moves to
  `uncertain_call_ids` — fail-closed: we do NOT auto-resend (could
  duplicate) and do NOT silently mark delivered (could lose). On success
  the call_id stays in `processing_call_ids` until the worker ACKs it via
  `acked_call_ids`; only the worker's ACK moves it to `delivered_call_ids`
  (the **receiver-verifiable durable receipt**). A tmux-server-side
  **receipt** (`@receipt_<sha256(call_id)[:16]>` session option, set
  atomically with the paste) gives cold recovery a way to distinguish
  "paste definitely happened" (receipt present → keep processing, no
  repaste) from "paste definitely did not happen" (receipt absent on a
  live session → move back to pending for one re-delivery) from "cannot
  tell" (session gone → uncertain). On cold restart, call_ids stranded in
  `processing_call_ids` for **STOPPED** sessions (tmux inbox gone) are
  moved to `uncertain_call_ids`; **LIVE** sessions keep their processing
  call_ids so the monitor's receipt reconciliation can decide.
  `send_session_message` raises `DeliveryUncertain` (a `RuntimeError`)
  when a call_id is in `uncertain_call_ids` — fail-closed, no silent
  auto-resume. The operator retries via the explicit
  `retry_uncertain_delivery` endpoint, which queries the tmux receipt:
  receipt present → move back to `processing` (no repaste, nudge Enter);
  receipt absent on a live session → move back to `pending` for one
  re-delivery; session gone → stays `uncertain`. The API surfaces HTTP
  400 on `DeliveryUncertain` so the caller knows an explicit retry is
  required. The
  Resident loop uses `wait`/`ack` (directed cursor), and a
  `followup:delivered` event is emitted when the worker ACKs a followup
  call_id. REVIEWED tasks no longer emit a terminal COMPLETED event on
  the worker's COMPLETED report — only REVIEW_PASSED does.
- **Why**: tmux stdin is NOT a verifiable durable receiver — bytes written
  to tmux may be consumed by the agent and then lost if the agent crashes,
  and the Hub cannot read back what the model actually processed. The
  previous send-first + pane-marker dedup approach relied on tmux pane
  history to detect already-sent messages, but pane history is bounded and
  the `[call_id:<id>]` marker can roll out of the scroll buffer, so cold
  recovery would not see it and would re-send (duplicate delivery). The
  fail-closed design instead treats any in-flight call_id whose outcome is
  unknown as `uncertain`: the operator must explicitly retry, which
  prevents both silent loss and silent duplication. The worker's ACK is
  the only proof of receipt, so `delivered` must be gated on it.
- **How**:
  - `_messaging.py::send_session_message`: for call_id-scoped messages,
    persist the body in `session.pending_messages[call_id]`, append to
    `pending_call_ids`, then kick `_pump_session_messages`. Sender skips
    call_ids already in `processing_call_ids`, `uncertain_call_ids`, or
    `delivered_call_ids`. If the call_id is in `uncertain_call_ids`
    (explicit retry), move it back to `pending_call_ids` on both session
    and task, save, re-pump, and raise `DeliveryUncertain` if it is still
    uncertain. After the pump, if the call_id is in `uncertain_call_ids`,
    raise `DeliveryUncertain` so the caller surfaces the failure.
  - `_messaging.py::_pump_session_messages`: acquires a per-session lock,
    then for each pending call_id: (1) **persist intent**: move call_id
    from `pending` to `processing` and save BEFORE the tmux write; (2)
    **send**: write the message (with `[call_id:<id>]` marker) to tmux;
    (3) **pre-side-effect failure** (exception before `_send_tmux_message`):
    roll back to `pending`; (4) **ambiguous failure** (exception inside
    `_send_tmux_message`): move to `uncertain_call_ids` (fail-closed, no
    auto-retry); (5) **success**: stay in `processing` until worker ACK.
    The message body stays in `pending_messages` until ACK.
  - `_messaging.py::_expire_processing_leases`: for sessions whose tmux
    inbox is gone (`STOPPED`), move `processing_call_ids` to
    `uncertain_call_ids` (fail-closed). For live sessions, processing
    call_ids stay in-flight (the message may already be in the tmux input
    buffer; re-delivering would risk a duplicate turn).
  - `_messaging.py::_mark_processing_as_uncertain`: moves call_ids from
    `processing` to `uncertain` on both session and task, clears
    `processing_call_ids_at`, and emits a `delivery:uncertain` event.
    Used on ambiguous tmux send failure and cold recovery.
  - `_messaging.py::_rollback_processing_to_pending`: moves call_ids from
    `processing` back to `pending` on both session and task, clearing
    `processing_call_ids_at`. Used only for proven pre-side-effect
    failures (tmux write not attempted).
  - `_state.py::_recover_uncertain_deliveries`: on cold start, moves
    `processing_call_ids` to `uncertain_call_ids` **only for STOPPED
    sessions** (tmux inbox gone, receipt unqueryable — fail-closed). LIVE
    (`WORKING`) sessions keep their processing call_ids so the monitor's
    `_recover_processing_via_receipt` can reconcile against the
    tmux-server receipt (present → keep processing; absent → pending;
    session gone → uncertain).
  - `_constants.py::DeliveryUncertain`: `RuntimeError` subclass raised by
    `send_session_message` when a call_id lands in `uncertain_call_ids`.
    Caught by API endpoints (`/tasks/{id}/start`,
    `/sessions/{id}/send`, `/lessons/summarize`) which return HTTP 400.
  - `_reports.py::_ack_call_ids`: commit only — moves call_ids from
    `pending`/`processing`/`uncertain` to `delivered`, removes the message
    body from `pending_messages`. Unknown call_ids are ignored (future-ID
    poisoning protection). The dispatch call_id
    (`dispatch:{task_id}:{dispatch_attempt}`) is implicitly ACKed on any
    report for that task; the legacy no-suffix form (`dispatch:{task_id}`)
    is also accepted for backward compatibility. For each ACKed followup
    call_id, emits a `followup:delivered` event with `delivered: true`.
  - `_reports.py::_emit_followup_delivered_if_followup`: looks up the
    call_id in `agent_tree._call_record`; if `action == "followup"`, emits
    a `followup:delivered` event correlated to the original followup
    call_id. This flips the followup outcome from `delivered: false`
    (published at followup time) to `delivered: true` (worker ACK proof).
  - `agent_tree.py::load_from_dict`: migrates persisted events with
    `recipient=None` to self-address (`recipient = author`) so the directed
    mailbox filter (`e.recipient == run_id`) still delivers them.
  - `_reports.py::_bridge_report_to_agent_event`: for REVIEWED tasks,
    `COMPLETED` → `PROGRESS`; `REVIEW_PASSED` → `COMPLETED`;
    `REVIEW_FAILED` → `PROGRESS`. `agent_tree.py::emit_event` reconciles
    run status: `ready_for_review`/`review_started`/`completed` →
    `WAITING`; `review_failed` → `RUNNING`.
  - `agent_tree.py::_events_for`: recipient-directed reads — a run only
    sees events where `recipient == run_id`. `wait`/`get_events` use
    `effective_since = max(since_sequence, run.ack_sequence)` so ACKed
    events are never re-delivered.
  - `_workspaces.py::_run_resident_agent`: the Resident cycle now uses
    `agent_tree.wait(WaitRequest(...))` (timeout_seconds=1.0) to fetch
    directed events since the cursor, then calls
    `agent_tree.ack(workspace.id, root_run.id, max_seq)` to advance the
    cursor to the highest delivered sequence. This makes `wait`/`ack`
    used in production, not just tests.
- **Honesty note**: this guarantees **at-most-once paste per call_id per
  live tmux session lifetime** (via the tmux-server receipt) plus
  Hub-side durable envelope/ACK (`pending_messages` +
  `call_payload_fingerprints` + `delivered_call_ids`). It does NOT
  guarantee exactly-once across a destroyed-and-recreated receiver tmux
  session: if the session is killed and a new one starts with the same
  name, the receipt is gone. Cold recovery then sees a LIVE session with
  the receipt absent and moves the call_id back to `pending` for **one**
  re-delivery (the paste definitely did not run in the new session).
  Only if the session is gone / unqueryable / STOPPED does cold recovery
  move the call_id to `uncertain` (fail-closed, operator retry). The
  worker's ACK (`acked_call_ids` → `delivered_call_ids`) is the only
  proof the model actually processed the message; the tmux receipt only
  proves the bytes reached the tmux input buffer. It also does NOT
  guarantee exactly-once of arbitrary external tool side effects the
  model invokes — those require the call_id to be propagated as an
  idempotency key by the model itself.

### fix: delivery state-machine correctness (followup non-terminal on uncertain, persist-fail re-raise, ACK-before-delivered ordering, reconcile resumes non-failed terminals)

- **What**: five correctness fixes to the fail-closed delivery state machine
  so that ambiguous/partial failures never leave the lifecycle in a
  terminal or silently-lost state:
  1. `followup` raising `DeliveryUncertain` no longer marks the run
     `FAILED` or emits a `FAILED` event. The `MESSAGE` intent was already
     persisted before the tmux send, so the run stays non-terminal
     (`RUNNING`) and the operator retries via `retry_uncertain_delivery`.
  2. `emit_event`'s duplicate-`call_id` branch now re-raises `_persist`
     failures (never returns success on a failed durable commit) and only
     calls `_wake_for_run` after the durable commit succeeds.
  3. ACK-vs-retry race: `_emit_followup_delivered_if_followup` runs
     `reconcile_followup_outcome` (which appends the
     `followup:outcome` event) **before** appending the
     `followup:delivered` event, and does not swallow persistence
     exceptions. After the pump, a `call_id` already present in
     `delivered_call_ids` is never downgraded back to `uncertain`.
  4. Transaction ordering in `_ack_call_ids`: lifecycle reconciliation
     (outcome event + delivered event + resident ack) runs **before** the
     session/task `delivered` mutation. Because `agent_tree._persist`
     saves the full workspace state, reconciling first guarantees that a
     persist failure leaves disk at `processing` with the payload intact.
  5. `reconcile_followup_outcome` no longer guards on
     `run.status not in _TERMINAL_STATUSES`. It always calls
     `_update_run_status(RUNNING, persist=False)` and delegates to the
     existing transition validator, which allows `COMPLETED`/`INTERRUPTED`
     → `RUNNING` (resume) and refuses `FAILED` → `RUNNING` (truly
     terminal). A `DeliveryUncertain` on a completed/interrupted run can
     therefore be recovered by operator retry.
- **Why**: the first pass had four state-machine holes: (a) `followup`
  treated an ambiguous tmux send as a terminal failure even though the
  intent was durable; (b) the duplicate-event path could return success
  after a failed persist; (c) the ACK path could commit `delivered`
  before the lifecycle events were durable, so a crash left a delivered
  call_id with no outcome/delivered event; (d) `reconcile` refused to
  resume completed/interrupted runs even though `_update_run_status`
  explicitly allows it.
- **How**: see `docs/working-logs/2026-08-16-agent-tree-durable-mailbox.md`
  "Review Round 33 Fixes" for the per-file diff and regression tests.
- **Regression tests added** (10, in `tests/test_agent_tree.py`):
  `test_followup_delivery_uncertain_keeps_run_non_terminal`,
  `test_reconcile_resumes_non_failed_terminal_runs` (parametrized:
  WAITING/COMPLETED/INTERRUPTED→RUNNING, FAILED stays FAILED),
  `test_reconcile_persist_fail_in_memory_retained_then_durable`,
  `test_retry_uncertain_reconcile_fail_compensates_then_succeeds`,
  `test_emit_event_duplicate_persist_fail_reraises_no_wake`,
  `test_ack_delivered_event_persist_fail_reload_keeps_processing_and_payload`,
  `test_ack_vs_retry_race_delivered_not_downgraded`, plus three supporting
  cases.
- **Validation**: `tests/test_agent_tree.py` 156 passed (rc=0),
  `tests/test_workspaces.py` 133 passed (rc=0),
  `tests/test_tmux_receipt_integration.py` 4 passed (rc=0),
  `tests/test_workspace_resident_agent.py` 60 passed (rc=0). mypy clean
  on `claude_hub/`; black/isort clean on touched files.

### feat: unified Agent Tree + Durable Mailbox coordination layer

- **What**: a single persistent coordination layer that converges the Resident
  Agent and managed-task dispatch into one parent/child delegation tree with an
  append-only event stream (durable mailbox). Exposes `spawn`, `send`,
  `followup`, `wait`, `interrupt`, and `list_runs` actions, plus cursor-based
  event replay.
- **Why**: Resident previously scanned global reports to find work; managed
  tasks had no first-class parent/child or supervisor relationship. Agents
  couldn't address messages to a specific run or wait on directed subtree
  events. There was no durable event log for restart replay.
- **How**:
  - `models/agent_tree.py`: `AgentRun` (id, path, parent, supervisor,
    executor_kind, status, context_ref, last_task_message, ack_sequence) and
    `AgentEvent` (monotonic sequence, call_id, correlation_id, type, author,
    recipient, action, target, fingerprint, payload). `ExecutorKind` covers
    `managed_task`, `native_subagent`, `external_job`. `SpawnRequest` carries
    an optional `session_id` for routing managed tasks to a specific worker.
  - `services/agent_tree.py`: `AgentTreeManager` owns the run tree, per-workspace
    append-only event log, call_id idempotency index, and per-run asyncio.Event
    waiters. `_wake_ancestors` notifies the supervisor chain so a root can
    `wait()` on its whole subtree.
    - **Full request fingerprints**: every action computes a SHA-256 hash of
      the canonicalized (action + all request fields) and persists it on the
      resulting `AgentEvent`. A call_id reused with a different payload is
      rejected even after a process restart.
    - **Outbox / crash recovery**: a run and its `DISPATCHED` event are
      persisted *before* the adapter spawn is invoked. On startup,
      `recover_pending_runs()` retries any `PENDING` run whose `context_ref`
      is `None` (spawn lost mid-flight); the managed-task adapter reuses the
      existing task tagged with `agent_run_id`. For interrupt, the
      `INTERRUPTED` intent event is persisted *before* the adapter call so a
      crash mid-interrupt leaves a durable record; recovery retries
      `adapter.interrupt` for any run that has an `INTERRUPTED` event but is
      not yet in `INTERRUPTED` status.
    - **Intent / delivery / outcome protocol**: every mutating action
      (`spawn`, `followup`, `interrupt`, `send`, `emit_event`) mutates
      in-memory state with `persist=False` during the intent phase, calls the
      executor adapter (delivery), then applies the outcome and calls
      `_persist()` exactly once. An adapter failure marks the run `FAILED`
      and persists that single outcome. A late outcome-phase persist failure
      keeps the in-memory state that matches the executor's actual state;
      the next successful persist (or restart reconciliation) catches up.
      `_append_event`, `_update_run_status`, and `_set_last_message` accept
      `persist: bool = True` to support batching.
    - **Recovery replays all unmatched followups in sequence**:
      `recover_pending_runs` finds every followup `MESSAGE` event that lacks
      a matching `:outcome` event and replays them in sequence order (not
      just the latest). Each followup is replayed with its *own* payload
      message (not the run's `last_task_message`). The managed-task adapter
      persists the `delivered_call_ids` receipt atomically with delivery, so
      a crash between delivery and outcome-event persist does not cause
      re-delivery on restart. After replaying followups, recovery still
      reconciles the run's status via the adapter's `get_status()`.
    - **Subtree messaging boundary**: `_validate_messaging_boundary` restricts
      `send`/`followup` so an author may message only its supervisor, a run in
      its own subtree, or itself. Cross-subtree (sibling) messaging is
      rejected with `400`.
    - **Outbound mixing fix**: a run's subtree mailbox excludes events authored
      by the run itself (unless addressed to itself), so a supervisor does not
      re-read its own sent messages.
    - **Terminal status guard**: `FAILED` is truly terminal — no transitions
      out. `INTERRUPTED` and `COMPLETED` may transition back to `RUNNING` via
      `followup` (resume). `interrupt` is a no-op on `FAILED` runs.
    - **ACK cursor bounds**: `ack_sequence` only moves forward and may not
      exceed the workspace's current max sequence.
    - **Resident root before bootstrap**: `_ensure_resident_root_run` is called
      before `ensure_workspace_agent` so the root run exists when the
      bootstrap prompt is built. The root run's `context_ref` is linked to the
      resident session id after the session is created.
    - **ack_sequence consumption**: the resident bootstrap prompt includes the
      root run's persisted `ack_sequence` as the starting `since_sequence` for
      `wait`, and event injection into the prompt uses
      `since_sequence=root_run.ack_sequence` so only unprocessed events are
      surfaced.
    - **Quota**: `MAX_CONCURRENT_CHILDREN = 32` active (non-terminal) children
      per parent; the 33rd spawn raises `RuntimeError`.
  - `services/agent_tree_adapters.py`: `ManagedTaskAdapter` wraps the existing
    task/session/report flow (spawn→create+start, followup→continue/start or
    re-create if deleted, interrupt→abort). `ResidentRootAdapter` is a no-op
    for spawn/send/followup (the resident picks up mailbox messages on its
    next cycle), aborts the resident session on interrupt, and returns
    `RUNNING` while the session exists. `NativeSubagentAdapter` and
    `ExternalJobAdapter` are in-memory stubs that satisfy the contract.
  - `api/agent_tree.py`: REST endpoints under `/api/agent-tree`. Every
    mutating action (`spawn`, `send`, `followup`, `interrupt`) enforces
    authority: the caller's session must own the `author_id` run
    (`run.context_ref == session_id`). Local network requests (auth
    disabled) skip the check.
  - **Resident master mode migration**: the resident prompt now delegates work
    via the agent tree API (`spawn` with `session_id`, `wait`, `ack`,
    `followup`, `interrupt`) instead of scanning the task board directly.
    `PATCH /tasks/{id} status=done` is retained for task acceptance.
  - Persistence: runs and events are serialized into each workspace's
    `state.json`; `load_from_dict` rebuilds the call_id index (using persisted
    fingerprints) and next sequence counter so idempotency and monotonic
    ordering survive restart.
- **Verified**: 88 agent-tree tests (root run, spawn, call_id idempotency,
  send, followup, wait immediate/blocking/timeout, interrupt, subtree scoping,
  emit_event status update, emit_event idempotency, report→event bridge,
  save/load round-trip with sequence continuity, concurrent spawns, concurrent
  waits, crash recovery via `recover_pending_runs` for spawn-lost,
  interrupt-lost, and followup-lost runs, multiple unmatched followups
  replayed in sequence with each event's own payload message,
  `delivered_call_ids` receipt persisted atomically so re-delivery is skipped,
  status reconciliation runs after followup replay, late-persist-failure keeps
  in-memory state, duplicate delivery does not re-trigger adapter,
  `ResidentRootAdapter` behaviour, followup resume `INTERRUPTED`/`COMPLETED`→`RUNNING`
  and `FAILED` cannot resume, API authority non-owner 403 / owner 200,
  `ManagedTaskAdapter.followup` starts TODO task and recreates deleted task,
  quota enforcement, terminal status guards, ACK cursor forward-only +
  max-sequence bounds, subtree messaging boundary rejection, outbound-mixing
  exclusion of self-authored events, legacy `followup`→`continue_task`, API
  endpoint smoke test, non-local no-cookie requests fail closed with 403,
  ManagedSession reads scoped to own workspace and owned subtree,
  cross-workspace reads rejected). Resident (60), workspace (133),
  sessions/state-policy/orchestrator-contract (238 combined) tests pass.
  black, isort, mypy clean.

### fix: persist custom env presets to backend so they survive cross-origin access

- **What**: user-defined Launch Environment presets (the named KEY=VALUE sets in
  "Manage Environment Presets") are now persisted to `~/.claude_hub/env_presets.json`
  and synced via new `GET/POST/PUT/PATCH/DELETE /api/env-presets` endpoints.
  Hidden built-in preset visibility is also persisted.
- **Why**: previously presets lived only in `localStorage` under key
  `claude-hub.launch-env-presets`, which is scoped to a single browser origin.
  Accessing the UI via LAN IP (e.g. `http://192.168.x.x:8173`) instead of
  `http://127.0.0.1:8173` silently hid every previously saved preset because the
  two origins have isolated localStorage.
- **How**: frontend `useLaunchEnvPresets` composable now loads from backend on
  first use (with localStorage as an optimistic cache and offline fallback),
  performs a one-time bulk-import merge when backend is empty but localStorage
  has data, and syncs every save/delete/hide mutation to the backend. Built-in
  presets remain hardcoded; their hidden state is now cross-origin. Backend
  never logs preset text values (which may contain API tokens).
- **Verified**: 39 new backend unit + API tests (service CRUD, persistence,
  corrupt-file handling, bulk-import merge, REST endpoints); frontend
  `pnpm lint`, `pnpm vue-tsc --noEmit`, `pnpm build` all pass; Playwright
  cross-origin verification sets a preset on localhost and reads it back via
  a different origin.

### fix: prevent orphan ttyd listeners from blocking new tabs

- **What**: new terminal tabs skip ports that already have a live listener,
  and a tab creation cancelled by backend reload now stops its unpersisted
  `ttyd` process and tmux session.
- **Why**: a reload could interrupt `create_tab()` after `ttyd` bound its port
  but before the tab reached `tabs.json`. The orphan listener survived with
  no owning tab, so later Codex tab creation repeatedly failed with
  `EADDRINUSE` and the UI only reported `Failed to duplicate tab`.
- **How**: `_get_next_port()` bind-probes each candidate without connecting to
  unrelated services, and startup retries if another process wins the bind
  race. Rollback tracks tmux ownership and completes despite repeated task
  cancellation before re-raising the original error.
- **Verified**: both regression tests failed before the implementation and
  pass after it; a live duplicate of the affected QFO Codex tab succeeded on
  port `10394` after three stale listeners were identified and stopped.

### fix: make Feedback Reaper summary tasks visible, idempotent, and retryable

- **What**: `/lessons/summarize` now exposes its managed Feedback Reaper task on
  the workspace board, in snapshots, and on the assigned agent instead of
  showing an apparently idle `no task` agent that could not be deleted.
  `system_internal` still owns the automatic review-skip/completion semantics;
  it no longer implies that lifecycle state must be hidden. The board receives
  a short system-task description rather than the up-to-100K prompt payload.
- **Idempotency**: summary triggers are serialized per workspace. Concurrent or
  repeated calls return the existing active `FeedbackSummaryRun` and task,
  rather than creating duplicate tasks or agent assignments. A missing persisted
  assignment is reset to a visible retryable Todo before redispatch.
- **Failure recovery**: the post-budget record selection is persisted as a
  bounded `*.pending.json` commit package. Records enter the processed index
  only after the Feedback Reaper posts a successful completion report.
  Prompt preparation failures retain a visible provisional task/run; dispatch
  errors, completion-persistence errors, and `blocked`/`needs_input` reports
  release the agent while preserving input for same-task retry. Manual abort
  and task deletion terminate the run, discard the pending package, and leave
  records available for a fresh summary task.
- **Cleanup**: removed legacy task
  `45e5e7eb-7551-4388-ab01-0f2c67fd5848` and its five active reports from the
  Claude Hub workspace state. Its former agent `cb-agent-2-8b1de3` was already
  absent; the delete-safe task record and summary-run audit remain available.
- **Verified**: 257 focused feedback/state-policy/workspace tests pass, including visible
  task/session ownership, repeated duplicate triggers,
  prompt/dispatch/completion failure retry, `needs_input` recovery, deferred
  processed-index commit, and abort/delete requeue behavior. The full backend
  suite reports 579 passed with the same 61 pre-existing pytest-asyncio
  event-loop failures documented on `main`; Black, isort, and mypy pass.

### fix: prevent codex/cursor session cross-wiring on cold restart

- **What**: after a full service cold restart (tmux server gone), same-type
  agent tabs no longer resume each other's conversations ("串台"). Codex and
  Cursor tabs now pin stable session ids to their tab and correctly attribute
  newly-created rollouts/chat-stores to the launching tab, with fail-closed
  quarantine whenever ambiguity is detected.
- **Why**: five interacting bugs caused the cross-wiring: (BUG-1) a single
  global launch epoch made all tabs' ts windows overlap; (BUG-2) a 600s
  discovery window picked up old unrelated rollouts; (BUG-3)
  `codex resume --last`/`--continue` fell back to any recent session in the
  same cwd when the intended sid was missing; (BUG-4) per-tab scanning with
  multiple concurrent launches caused N-way ambiguity; (BUG-5) ttyd lazy
  execution meant the epoch was stamped ~1s after codex had already started
  writing.
- **How**:
  - Removed the `--last`/`--continue` fallback for codex; only resume when the
    persisted sid is verified to exist on disk, otherwise start fresh.
  - New global `GLOBAL_CODEX_LAUNCH_LOCK` serializes cold codex launches so
    signal attribution sees exactly one new/appended rollout per tab.
  - Fence-poll with 0.7s silence after rollout activity (replaces the 600s
    wait), 200ms × 30 poll budget (~6s wall). A rolling observation snapshot
    lets the fence settle while the immutable pre-launch scan remains the
    attribution baseline.
  - Per-tab new-pin timestamp window anchored in `ensure_tmux_session`
    immediately before `tmux new-session -d`, with bounds `[-2s, +8s]`.
  - Phase-R reconciliation (R1-R8) verifies existence, cwd-realpath match,
    ts-window/growth, non-empty, no duplicate sids, bijection between
    expected and actual new/appended sids per cwd against one global pre-launch
    scan; extras trigger whole-cwd quarantine rather than risking
    misattribution. Signal failures must be salvaged before any sibling launch
    or immediately roll back the cwd batch.
  - Cursor CLI is a *constructive pin*: `agent --resume <uuid>` creates a
    fresh chat store immediately if one does not exist, so every cursor tab
    now pins a uuid4 at construction and always passes `--resume`; persisted
    ids resume only after cwd-scoped verification. Missing/wrong-cwd ids rotate
    to a constructive fresh uuid. Verification uses `store.db` meta key=0
    (hex-JSON, `agentId` field) with `meta.json` realpath fallback via
    `services/_cursor_verify.py`.
  - New persisted field `resume_quarantined` (default False, back-compat) on
    each tab; set True on atomic rollback/Phase-R failure, cleared on
    successful fresh pin or verified resume. Quarantine verifies tmux teardown
    before clearing the last known owner sid and surfaces cleanup failures.
- **Verified**:
  - V0 empirical probes confirmed codex 0.146.1 rollout format
    (`{type:session_meta, payload:{id, session_id, cwd, timestamp}}` with
    `payload.id` the canonical sid; `archived_sessions/` flat layout;
    `session_id` differs from `id` on forks).
  - 98 unit tests in `test_ttyd_manager.py` + `test_codex_sessions.py` (all
    pass), including wrong-cwd rejection and fail-closed teardown.
  - 8 V4/V6 integration tests in `test_recovery_integration_v4v6.py`
    (archived-sessions flat scan, verified-resume append, 3-codex same-cwd
    no-cross-wiring, quarantined-tab fresh start, cursor `--resume` pinning,
    R8 extra-sid quarantine, cursor DB verification, single-tab fresh pin).
  - Real-process cold-restart oracle: 7 tabs across 3 cwd values (3 Codex,
    2 Cursor, Claude, Terminal), exact tab/type/cwd/full-SID/tmux/port marker
    bijection, active + flat-archived Codex resumes, 0 cross-wiring. Uses a
    private tmux server; cold recovery 7.51s and full focused test 8.88s.
  - Full backend suite: 566 passed; the same 61 pre-existing pytest-asyncio
    running-loop failures as `main@2144b4a`; no new failure class or count.
  - `black` / `isort` / `mypy` clean on all touched files.

### fix: bound Feedback Reaper prompt size for small-context agents

- **Strategy — SIMPLE + dynamic budgeting (5 commits, 8 files)**: shrinks the
  Feedback Reaper prompt without redesigning the feedback pipeline. Bounds are
  hard per-field caps plus a **dynamic oldest-first budget trim**: at assembly
  time the oldest digests are dropped until the serialized prompt fits under
  the 100K-char hard ceiling (down to zero digests if necessary). Verbose fields
  are clipped to fixed maxima, unneeded local metadata is omitted, the inline
  worker/reviewer lesson index is tightened by small constant factors, and a
  two-phase prepare/commit pattern ensures dropped digests carry over to the
  next run. No new subsystems, no retrieval overhaul. Delivered across five
  commits (initial bound + fingerprint/limit correction + per-field caps/hard
  budget/deferred commit + legacy-fingerprint sanitization) touching 8 files:
  `CHANGELOG.md`, `backend/claude_hub/models/schemas.py`,
  `backend/claude_hub/services/feedback_lessons.py`,
  `backend/claude_hub/services/workspace_manager/_feedback.py`,
  `backend/claude_hub/services/workspace_manager/_prompts.py`,
  `backend/claude_hub/services/workspace_manager/_tasks.py`,
  `backend/tests/test_feedback_lessons.py`, `backend/tests/test_workspaces.py`.
- **What**: the internal Feedback Reaper summarization prompt (which runs
  periodically to extract reusable workspace lessons) could grow unboundedly:
  incremental mode included ALL unprocessed records (223 in one workspace →
  ~1.46M chars that exceeded codex's 1,048,576-char request limit), verbose
  validation/risks/final_summary/title/path/tag/id fields were included verbatim,
  and active_lessons carried full summary text with a 50-entry cap. Even after
  the v4 cap/fingerprint fixes, adversarial long titles/tags/paths could blow
  the promised 128K-char bound. v5 closes the last gaps. Five fixes: (1) **all
  modes cap at 30 digests per run** (the previous "INCREMENTAL skips the cap"
  was a bug that let backlogs grow without bound); remaining unprocessed
  records stay uncached and are picked up on the next run. The request schema
  keeps the legacy `limit` range 1..200 (backward compat); the store clamps to
  30 internally rather than returning HTTP 422. (2) **Every free-text field in
  both input_task_digests and active_lessons is capped**: task_id ≤ 64, title ≤
  80, final_summary ≤ 200, changed_files paths ≤ 120 chars and ≤ 6 items,
  validation/risks ≤ 160 chars and ≤ 2 items each, report_states ≤ 8 items
  each ≤ 32 chars; active-lesson id ≤ 80, title ≤ 80, summary ≤ 120, tags ≤ 6
  each ≤ 24 chars. The `fingerprint` field is **never truncated** (it is the
  authoritative 25-char `workspace:<16-hex>` merge key — Reaper echoes it
  verbatim on POST and the server merges on exact match). Ellipsis accounting
  (`value[:n-3] + "..."`) means emitted strings never exceed `n`. (3) **Dynamic
  oldest-first budgeting under a 100K-char hard cap**
  (`REAPER_PROMPT_HARD_CHAR_LIMIT = 100_000`, ≈25K tokens) enforced at
  prompt-assembly time as defense in depth: if after per-field capping the
  serialized prompt still exceeds budget, the oldest digests are popped one by
  one (strict — all the way to zero digests if necessary) until it fits.
  Together with the deferred processed-index commit (cycle-7 below) this
  guarantees carry-over: dropped digests stay unprocessed and are retried on
  the next Reaper run. (4) **Compact serialization** drops path/sha256/
  summarized_at/completed_at local metadata and unused package fields beyond
  run id/mode/input_record_ids. (5) **Active lessons dedup payload retains the
  exact `fingerprint`** plus ≤120-char summary snippet and caps at 20 so the
  Reaper can deterministically echo an existing fingerprint on POST to merge
  near-duplicates; the instruction preamble was compressed from ~75 to ~28
  lines and explicitly tells the Reaper to use the fingerprint field and
  keep its own POST fields within char bounds. Additionally, the worker/
  reviewer lesson index is tightened (limit 8→5, title 72→50 chars, tags
  capped at 4, shorter boilerplate).
- **Why**: after the prior compact-index fix the inline worker/reviewer block
  was ~340 tokens, but the Reaper prompt itself — generated when summarizing
  workspace feedback — could exceed codex's hard 1,048,576-character request
  ceiling when a workspace accumulated many completed tasks between reaper
  runs.
- **How**: `_REAPER_MAX_DIGESTS_PER_RUN = 30` caps all modes in
  `prepare_summary_input`; `REAPER_PROMPT_HARD_CHAR_LIMIT = 100_000` is the
  defense-in-depth global budget enforced in `_build_workspace_feedback_summary_
  prompt` (oldest digests dropped if exceeded). Field-level constants
  (`_DIGEST_MAX_TASK_ID=64`, `_DIGEST_MAX_TITLE=80`, `_DIGEST_MAX_FINAL_SUMMARY=
  200`, `_DIGEST_MAX_ITEM_CHARS=160`, `_DIGEST_MAX_PATH_CHARS=120`,
  `_DIGEST_MAX_CHANGED_FILES=6`, `_DIGEST_MAX_VALIDATION_ITEMS=2`,
  `_DIGEST_MAX_RISKS_ITEMS=2`, lesson caps `_LESSON_MAX_ID=80`, `_LESSON_MAX_
  TITLE=80`, `_LESSON_MAX_SUMMARY=120`, `_LESSON_MAX_TAG_LEN=24`, `_LESSON_MAX_
  TAGS=6`) are applied both at digest creation (`_digest_task_record`) and in
  `_compact_digest_for_prompt()` (so pre-v5 cached records are also compacted).
  Fingerprint is preserved verbatim — never truncated.
  `_build_workspace_feedback_summary_prompt` compresses instructions and
  includes id/fingerprint/title/summary/tags/confidence in active_lessons;
  payload example shows the optional `fingerprint` merge field and tells the
  Reaper to keep its own POST fields within bounds. `_CONTEXT_LIMIT_DEFAULT`
  8→5, title/boilerplate tightened in `_lesson_context_block_from_payload`.
  `FeedbackSummaryRequest.limit` default 50→30, schema max stays at 200
  (clamped internally at the store). `FEEDBACK_SUMMARY_PROMPT_VERSION`
  bumped 3→5 (v5 adds comprehensive per-field caps + hard budget; the v4
  fingerprint/title/summary/tag/id shapes are preserved so this is backward
  compatible with in-flight Reaper runs).
- **Verified**:
  - *Normal workspace* (277 task records, 5 active lessons): full prompt
    ~57,036 chars / ~14,259 tokens (~96.1% char reduction from the 1.46M-char
    codex failure).
  - *Adversarial worst-case* (30 tasks each with 5000-char title/final_summary/
    validation/risks, 20×1500-char changed_files, plus 20 active lessons with
    long titles/summaries/tags): prompt ~69,660 chars / ~17,415 tokens, under
    both the 100K hard budget and the 128K/32K target; fingerprint field
    preserved exactly (`workspace:<16-hex>`); long 5000-char strings do not
    appear verbatim.
  - 153 lesson/feedback/reaper tests pass (23 feedback + 130 workspace),
    including new adversarial and contract tests covering long outer
    task_id clamping, unknown-fingerprint rejection, strict-budget zero-
    digest termination, exact post-budget digest/ID alignment, next-run
    carry-over for budget-dropped records, and legacy oversized-
    fingerprint sanitization:
    `test_compact_digest_for_prompt_bounds_every_free_text_field`,
    `test_prepare_summary_input_clamps_large_limit_to_max_digests`,
    `test_feedback_summary_request_accepts_legacy_limit_range`,
    `test_create_lesson_merges_deterministically_when_fingerprint_echoed`,
    `test_create_lesson_rejects_unknown_client_fingerprint`,
    `test_prepare_summary_input_clamps_outer_task_id`,
    `test_budget_loop_drops_oldest_and_carry_over_works`,
    `test_legacy_oversized_fingerprints_are_sanitized_in_prompt`,
    `test_create_lesson_rejects_unknown_fingerprint_e2e`,
    `test_reaper_prompt_stays_under_hard_budget_with_adversarial_input` (e2e).
    black/isort/mypy clean.
- **Cycle-7 hardening**: three correctness fixes on top of v5:
    (a) **Outer `task_id` clamped** at record-build time so the compact
    wrapper `task_id`, `input_record_ids`, and digest-inner `task_id`
    all respect `_DIGEST_MAX_TASK_ID=64` even for pre-v5 legacy cache
    entries or records with oversized id-in-JSON;
    (b) **Client fingerprints validated**: `create_lesson` rejects a
    client-provided fingerprint that does not reference an existing
    active lesson with HTTP 400 — bogus fingerprints no longer create
    unreachable lessons with non-deterministic keys. The server always
    recomputes a canonical `workspace:<16hex>` fingerprint when the
    client omits the field;
    (c) **Processed-index write deferred until after budget trimming**:
    `prepare_summary_input()` no longer marks records processed on
    entry selection. The new `commit_summary_input(summary_input,
    committed_paths)` method writes the index AFTER
    `_build_workspace_feedback_summary_prompt` returns the final
    (post-budget) digest set, so records dropped by the hard global
    budget are **never** marked processed and are retried on the next
    Reaper run (carry-over). `FeedbackSummaryRun.input_record_ids`
    and the audit-log `input_record_ids=` field now reflect the final
    committed IDs, not the pre-budget selection. The budget-dropping
    loop is strict (`while len(prompt) > budget and len(digests) >= 1`)
    and terminates cleanly even when the fixed preamble alone exceeds
    the budget (zero-digest prompt, no crash, all records carried over).
    Internal `_path` bookkeeping is stripped from the serialized prompt
    so local filesystem paths never leak to the agent.
- **Cycle-9 hardening**: legacy fingerprint sanitization so pre-v5 lessons
    with non-canonical or oversized fingerprints cannot bypass the hard
    prompt ceiling. Valid canonical fingerprints
    (`workspace|family|global:<16hex>`, ≤26 chars) are still passed
    verbatim as merge keys; any fingerprint longer than 64 chars or not
    matching the canonical regex is replaced with `""` in the
    `active_lessons` payload (the Reaper cannot merge against a
    non-canonical fp anyway, since the server rejects unknown client
    fingerprints with HTTP 400). This enforces the 100K budget even on
    workspaces that accumulated lessons with adversarial fingerprints
    before the v5 validator shipped. New persisted-legacy regression
    `test_legacy_oversized_fingerprints_are_sanitized_in_prompt` writes
    four lessons (one canonical, one 5KB oversized fp, one short
    non-canon fp, one empty fp) directly to disk via
    `_write_lesson_index()` and asserts (i) the prompt stays under the
    hard budget, (ii) oversized/non-canon fps are absent from the
    serialized prompt, (iii) the good canonical fp appears verbatim,
    (iv) every fp in the payload is either `""` or matches the canonical
    regex, and (v) internal `_path` bookkeeping does not leak.

### fix: restore terminal history and resize injection on first load

- **What**: terminal iframe HTML once again receives the history replay,
  scroll-to-bottom, manual refresh, and in-iframe ResizeObserver/fit guards.
  First opening an existing agent tab can therefore recover scrollback and
  keep the bottom prompt inside the pane without repeated layout toggles.
- **Why**: the earlier desktop-fit fix added the literal CSS example
  `html/body {width:100%;height:100%}` inside a Python f-string comment without
  escaping its braces. Every terminal HTML request raised `NameError: name
  'width' is not defined` while constructing the injected script; the broad
  fallback caught the exception and silently served untouched ttyd HTML, so
  all history and resize recovery code was absent at runtime.
- **How**: escape the braces in the generated-script comment and add a proxy
  regression test that executes the complete HTML injection path, consumes the
  streaming response, and asserts the history, fit, refresh, and ResizeObserver
  guards are present with a correct content length.
- **Verified**: targeted backend unit coverage plus real Playwright checks of
  the complete proxied ttyd document and first-load xterm geometry/history.

### fix: trim workspace lessons index in agent prompts for smaller agents

- **What**: the lessons index block injected into every worker and reviewer
  prompt is shortened in three ways: (1) relevance-filters to the top 8 lessons
  via the existing token-overlap search (with a confidence+hits fallback when
  the query is empty or yields no matches) instead of dumping up to 20 lessons
  unconditionally; (2) compacts each lesson to one pipe-delimited line
  (`id | title | tags | conf`) with long titles ellipsized; (3) **removes the
  instruction to read `docs/working-logs/lessons-catalog.md`** — that file is a
  25 KB human-facing catalog covering 10 different workspaces (H20 GPU tasks,
  etc.), does not contain this workspace's lessons, and was causing weak
  agents (cursor, codex) that dutifully read it to burn ~6K tokens on
  cross-workspace cruft. On-demand detail now points exclusively to the
  workspace-scoped `GET /api/workspaces/<id>/lessons/<id>` endpoint, which
  returns only the requested lesson body.
- **Why**: two problems compounded. (a) The inline index dumped all lessons
  regardless of task relevance, wasting tokens as the catalog grew. (b) The
  catalog-file instruction — the real culprit — sent agents to a 25 KB
  cross-workspace document that was never regenerated per-workspace
  (`render_lessons_catalog_md()` is defined but never called by the backend).
  Smaller-context agents would either stall reading it or ignore lessons
  entirely. After this fix the inline block is ~1.3K chars (~340 tokens) and
  agents only pay for lessons they actively fetch.
- **How**: `FeedbackLessonStore.lesson_context_payload` calls
  `search_lessons()` (token-overlap against title/summary/do/avoid/tags) with
  a confidence+hit-count fallback instead of slicing the unordered list. It
  drops unused `scope`, `hit_count`, and `success_count` fields from the
  payload. Default limit moves from 20 to `_CONTEXT_LIMIT_DEFAULT = 8`.
  `_lesson_context_block_from_payload` renders a denser pipe-delimited list,
  ellipsizes titles over 72 chars, and replaces the catalog-file pointer with
  a workspace-scoped API endpoint reference.
- **Verified**: 3 lesson-specific tests (injection+tracking, CJK relevance
  filtering, emoji no-match fallback; negative assertion that
  lessons-catalog.md is no longer referenced), 63 lesson+review tests pass;
  full backend suite 526 passed (83 pre-existing asyncio event-loop failures
  unchanged from main). `black`, `isort`, and `mypy` are clean.

### fix: agent control-plane requests bypass loopback proxies

- **What**: every agent-facing Claude Hub API command now bypasses configured
  proxies for `localhost`, `127.0.0.1`, and `::1`. This covers resident board
  reads and task orchestration, worker progress reports, dispatcher decisions,
  reviewer verdicts, recovery prompts, lesson maintenance, and resident
  heartbeats.
- **Why**: managed tmux sessions can inherit `HTTP_PROXY` / `HTTPS_PROXY` from
  the local machine. The generated `curl -sS http://localhost:8173/...`
  commands then sent Hub control-plane traffic through that proxy, returning
  `502 Bad Gateway`. Resident cycles appeared to run but safely did no work,
  while reports and heartbeats could be lost across every agent role.
- **How**: all generated internal API examples share a curl prefix with
  `--noproxy 'localhost,127.0.0.1,::1'`. External traffic keeps its existing
  proxy behavior because bypassing is limited to loopback hosts.
  `--fail-with-body` makes HTTP 4xx/5xx responses fail visibly instead of being
  mistaken for successful reports.
- **Verified**: the regression test first failed against the old templates,
  then passed after the fix. Resident and orchestrator prompt tests, the
  non-browser backend suite, formatting, import order, isolated typing, and a
  live proxy/no-proxy board probe cover prompt rendering, orchestration,
  reporting, and runtime behavior.

### fix: desktop terminal input box no longer disappears; refresh button now recovers display

- **What**: on desktop (canvas renderer), the bottom agent input box / prompt
  line would frequently render off-screen, showing a blank area at the bottom
  of the terminal. Clicking the ↻ refresh icon in the pane header would reload
  history but **not** recover the missing rows; only switching layouts (which
  forces a window resize) made the input reappear.
- **Why**: xterm.js's FitAddon.fit() can fire during initial iframe load before
  the parent flex container chain reaches its final dimensions — TabBar and
  pane-header both have 180ms max-height CSS transitions, so the terminal
  container is ~28–76px shorter than final when the first fit() runs, producing
  too few rows and clipping the bottom input. Once the container settles there
  was no in-iframe observer to re-run fit(); the parent frame's ResizeObserver
  posts a `terminal-resize` message but that message can arrive before the
  iframe terminal is ready and get dropped, and if the container is already at
  its final size when the iframe script attaches the parent ResizeObserver
  never fires again. Additionally, `refreshHistoryFromTmux()` only called
  `term.refresh()` (a glyph repaint) without re-running fit(), so the manual
  refresh path could not correct stale rows.
- **How** (three overlapping defenses):
  1. **ResizeObserver inside the iframe** (injected via `terminal.py`'s
     `setupResizeGuard`) observes `document.body` and the xterm container
     directly and calls `fitAddon.fit()` (debounced 150ms, with an rAF follow-up
     fit to catch CSS transition frames). Ignoring sub-8px observations prevents
     fitting to a 0-sized pre-layout element. `window.resize` listener is the
     fallback where ResizeObserver is unavailable.
  2. **fit() after every history refresh**: `writeHistorySnapshot.done()` now
     schedules `term.__claudeHubRequestFit()` on the next frame after replaying
     tmux content. This makes the manual ↻ button and auto-round-complete
     refresh both recalculate rows, matching the user expectation that
     "refresh" fixes display problems — no more need to toggle the layout.
  3. **Belt-and-suspenders delayed fit on terminal-ready**: two late fits at
     250ms and 500ms after `hookTerm` cover the worst-case CSS transition
     settling window for the initial iframe load.
  On the frontend (`TerminalView.vue`), the manual refresh path now also sends
     an explicit `terminal-resize` message, and cached-tab reactivation on
     desktop schedules a resize at 200ms to guarantee fit when switching back
     to a tab that had stale rows. Mobile (WebGL + visualViewport keyboard
     handling) is unchanged.
- **Verified**: `pnpm lint` clean, `pnpm build` clean (vue-tsc + vite), backend
  `py_compile` + import clean. Manual verification: desktop claude/codex tabs
  show the prompt line on load; refresh button recovers the view without
  switching layouts; resizing the window, opening DevTools, switching tabs, and
  switching layouts all keep the terminal correctly filling its pane.

### feat: mobile terminals support long-press to select text and copy

- **What**: on touch devices you can now **long-press** directly on terminal
  text to start a selection — no need to open the mobile controls panel and
  toggle the "选择" (Select) mode first. Long-press selects the word under your
  finger, dragging extends the selection cell-by-cell, and lifting your finger
  copies the selection to the clipboard (with the existing 已复制/无选择/复制失败
  toast). A normal quick swipe still scrolls the terminal with native inertia.
- **Why**: mobile users reported they still could not long-press to select and
  copy ("移动端还是无法长按选中文本进行复制呢"). The prior work only added an
  explicit select-mode toggle; the natural long-press gesture — the first thing
  users try — did nothing.
- **How**: extended the touch handlers injected into the ttyd proxy page
  (`backend/claude_hub/api/terminal.py`). On touchstart (when explicit select
  mode is off) a ~450ms long-press timer is armed; moving the finger past a
  10px tolerance before it fires cancels it so swipes still scroll. When it
  fires, an implicit selection session starts at the press point, selects the
  word there (via xterm's `selectWordAt` when available, else a manual
  ASCII word-boundary fallback; wide/non-latin lines fall back to a single-cell
  anchor so we never highlight the wrong span), and drag/lift reuse the existing
  `_applySelection` / `copyCurrentSelection` helpers. The explicit "选择" toggle
  and desktop mouse selection are unchanged. No frontend or backend API change.
- **Verified**: automated Playwright touch test (Pixel 5 emulation, coarse
  pointer) against a live proxied terminal — (1) bare long-press selects and
  copies the whole word `bytedance@K22QP73Q9Y` (clipboard verified); (2)
  long-press + drag extends to `bytedance@K22QP73Q9Y backend %` and copies;
  (3) a quick swipe scrolls (scrollTop 737→549) with no selection. Backend
  `black`/`isort`/`mypy` and the terminal test suite (`test_terminal_proxy.py`,
  `test_terminal_replay.py`, `test_terminal_input_latency_guard.py`, 32 passed)
  pass; the rendered injected script passes `node --check`.

### fix: mobile agent terminals (Codex) lost their bottom input box — force WebGL renderer on touch devices

- **What**: after the "terminal显示优化" MR
  (`102bde1`, merged `84906d7`) switched the ttyd/xterm renderer from WebGL to
  the 2D **canvas** renderer, mobile browsers (Android/HarmonyOS) stopped
  painting the bottom rows of agent TUIs — the Codex composer/input box at the
  bottom of the terminal rendered as a blank area (see reported HarmonyOS
  screenshot). Desktop was unaffected.
- **Why**: ttyd 1.7.7 here bundles **xterm.js v5**, which selects its renderer
  via a loaded addon; the canvas renderer switch was made globally
  (`ttyd_manager` `-t rendererType=canvas`) to fix desktop mouse
  selection-highlight alignment. The 2D canvas renderer misrenders on some
  mobile GPUs (a real-device/GPU-driver issue; it does not reproduce in
  headless SwiftShader, which rasterizes canvas on the CPU). WebGL was the
  pre-MR default and renders those bottom rows correctly on mobile.
- **How**: a single ttyd process serves every client, so the renderer must be
  chosen per client. ttyd applies `?rendererType=` from the iframe URL query
  with the highest precedence (its own `parseOptsFromUrlQuery`). `TerminalView`
  now appends `?rendererType=webgl` to the terminal proxy iframe `src` on
  coarse-pointer (touch) devices only, keeping the desktop canvas renderer (and
  its selection-highlight fix) untouched. Mobile text selection is unaffected
  because it uses the renderer-independent touch "select" copy mode. No backend
  change.
- **Verified (simulated)**: full Vue app under Playwright emulation — mobile
  (touch) loads `…/?rendererType=webgl`, WebGL renderer active (1 webgl canvas),
  terminal fits (39 rows) and the bottom "input box" rows render at the bottom;
  desktop loads `…/` (no query), canvas renderer active (4 layers), bottom box
  rendered. Frontend `lint` + `build` (vue-tsc) pass.

### fix: Goal Packet (and impl-review-failed) review-pass callbacks survive a stopped worker

- **What**: when a reviewer approved (or rejected) a Goal Packet while the
  worker agent's session was `STOPPED` (post-backend-restart race, tmux crash,
  cold `claude --resume`), the task silently stranded at `status=review` with
  `goal_packet.status=approved`/`rejected` and `reviewed_cycle == review_cycle`
  — the worker never received the "begin implementation" continue prompt and
  no recovery path fired. The HTTP response to the reviewer carried a 500, but
  the UI showed "Approved" because the verdict was already persisted.
- **Why**: `continue_task(...)` raises `RuntimeError` ("Review task original
  agent is stopped") on a stopped worker BEFORE its internal try/except around
  tmux delivery. The two calls in `_handle_goal_packet_review_report` (GP
  approve / GP reject) and the one in `_handle_review_report` (impl-review
  failure reopen) were `await`ed bare, so the exception propagated all the way
  out of `_after_report_recorded` to the HTTP layer. Meanwhile the
  `create_report` fast-path had already sealed the round (advanced
  `reviewed_cycle`, transitioned `goal_packet.status`), which in turn made the
  reaper's sealed-round guard skip the task forever. Secondary: the fast-path
  called `compute_reviewer_verdict_task_update` with default
  `human_acceptance_for_passed=True`, so GP approvals incorrectly showed the
  "Awaiting human acceptance" chip (until/unless `continue_task` cleared it).
- **How**:
  - Wrap all three bare `continue_task(...)` calls in try/except that invokes
    `_mark_prompt_dispatch_stalled(...)` (session marked `NEEDS_INPUT`,
    `prompt_dispatch_stalled` risk report recorded) instead of letting the
    exception escape, matching the pattern already used by `request_task_review`
    and `continue_task`'s own tmux-delivery block.
  - In the `create_report` fast-path, detect GP reviews (same predicate already
    used for the packet status transition) and pass
    `human_acceptance_for_passed=False` + clear any pre-existing
    `human_acceptance_requested_at`, so GP verdicts never carry the
    human-acceptance flag.
  - Defensive clear of `human_acceptance_requested_at` in the GP handler's
    `already_applied` branch for any pre-fix stranded state with a stale flag.
  - New `_task_has_post_cycle_report` and `_sealed_cycle_verdict_state` helpers;
    `_auto_continue_stopped_task` now also fires for WORKER sessions in
    `status=REVIEW` when (a) the round is sealed (`current_round_has_verdict`),
    (b) `human_acceptance_requested_at is None`, (c) no next-cycle report
    exists, and (d) the sealing verdict state is `REVIEW_PASSED` or
    `REVIEW_FAILED` (NOT `review_needs_input`, which must wait for a human).
    On match it calls `continue_task` directly, which re-opens the work phase
    and dispatches the continue prompt — recovering pre-existing stranded
    tasks and any future strandings from cold-resume/session-flip races. The
    predicate explicitly excludes post-impl-approval parking (human_acceptance
    set) and `review_needs_input` parking so those still wait on human action.
    If `continue_task` raises during recovery (worker still STOPPED), the
    session is NOT forced to WORKING; downstream stall detection / the next
    monitor tick will retry.
  - Negative tests: `REVIEW+APPROVED+human_acceptance set` (post-impl parking)
    and `REVIEW+review_needs_input` (human-judgment parking) are NOT
    auto-continued. Positive tests for GP-approved, GP-rejected, and
    impl-review-failed sealed-verdict recovery.

### fix: terminal display — selection alignment, mobile text selection, and reliable scroll-to-bottom on tab switch

- **What**: three long-standing terminal display defects are fixed:
  1. **Desktop text selection drifted visually.** Dragging to select terminal
     text painted the highlight offset from the glyphs it covered, with the
     mismatch growing down the screen ("串行/错位"). The *copied* text was
     always correct — only the rendered highlight was wrong.
  2. **Mobile could not select terminal text.** Long-press/drag only scrolled;
     there was no way to select and copy text on touch devices.
  3. **Switching to a terminal tab sometimes showed history, not the bottom.**
     The newly-activated tab occasionally stuck mid-scrollback instead of at
     the latest prompt.
- **Why**:
  1. The terminal used the xterm.js v4 **WebGL renderer** (bundled by ttyd
     1.7.x). Its selection layer mis-renders the highlight rectangle (xterm.js
     issue #5198); there is no `.xterm-selection` DOM node in WebGL mode, so
     the usual CSS workaround does not apply. Confirmed via Playwright: the
     selection *model* is correct at devicePixelRatio 1/1.25/1.5/2, scrolled or
     at bottom — only the WebGL-painted highlight drifts.
  2. `.xterm-screen { pointer-events: none }` (added for mobile inertial
     scroll passthrough) plus `user-select: none` disabled all touch selection,
     and xterm.js v4 has no native touch text-selection.
  3. Tab activation issued a single fixed-delay `scroll-to-bottom` that no-ops
     when the term is not ready or content is still settling.
- **How**:
  - **#1** `rendererType` switched **`webgl` → `canvas`** in
    `ttyd_manager.py`. The 2D canvas renderer paints the selection on its own
    aligned `xterm-selection-layer`. Input latency is unaffected (the SAB fast
    path is renderer-independent); measured render throughput is comparable for
    terminal-style output. `.xterm-screen { pointer-events: none }` is now
    scoped to `@media (pointer: coarse)` so desktop mouse interaction stays on
    the screen element.
  - **#2** New mobile **"选择" (select-text) mode**: a MobileControls toggle
    posts `terminal-select-mode` to the active terminal iframe. While active,
    single-finger touches drive the xterm selection model directly from
    computed cells (xterm.js v4 ignores synthetic MouseEvents), so partial and
    multi-row selection match desktop; native inertial scroll is preserved when
    the mode is off. On lift the selection is copied to the clipboard (with a
    `document.execCommand` fallback) and MobileControls shows "已复制".
  - **#3** `scrollBottomWhenReady` now waits for any in-flight replay to
    settle, scrolls, then **verifies it reached the bottom and the
    scrollHeight is stable** across two ticks, retrying briefly otherwise. It
    remains scroll-only — no history replay is reintroduced (per the 2026-06-09
    prompt-first activation change).
- **Validation**: reproduced and verified each fix against an **isolated**
  second backend (`HOME`/`PORT`/`TTYD_BASE_PORT` overridden so the live
  workspace state was never touched) with Playwright: canvas selection
  highlight aligned + visible (drift 0), mobile touch selection + copy working,
  and 5/5 tab switches landing at the bottom (including while output was still
  streaming). Perf A/B (standalone ttyd, SwiftShader): canvas ≈ webgl for a
  4000-line burst (median 20 ms vs 19 ms). `black`/`isort`/`mypy` clean on
  touched files; frontend `lint` + `build` (vue-tsc) pass; backend
  `pytest` 571 passed (CI-style, E2E replay/latency excluded as in CI);
  `test_terminal_proxy.py` + `test_terminal_input_latency_guard.py` pass.
- **Files**: `backend/claude_hub/services/ttyd_manager.py`,
  `backend/claude_hub/api/terminal.py`,
  `frontend/src/components/TerminalView.vue`,
  `frontend/src/components/MobileControls.vue`,
  `frontend/src/types/index.ts`,
  `docs/working-logs/2026-07-24-terminal-selection-scroll.md`.
### feat: Codex local session picker — resume any local Codex conversation from the new-tab dialog

- **What**: when creating a new Codex terminal tab, the "Create New Terminal"
  dialog now shows a session picker that lists every local Codex session
  (grouped by working directory, most-recent-first). Selecting a session
  launches the tab with `codex resume <session-id>`, continuing that exact
  conversation; leaving "Start a fresh session" selected starts a new one.
- **Why**: users accumulate many Codex sessions across working directories on
  their machine and wanted to pick up a specific prior conversation from the
  Hub UI rather than retyping context. Remote-workspace session selection was
  explicitly deferred to a follow-up (out of scope).
- **How**:
  - New backend endpoint `GET /api/codex/sessions` (`api/codex.py`) returns
    sessions grouped by cwd, each with `session_id`, `cwd`, `start_time`
    (ISO), and a display `title`. Backed by `list_codex_sessions()` in
    `ttyd_manager.py`, which walks active `~/.codex/sessions/` and archived
    `~/.codex/archived_sessions/` rollouts (reusing `_codex_iter_rollouts`),
    dedupes by stable `session_id` (keeping the most recent rollout), and
    extracts the title from the first real user message via
    `_codex_session_title()`, skipping boilerplate
    `<environment_context>` / `<permissions instructions>` /
    `<recommended_plugins>` / `# AGENTS.md instructions` blocks.
  - `TerminalTabCreate` gained an optional `agent_session_id` field; the
    create-tab handler and `TTYDManager.create_tab` / `TTYDProcess` thread it
    through. A fresh tab created with an explicit session id sets
    `_has_explicit_session_id`, which makes `_should_recover()` return True so
    `_codex_launch_command` builds `codex resume <id> || codex` (solo flags on
    both branches). A generated uuid4 placeholder (for conversation pinning)
    does NOT trigger recovery, preserving fresh-tab behavior.
  - Frontend: new `CodexSessionSelector.vue` component fetches and renders the
    grouped list with a radio for "fresh session" vs. a specific session.
    Wired into `TabBar.vue`'s create-tab modal (shown only for local Codex
    tabs) and `TerminalTabCreate` / `TerminalTab` types gained
    `agent_session_id`.
- **ACs** (reviewer-checkable): endpoint returns grouped sessions with the
  four fields and correct sort order; title extraction skips boilerplate;
  duplicate session ids collapse to the newest rollout; an explicit session id
  produces `codex resume <id>` while a fresh tab stays `codex`; the picker only
  surfaces for local Codex tabs; solo flags apply to both resume and fallback.

### fix: Codex session restore across backend restarts — pin per-tab UUID instead of cwd-scoped `--last`

- **What**: codex (and to a lesser degree claude code) tabs would frequently
  restore the wrong conversation — or fail to find any — after a backend
  restart or machine reboot. Claude tabs already pinned a `--session-id` at
  launch, but codex tabs relied on `codex resume --last`, which is scoped to
  "most recent session in current cwd". Because every workspace agent tab
  shares the same working directory, concurrent restarts cross-wired tabs:
  tab A could resume tab B's conversation and vice versa.
- **Why**: codex has no `--session-id` launch flag (unlike Claude Code's
  `--session-id`/`--resume <uuid>` pair), so there was no way to hand an
  exact session UUID to a fresh codex process. The only resume handles are
  `codex resume <uuid>` (positional arg, requires that UUID to already exist
  in codex's session index) and `codex resume --last` (cwd-relative MRU,
  racy under concurrency).
- **How**:
  - `TTYDProcess.__init__` now generates a placeholder UUID for codex tabs at
    construction time (same shape as Claude) and persists it through state,
    so the tab has a stable session-id slot across restarts even before the
    real codex-assigned UUID is known.
  - New helper `_discover_codex_session_id(launch_epoch)` runs post-launch
    and reads `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (and
    `~/.codex/archived_sessions/rollout-*.jsonl` for older sessions),
    parsing the first-line `session_meta` JSON to find the rollout
    unambiguously started near the tab's launch time, then overwrites the
    placeholder with the real codex UUID. Prefers `payload.session_id`
    (stable across resumes, present since codex 0.143) and falls back to
    `payload.id` (filename match) for older rollouts. Discovered UUIDs are
    persisted to state so future restarts use the verified id.
  - Verification gate: `_codex_id_exists(sid)` checks whether a rollout
    file with a matching `session_id` exists in active or archived
    sessions — NOT `session_index.jsonl`, which empirically only records
    interactive/VSCode sessions (`source: "vscode"`) and never CLI-launched
    sessions (`source: "cli"`, which is what Claude Hub spawns). The
    initial revision (review-1) used the index and would have rejected
    every real CLI session id (0/73 workspace sessions were indexed); the
    rollout-based gate fixes this while still rejecting random placeholder
    UUIDs (they have no rollout), so unverified ids continue to fall back
    to `codex resume --last` with a `|| codex` (fresh) fallback — same
    behavior as before this fix when verification fails, never worse.
  - Three discovery paths cover every lifecycle: (a) startup backfill
    `_backfill_codex_session_ids` uses tmux `session_created` time to
    correlate long-running codex tabs with on-disk rollouts (same
    conservative thresholds as Claude backfill: 90s match window, 600s
    isolation, 5s mtime slack), (b) post-`start_all_tabs` bulk sweep
    `_discover_codex_sessions_after_start` for parallel-started tabs, (c)
    per-tab scheduled discovery after `create_tab` and
    `ensure_tab_tmux_session` (5s delay, 30s window).
  - `_codex_launch_command(recover=True)` now emits `codex resume <verified-uuid>`
    when the pinned id is in the session index, otherwise falls back to
    `codex resume --last` as before.
  - Defensive: `_claude_session_arg()` now generates a fresh UUID if a
    claude tab somehow has `agent_session_id=None` at launch, instead of
    emitting `--session-id None`.
  - Defensive: `update_tab()` resets `agent_session_id` when switching
    agent types (claude↔codex↔cursor/terminal) so an old uuid isn't
    accidentally reused against the wrong CLI.
  - Cursor (`agent --continue`) shares the same cwd-race bug but is
    explicitly OUT OF SCOPE per this task's Goal Packet (cursor resume
    semantics differ and need separate investigation).
- **Files**: `backend/claude_hub/services/ttyd_manager.py`,
  `backend/tests/test_ttyd_manager.py`, plus this changelog and working log.
- **Validation**:
  - 85/85 tests in `test_ttyd_manager.py` pass, including new tests
    covering: codex placeholder generation, cursor/terminal `None`
    preservation, verified-uuid resume in both solo/non-solo modes (launch
    and switch_env respawn), `session_meta` parsing (UTC-aware,
    `session_id`-over-`id` preference), bad-rollout handling, rollout-based
    id verification (active + archived + cwd filter + placeholder
    rejection; `_codex_iter_rollouts` walks both dirs), claude defensive
    uuid generation, agent-type-change reset, and full state round-trip.
  - Empirical sanity check against real codex home (513 unique session_ids
    across active/archived rollouts; 73 workspace-cwd rollouts):
    `_codex_id_exists` now verifies the newest workspace-cwd session (the
    review-1 index-based gate returned False for all 73 because CLI
    sessions are never added to `session_index.jsonl`); a random uuid4
    placeholder is still correctly rejected.
  - `black`, `isort --check`, `mypy claude_hub/services/ttyd_manager.py`
    all clean.
- **Risk**: low. Codex resume behavior is strictly a superset: rollout-
  verified ids use `resume <uuid>`, unverified/ambiguous/unknown fall back
  to the pre-fix `resume --last` path. Ambiguity in backfill (multiple
  rollouts in the match window) is handled conservatively — the tab keeps
  its placeholder and falls back to `--last`, rather than risking a cross-
  wire. Worst case is the same pre-fix behavior for a tab, never worse.

### perf: Prompt compaction and revision-resume briefing for long autonomous tasks

- **What**: three-legged optimization to reduce prompt size and combat context rot
  on long autonomous/complex tasks: (a) tightened static prompt scaffolding
  (assignment / review / continue blocks), (b) tiered reviewer report history
  (last 4 reports full, older reports summarized with verbose fields truncated
  to 240 chars and bulky structured fields replaced by counts), (c) a new
  revision-resume briefing used by hard recovery after the first iteration
  instead of replaying the full assignment prompt.
- **Why**: under autonomous+complex, long-running tasks accumulated unbounded
  reviewer history and replayed full assignment text on every hard recovery,
  leading to "context rot" late in iterations where the agent lost track of
  earlier decisions and issued contradictory instructions.
- **How**:
  - `_prompts.py`: removed the worked `/api/orders` compact-skeleton example,
    collapsed role primitives, model pinning, and observability rules onto
    tighter lines, kept all semantic invariants (subagent ledger, opus/sonnet
    pinning per primitive, P-JUDGE pre-flight, GP plan-gate exit criteria,
    adversarial defect hunt).
  - Reviewer bootstrap tightened from ~952 to ~487 tokens by collapsing
    repetitive "reviewer mindset" / "operating contract" / "exit rules" /
    "reporting style" sections into a single tighter block (the per-review
    prompt repeats format/exit-criteria details anyway, so the bootstrap only
    needs to set mindset and endpoint).
  - Fixed a double-serialization bug where the trigger report was rendered
    twice in review prompts (once as "Trigger report (full JSON)" and once
    inside "Task history JSON"). `_serialize_task_reports_for_review` now
    defaults to `include_trigger=False` and excludes the trigger from the
    history list, with a relabeled header making the split explicit ("prior
    reports" vs trigger block).
  - New helpers `_serialize_report_for_review` and
    `_serialize_task_reports_for_review` implement the tiered window:
    `_FULL_REPORT_WINDOW = 4`, `_SUMMARY_VERBOSE_FIELD_MAX = 240`,
    `_MAX_REPORT_HISTORY = 12`.
  - New `_build_revision_resume_prompt` produces a tight briefing (~380 tokens)
    with task metadata, compact GP (objective + top acceptance + out-of-scope),
    current changed files (dedup from worker reports, last 20), latest reviewer
    blocking feedback (truncated to 1500 chars), and 4-step resume
    instructions. `_build_hard_recovery_worker_prompt` now branches to this
    briefing on iteration ≥ 2 or review_cycle ≥ 2 for autonomous tasks.
  - Added a one-line orchestrator reminder on continue: "if your own context
    feels decayed, prefer a fresh sub-agent rather than reasoning in the main
    thread."
  - All existing enforcement/contract text for simple/reviewed/direct modes and
    non-claude runtimes (cursor/codex/terminal) preserved; tests updated only
    to match shortened wording where assertions were string-exact.
- **Validation**:
  - Static scaffold: `_orchestrator_contract_block` ~1230 → ~664 tokens;
    `_review_workflow_block` ~1338 → ~797 tokens; worker bootstrap ~491 → ~305;
    reviewer bootstrap ~952 → ~487; assignment for autonomous+complex 2538 → 1724
    tokens (~32% reduction); reviewer bootstrap+body for 1-prior case 3597 → 2554
    tokens (~29% reduction), measured apples-to-apples against main via a
    subprocess-isolated fixture (synthetic task, no lessons/REVIEW.md, trigger
    + one review_passed prior report).
  - Hard recovery on iter≥2: ~1.7k token full-replay path → ~380 token resume
    briefing.
  - Reviewer history bounded growth: past the 4-report full window each
    additional report adds ~80 tokens (truncated) instead of ~600–1200 tokens
    (full ledger + structured fields).
  - `pytest` (273 relevant tests) passes; `black`, `isort`, `mypy` clean.

### feat: Unified UI design system — Inter font, consistent buttons, refined typography

- **What**: comprehensive visual polish pass establishing a unified design
  system across the entire frontend. Buttons, inputs, toggles, and
  interactive controls now share consistent sizing, border-radius, focus
  rings, hover states, and typography; the Inter and JetBrains Mono web
  fonts replace the generic system font stack; light-theme accent color is
  now blue (matching dark theme) for cross-theme consistency.
- **Why**: button styles were defined independently in three places
  (TabBar `.btn` family, AgentWorkspaceView `.tool-button` family, and
  EnvPresetManager duplicate `.btn` family) with inconsistent padding,
  font-size, border-radius (4px vs 5px vs 7px), and hover behavior. Form
  inputs also varied between modals. The system font stack produced
  slightly different rendering on each OS; Inter provides a consistent,
  modern look.
- **How**:
  - Added Inter (400/500/600/700) and JetBrains Mono (400/500) from Google
    Fonts via `<link>` in `index.html`. Enabled OpenType features
    (`cv02`, `cv03`, `cv04`, `cv11`) and `-webkit-font-smoothing` for
    crisp rendering.
  - New global CSS primitives in `App.vue`: `.ch-btn`, `.ch-btn--primary`,
    `.ch-btn--danger`, `.ch-btn--ghost`, `.ch-btn--warning`, `.ch-btn--sm`,
    `.ch-btn--lg`, `.ch-btn--icon`, `.ch-btn--chip`, `.ch-segmented`,
    `.ch-segmented__btn`, `.ch-input`, `.ch-select`, `.ch-textarea`,
    `.ch-label`, `.ch-hint` — plus design tokens for font sizes
    (`--ch-font-size-{xs,sm,base,md,lg}`), font families
    (`--ch-font-sans`, `--ch-font-mono`), button shadows, and an
    `--ch-radius-xl` (16px) radius tier.
  - Updated radius scale from `5/7/10` to `6/8/12/16` (sm/md/lg/xl) for a
    softer, more modern look.
  - Replaced the light-theme neutral-gray accent (`#4a4a44`) with the
    same blue palette used in dark theme (`#60a5fa`/`#3b82f6`/`#2563eb`).
  - Standardized button heights: 32px default, 28px small, 40px large,
    44px for the login CTA.
  - All interactive elements now have consistent `:focus-visible` styles
    with a 3px accent ring.
  - Primary CTAs gain subtle hover elevation (`translateY(-1px)` +
    shadow).
  - Migrated all components (TabBar, AgentWorkspaceView, LoginView,
    LayoutSelector, NetworkAccessMenu, EnvPresetManager,
    AgentConfigFields, MobileControls, AgentStatusFloatingPanel,
    AgentAvatar, MarkdownContent) to use global `ch-btn` classes and
    updated CSS tokens; removed duplicate `.btn` definitions from TabBar
    and EnvPresetManager.
  - Font weights normalized: 500 for most UI text, 600 for headings/CTAs
    (reduced overuse of 700/800 weights that looked heavy).

### fix: Robust dispatch-chain recovery for goal packet review and continue prompts

- **What**: four chained fixes for the intermittent "agent fails to submit
  goal packet review request" and "goal packet approved but worker never
  resumes" bugs. Together they close three gaps where a tmux submit failure
  or stale terminal state could leave a task in WORKING/REVIEW state with an
  IDLE agent and no automatic recovery path.
- **Why**: three independent gaps combined to produce the reported symptoms:
  (1) the prompt-stall detector only checked for the initial-dispatch and
  review prefixes — a continue or hard-recovery prompt stuck in the input box
  was invisible to Enter-retry and never escalated; (2) `continue_task`
  didn't wrap `send_session_message` in a try/except, so a tmux submit
  verification RuntimeError propagated to the HTTP layer while state was
  already persisted as WORKING (leaving the worker idle with no prompt);
  (3) the auto-continue nudge logic returned `None` for the clean-idle case
  (no error/completion patterns in terminal output), so an agent sitting at
  a fresh input prompt after a failed delivery was never nudged. A fourth
  fix adds a reaper safety net for pending Goal Packet reviews whose
  reviewer-create step threw before setting `review_requested_at`.
- **How**:
  - Replaced `_expected_pending_prompt_prefix(session, task) -> str` with
    `_expected_pending_prompt_prefixes(...) -> list[str]` covering four
    prefixes: initial dispatch (`"New workspace task assigned."`), review
    (`"Review workspace task."`), continue (`"Continue workspace task from
    review."`), and hard-recovery (the ⚠️ prefix). The hard-recovery prefix
    is conditionally included based on `session.hard_recovery_task_id` and
    timestamp comparisons; both the monitor's stall detector and the
    reaper's `_reviewer_prompt_still_pending` iterate all prefixes.
  - Wrapped the `send_session_message` call in `continue_task` with
    try/except that calls `_mark_prompt_dispatch_stalled` on failure —
    matching the existing pattern in `_request_task_review`. The endpoint
    now returns 200 (state is consistent) instead of 400; the stall-marker
    report flags the session for nudge recovery.
  - Fixed `_mark_prompt_dispatch_stalled` to preserve the task's existing
    status for worker/orchestrator sessions (only reviewer stalls set
    status=REVIEW). Previously the function hardcoded status=REVIEW, which
    incorrectly re-opened a review phase when a worker's own initial or
    continue prompt failed to submit; that in turn triggered the
    ATTENTION+WORKING handler in `_refresh_session_statuses` which fired
    `_request_task_review` and completed the spurious transition.
  - Added a guard in `_refresh_session_statuses`: the ATTENTION+WORKING
    auto-review path now skips firing `_request_task_review` when the
    latest report already carries `risk_level=prompt_dispatch_stalled`
    (delivery failure on the worker's own prompt is not a request for
    human/reviewer attention — it's a transport failure the nudge loop
    recovers from).
  - Added a clean-idle nudge path in `_auto_continue_stopped_task`: when a
    non-reviewer worker is past the grace period, shows no error patterns,
    and no completion patterns, it now sends an inspect-state nudge
    (`AUTO_CONTINUE_IDLE_PROMPT_MESSAGE`) instead of returning `None`. To
    avoid false positives on agents legitimately reading/thinking, the
    clean-idle case uses a dedicated longer grace
    (`AUTO_CONTINUE_CLEAN_IDLE_GRACE_SECONDS=60`) vs the existing
    20-second grace for error/completion cases (`AUTO_CONTINUE_IDLE_GRACE_SECONDS`).
  - Extended the fallback reaper `_reap_stuck_reviews` to recognize a
    third wedged state: REVIEWED-mode task in WORKING with an approved-or-
    pending Goal Packet but no `review_requested_at` (reviewer creation
    threw between binding the task and dispatching the GP review prompt).
    `_review_dispatch_in_reaper_grace` now also treats
    `goal_packet.updated_at` as a grace anchor when the GP is in
    PENDING_REVIEW state; the trigger uses trigger_state=WORKING so the
    re-dispatched review prompt carries GP-approval instructions.
- **Tests**: `tests/test_workspaces.py` (119 pass); two existing tests
  updated to reflect corrected behavior
  (`test_continue_task_marks_working_before_send_verification_failure`,
  `test_monitor_surfaces_worker_prompt_stuck_in_input`); mypy clean.

### feat: Auto-refresh visible terminal when agent finishes a turn

- **What**: when an agent's runtime status transitions from `working` to
  `idle` (shell prompt visible) or `attention` (agent waiting for input),
  the currently-displayed terminal automatically triggers a history refresh +
  scroll-to-bottom. This avoids display misalignment caused by output content
  changes during a turn.
- **Why**: after a round of conversation processing, the terminal output can
  end up in a visually misaligned state (e.g. the prompt line not at the
  bottom). Manually hitting the refresh button worked but was easy to forget;
  auto-refreshing on the working → idle/attention edge keeps the view in sync.
- **How**:
  - Added a `currentAgentStatus` computed in `TerminalView.vue` that resolves
    the `agentStatuses` store entry for `props.tabId` (the only terminal this
    component displays).
  - Added a `lastAgentStatus` ref that tracks the previously-seen status; it is
    reset to `null` whenever `props.tabId` changes so cross-tab status
    comparisons never fire a spurious refresh.
  - Added a `watch(currentAgentStatus, …)` that detects the
    `working` → `idle`/`attention` edge and calls the component-local
    `postTerminalHistoryRefresh(props.tabId, { reason: 'auto-round-complete',
    scrollToBottom: true })`. The global
    `window.__claudeHub.refreshTerminalHistory` is intentionally **not** used
    to avoid split-pane targeting issues.
  - Only the displayed terminal (`props.tabId`) is refreshed; hidden/cached
    iframes and other panes are untouched.
- **Backend accuracy**: the auto-refresh depends on the `working` →
  `idle`/`attention` transition, so the backend `_classify_agent_status`
  (`ttyd_manager.py`) was made more responsive:
  - A bare shell prompt (`❯`, `$`, `#`, …) on the last line and the
    Claude/Codex idle hints (`? for shortcuts`, `/ for commands`) now take
    priority over working markers (`esc to interrupt`, the spinner line, …)
    in the scrollback above. Previously an agent that had finished and
    returned to a prompt but still showed `esc to interrupt` a few lines up
    lingered in `working` for up to 3 minutes.
  - `_WORKING_FRAME_STALE_SECONDS` was reduced from 180.0 to 45.0, so a
    frozen working frame (an agent that stopped without a clean shell prompt)
    flips to `attention` in ~45s instead of ~3min. 45s is still well above
    the ~1s spinner tick and 5s frontend poll, so slow tool calls that
    briefly stop repainting are not false-positived.
- **Validation**: `pnpm lint` and `pnpm build` (vue-tsc + vite) both pass
  with no errors. Backend: `black`/`isort`/`mypy` clean on touched files;
  `pytest tests/test_ttyd_manager.py` — 74 passed (4 new tests for the
  reorder and staleness window).

### feat: Hard context recovery for agents stuck on persistent API errors

- **What**: when a Claude agent (worker or reviewer) gets stuck on a
  repeated API error (4xx/5xx, overloaded, etc.) and soft "please continue"
  prompts fail to revive it after 3 attempts, the monitor now escalates to
  **hard recovery**: interrupt the agent (Escape+Ctrl-C), send `/clear` to
  wipe the corrupted context window, wait for settle, then re-inject the
  task/review prompt within the **same conversation** (session_id preserved).
  This avoids losing the entire conversation when transient API errors leave
  the agent wedged on an error screen.
- **Why**: previously, the monitor would only send soft text prompts (up to 10
  attempts) and then mark the task NEEDS_INPUT for manual intervention. When
  the error was a transient API issue that left the Claude CLI stuck showing
  an error dialog, no amount of "please continue" prompts could get the agent
  back to a usable state because the dialog blocked input; only `/clear`
  resets the conversation to a working prompt.
- **How**:
  - Added `agent_session_id: Optional[str]` to `TerminalTab` schema; surfaced
    from `TTYDProcess.to_schema()` so the CLI's `--session-id` UUID is logged
    during recovery for diagnostics.
  - Added `hard_recovery_task_id`, `hard_recovery_attempts`,
    `last_hard_recovery_at` fields to `ManagedSession`; counter resets on new
    task dispatch, `continue_task`, and review request.
  - New constants: `AUTO_CONTINUE_SOFT_ATTEMPTS_BEFORE_HARD_RECOVERY=3`,
    `AUTO_CONTINUE_MAX_HARD_RECOVERIES=2`, `INTERRUPT_SETTLE_SECONDS=1.0`,
    `CLEAR_CONTEXT_SETTLE_SECONDS=1.5`.
  - Extended `_auto_continue_stopped_task` to also fire for REVIEW-phase
    reviewers (previously WORKING-only) with per-phase ownership guards and a
    reviewer-specific continue message.
  - Added `_perform_hard_recovery()`: logs warning with `agent_session_id`,
    calls `_interrupt_session`, waits 1s, sends `/clear`, waits 1.5s, sends
    role-specific recovery prompt (worker gets task description + goal packet
    + curl endpoint; reviewer gets task reports JSON + trigger report + goal
    packet).
  - Hard recovery is **Claude-only** (`/clear` is a Claude CLI slash command);
    Codex/Cursor continue on the existing soft-prompt path.
  - After 2 hard recoveries exhaust without progress: workers → REVIEW +
    NEEDS_INPUT (manual intervention); reviewers → `_release_stale_reviewer_for_task()`
    so the existing reviewer reaper re-dispatches with a fresh session.
  - Added `AUTO_CONTINUE_REVIEWER_MESSAGE`, `HARD_RECOVERY_WORKER_MESSAGE`,
    `HARD_RECOVERY_REVIEWER_MESSAGE` constants; added `_build_hard_recovery_worker_prompt()`,
    `_build_hard_recovery_reviewer_prompt()`, `_agent_session_id_for_session()`,
    and `_latest_report_for_task()` helper methods in `_prompts.py` and
    `_monitor.py`.
  - Reset hard recovery fields in `_release_task_session`,
    `_release_reviewer_session`, `_cleanup_reviewer_for_terminal_task`,
    `_assign_current_task`, `_dispatch_task_to_session`, `continue_task`, and
    `_request_task_review` to prevent stale counters across task lifecycles.
- **Validation**: `black`/`isort`/`mypy` clean on all touched production
  files; 151 unit tests pass (including 13 new tests in
  `tests/test_hard_recovery.py` covering schema fields, constants,
  normalization defaults, escalation thresholds, agent-type gating, role
  detection, and `_latest_report_for_task`).
- **Doc**: `docs/working-logs/2026-07-11-agent-error-hard-recovery.md`
  covers module design, key issues/pitfalls, and test inventory.

### fix: Codex restart now keeps solo mode when resuming a prior session

- **What**: when a Codex agent that was launched in **solo mode** restarted —
  either via the **Switch Env** ⚙ hot-swap or via reboot recovery — the relaunch
  command was `codex resume --last || codex --ask-for-approval never --sandbox
  danger-full-access`. The solo flags rode on the *fresh fallback* only. Because
  `codex resume --last` normally **succeeds**, the resumed session relaunched as
  plain `codex`, silently dropping solo mode (`--ask-for-approval never --sandbox
  danger-full-access`).
- **Why**: `codex resume` accepts `-a/--ask-for-approval` and `-s/--sandbox`
  (verified against codex-cli 0.144.1), but the resume branch never passed them.
  The `||` fallback masked the defect in tests that only asserted the flags
  appeared *somewhere* in the command string.
- **How**: extracted a single `TTYDProcess._codex_launch_command(recover)` helper
  used by both `_agent_start_command()` (reboot recovery) and `switch_env()`
  (hot-swap). It appends the solo flags to **both** the `codex resume --last`
  branch and the fresh fallback when `solo_mode` is set, and to neither when it
  is not. Non-solo behavior is unchanged.
- **Validation**: `black`/`isort`/`mypy` clean on touched files;
  `tests/test_ttyd_manager.py` **70 passed**. Strengthened the codex recovery and
  switch-env solo tests to assert the solo flags appear on the *resume branch*
  specifically (`codex resume --last --ask-for-approval never --sandbox
  danger-full-access`), and that non-solo resume carries no solo flags.

### fix: "Clear context" now applies to the reviewer, not just the worker

- **What**: when a task has the **"Clear context"** checkbox ticked
  (`task.clear_context`), the independent reviewer session now receives `/clear`
  before its review prompt — matching the worker/orchestrator behavior. Before
  this fix the flag was honored only on the worker dispatch path, so the reviewer
  ran with stale context.
- **Why**: the reviewer's `/clear` decision keyed solely off its own prior-review
  history (`previous_review_task_id`), never reading the user's per-task
  `clear_context` choice. A fresh reviewer (no prior review) or a same-task
  re-review therefore skipped `/clear` even when the user explicitly asked to
  clear context.
- **How**: in `_request_task_review` (`workspace_manager/_reports.py`) the clear
  decision now ORs the user flag with the existing unrelated-prior-review
  heuristic: `should_clear_context = bool(task.clear_context) or (previous_review_task_id is not None and previous_review_task_id != task.id)`.
  Default behavior is preserved when the flag is unset (fresh reviewer pays no
  round-trip; same-task re-review keeps context; unrelated prior review still
  clears).
- **Validation**: `black`/`isort`/`mypy` clean on touched files; full
  `tests/test_workspaces.py` **119 passed** including a new regression test
  (`test_reviewer_honors_task_clear_context_flag`) asserting a fresh reviewer
  receives `/clear` before the review prompt when `clear_context` is set.

### fix: env restart (switch-env) solo mode now works for Claude; add Codex env restart with solo mode

- **What**: the **Switch Env** ⚙ hot-swap action previously had two issues.
  (1) It was Claude-only — Codex tabs had no way to change env / toggle solo
  mode without deleting and recreating the tab. (2) The relaunch command for
  Claude already correctly propagated solo mode (`IS_SANDBOX=1 claude
  --dangerously-skip-permissions`), verified by existing tests.
- **Backend**: `TTYDProcess.switch_env()` now supports both `AgentType.CLAUDE`
  and `AgentType.CODEX`. For Codex it respawns with `codex resume --last ||
  <fresh>` (solo: `codex --ask-for-approval never --sandbox danger-full-access`),
  matching the existing `_solo_command()` / `_agent_start_command()` launch
  semantics. Unsupported agent types (cursor, terminal) and remote tabs still
  raise 400 as before. Docstrings updated on `switch_env`, the manager-level
  method, the API endpoint, and `SwitchEnvRequest`.
- **Frontend**: the gear / Switch Env button now appears for both Claude and
  Codex tabs in TabBar and AgentWorkspaceView (`canSwitchAgentEnv` updated).
  The solo-mode help text in the modal is agent-type-aware: Codex shows
  `--ask-for-approval never --sandbox danger-full-access`; Claude shows
  `IS_SANDBOX=1` / `--dangerously-skip-permissions`.
- **Tests**: added `_make_codex_process` helper and four new test cases
  covering non-solo respawn, solo respawn, solo toggle, and rollback on tmux
  failure for Codex. Updated the unsupported-agent-type test to assert on
  cursor/terminal instead of codex. All 70 ttyd_manager tests pass.

### feat: resident behavior — run-now, next-run visibility, managed periodic tasks

- **What**: the per-workspace resident agent gains three interaction
  improvements. (1) A **"Run now"** control (status card + agent-manager list,
  backed by `POST /api/workspaces/{id}/resident/run`) forces the resident to
  run on the next monitor tick. (2) A **next-run countdown** in the status card
  ("next run in 4m" / "due now" / "queued" / "paused") makes the otherwise
  opaque trigger visible. (3) **Structured recurring tasks** — an add / edit /
  remove / enable list (`resident_agent_periodic_tasks`) that replaces burying
  recurring work in the free-text directive; enabled entries render as an
  explicit every-cycle checklist in both resident prompts. Directive edits now
  spell out their timing: **Save** applies on the next scheduled cycle, **Save &
  run now** applies immediately.
- **Why**: the resident was meant to run periodic tasks *or* act on an updated
  guiding directive, but editing the directive gave no signal about what changed
  or when it would take effect, there was no way to see or force the next run,
  and recurring tasks could not be managed.
- **How**: new `ResidentPeriodicTask` model and workspace fields
  (`resident_agent_periodic_tasks`, `resident_agent_run_requested_at`,
  `resident_agent_next_run_at`). `request_resident_run` stamps a one-off flag
  that `_resident_agent_due` honors *before* the paused early-return (respects
  Enable, bypasses Pause); `_run_resident_agent` consumes it on fire but the
  WORKING-skip defers a busy resident without dropping the request.
  `_resident_next_run_at` mirrors the overdue-backstop arm for the advisory
  countdown (never gates execution). Periodic tasks are trimmed / blank-dropped
  / order-preserving and render `""` when none are enabled, keeping legacy
  prompts byte-identical.
- **Validation**: `black`/`isort`/`mypy` clean; frontend `eslint` + `vue-tsc` +
  `vite build` clean; backend tests via the CI path
  (`--ignore=tests/test_terminal_replay.py
  --ignore=tests/test_terminal_input_latency_perf.py`) **536 passed**
  (+19 new: 14 resident unit, 5 API). See
  `docs/working-logs/2026-07-01-resident-behavior-optimization.md`.

### fix: restore green CI on main (misplaced browser perf test + non-blocking E2E)

- **What**: the backend CI job now also ignores
  `tests/test_terminal_input_latency_perf.py`, and the `terminal-e2e`
  (Playwright) job is marked `continue-on-error: true` so it reports signal
  without gating merges to `main`.
- **Why**: `test_terminal_input_latency_perf.py` is a Playwright browser E2E
  harness (it drives xterm.js via chromium and needs tmux/ttyd, none of which
  the backend job installs). Worse, merely *collecting* it left a running
  asyncio event loop, which broke every later `asyncio.run()` test in the same
  process — so the backend `Run pytest` step stayed red even after the
  black/isort/mypy fixes landed in `5d34b11`. Separately, the browser-driven
  `terminal-e2e` job has never been reliably green on shared GitHub runners
  (ttyd/tmux startup blows past Playwright deadlines under CPU contention), so
  it was blocking otherwise-passing merges.
- **How**: add `--ignore=tests/test_terminal_input_latency_perf.py` to the
  backend pytest invocation (alongside the existing `test_terminal_replay.py`
  ignore) and set `continue-on-error: true` on the `terminal-e2e` job, mirroring
  the informational `security-audit` job. Both browser suites still run for
  signal in `terminal-e2e`; neither can mask or gate the backend job.
- **Validation**: `black --check .`, `isort --check .`, `mypy .`, and
  `uv run pytest --ignore=tests/test_terminal_replay.py
  --ignore=tests/test_terminal_input_latency_perf.py` all green locally; backend
  job goes red → green in CI.

### feat: tag agent-created workspace tasks + rename resident mode to "Autopilot"

- **What**: workspace tasks now carry an **origin** (`human` | `resident`), and
  the task board renders an **"Agent" tag** on any task the resident self-driven
  agent created — so human-created and resident-proposed/orchestrated tasks are
  visually distinguishable at a glance. The resident's self-driving mode is now
  labelled **"Autopilot"** in the UI (previously "Master") with its own dedicated
  badge style (violet "agent" theme) instead of reusing the muted paused-badge
  styling, so the resident row reads clearly.
- **Why**: with Autopilot the resident now creates and dispatches its own
  tasks; previously these were indistinguishable from human-entered tasks on the
  board, making it hard to see what the agent was driving on its own. "Master"
  was an opaque label and collided conceptually with the existing `orchestrator`
  worker role and `autonomous` task mode; "Autopilot" reads as plain self-driving.
- **How**:
  - Backend: new `WorkspaceTaskOrigin` enum (`backend/claude_hub/models/schemas.py`)
    with an `origin` field on `WorkspaceTaskCreate` and `WorkspaceTask`
    (defaults to `human`). `_create_task` persists the payload origin; legacy
    normalization (`_normalize_task_item`) backfills missing/invalid values to
    `human`. The resident prompt (`_workspaces.py`, both autopilot and read-only
    paths) now always includes `"origin":"resident"` in its `POST /tasks`
    payloads. The value is **self-declared by the resident** — the `POST /tasks`
    endpoint only sees the authenticated user, not the calling session — so it is
    a display hint, not a backend-enforced ownership guarantee.
  - Frontend (`frontend/src/components/AgentWorkspaceView.vue`,
    `frontend/src/types/index.ts`): `WorkspaceTaskOrigin` type + `origin?` field;
    an `Agent` origin badge on task cards (shown when `origin === 'resident'`);
    a dedicated badge style for the resident Autopilot badge. The "Autopilot"
    rename is **UI-copy only** — the backend field (`resident_agent_master_mode`),
    API, and prompt-internal naming are unchanged.

### refactor: resident Master mode is now an autonomous orchestrator (not a coder)

- **What**: redefined the resident agent's **Master mode** from "self-iterate on
  its own git worktree and commit code" into an **autonomous orchestrator /
  product-owner**. With Master mode ON, each bounded cycle the resident now:
  reads the board (`GET /api/workspaces/{ws}/board`), decides what the workspace
  needs next from recent task outcomes + the user directive, **creates tasks**
  (`POST /tasks`, default `reviewed` mode, capped at 3/cycle),
  **dispatches** each to an **existing orchestrator worker session** (`POST
  /tasks/{id}/start` with an explicit `target_session_id`), lets them go through
  review, and **accepts the results itself** once review has passed (`PATCH
  /tasks/{id}` → `{"status":"done"}`) or sends them back with feedback (`POST
  /tasks/{id}/continue`). It posts a heartbeat report each cycle. Master mode OFF
  is unchanged (read-only: propose TODO tasks + curate lessons, no reports).
- **Why**: the user wanted Master mode to let the resident drive the workspace's
  requirements end-to-end — create work, get it executed and reviewed, and
  validate it — rather than write code on a side branch. The orchestrator framing
  keeps the resident out of the editor and reuses the existing task/worker/review
  machinery.
- **How**: this is a **prompt-level** change only — no new endpoints, schema
  fields, or service methods. `_build_resident_master_prompt`
  (`backend/claude_hub/services/workspace_manager/_workspaces.py`) was rewritten
  to the orchestrator prompt; the now-unused `_resident_worktree_slug` helper was
  deleted. Tasks use the **default `reviewed` mode**, so a reviewer agent vets the
  work before it returns to the resident for final acceptance — the backend may
  reuse an idle reviewer or auto-spawn a short-lived ephemeral one, which is
  allowed. The resident's acceptance step keys off the post-review
  `human_acceptance_requested_at` signal (set after a reviewer PASS or an
  auto-skipped low-risk review), so it never accepts while the reviewer is still
  working. The resident's own hard limit is **never provisioning or deleting
  orchestrator worker sessions**: it only dispatches to pre-existing
  `orchestrator` sessions with an explicit `target_session_id` (so `start_task`
  never auto-creates a default worker), and degrades to proposal-only (TODO
  tasks, no dispatch) when no worker agent exists. Toggling Master mode still does
  **not** respawn the resident session (the prompt is recomputed each cycle). The
  frontend Master-mode hint copy was updated to describe the orchestrator
  behavior.

### fix: allow Done when a task reaches final acceptance with a stale Goal Packet

- **What**: a workspace task that has reached final acceptance (status
  `review` with a final-acceptance signal — `human_acceptance_requested_at`
  set, `review_skipped_at` set, a final `review_passed` verdict, or a reported
  `completed`) can now be marked **Done**, even when its `goal_packet.status`
  is still `pending_review`/`rejected`. This unblocks autonomous tasks (e.g.
  the long-running `workspace常驻agent` task) that were stuck in `review` with
  no actionable Done control.
- **Why**: the Done button is gated frontend-side by
  `awaitingHumanAcceptance()` in `AgentWorkspaceView.vue`. Its first check
  short-circuited to `false` whenever `goal_packet.status` was
  `pending_review`/`rejected` — *before* evaluating any final-acceptance
  signal. That short-circuit is a **pre-implementation** plan-approval gate
  (commit `00aecc6`), but autonomous tasks never transition their packet to
  `approved`, so a stale `pending_review` permanently hid Done even after the
  work was complete and review-passed. The backend already permits the
  `review → done` transition; the blocker was purely the frontend gate.
- **How**:
  - Extracted the gate into a pure, store-free module
    `frontend/src/utils/taskAcceptance.ts`
    (`hasReportedCompletion`, `hasFinalAcceptanceSignal`,
    `awaitingHumanAcceptance`, `hasBlockingReviewResult`, `canMarkDoneTask`),
    each taking the task plus its resolved latest report / latest review
    report.
  - The Goal Packet `pending_review`/`rejected` gate is now applied **only
    when there is no final-acceptance signal yet**, so the pre-implementation
    plan-approval gate is preserved (during plan approval the verdict path
    sets `human_acceptance_for_passed=False`, so no final signal exists) while
    a post-review stale packet no longer strands the task. A blocking
    `review_failed`/`review_needs_input` verdict still suppresses Done.
  - `AgentWorkspaceView.vue` now delegates to the util via thin store-backed
    wrappers; call-site signatures are unchanged.
  - Tests: `frontend/tests/taskAcceptance.test.mjs` (node:test) covers stale
    `pending_review` + `review_passed` → Done shown; `pending_review`/`rejected`
    + no signal → Done hidden (plan gate preserved); `review_failed` /
    `review_needs_input` → Done suppressed; happy `completed` path; and the
    `ready_for_review`/non-`review`-status exclusions. `pnpm lint` + `pnpm
    build` clean.

### fix: new codex agent no longer queues its bootstrap prompt line-by-line

- **What**: creating a new codex (GPT-5.5) workspace agent no longer piles up the
  initial bootstrap prompt as one "Queued follow-up input" per line, leaving the
  agent stuck re-feeding its own startup message instead of executing. The
  multi-line prompt now lands as a single composer entry and the agent begins
  work normally.
- **Why**: `_send_tmux_message`
  (`services/workspace_manager/_tmux_queries.py`) delivered the prompt with
  `tmux paste-buffer` and no flags. Plain `paste-buffer` (a) replaces every LF
  with CR and (b) emits no bracketed-paste control codes. The codex TUI runs
  with bracketed-paste mode enabled, so it read each bare CR as Enter and
  submitted every prompt line on its own, stacking "Queued follow-up inputs".
  Claude/Cursor masked the same byte stream by collapsing the CR burst into a
  `[Pasted Content N chars]` placeholder; codex does not. This is distinct from
  the whole-message auto-continue re-feed loop fixed earlier (commit `1553f9b`).
- **How**:
  - Paste with `paste-buffer -p -r`: `-p` wraps the buffer in bracketed-paste
    markers (`ESC[200~ … ESC[201~`) and `-r` disables tmux's default LF→CR
    replacement so newlines stay newlines. The pairing is required — `-p` alone
    still converts LF→CR; `-r` alone omits the markers a non-bracketed reader
    needs. The existing single-Enter submit + pending-input verify/retry path is
    unchanged.
  - Verified by a tmux byte-stream repro (before: `…H2O\rSession…\r…`; after:
    `ESC[200~…H2O\nSession…\nESC[201~`).
  - Tests: `backend/tests/test_workspaces.py` adds
    `test_send_tmux_message_pastes_with_bracketed_paste_flags`, asserting
    `paste-buffer` is invoked with both `-p` and `-r`, targets the pane, and runs
    after `load-buffer`.

### fix: resolve task markdown artifacts produced inside git worktrees

- **What**: clicking a task report's markdown artifact link (e.g.
  `docs/working-logs/2026-06-26-adhd-skill-analysis.md`) no longer shows
  "Artifact not found" in the Markdown Preview when the file was produced inside
  the agent's git worktree. Such worktree-only artifacts now preview correctly
  and also appear in the workspace's "Markdown Outputs" list.
- **Why**: agents do their work in isolated git worktrees (sibling dirs created
  per the mandatory workflow), so a report's markdown artifact frequently lives
  only in the worktree — not under the main workspace path. The preview
  resolver (`_markdown_allowed_roots` in
  `services/workspace_manager/_artifacts.py`) only allowed the workspace path and
  the session `workspace_path` (which is also the main path) as roots, so the
  relative ref resolved to a nonexistent file → `KeyError` → 404. The same gap
  silently dropped those artifacts from the markdown-documents list.
- **How**:
  - `_markdown_allowed_roots` now also includes the workspace's git worktree
    directories, enumerated via `git worktree list --porcelain` run from the
    workspace path (new `_git_worktree_roots` / `_read_git_worktree_roots`
    helpers). The existing `_ensure_path_under_roots` containment check, the
    markdown-only suffix gate, and the `_path_looks_like_real_file` guard are all
    preserved, so path-escape safety is unchanged.
  - Worktree enumeration is cached per workspace for a short TTL
    (`WORKTREE_ROOT_CACHE_TTL_SECONDS`, bounded by `WORKTREE_LIST_TIMEOUT_SECONDS`)
    so building the board — which resolves every report ref — does not spawn one
    `git` subprocess per ref. When git is unavailable or the path is not a repo,
    enumeration returns empty and behavior is unchanged (graceful degradation).
  - Fix is resolution-side only: agent reporting, the report schema, session
    bookkeeping, and the frontend are untouched, so historical reports become
    previewable too.
  - Tests: `backend/tests/test_workspaces.py` adds
    `test_preview_markdown_artifact_resolves_from_git_worktree`, which creates a
    real git worktree, reports a markdown artifact that exists only there, and
    asserts the preview returns 200, the doc appears in `markdown_documents`, and
    an out-of-all-roots markdown path still 404s.

### fix: stop auto-continue from spamming a busy codex agent

- **What**: a codex (GPT-5.5) agent that is actively working is no longer
  misclassified as idle and re-prompted, which previously caused codex to pile
  up "Queued follow-up inputs" — the "codex keeps infinitely sending tasks"
  symptom.
- **Why**: both the runtime-status classifier
  (`ttyd_manager._classify_agent_status`) and the auto-continue busy-check
  (`workspace_state_policy.auto_continue_output_looks_busy`) only inspected the
  bottom ~10–12 lines of the captured pane. Codex renders its working indicator
  (`⠞ Working  4.03k tokens` or `• Working (3s • esc to interrupt)`) **above** a
  tall persistent bottom chrome — the `›`/`❯` composer, a "Queued follow-up
  inputs" panel that grows per queued item, and a model footer — so a busy codex
  frame fell outside both scan windows and read as IDLE. The 5s monitor loop
  then auto-prompted the still-WORKING task, and codex queued each prompt.
- **How**:
  - New leaf module `backend/claude_hub/services/agent_status_markers.py` is the
    single authoritative home for the codex working-marker set
    (`CODEX_WORKING_STATUS_RE` + `codex_output_is_working`), covering both the
    braille-spinner/token format and the legacy bullet/`esc to interrupt`
    format. It scans a wider, bounded window (60 lines) so the indicator above
    the chrome is seen. Keeping one explicit marker set per agent type (rather
    than loosening the shared Claude/Cursor regexes) follows the cursor-agent
    lesson.
  - `_classify_agent_status` calls the codex detector (keyed on
    `agent_type == CODEX`) after the ATTENTION check and routes through the
    existing `working_or_stale()` guard, so a frozen codex frame past
    `_WORKING_FRAME_STALE_SECONDS` still surfaces as ATTENTION.
  - `auto_continue_output_looks_busy` calls the same detector first, so the
    classifier and the busy-check agree by construction.
  - Tests: `backend/tests/test_ttyd_manager.py` and
    `tests/test_workspace_state_policy.py` add codex working/idle/frozen frames
    (modeled on real `backend.log` captures) with a tall queued-inputs chrome
    that defeats the old bottom-N window; the non-codex agent type is asserted
    unaffected.

### feat: hot-switch env/model on a live Claude tab (resume conversation)

- **What**: a new per-Claude-tab **Switch Env** action opens a dialog where you
  can pick a different env preset (or edit KEY=VALUE pairs directly) and toggle
  solo mode. Confirming kills the running `claude` process inside the tab's tmux
  pane and relaunches it with `claude --resume <session-id>` against the new
  env/model/base-url, preserving the conversation history. Pane scrollback and
  the WebSocket connection survive because we use `tmux respawn-pane -k`
  instead of restarting ttyd.
- **Why**: previously the only way to change model or API endpoint was to open
  a brand-new tab and lose context; ad-hoc edits via `PUT /api/tabs/{id}`
  restarted ttyd but kept the old tmux session running, so the live agent never
  actually saw the new env.
- **How**:
  - Backend (`backend/claude_hub/services/ttyd_manager.py`): new
    `TTYDProcess.switch_env()` validates local+Claude+live-session preconditions,
    rewrites `<tabid>.sh` and `<tabid>.settings.json`, builds the resume command
    (solo: `IS_SANDBOX=1 claude --dangerously-skip-permissions --settings ...
    --model ... --resume <sid> || <fresh-pinned>`; non-solo: `claude --settings
    ... --model ... --resume <sid> || <fresh-pinned>`, both wrapped with
    `; exec $SHELL` so the pane stays alive on error), then runs
    `tmux respawn-pane -k -t <session> -- $SHELL -lc <wrapped>`. New
    `TTYDManager.switch_env()` persists state and invalidates the status cache.
  - API (`backend/claude_hub/api/tabs.py`, `models/schemas.py`): new
    `SwitchEnvRequest { env, solo_mode? }` and
    `POST /api/tabs/{tab_id}/switch-env` returning the updated tab; 404 for
    missing tabs, 400 for non-Claude / remote / stopped tabs.
  - Frontend (`frontend/src/components/TabBar.vue`, `stores/terminalStore.ts`,
    `types/index.ts`): hover-revealed ⚙ button on each Claude tab opens a modal
    with warning text, env preset dropdown, Manage Presets, KEY=VALUE textarea
    pre-filled from the current tab's env, solo-mode checkbox defaulted to the
    tab's current setting, and a Restart Agent button that calls the endpoint
    and refreshes.
- Out of scope: server-side preset storage, Codex/Cursor switching, cwd/remote
  profile changes, graceful drain of in-flight work (the dialog warns and the
  running process is killed).
- Agent Workspace support: the same ⚙ Switch Env action is available on Claude
  workers/reviewers/orchestrators in both the in-board agent status cards and
  the Manage Agents modal (hidden for non-Claude agents and remote targets),
  wired through to the same backend endpoint via the underlying `tab_id`.
  Workspace mode now also renders its own toast stack (combining workspace and
  terminal-store notifications) so success/error feedback shows up there.
- Toast backgrounds (warning/success/info) are now opaque instead of
  semi-transparent, fixing the visual overlap where badges/buttons behind the
  toast showed through.

### feat: per-workspace resident self-driven agent + delete-workspace endpoint

- **What**: workspaces can now opt into a **resident self-driven agent** — a
  standing Claude session that wakes on a configurable interval to maintain the
  workspace. Each cycle it performs the user's recurring-task directive, curates
  the workspace lesson catalog (create/merge new lessons, archive stale ones),
  and **proposes** new tasks in `TODO` for the user to approve. It never
  auto-starts work, spawns workers, merges branches, or takes destructive
  actions. A new `DELETE /api/workspaces/{workspace_id}` endpoint (204) also
  fully removes a workspace and all of its state.
- **Why**: long-lived workspaces accumulate maintenance work (lesson hygiene,
  recurring checks, surfacing follow-up tasks) that no one explicitly dispatches;
  a resident agent keeps that work moving without taking risky autonomous action.
  Workspaces previously had no delete path at all.
- **How**: new `WorkspaceSessionRole.RESIDENT = "resident"` role (excluded from
  task dispatch and review). `Workspace` / `WorkspaceCreate` / `WorkspaceUpdate`
  gain `resident_agent_enabled`, `resident_agent_interval_minutes`, and
  `resident_agent_directive` (plus server-managed `resident_agent_session_id` /
  `resident_agent_last_run_at`). The background monitor calls a new
  `_tick_resident_agents()` each loop; due workspaces ensure a resident session,
  receive `build_resident_agent_prompt(...)`, and stamp the last-run time.
  `delete_workspace(...)` tears down sessions + ttyd tabs unconditionally and
  removes the on-disk state dir. See
  `docs/working-logs/2026-06-25-workspace-resident-agent.md`.
- **UI**: the resident config lives in a dedicated **Resident Agent** popup
  (opened from a summary row on the Create/Edit Workspace modal) that mirrors the
  Add-Agent form — Title, Agent Type / YOLO / Env Preset (shared
  `AgentConfigFields`), Run On (Local/Remote), Remote Server, Working Directory
  with a reused directory browser, and Auto reconnect. Every field stays visible
  at all times; the block is disabled/grayed via a `<fieldset disabled>` until
  **Enable** is checked, so the resident config reads like the normal agent
  launcher but its role is fixed to `resident` and it never dispatches normal
  tasks. `Workspace` / `WorkspaceCreate` / `WorkspaceUpdate` gain
  `resident_agent_title` / `resident_agent_target` /
  `resident_agent_remote_profile_id` / `resident_agent_cwd` /
  `resident_agent_remote_reconnect`; these placement fields flow into the
  resident `EnsureWorkspaceAgentRequest` and a change to any of them invalidates
  the live resident session so the next monitor tick respawns it with the new
  placement. The popup is a **fixed-height** flex column (pinned title + Enable
  toggle, scrolling config body, pinned Done footer) so it keeps a stable size
  whether or not Enable is checked and never exceeds the viewport.
- **Master mode**: a new optional `resident_agent_master_mode` toggle (default
  off) lets the resident do real **self-iteration** instead of read-only upkeep.
  When on, `build_resident_agent_prompt(...)` (now threaded with the resident's
  own `session_id`) emits a different prompt: the resident **self-provisions its
  own git worktree** on a `resident/<slug>` branch (idempotent — reuse the dir,
  re-attach an orphaned branch, or create fresh), does **one bounded enrichment
  iteration per wake**, and commits only on that branch. It still **NEVER**
  merges, pushes, force-pushes, touches the main checkout, or auto-starts tasks —
  a human integrates the branch later. Each cycle it posts a **session-scoped
  heartbeat report** so its activity is finally legible. Toggling the flag does
  NOT respawn the resident (the prompt is recomputed every cycle), so it is
  deliberately excluded from the launch-config invalidation set. The resident
  card now shows a **Master** badge plus a "last run … ago" + latest-heartbeat
  meta line; the config popup gains the Master-mode checkbox and a `· Master`
  pill in the summary row.
- **Resident lifecycle buttons**: the Resident Agent config popup's bottom row
  now carries three lifecycle buttons (replacing the single "Done") plus a Done
  to dismiss the sub-modal. In **edit mode** each button acts immediately via
  `PATCH /api/workspaces/{id}` through the Pinia store and refreshes the board —
  no separate Save needed: **Create resident** sends the full resident payload
  with `resident_agent_enabled: true` (disabled once a resident already exists),
  **Pause/Resume** toggles `resident_agent_paused`, and **Delete resident**
  confirms then sends `resident_agent_enabled: false` to tear down **only** the
  resident (its session/tab) while keeping the workspace. In **create mode**
  (no workspace id yet) the three buttons are disabled and a hint notes the
  resident is created together with the workspace via the parent "Create
  workspace" button. A directive-timing hint under the directive textarea
  clarifies a changed directive is saved immediately but only takes effect on
  the resident's next scheduled cycle (保存后于下个周期生效，不会立即重新运行).
- **Resident modal UI polish**: shortened the lifecycle button labels from
  "Create resident" / "Delete resident" to **Create** / **Delete** (the modal
  title and single-word Pause already give context) and added
  `white-space: nowrap` to `.modal-actions` buttons so labels never wrap or
  overflow below the button. Removed the redundant **Working Directory** field
  from the resident config — the resident now always runs in the workspace's
  own directory (`resident_agent_cwd` is left empty, so the backend's
  `payload.cwd or workspace.path` fallback applies). Rewrote the resident copy
  to pure, concise English (no mixed Chinese), keeping each hint to 1-2 lines.
- **Resident schedule legibility**: clarified the modal copy so it no longer
  reads as if the resident only acts when there is work to dispatch. The
  **Enable** hint now states it "wakes every interval on its own — even when
  idle, with no task running" (matching the trigger's overdue backstop in
  `_resident_agent_due`), and the **Master mode** hint now leads with "Changes
  what each cycle does, not whether it runs" so the toggle is understood as
  per-cycle behavior (self-iterate + heartbeat vs. read-only upkeep) rather
  than an on/off switch for scheduling. Copy-only change; no behavior change.

### fix: allow Done after a reported Completed even without a review verdict

- **What**: a reviewed/auto task that reported `completed` (so it sits in `review`
  status) now shows an enabled **Done** button even when no AI review verdict was
  ever produced. Previously the Done button only appeared after a reviewer verdict
  (`review_passed`), a review skip, or an explicit human-acceptance request — so a
  simple task the agent just reported `completed` on was stuck in `review` with no
  way for the human to finish it.
- **Why**: some tasks are simple enough that the agent reports `completed`
  directly and no review verdict is needed. In that state
  `human_acceptance_requested_at` / `review_skipped_at` stay null and there is no
  `review_passed` report, so `awaitingHumanAcceptance` returned false and hid
  Done. The backend already permits the `REVIEW → DONE` transition; the blocker
  was purely the frontend gate. Reporting Completed should itself permit
  transition to Done.
- **How** (`frontend/src/components/AgentWorkspaceView.vue`): added
  `hasReportedCompletion(task)` (latest board report state is `completed`) and
  OR'd it into `awaitingHumanAcceptance`. `ready_for_review` is excluded — it
  signals the agent is asking for AI review, so the task waits for a verdict. All
  existing guards are preserved: a `pending_review` / `rejected` Goal Packet still
  hides Done, and `hasBlockingReviewResult` (latest review verdict `review_failed`
  / `review_needs_input`) still suppresses it. Active reviews are unaffected —
  while a review is running the latest report is `review_started`, not
  `completed`, so the badge still reads "AI reviewing".

### feat: recover agent conversations on startup after a machine reboot

- **What**: when the backend starts and restores tabs from `~/.claude_hub/tabs.json`,
  any agent tab whose tmux session is gone (the signature of a machine reboot, as
  opposed to a backend-only restart where tmux survives) now relaunches by
  **resuming its prior conversation** instead of starting fresh. Claude, Codex,
  and Cursor agent tabs are covered; terminal and remote tabs are unaffected.
- **Why**: tmux sessions (`claude-hub-<tab_id>`) survive a backend restart — the
  tab reattaches and no resume is needed — but die on a full machine reboot.
  Previously a reboot relaunched every agent as a brand-new conversation, losing
  all prior context. Agents already expose CLI resume, so startup recovery just
  wires it up.
- **How** (`backend/claude_hub/services/ttyd_manager.py`):
  - Each claude tab gets a stable `agent_session_id` (UUID) pinned at first
    launch via `claude --session-id <id>`, persisted in `tabs.json`, and used on
    recovery via `claude --resume <id> || <fresh-pinned>`. A per-tab id is
    required because many agent tabs share one cwd, so the cwd-scoped
    `--continue` would collide across tabs; `--session-id`/`--resume` keeps each
    tab's conversation distinct. The `|| <fresh>` fallback re-pins the same id so
    a legacy tab with no recorded session still recovers cleanly next time.
  - Codex (cannot pin an id at launch) recovers via `codex resume --last || <fresh>`;
    Cursor via `agent --continue || agent`.
  - Recovery is gated by `_should_recover()` = restored-from-persisted-state AND
    tmux session absent AND local target AND a resumable agent type — so live
    reattaches (backend restart), terminal tabs, and remote tabs never resume.
- **Tests** (`backend/tests/test_ttyd_manager.py`): cover stable id pinning on
  fresh claude launch, non-claude agents not pinning an id, the `_should_recover`
  gate (persisted + session-gone only; fresh/terminal/remote never), the
  resume-with-fallback command for each agent type, live-session reattach
  emitting no resume flag, and `agent_session_id` round-tripping through
  `to_dict`/`_load_state`.
- **Backfill for tabs already running before this feature**: such tabs have no
  pinned `agent_session_id`, so they could not resume on reboot. On startup,
  while tmux sessions are still alive, `_backfill_agent_session_ids()`
  correlates each pre-feature claude tab's `tmux session_created` time with the
  start times of conversations logged under
  `~/.claude/projects/<cwd-key>/<sid>.jsonl`, and pins the id **only on an
  unambiguous match** (best within 90s, runner-up ≥ 600s away, file modified
  during the session) — every uncertain case is logged and skipped, because
  cross-wiring a tab to the wrong conversation is worse than a fresh start.
  Pinned ids are persisted so the next reboot resumes. Covered by 8 tests
  (5 pure-decision cases + 3 manager-level pin/skip cases) and an adversarial
  safety review.

### fix: restate report endpoint in follow-up nudges so context-cleared agents can report

- **What**: after an agent's context was cleared (`/clear`), follow-up messages
  that asked it to report progress — the continue-from-review prompt and the
  background auto-continue nudges — told the agent to "report" but no longer
  included the report endpoint. The endpoint only appeared in the bootstrap and
  assignment prompts, which `/clear` wipes, so a cleared agent had no curl
  target and could not find the 上报 (report) API.
- **Root cause**
  (`backend/claude_hub/services/workspace_manager/_prompts.py`,
  `_monitor.py`): `_build_continue_prompt` and the
  `AUTO_CONTINUE_MESSAGE` / `AUTO_REPORT_MISSING_MESSAGE` nudges instructed the
  agent to report with the same `task_id` but omitted the endpoint. The two
  `/clear`-sending dispatch paths (assignment prompt, reviewer review prompt)
  already re-supply it and were unaffected.
- **Fix**: added a shared `_report_endpoint_curl(session, task_id)` helper that
  renders the per-session curl example (honoring `remote_forward_port`), and
  injected it into the continue prompt and both auto-continue nudges so every
  message that asks a possibly-cleared agent to report carries a concrete curl
  target.
- **Tests** (`backend/tests/test_workspace_orchestrator_contract.py`): cover the
  helper (session/task interpolation, `TASK_ID` placeholder default, remote
  forward port), the continue prompt including the endpoint, and a behavioral
  test driving the real `_auto_continue_stopped_task` for both the interruption
  and report-missing branches, asserting each sent nudge restates the endpoint.

### fix: fallback reaper no longer re-dispatches a genuinely-working reviewer

- **What**: a review card could show a duplicate `ready_for_review` entry
  labelled "fallback reaper" ("重新分派卡住的 review 任务（fallback
  reaper）") posted while a reviewer was actively reviewing — a confusing
  duplicate report and wrong-looking status.
- **Root cause** (`backend/claude_hub/services/workspace_manager/_tmux_queries.py`):
  the fallback reaper `_reap_stuck_reviews` gated re-dispatch on
  `not _reviewer_is_active(task)`, which treats a bound, non-stopped reviewer
  as inactive whenever its `runtime_status` is `IDLE`. The terminal classifier
  reports IDLE between bursts, and a reviewer silently reading a large review
  prompt produces no frame change for minutes, so `last_activity_at` goes stale,
  the 60s reaper grace lapses, and the reaper re-dispatched a healthy reviewer.
- **Fix**: the reaper now requires positive evidence of a failed dispatch. New
  reaper-only predicate `_reviewer_dispatch_stuck(task)` returns True only when
  there is no `review_session_id`, the reviewer session is missing, the session
  is `STOPPED`, or the reviewer is bound to a different task. A bound,
  not-stopped reviewer is presumed mid-review and is never reaped, no matter how
  long it sits IDLE. A backstop `_reviewer_prompt_still_pending(task)` still
  recovers a genuine silent send-failure by checking whether the review prompt
  is verifiably stuck in the reviewer's tmux input box (the same signal the
  monitor's stall detector uses). `_reviewer_is_active` is left unchanged for
  its other callers (report-recovery, `continue_task`, task updates), where
  IDLE-means-available is correct.
- **Tests** (`backend/tests/test_workspaces.py`): a unit test of the
  `_reviewer_dispatch_stuck` predicate, plus integration cases asserting the
  reaper keeps a bound + IDLE reviewer, still re-dispatches a missing/STOPPED
  reviewer, and re-dispatches when the review prompt is still pending in the
  input box.
- See `docs/working-logs/2026-06-19-review-dispatch-reaper-active-reviewer.md`.

### fix: prune orphan reviewer terminal tabs invisible in Manage Agents

- **What**: a workspace could accumulate reviewer terminal tabs that no longer
  had a backing `ManagedSession`. They showed in the terminal tab bar but were
  absent from the "Manage Agents" board (which lists sessions), so they could
  not respond to dispatch and could not be deleted from that UI — appearing as
  "many reviewers that don't work and can't be removed".
- **Root cause**: terminal tabs (`ttyd_manager`) and managed sessions
  (`workspace_manager`) persist to separate state files. When a session was
  removed without its tab (e.g. historical temporary-reviewer lifecycle
  desync), the tab was orphaned permanently — nothing reconciled tabs against
  sessions.
- **Fix** (`backend/claude_hub/services/workspace_manager/_tmux_queries.py`):
  added `_prune_orphan_workspace_tabs`, an idempotent reconciler that deletes
  managed tabs (with this `workspace_id`) that have no backing session. It is
  conservative: manual tabs (no `workspace_id`) are never touched, tabs backing
  a live session are kept, and tabs created within
  `ORPHAN_TAB_PRUNE_GRACE_SECONDS` (60s) are kept to avoid racing the
  create-tab → session-registration window. It runs on `get_board` (so opening
  the workspace cleans up) and in `_dispatch_workspace_locked`.
- **Tests**: `backend/tests/test_orphan_tab_reconcile.py` covers prune-orphan,
  preserve-manual, preserve-live-session, preserve-within-grace,
  ignore-other-workspace, and a mixed set.

### fix: "Clear context" checkbox now honored on every dispatch path

- **What**: a task created with the "Clear context" checkbox often did not
  actually clear the delegated worker agent's context. The send mechanism was
  fine (`/clear` works for claude, codex, and cursor — verified live), but the
  dispatch decision logic silently dropped the stored `task.clear_context`
  flag on the most common paths.
- **Root cause** (`backend/claude_hub/services/workspace_manager/_dispatch.py`,
  `_choose_dispatch_target`): the related-task continuity branch and the
  "Continuing previous task assignment" branch returned a hardcoded
  `clear_context=False`; the user-selected-target branch and the
  dispatcher-decision paths read only `payload.clear_context`, ignoring the
  flag persisted on the task at creation. So a checkbox set at task-creation
  time never reached `_dispatch_task_to_session`, which is the code that sends
  `/clear`.
- **Changes**:
  - `_choose_dispatch_target` resolves an explicit `requested_clear` once
    (inline `payload.clear_context` wins, then stored `task.clear_context`,
    then `None` → each branch's existing default/prior-history heuristic) and
    applies it consistently across all five dispatch branches.
  - The dispatcher-decision wait path preserves the stored flag instead of
    overwriting it with `payload.clear_context`.
  - `apply_dispatch_decision` ORs the stored flag so an explicit user opt-in is
    never overridden by the dispatcher agent's discretion.
  - New regression test `test_related_task_clear_context_checkbox_sends_clear`
    asserts `/clear` precedes the task prompt on the related-task path.
- **Not changed**: `/clear` is the correct clear command for all three agent
  types (no per-agent divergence needed); the reviewer cross-task `/clear`
  heuristic and the tmux send/paste mechanism are untouched.

### fix: CLI task detail keeps reviewed acceptance evidence visible

- **What**: `task status` and Feishu `task_detail` cards now surface the most
  recent non-empty `acceptance_check` from task report history even when a newer
  reviewer report is the latest progress report.
- **Files**: `backend/claude_hub/cli/commands/tasks.py`,
  `backend/claude_hub/cli/commands/feishu.py`,
  `backend/claude_hub/cli/feishu_cards.py`, `backend/tests/test_cli.py`,
  `backend/tests/test_feishu_commands.py`.

### feat: typed CLI control-plane status displays

- **What**: third-party agents can now inspect Claude Hub backend state without
  scraping raw board JSON.
- **Changes**:
  - Adds typed display commands for workspace summaries, markdown/snapshot
    discovery, agent runtime rosters, single-session runtime status, and task
    Goal Packet / review / acceptance detail.
  - Enriches Feishu status, overview, and task-detail cards with task/session
    counts, runtime state, snapshot/Markdown discovery, Goal Packet status, and
    acceptance-check summary.
- **Files**: `backend/claude_hub/cli/commands/workspaces.py`,
  `backend/claude_hub/cli/commands/sessions.py`,
  `backend/claude_hub/cli/commands/tasks.py`,
  `backend/claude_hub/cli/commands/feishu.py`,
  `backend/claude_hub/cli/feishu_cards.py`.

### feat: reviewer prompt hardened against sycophancy / low defect-detection

- **What**: the independent reviewer agent caught bugs/risks too rarely and
  deferred too easily to the implementation agent (user体感 report). The
  reviewer prompt is reframed from confirmatory ("review against criteria") to
  adversarial defect-hunting.
- **Changes** (prompt-only, in
  `backend/claude_hub/services/workspace_manager/_prompts.py`):
  - **Bootstrap contract** (`_build_reviewer_bootstrap_prompt`) gains a
    "Reviewer mindset" preamble: the reviewer's primary job is to FIND defects,
    not confirm success; approval is the exception not the default; do not defer
    to the implementation agent's confidence/tone/report polish; disregard
    formatting and verbosity and judge substance. Self-reported validation is
    reframed as "claims to verify, not proof" with a requirement to
    independently inspect the highest-risk claims.
  - **Standard implementation review** (`_review_workflow_block`, non-Goal-Packet
    branch) gains a forced pre-verdict "Adversarial defect hunt" step that
    enumerates concrete failure modes (edge/boundary inputs, error paths,
    concurrency/races, regressions, scope leakage, security assumptions) and
    requires checking each against the code before any verdict.
  - **`review_passed` bar tightened** in both the bootstrap exit rules and the
    workflow exit criteria: passing now requires having actively attempted to
    break the change and found no blocking defect; passing on the absence of an
    attempt or on a confident-looking report is disallowed.
- **Why**: grounded in LLM-as-a-Judge research (forced reasoning before verdict,
  adversarial verification, disregard-style/leniency-bias mitigation) and common
  community AI-review practice (treat self-reported validation skeptically).
  Changes are additive wording; the Goal Packet approval gate and all verdict /
  state-machine logic are unchanged.
- **Files**: `backend/claude_hub/services/workspace_manager/_prompts.py`.

### fix: related/continuity-pinned queued tasks migrate off a stuck agent

- **What**: a queued task pinned to a specific agent for context continuity
  (`dispatch_reason` "Related to task ...", "Continuing previous task
  assignment", or a prior reassignment) could starve forever when that agent was
  runtime-idle but still bound to a non-`DONE` task parked in `REVIEW`
  (review passed yet awaiting human acceptance). The board showed the task
  `Queued` and the agent `idle`, while other agents sat free — the live "queue
  排不上" symptom that survived the earlier WORKING-agent fix.
- **Root cause**: the rebalancer (`_next_reassignable_queued_task`) only
  migrated tasks whose `dispatch_reason` was exactly "Queued behind existing
  workspace agent". Every continuity-pinned task was skipped, so it waited
  indefinitely for an agent that `_can_dispatch_to` rejects until a human
  resolves its review task.
- **Fix**: invert the gate. A queued task is now migratable under any
  `dispatch_reason` *except* an explicit operator pin ("User selected target
  agent"), and only when its assigned agent currently cannot be dispatched to
  (`not _can_dispatch_to`). Agent preference is preserved: an available pinned
  agent still picks the task up itself via `_next_queued_task`; migration (with
  `clear_context=True`) only happens when the pinned agent is genuinely stuck.
- **Files**: `backend/claude_hub/services/workspace_manager/_dispatch.py`,
  `backend/tests/test_workspaces.py`.

### fix: auto-queued tasks migrate off a busy WORKING agent, not only a REVIEW-held one

- **What**: a task that auto-queued behind a busy agent could stay stuck in the
  `Queued` column (board showed "task queued" while its agent showed "runtime
  working") even when another agent sat idle. This is the reported "queue排不上"
  symptom.
- **Root cause**: dispatch pins each auto-queued task to a specific agent and
  only launches it when that agent is `IDLE`. The rebalancing path
  (`_next_reassignable_queued_task`) that migrates a queued task to a newly free
  agent only triggered when the originally-assigned agent was holding an
  unresolved `REVIEW` task — a task queued behind a genuinely `WORKING` agent
  was never rebalanced and starved until that one agent went idle.
- **Fix**: broaden the rebalance condition from "assigned agent is holding a
  REVIEW task" to "assigned agent cannot currently be dispatched to"
  (`not _can_dispatch_to`), covering WORKING / REVIEW-held / STOPPED / OFFLINE.
  Only auto-queued tasks (`dispatch_reason == "Queued behind existing workspace
  agent"`) are eligible, so user-selected and related-task-pinned tasks stay
  bound to their agent for context continuity.
- **Files**: `backend/claude_hub/services/workspace_manager/_dispatch.py`,
  `backend/tests/test_workspaces.py`.

### fix: bound reviewer on a reopened task is no longer auto-prompted as a pseudo-worker

- **What**: after a `review_failed` reopen, the task could stall for ~5 minutes
  — the system repeatedly auto-prompted the reviewer to "report" while silently
  dropping every verdict it re-posted, until the fallback reaper finally
  unstuck it. This is the reported "reviewer被占据 / 上报始终收不到完成信号"
  symptom.
- **Root cause**: after `review_failed`, the reviewer session intentionally
  stays bound to the task (`current_task_id`) so the same reviewer handles the
  next cycle. But the monitor's `_auto_continue_stopped_task` treated *any* idle
  session bound to a `WORKING` task as the task's worker, so it auto-prompted
  the bound reviewer (`action=report_missing`). The reviewer re-posted
  `review_failed`, which is correctly dropped as a stale duplicate (no review in
  flight), stranding the task until `_reap_stuck_reviews` fired.
- **Fix**: guard `_auto_continue_stopped_task` so only the task's worker
  (`task.session_id`) is auto-continued; an idle reviewer
  (`task.review_session_id`) bound to the reopened task is skipped. This
  preserves the intentional reviewer-binding design while stopping the stall.
- **Files**: `backend/claude_hub/services/workspace_manager/_monitor.py`,
  `backend/tests/test_workspaces.py`.

### feat: Claude Hub CLI exposes the full REST control surface

- **What**: broadens the `claude-hub` CLI from task/session inspection into a
  full Claude Hub control plane for external agents such as Hermes. New typed
  command groups cover auth checks, system network access, terminal tabs,
  terminal history/proxy URLs, local filesystem browsing, remote profiles and
  remote filesystem browsing, clipboard image upload, and a generic `api raw`
  escape hatch for any current or future REST endpoint.
- **Workspace/task/session/lessons coverage**: existing command groups now cover
  workspace update/dispatch/artifact preview/attachment download, task
  update/delete/spawn/dispatch-decision/feedback reap, session delete and
  attachment-aware send, richer agent creation flags, and lesson create /
  summarize flows. Complex request bodies can be supplied with `--payload-json`
  so agents are not blocked on one flag per schema field.
- **Feishu/Hermes surface**: `feishu build-card` adds cards for tabs, runtime
  status, network access, filesystem, remote profiles, remote filesystem,
  generic command results, and an action catalog. `feishu parse-action` now
  returns a suggested CLI command when the callback contains enough IDs. The
  Hermes `claude-hub` skill documents typed commands plus `api raw` as the
  complete-control model and no longer advertises a nonexistent built-in
  `feishu-bot` command.
- **Files**: `backend/claude_hub/cli/client.py`,
  `backend/claude_hub/cli/main.py`, `backend/claude_hub/cli/commands/common.py`,
  `backend/claude_hub/cli/commands/rest.py`,
  `backend/claude_hub/cli/commands/workspaces.py`,
  `backend/claude_hub/cli/commands/tasks.py`,
  `backend/claude_hub/cli/commands/sessions.py`,
  `backend/claude_hub/cli/commands/lessons.py`,
  `backend/claude_hub/cli/commands/feishu.py`,
  `backend/claude_hub/cli/feishu_cards.py`, `backend/tests/test_cli.py`, and
  `backend/tests/test_feishu_commands.py`.

### feat: CLI task/session inspection and richer Feishu collaboration cards

- **What**: expands the `claude-hub` CLI surface used by third-party agents such
  as Hermes. `task get/report/review/accept` expose task detail, progress,
  review history, and human-acceptance transitions; `session list/logs` expose
  managed-session inventory and recent terminal output; `session report` can now
  submit bilingual messages and structured report fields via `--payload-json`.
- **Feishu cards**: `feishu build-card` now covers display kinds for
  `workspaces`, `overview`, `agents`, `task_detail`, `reports`, `terminal`, and
  `lessons` in addition to the existing interactive/status/task cards, giving
  Feishu-facing agents card-ready JSON for the main Claude Hub workflows.
- **Files**: `backend/claude_hub/cli/client.py`,
  `backend/claude_hub/cli/commands/tasks.py`,
  `backend/claude_hub/cli/commands/sessions.py`,
  `backend/claude_hub/cli/commands/feishu.py`,
  `backend/claude_hub/cli/feishu_cards.py`,
  `backend/tests/test_cli.py`, and `backend/tests/test_feishu_commands.py`.

### feat: Feishu interactive cards over the `claude-hub` CLI

- **What**: a `feishu` CLI group with two stateless, IO-free helpers for an
  external agent that is itself a Feishu bot — it sends cards to a human and
  receives the `card.action.trigger` callback in the same process, then drives
  Hub through the CLI ("Scenario A"). `feishu build-card` prints a card's JSON
  for the agent to send; `feishu parse-action` parses a raw callback into a
  normalized `{token, action, form, operator_id, chat_id}` decision (payload as
  an argument or on stdin; foreign cards exit non-zero with a `null` line).
  Kinds: `approval`, `needs_input`, `plan_confirm` (interactive, carry a
  correlation token) and `status`, `task` (render live workspace data, no token).
- **How**: card construction and callback parsing are pure functions in
  `feishu_cards.py`; the two CLI commands just shell them out as JSON. Because
  the agent sends and receives in one process, Hub is not in the Feishu loop —
  no outbound sender, no token/result store, no `/api/feishu/cards/*` endpoints,
  and no chat-id bindings. `build-card` reaches the backend only to read live
  board data for the display kinds.
- **Files**: `backend/claude_hub/cli/feishu_cards.py` (card builders +
  `parse_card_action`), `backend/claude_hub/cli/commands/feishu.py` (the two
  thin commands), `README.md`,
  `docs/working-logs/2026-06-16-feishu-card-cli.md`,
  `backend/tests/test_feishu_parse.py`, and
  `backend/tests/test_feishu_commands.py`.

### fix: reviewer /clear decision keyed off the reviewer session, not other tasks' fields

- **What**: when a reviewer session was reused for an unrelated new task, it
  sometimes started reviewing without first clearing its terminal context, so
  the prior task's conversation leaked into the new review (wasted context and
  risk of mid-review auto-compact).
- **Root cause**: the cross-task `/clear` decision relied on
  `_has_prior_review_history`, which scanned other tasks for one whose
  `review_session_id` pointed back at the reviewer. That field is nulled by the
  abort (`_dispatch.py`), review-skip, and stale-reviewer-release paths, so a
  reviewer that genuinely still held a prior task's context could appear to have
  no history — and the unrelated review dispatched without `/clear`.
- **Fix**: track the reviewer's last-dispatched review task on the session
  itself (`ManagedSession.last_review_task_id`), set whenever a review prompt is
  actually sent. `_request_task_review` now clears iff that value is set and
  differs from the incoming task id. New task → clear; same task re-review
  (review_failed→fix→completed, or goal-packet then implementation) → keep;
  brand-new reviewer with no prior review → no `/clear` round-trip. Removed the
  now-unused `_has_prior_review_history`.
- **Files**: `backend/claude_hub/models/schemas.py`,
  `backend/claude_hub/services/workspace_manager/_reports.py`,
  `backend/claude_hub/services/workspace_manager/_dispatch.py`,
  `backend/claude_hub/services/workspace_manager/_normalize.py`,
  `backend/tests/test_workspaces.py`.

### feat: click task attachment image to preview at full size

- **What**: task attachment thumbnails in the task detail panel were not
  clickable, so users could not view the image at full size. Clicking a
  persisted attachment thumbnail now opens a full-screen lightbox preview.
- **Behavior**: the enlarged image is scaled to fit the viewport while
  preserving aspect ratio; the overlay closes on backdrop click, the `Escape`
  key, or the close button. The thumbnail shows a pointer cursor and an accent
  border on hover.
- **Files**: `frontend/src/components/AgentWorkspaceView.vue`.

### feat: add the `claude-hub` command-line interface

- **What**: a new `claude-hub` CLI (Click-based, installed as a console script
  via `[project.scripts]`) that drives the Agent Workspace REST API from a
  shell. Command groups: `workspace` (list/create/board, with `status` as an
  alias), `task` (list/create/start/send/continue/abort), `agent`
  (list/create), `session` (send/report), and `lessons` (list/get). Global
  options `--base-url`/`CLAUDE_HUB_URL`, `--token`/`CLAUDE_HUB_TOKEN`,
  `--cookie`, `--json`, `--config`/`CLAUDE_HUB_CONFIG`, and `-v`/`--verbose`.
- **Convenience**: `task send` lets a human or agent push a follow-up message
  to a running task without finding the underlying session id.
- **Hardening**: defensive rendering for non-dict rows in table output,
  real request-URL logging under `--verbose`, and cookie/config edge handling
  (config precedence flags > env > TOML file > defaults; loopback bypasses
  auth so a local backend needs no token). The HTTP client uses
  `trust_env=False` so ambient proxy env vars do not hijack loopback requests.
  Commands exit non-zero on API errors.
- **Files**: `backend/claude_hub/cli/` (package), `backend/tests/test_cli.py`,
  `backend/pyproject.toml` (adds `click` + the `claude-hub` script), `README.md`,
  `docs/working-logs/2026-06-15-claude-hub-cli.md`.

### fix: stop.sh now reliably kills the backend worker and all ttyd processes

- **Symptom**: restarting the backend appeared to "not take effect" — after
  running `stop.sh` and relaunching, the served terminal JS was still the old
  version, and stale `ttyd` processes piled up across runs.
- **Root cause**: two pattern bugs in `stop.sh`. (1) With `--reload`, uvicorn
  runs a 3-process tree; the process that actually binds the port is a
  multiprocessing-spawned worker whose command line is `python -c from
  multiprocessing.spawn import spawn_main; ...` — it contains no `uvicorn`
  token, so `pkill -f "uvicorn claude_hub.main:app"` killed the launcher and
  supervisor but left the worker holding the port (the supervisor would then
  resurrect it). (2) `pkill -f "ttyd --port 100"` only matched ports `100xx`,
  while ttyd is spawned across `10xxx`–`11xxx`, so nearly every ttyd survived.
- **Fix**: after the pattern kill, reap whatever still LISTENs on the backend
  port (`lsof -tiTCP:$PORT -sTCP:LISTEN`, TERM then KILL); broaden the ttyd
  pattern to `ttyd --port`. Backend port is configurable via `CLAUDE_HUB_PORT`.
- **Files**: `stop.sh`.

### fix: prevent reviewer dispatch from stealing a busy reviewer

- **Symptom**: multiple tasks waiting for review would all be bound to the
  same reviewer (e.g. `cb-reviewer-1`), but only one would actually progress
  — the others appeared stuck in "Awaiting AI review" until the fallback
  reaper recovered them after ~60s.
- **Root cause**: `_select_or_create_reviewer` unconditionally reused
  `task.review_session_id` (the reviewer from a prior round) without checking
  whether that reviewer was already busy with another task's active review.
  Since every task historically used the first created reviewer, all tasks
  carried `review_session_id=<first-reviewer>` and the last one to request
  review "won", stranding the rest.
- **Fix**: add `_reviewer_is_busy_with_other_task()` that checks both the
  reviewer session's own task binding and whether any other task in the
  workspace claims this reviewer with an in-flight review. When the
  historically-assigned reviewer is busy, fall through to
  `_first_available_reviewer` instead of stealing the session.
- **Files**: `backend/claude_hub/services/workspace_manager/_review.py`,
  `backend/tests/test_workspaces.py`.

### perf: remove per-frame regex/decode from terminal output path (input latency v4)

- **Symptom**: terminal typing still felt laggy ("不跟手") *under heavy output*
  even after the v3 layout-reflow fix. Measured with a new Playwright
  keystroke-to-glyph harness: idle was fine (p50 18ms) but under a wide-line
  flood it ballooned to **p50 151ms / p95 613ms** (n=45) — exactly the felt lag.
- **Root cause**: the v3 round removed the per-frame DOM reflow but left a second
  per-frame cost. On **every output frame** the injected `term.write` wrapper ran
  `noteResyncPressure(data)` → `terminalDataStats(data)`, which did a
  `TextDecoder().decode()` + **four regex `.replace()` passes** + a `.match(/\n/g)`
  over the whole frame. Under fast output that dominated the main thread and
  starved keystroke echo.
- **Fix**: replace `terminalDataStats` with an allocation-free O(n) byte/char
  scan — count `0x0a` for exact `lineBreaks`, use raw length as an approximate
  `chars`. The only consumers are the coarse burst thresholds in
  `hasEnoughResyncPressure()` (`chars >= 4096` OR `lineBreaks >= 8`), so an
  approximate `chars` merely arms the idle resync marginally earlier — harmless.
  Removed the now-dead `terminalDataText` helper.
- **Result** (same harness, after): under-load **p50 79ms / p95 225ms**
  (from 151 / 613) — ~48% / ~63% lower; idle unchanged. Resync E2E correctness
  guards (`test_terminal_replay.py`) still pass.
- **Diagnostic harness**: `backend/tests/test_terminal_input_latency_perf.py` —
  opt-in, run-on-demand (`uv run pytest … -s`), **not** a CI timing gate
  (absolute latencies are machine-dependent). The static guard
  `test_terminal_input_latency_guard.py` now also asserts `terminalDataStats`
  contains no `TextDecoder`/`.replace(`/`.match(`.
- **Files**: `backend/claude_hub/api/terminal.py`,
  `backend/tests/test_terminal_input_latency_perf.py`,
  `backend/tests/test_terminal_input_latency_guard.py`. See
  `docs/working-logs/2026-06-14-terminal-input-latency-v4.md`.

### perf: remove per-frame layout reflow from terminal output path (input latency v3)

- **Symptom**: terminal typing felt laggy / detached ("不跟手") again, despite a
  prior optimization round. Root cause: the injected `term.write` wrapper in
  `backend/claude_hub/api/terminal.py` consulted "is the viewport at the bottom?"
  on **every output frame** (`viewportIsAtBottom()`/`needsBottomScroll()`), and
  each call read `scrollTop`/`scrollHeight`/`clientHeight` off `.xterm-viewport`
  — forcing a synchronous layout reflow per frame. Under fast output the main
  thread spent its time in layout instead of processing keystrokes.
- **Why the earlier round didn't help**: the prior SharedArrayBuffer/Atomics
  keystroke path only carries *synthetic*/mobile keys; focused desktop typing
  goes straight xterm.js → ttyd WS → tmux, so that work was off the real hot
  path. WebGL/TCP_NODELAY/COOP-COEP are all still present.
- **Fix**: replace the per-frame DOM geometry read with an event-driven cached
  flag (`domAtBottomCached`) plus a cached viewport node (`cachedViewportEl`).
  `recomputeDomAtBottom()` is the single geometry-reading function, called only
  at state-changing edges: the viewport `scroll` listener, each programmatic
  scroll-to-bottom (bottom-follow `run()`, history-snapshot done, auto-resync
  completion), and the resize/fit paths. The hot path now does zero DOM reads.
- **Resize staleness**: the closure exposes `term.__claudeHubRecomputeBottom` so
  the sibling `setupResizeGuard` can refresh the flag after a debounced
  `onResize` or mobile-keyboard `fit()` (layout changes that don't fire a scroll
  event). Behavior-preserving for scroll/bottom-follow.
- **Files**: `backend/claude_hub/api/terminal.py`. See
  `docs/working-logs/2026-06-14-terminal-input-latency-v3.md`.

### perf: speed up agent-workspace board, add loading skeleton, smooth mobile input

- **Board latency (backend)**: loading/switching workspaces felt slow (the
  board API took 2-3s). Root cause was a blocking `os.system("tmux has-session")`
  spawned **per tab** inside the async status refresh, serializing the event
  loop on every board poll. Fixes: (1) batch existence into a single
  `tmux list-sessions` call (`_tmux_list_sessions`) whose result set is passed
  down to each `get_tab_agent_status`; (2) convert remaining async-context
  callers to a non-blocking `_tmux_session_exists_async`; (3) drop the redundant
  per-tab existence check inside `_classify_agent_status` (the caller already
  verified aliveness); (4) make the sync startup fallback use `subprocess.run`
  (no shell) instead of `os.system`. Measured board latency dropped to ~115-170ms
  warm (274ms cold) from the reported 2-3s.
- **Loading animation (frontend)**: the board had no loading state on workspace
  switch. Added a shimmering skeleton overlay (`boardLoading`) with a fade
  transition, collapsed to a single-column layout on mobile and disabled under
  `prefers-reduced-motion`.
- **Mobile input lag (frontend)**: typing in the task-detail compose field felt
  laggy on mobile. The 2.5s board poll replaces the entire `board` object,
  forcing Vue to re-render the large open detail subtree (which hosts the focused
  textarea) and competing with keystroke handling on weaker mobile CPUs. Fix:
  `refreshBoard` now skips a background tick while a text field is focused and
  resumes on blur; explicit refreshes are unaffected.
- Also removed a stray `//` JS comment from `frontend/package.json` that made the
  file invalid JSON and broke all `pnpm` commands (lint/type-check/build) in CI.
- **Board payload (backend + frontend)**: even after the latency fix, mobile/LAN
  users still felt multi-second lag on load and every 2.5s poll. Root cause was
  **payload size**: `GET /workspaces/{id}/board` shipped ~2.6 MB uncompressed
  every tick, of which report history was ~87% (1138 reports), while the UI only
  needs the latest report per task for board cards plus the open task's full
  history for the detail panel. Fixes: (1) enable `GZipMiddleware`
  (`minimum_size=1024`), placed inside `CoopCoepMiddleware` so COOP/COEP headers
  still apply and the WebSocket scope is untouched; (2) trim `board.reports` to
  the latest report per task (new `latest_reports_per_task_for_workspace`);
  `markdown_documents` stays complete (built server-side from full history);
  (3) add an on-demand `GET /workspaces/{id}/tasks/{task_id}/reports` endpoint
  the detail panel hydrates from when a task is opened, with the 2.5s poll
  refetching a task's history only when its latest report id changes. Measured on
  a ~40-session production-like state: board dropped from 2.6 MB → 657 KB
  uncompressed (1138 → 80 reports) → **164 KB on the wire with gzip** (~94%
  reduction).
- **Conditional board requests (ETag / 304)**: the board endpoint now sends a
  content-based `ETag` and honors `If-None-Match`. The tag is hashed over
  normalized, order-independent board content with volatile per-session
  timestamps excluded (`sessions[].updated_at`, `sessions[].last_activity_at`,
  `markdown_documents[].updated_at`) — at idle these tick every refresh but
  change nothing the UI renders, so excluding them lets an unchanged board match.
  An idle 2.5s poll now returns a **bodyless `304`** instead of re-shipping the
  gzipped payload (verified against the ~40-session state: steady-state polls
  drop from ~176 KB to 0 bytes on the wire); the tag rotates the instant real
  content changes, so a new report still surfaces within one poll. `Cache-Control:
  no-cache` forces revalidation each tick rather than blind caching. Frontend
  `fetchBoard` stores the per-workspace ETag, sends it back, and keeps the
  existing `board.value` on a 304.
- **Files**: `backend/claude_hub/services/ttyd_manager.py`,
  `frontend/src/components/AgentWorkspaceView.vue`, `frontend/package.json`,
  `backend/claude_hub/main.py`,
  `backend/claude_hub/services/workspace_manager/_tmux_queries.py`,
  `backend/claude_hub/api/workspaces.py`,
  `frontend/src/stores/workspaceStore.ts`,
  `backend/tests/test_workspaces.py`.

### fix: replace reviewer-verdict timestamp heuristics with an ordinal review-cycle model

- A passed/parked task could be silently stranded in the "Working" column and
  never re-enter review. Two coupled defects: (1) stale reviewer-verdict
  timestamps from an earlier round survived into a later round and swallowed a
  fresh `ready_for_review`; (2) the runtime layer mutated the review layer —
  free-form terminal chat flipped the agent runtime to WORKING, the monitor
  reopened the REVIEW task, and the reconcile path then re-forced REVIEW with
  the stale timestamps. The two fought and the verdict never advanced.
- **Product behavior locked**: a passed task is a *parked* (awaiting-acceptance)
  state — `status=REVIEW` with `human_acceptance_requested_at` set and
  `human_accepted_at=None`. It leaves parked **only** via human acceptance
  (→ DONE) or `continue_task` from the task board (→ a fresh work round).
  Free-form agent activity in the terminal never moves it or touches review
  fields.
- Replaced the wall-clock timestamp heuristics with an ordinal mechanism:
  `WorkspaceTask.review_cycle` (current round, default 1),
  `WorkspaceTask.reviewed_cycle` (round of last applied verdict, default 0), and
  `AgentReport.review_cycle` (stamped at intake with the task's current round).
  A reviewer verdict applies iff it opens a fresh round
  (`report.review_cycle > reviewed_cycle`) **and** a review is actually in flight
  — the in-flight requirement rejects a stale echo that arrives after
  `continue_task` already bumped the cycle and cleared `review_requested_at`.
  Reopen paths (`continue_task`, review-failed, goal-packet supplement) increment
  `review_cycle`; applying a verdict advances `reviewed_cycle`.
- Decoupled the runtime layer: removed the monitor's runtime-reopen of REVIEW
  tasks and made the reconcile path cycle-aware, so terminal activity can no
  longer drag a parked task back to WORKING or resurrect a prior-round verdict.
- New pure predicates `report_opens_review_round` and `current_round_has_verdict`
  in `workspace_state_policy`; `compute_reviewer_verdict_task_update` now emits
  `reviewed_cycle`.
- **Files**: `backend/claude_hub/models/schemas.py`,
  `backend/claude_hub/services/workspace_state_policy.py`,
  `backend/claude_hub/services/workspace_manager/_reports.py`,
  `backend/claude_hub/services/workspace_manager/_review.py`,
  `backend/claude_hub/services/workspace_manager/_monitor.py`,
  `backend/claude_hub/services/workspace_manager/_dispatch.py`,
  `backend/claude_hub/services/workspace_manager/_normalize.py`,
  `backend/tests/test_workspace_state_policy.py`,
  `backend/tests/test_workspaces.py`

### refactor: unify reviewer-verdict state logic in the pure policy layer

- Behavior-preserving cleanup of the workspace task state machine following the
  stale-verdict fix below. The review-state predicates that had been
  copy-pasted inline across the `workspace_manager` mixins (~10 sites) and the
  three independent `status=REVIEW` verdict-writers (kept consistent only by
  idempotency guards) are now expressed once in the side-effect-free
  `workspace_state_policy` layer.
- New pure functions (scalar-field signatures, fully unit-tested):
  `review_in_flight`, `reviewer_verdict_already_applied`,
  `reviewer_verdict_actionable`, `reviewer_verdict_still_authoritative`,
  `review_verdict_terminal`, and `compute_reviewer_verdict_task_update` — the
  last builds the reviewer-verdict task-field update dict for all three writers
  (`create_report` fast-path, `_handle_review_report`,
  `_handle_goal_packet_review_report`). Per-call-site `preserve_*` /
  `human_acceptance_for_passed` flags preserve each writer's exact historical
  timestamp policy, so no state-machine semantics change. The mixins now
  delegate to these functions; goal-packet transition, autonomous recompute,
  and `continue_task` side effects remain in the mixins.
- The `_reconcile_task_report_statuses` monitor repair path is intentionally
  left as-is (it keys off report-time timestamps, structurally different).
- **Files**: `backend/claude_hub/services/workspace_state_policy.py`,
  `backend/claude_hub/services/workspace_manager/_reports.py`,
  `backend/claude_hub/services/workspace_manager/_review.py`,
  `backend/claude_hub/services/workspace_manager/_monitor.py`,
  `backend/claude_hub/services/workspace_manager/_dispatch.py`,
  `backend/claude_hub/services/workspace_manager/_tmux_queries.py`,
  `backend/tests/test_workspace_state_policy.py`

### fix: stop stale duplicate reviewer verdict from stranding a reviewed task in WORKING

- A reviewed task that posted `completed` could get stuck in the "Working"
  board column and never enter "Review". Root cause: after a goal-packet
  `review_passed`, `continue_task` reopens the task to WORKING and clears
  `review_requested_at` / `review_completed_at`. A second (duplicate / stale)
  goal-packet `review_passed` arriving while the agent was still implementing
  was misrouted as an implementation-phase verdict, writing a phantom
  `review_completed_at` / `reviewed_at` / `status=REVIEW`. The monitor
  runtime-reopen heuristic then flipped the task back to WORKING and the
  late-report suppression guard dropped the genuine `completed` report,
  permanently stranding the task.
- Fix: a shared invariant `_reviewer_verdict_actionable(task, report)` — a
  reviewer terminal verdict may only mutate task state when it idempotently
  replays an already-recorded verdict (`review_completed_at >= report.created_at`)
  or a review is genuinely in flight (`review_requested_at` set and
  `review_completed_at` None). Enforced at both chokepoints: the `create_report`
  reviewer fast-path (gated, with `task_status` zeroed) and
  `_handle_review_report` (early return). Stale verdicts are still recorded for
  audit but mutate no task state.
- **Files**: `backend/claude_hub/services/workspace_manager/_reports.py`,
  `backend/claude_hub/services/workspace_manager/_review.py`,
  `backend/tests/test_workspaces.py`

### ci: add pytest-cov backend coverage reporting

- Added `pytest-cov>=5.0` to the `backend/pyproject.toml` `dev` optional
  dependencies so any developer can produce a coverage report locally with
  the same flags CI uses.
- The CI `backend` job now appends
  `--cov=claude_hub --cov-report=xml:coverage.xml --cov-report=term-missing`
  to its existing `pytest` invocation (no extra pytest execution — coverage
  is collected on the same run). Per-test pass/fail still short-circuits via
  `-x` and the terminal-replay E2E file is still ignored; nothing changes
  functionally.
- Added a follow-up `Upload backend coverage to Codecov` step that ships
  `backend/coverage.xml` to codecov.io with `flags: backend` and
  `fail_ci_if_error: false`, so coverage reporting is visible in PRs even
  before the repo has a Codecov token configured.
- **Frontend coverage is explicitly out of scope for this round** — it is
  gated on T1 migrating the suite to Vitest. A `// TODO(T1)` comment was
  added above the `test:unit` script in `frontend/package.json` documenting
  the target (`Vitest + @vitest/coverage-v8`) and a matching `TODO(T1)`
  comment was added in the `frontend` CI job right above the unit-test step
  so nobody adds coverage ad-hoc with the node:test runner.
- **Files**: `backend/pyproject.toml`, `.github/workflows/ci.yml`,
  `frontend/package.json`

### ci: add security-audit job + Dependabot config

- New informational-only CI job `security-audit` — named to make it clear it
  **never fails the pipeline**. Steps:
  1. Install backend deps (Python + uv, same caching pattern as the backend
     job, suffixed `-security-` so the caches don't collide).
  2. Install Bandit inside the step only (no bloat in `dev` deps / the
     local lockfile), run it recursively over `backend/claude_hub` writing
     `bandit-report.txt`, `|| true` because many Bandit rules fire false
     positives against tmux / file-management code.
  3. Python dep CVE audit step is stubbed with a `TODO(PY-AUDIT)` comment
     because `uv` does not yet expose a first-class `uv audit` subcommand.
     Once that lands, the stub can be replaced with the real invocation.
  4. Install frontend deps (Node 20 + pnpm 9, same caching pattern,
     `-security-` suffix).
  5. `pnpm audit --prod --audit-level high || true` — production-only,
     high-severity threshold, never red.
  6. `actions/upload-artifact@v4` uploads `backend/bandit-report.txt` if
     present, with `if-no-files-found: ignore` and `if: always()` so the
     artifact survives any failing step and can be inspected post-hoc.
- Added `.github/dependabot.yml` with three weekly updaters, each grouping
  every dependency change into a single PR to avoid PR spam:
  - `pip` → `/backend`
  - `npm` → `/frontend`
  - `github-actions` → `/`
- **Files**: `.github/workflows/ci.yml`, `.github/dependabot.yml` (new)

### chore: fix Dockerfile build and expand docker-compose with env/volume/healthcheck

- **`docker/Dockerfile` (backend)** — two build-blocking bugs fixed:
  1. `apt-get install` was effectively just `curl`. Added the missing
     **hard runtime requirement `tmux`** (without it, creating a terminal
     tab fails at runtime), plus `git`, `python3`, and `build-essential` so
     agents running inside the container can clone repos, compile wheels,
     and invoke system Python. `ca-certificates` also added so HTTPS fetches
     (ttyd / uv / curl) stay trusted on the base slim image.
  2. `RUN uv sync --no-dev --frozen` was failing because `uv.lock` was
     never copied into the layer. Added `COPY backend/uv.lock ./` right
     after the `pyproject.toml` / `README.md` copy so `--frozen` is
     satisfiable. Also reordered COPY lines for optimal layer caching:
     manifest + lock first → `uv sync` → source code last, so source-only
     commits reuse the (expensive) dependency layer.
  3. Added a comment on the `EXPOSE 8173` line documenting that it is the
     FastAPI/uvicorn HTTP + WebSocket port.
- **`docker/Dockerfile.frontend`** — `node:20-slim` lacks basic system
  packages that make Node builds flaky on networks with TLS proxies or
  localised timestamps. Added `ca-certificates` and `tzdata` via the same
  `apt-get install` pattern used in the backend image. COPY lines were
  also reordered for layer caching (manifest + config → `pnpm install` →
  source → build); `EXPOSE 5173` now carries a comment explaining it is
  the Vite preview server port.
- **`docker/docker-compose.yml`** — four functional gaps closed:
  1. **`env_file: ../.env`** on the `backend` service so Settings env vars
     (`ANTHROPIC_API_KEY`, `DATABASE_URL`, proxy env, etc.) actually reach
     the container instead of only the host.
  2. **Named volume `claude_hub_state`** mounted at `/root/.claude_hub` in
     the backend container so `tabs.json`, workspace state, tmux sockets,
     logs and feedback lessons survive `docker compose restart` /
     re-builds. The volume is declared at the top-level `volumes:` key
     with an explanatory comment.
  3. **Backend `healthcheck`** using `curl -f http://localhost:8173/api/health`
     with `interval: 30s`, `timeout: 10s`, `retries: 5`, and a 20 s
     `start_period` so docker and downstream orchestrators know when the
     FastAPI app is actually serving instead of just the process being up.
  4. **TODO(prod)** comment on the `frontend` service explicitly calling
     out that `pnpm preview` is the dev-mode Vite preview server and a
     real deployment should use a multi-stage build copying `dist/` into
     an Nginx/Caddy container — kept as a clear marker, not removed, per
     the task scope.
- **Files**: `docker/Dockerfile`, `docker/Dockerfile.frontend`,
  `docker/docker-compose.yml`

### fix: remove hardcoded developer laptop path from workspace form defaults

- Replaced the hardcoded default `'/Users/bytedance/claude_hub'` in the
  workspace creation form (AgentWorkspaceView.vue) with an empty string in
  both the initial form state and `resetWorkspaceForm()`. The backend now
  expands `$HOME`/its own default when the cwd field is submitted empty, so
  production users no longer see a reference to a local dev machine.
- **Files**: `frontend/src/components/AgentWorkspaceView.vue`

### fix: checkAuth() no longer swallows network errors into an infinite spinner

- Added a reactive `checkAuthError: false` flag to `authStore`, and set it
  (plus clear `isLoading`) in the catch branch so the UI can exit the
  spinner state on backend unreachable / 5xx.
- Added a top-of-page retry banner in `App.vue` that surfaces when
  `checkAuthError` is true, with a Chinese warning message ("认证检查失败，
  无法连接后端…") plus a "刷新重试" button that re-runs `checkAuth` and
  re-fetches tabs on success, and a close button that sets
  `checkAuthError = false`.
- **Files**: `frontend/src/stores/authStore.ts`, `frontend/src/App.vue`

### chore: tighten ESLint on `any`; fix trivially-typed usages

- Changed `@typescript-eslint/no-explicit-any` from `off` to `warn` in
  `eslint.config.js` so remaining `any` usages surface while CI stays green.
- Fixed the four trivial `any` warnings that did occur (all in
  TerminalView.vue): broadened `registerIframe` parameter type to the
  actual template-ref union, and replaced `(iframe as any)` casts on a
  transient SAB-script property with a typed `HTMLIFrameElement &` helper.
  All warnings now resolved at the current codebase size.
- **Files**: `frontend/eslint.config.js`, `frontend/src/components/TerminalView.vue`

### fix: consolidate reactive window globals into a namespaced object with proper cleanup

- Created `window.__claudeHub = {}` exactly once at app bootstrap (main.ts)
  with a single shared TypeScript interface in `src/types/index.ts`.
- Migrated all previous stray globals off the top-level `window` namespace
  and into `window.__claudeHub`: `__activePaneTabId`,
  `__claudeHubTerminalState`, `__registerTerminalIframe`,
  `__refreshTerminalHistory`, and `__sendTerminalKey`. Every consumer
  updated (App.vue, TerminalView.vue, TerminalGridView.vue, MobileControls.vue,
  TerminalPane.vue).
- Removed the DUPLICATE write of `__activePaneTabId` from
  TerminalGridView.vue — App.vue is now the single authoritative writer.
- Added onUnmounted cleanup in TerminalView.vue for the three globals it
  registers (registerTerminalIframe, refreshTerminalHistory, sendTerminalKey).
- SAB-ring globals (`__CLAUDE_HUB_SAB_BUFFER__`, `__claudeHubDrainSabRing`)
  are intentionally left alone — they live inside each iframe's own
  `contentWindow`, not the top-level window.
- **Files**: `frontend/src/main.ts`, `frontend/src/types/index.ts`,
  `frontend/src/App.vue`,
  `frontend/src/components/TerminalView.vue`,
  `frontend/src/components/TerminalGridView.vue`,
  `frontend/src/components/MobileControls.vue`,
  `frontend/src/components/TerminalPane.vue`

### fix: replace single error-string anti-pattern with a stacked notification queue

- **terminalStore** and **workspaceStore** both had a single shared
  `error: ref<string>` that every concurrent API failure would overwrite,
  hiding earlier errors. Replaced each with:
  - A reactive `notifications: StoreNotification[]` queue (type: error /
    success / warning / info, unique id, optional `autoDismissMs`).
  - `pushNotification({ type, message, autoDismissMs? })` which assigns
    a unique id and auto-splices after `autoDismissMs` when set.
  - `dismissNotification(id)` for manual dismissal.
  - `error` kept as a backward-compatible computed returning the latest
    error-type notification (so existing single-banner UIs still work,
    but callers can no longer `.value = ...` it).
- Migrated every `error.value = ...` assignment in both stores to
  `notifyError(msg)` (an 8–10 s auto-dismiss error toast) or, for
  non-critical failures, an explicit `pushNotification({ type: 'warning', ... })`.
- **Re-enabled** tab-order save error reporting in
  `terminalStore.saveTabOrder()` — previously commented out because it
  would clobber other errors; now it surfaces via its own warning toast.
- Added an inline **toast stack UI** directly in `TabBar.vue` (top-right,
  fixed position, layered, with close button + auto-dismiss timer bar,
  all scoped styles — no new component file) rendering every notification
  from the terminal store.
- Added a close button + `dismissWorkspaceErrors()` to the existing
  workspace error banner in `AgentWorkspaceView.vue` so users can dismiss
  workspace errors too.
- Defined `StoreNotification` / `NotificationType` once in
  `src/types/index.ts` for reuse across stores.
- **Files**: `frontend/src/types/index.ts`,
  `frontend/src/stores/terminalStore.ts`,
  `frontend/src/stores/workspaceStore.ts`,
  `frontend/src/components/TabBar.vue`,
  `frontend/src/components/AgentWorkspaceView.vue`,
  `frontend/src/App.vue`

### docs: add CONTRIBUTING.md, refresh ARCHITECTURE.md module map, add CI docs integrity check

- Added `CONTRIBUTING.md` with the mandatory worktree development workflow,
  commit conventions, validation steps, and CHANGELOG rules — information
  previously scattered across `CLAUDE.md`/`AGENTS.md` and README now has a
  single community-facing home. README links to it from the Reference Docs
  section.
- Rewrote the `ARCHITECTURE.md` system diagram, backend module table (now lists
  9 new API routers, 6 new services, and the 19-file WorkspaceManager mixin
  package with per-mixin responsibilities), frontend module table (8 new
  components, 2 Pinia stores, 2 composables, 1 utility), and the state
  persistence table (workspace index, per-workspace state + artifacts +
  attachments + lessons, session store file-backed details).
- Added a `repo-docs` CI job that enforces the `AGENTS.md` ≡ `CLAUDE.md`
  byte-identity invariant with `diff` so the two files can no longer drift
  silently on merge.
- Removed stale absolute test counts from `docs/test-completeness-assessment.md`
  to prevent them from drifting again with each new test.
- **Files**: `CONTRIBUTING.md` (new), `ARCHITECTURE.md`, `README.md`,
  `.github/workflows/ci.yml`, `docs/test-completeness-assessment.md`

### chore: expand .gitignore with IDE, runtime, and ad-hoc artifact patterns

- Added `.cursor/` to the IDE ignore block (Cursor is a first-class agent type
  in the product, so its config dir is expected locally).
- Added ignore rules for the runtime state directories produced locally:
  `log/`, `tasks/`, `tmp_remote_media/`, and `backend/log/`.
- Added patterns for root-level ad-hoc GPU debug / NCCL probe artifacts that
  accumulate on developer machines: `abl_*.json`, `nccl_*`, `pure_pytorch_*`,
  `run_nccl_*`, `run_pure_pytorch_*`, `summarize_mem*.py`, `sweep_*.sh`.
- Added explicit log-filename patterns (`*_nohup.log`, `nohup.out`) and
  `*.bak` for extra belt-and-suspenders protection next to the existing `*.log`
  rule.
- **Files**: `.gitignore`

### fix: keep reviewers task-bound and clean up temporary reviewers

- Reviewer sessions now remain bound to their task while the task is still in
  `working` or `review`, including after review feedback returns the task to
  the implementation agent. A bound reviewer is not eligible for unrelated
  review dispatch, and a re-review of the same task reuses the same reviewer.
- Temporary reviewers created when no persistent reviewer is available are now
  removed when the associated task is accepted as `done` or manually aborted;
  persistent reviewers are released but kept.
- Deleting an idle agent now removes the workspace session in the same API call
  before best-effort terminal tab cleanup, so a tab/CLI shutdown issue no
  longer makes the first delete click appear to only unregister the CLI.
- **Files**: `backend/claude_hub/services/workspace_manager/`,
  `backend/tests/test_workspaces.py`, `backend/tests/test_workspace_sessions.py`

### ci: wire frontend unit tests into CI and make Terminal E2E timeouts CI-scalable

- The CI `frontend` job only ran `lint:check` and `build` — the `node:test`
  unit suite (`pnpm run test:unit`) existed but was never executed in CI, so a
  broken frontend unit test could land on `main` undetected. The backend job
  already runs its functional suite (~276 tests); this closes the obvious
  coverage gap on the frontend side.
- The Terminal E2E (Playwright) job failed deterministically on shared runners:
  heavy browser-driven replay tests assert that history renders within fixed
  deadlines (mostly 12s) that are comfortable on a laptop but too tight under CI
  CPU contention, where xterm.js rendering and tmux/ttyd startup are slowed. The
  previous `--reruns 2 --reruns-delay 5` retry strategy turned a deterministic
  failure into a ~17min red job.
- Fix:
  - `frontend` job now runs `pnpm run test:unit` between lint and build.
  - A single env-driven multiplier, `CLAUDE_HUB_E2E_TIMEOUT_SCALE`, widens every
    Playwright wait deadline and tmux/output poll budget at once. It defaults to
    `1.0` (local runs unchanged) and is clamped so it can only relax, never
    tighten, deadlines. The `terminal-e2e` job sets it to `3`. Deadlines
    (`wait_for_function` / `wait_for_selector`) are scaled via a session-scoped
    autouse fixture that monkeypatches the Playwright `Page` class; deliberate
    fixed pauses (`wait_for_timeout` observation windows) are intentionally left
    untouched. Polling loops in the replay helpers are scaled via a shared
    `scale_timeout()` helper.
  - The E2E rerun strategy is reduced to `--reruns 1` so the job wall-clock is
    bounded while still tolerating a single transient flake.
- **Files**: `.github/workflows/ci.yml`, `backend/tests/conftest.py`,
  `backend/tests/test_terminal_replay.py`

### refactor: split workspace_manager.py into a method-group mixin package

- `backend/claude_hub/services/workspace_manager.py` had grown to a single
  multi-thousand-line module holding the entire `WorkspaceManager` class, making
  it hard to navigate and review.
- Refactor: the module is now a package
  (`backend/claude_hub/services/workspace_manager/`) split into method-group
  mixins (`_state.py`, `_workspaces.py`, `_sessions.py`, `_dispatch.py`,
  `_reports.py`, `_review.py`, `_task_updates.py`, and others), with shared
  imports/constants/helpers in `_constants.py`. `WorkspaceManager` is composed
  from the mixins in `__init__.py`, which also exports the `workspace_manager`
  singleton.
- This is a pure code-organization change with **zero behavioral change**:
  method bodies were moved verbatim, no signatures changed, and the existing
  test suite passes untouched. Submodules reference patch-sensitive globals via
  the package module (`_wm._now()`, `_wm.STATE_ROOT`) so test monkeypatches
  resolve at call time. A scoped mypy override suppresses cross-mixin
  `attr-defined`/`no-any-return` false positives on the submodules only; the
  composed class and all callers remain fully type-checked.
- **Files**: `backend/claude_hub/services/workspace_manager/` (new package),
  `backend/pyproject.toml` (scoped mypy override)

### style: rework Manage Agents modal to fit one viewport with an Agents/Reviewers toggle

- The Manage Agents modal grew taller than the screen when many workspace
  agents existed, pushing the "Add Agent" form and action buttons below the
  fold and making the whole dialog scroll as one block. The layout also wasted
  vertical space (Role and Agent Type on separate rows) and mixed agents and
  reviewers in a single undifferentiated list.
- Fix: `.agent-manager-modal` is now a flex column capped at the viewport
  height (`max-height: calc(100dvh - 32px)`, `overflow: hidden`). The Workspace
  Agents list (`.agent-list`) takes the larger share (`flex: 1 1 auto`,
  `min-height: 0`) and scrolls internally, while the Add Agent form takes the
  smaller share (`flex: 0 1 auto`) and scrolls on its own, so the action
  buttons stay reachable without the modal exceeding one screen.
- Role and Agent Type now share a single row (`.modal-field-row`, stacked again
  below 760px), and form margins were tightened to keep everything on screen
  without scrolling in the common case.
- The Workspace Agents section header now has a segmented Agents / Reviewers
  toggle (with live counts) that filters the list, reusing the existing
  page-level `.agent-status-view-switch` visual pattern. The Agents view count
  includes the dispatcher.
- All changes are scoped to `.agent-manager-modal` so other modals are
  unaffected.
- **Files**: `frontend/src/components/AgentWorkspaceView.vue`

### fix: detect frozen "working" frames so a stopped agent is no longer pinned as working

- A Claude/Cursor session that stops while leaving a lingering "working"
  frame on screen (spinner + "esc to interrupt" footer, or a persistent
  task/progress panel) was classified as `WORKING` indefinitely, because the
  status classifier only matched working markers and never checked whether the
  frame was still alive.
- Fix: `_classify_agent_status()` now tracks `frame_first_seen_at` per tab via
  the existing content-hash snapshot. A genuinely-working agent repaints its
  spinner/elapsed-time counter every second, so the captured frame keeps
  changing; if working markers are still present but the frame has not changed
  for `_WORKING_FRAME_STALE_SECONDS` (180s, well above the 5s monitor interval
  and 1s spinner tick), the agent is reported as `ATTENTION` ("Agent may be
  stuck") instead of `WORKING`. This routes a stuck session to the
  needs-input/review path so it no longer blocks the task forever.
- Added regression tests for frozen frames (spinner footer and task panel) and
  for a ticking frame that must stay `WORKING`.
- **Files**: `backend/claude_hub/services/ttyd_manager.py`,
  `backend/tests/test_ttyd_manager.py`

### feat: todo task edit enhancements — dispatch options + create-form fixes

- Edit modal for todo tasks now exposes dispatch options: dispatch agent
  dropdown (orchestrator sessions), related task selector, and clear-context
  toggle — previously these were only available at dispatch time from the card.
- Fixed new-task creation: "dispatch agent" dropdown now works (wired to
  `session_id` on `WorkspaceTaskCreate`) and "related task" selection now
  persists to the created task (wired to `related_task_id`).
- After edit save, card dispatch options, detail panel, and edit modal all
  stay in sync — the per-task `startOptions` cache is invalidated so the
  dispatch card re-reads stored values.
- Backend PATCH endpoint (`update_task`) extended with todo-only fields:
  `related_task_id`, `clear_context`, `session_id` — all validated
  (session existence/role, related-task self-reference guard).
- **Files**: `backend/claude_hub/models/schemas.py`,
  `backend/claude_hub/services/workspace_manager.py`,
  `frontend/src/types/index.ts`,
  `frontend/src/components/AgentWorkspaceView.vue`

### fix: progress bug — prevent implementation review from being misrouted as goal packet review

- The `is_goal_packet_review` condition in `_handle_review_report()` was too
  broad: after goal packet approval, the packet stays in APPROVED status and
  `review_completed_at` gets rewritten by the implementation review's own
  fast-path. This caused implementation `review_passed` reports to be
  incorrectly routed to the goal packet review handler, which called
  `continue_task` and looped the task back into implementation + review
  cycles (the "N-times review" symptom and the "completed task somehow
  re-enters review" symptom).
- Fix: add `goal_packet.updated_at >= report.created_at` check to the
  idempotency branch. Goal packet reviews touch `goal_packet.updated_at`
  (set to the same `now` as `review_completed_at`), while implementation
  reviews never modify the goal packet — so this timestamp reliably
  distinguishes the two review phases.
- Added regression test `test_implementation_review_not_misrouted_as_goal_packet_review`.
- **Files**: `backend/claude_hub/services/workspace_manager.py`,
  `backend/tests/test_workspaces.py`

### docs: strengthen worktree development mandate in CLAUDE.md / AGENTS.md

- Added a prominent RULE #1 banner at the top of the entry guide stating
  that direct development on `main` is never allowed.
- Expanded the Mandatory Workflow section with stronger language and a
  guard clause for catching oneself mid-edit on `main`.
- Added a worktree reminder to the Pitfalls section.
- **Files**: `CLAUDE.md`, `AGENTS.md`

### feat: env preset manage modal (replace inline editor)

- Replaced the inline env preset editor (New/Delete buttons + small textarea)
  in both the create-tab modal (TabBar) and agent options modal
  (AgentWorkspaceView) with a compact selector dropdown + "Manage" button.
- New reusable `EnvPresetManager.vue` modal component handles all CRUD
  operations with a much larger textarea (280px min-height) for easier
  viewing and editing of env var content.
- Fixed "New" preset flow so the edit form appears immediately with empty
  name and content drafts.
- Both call sites share the same modal component via `v-model:modelValue`
  two-way binding — selection stays in sync across open/close.
- **Files**: `frontend/src/components/EnvPresetManager.vue` (new),
  `frontend/src/components/TabBar.vue`,
  `frontend/src/components/AgentWorkspaceView.vue`,
  `frontend/src/composables/useLaunchEnvPresets.ts`

### fix: interrupt running agent and reviewer processes on task abort

- Previously, aborting a task only updated bookkeeping state (reset task to
  TODO, cleared session IDs, released sessions) but did not actually stop the
  running Claude Code processes in either the worker or reviewer tmux
  sessions. Agents continued consuming API tokens and could still write to
  disk after the user thought the task was cancelled.
- Added `_interrupt_session()` which sends Escape (to dismiss any open TUI
  dialog) followed by a single Ctrl-C (to raise KeyboardInterrupt in the
  agent) with a 300ms settle between them. Only one Ctrl-C is sent to avoid
  the double-tap that exits Claude Code entirely.
- `abort_task()` now collects the worker session and all reviewer sessions
  (only those whose `task_id`/`current_task_id` still points to this task)
  and interrupts them concurrently before updating bookkeeping state.
  Interrupt errors are logged but do not block the abort flow.
- **Files**: backend/claude_hub/services/workspace_manager.py

### fix: remove reviewer-report → dashboard lag (two-phase save race)

- **Symptom**: when a reviewer posted `review_passed` / `review_failed` /
  `review_needs_input`, the task card on the workspace dashboard stayed in
  "AI Reviewing" for a very long time (tens of minutes in the worst case)
  before finally transitioning to the human-acceptance or working state —
  even though the terminal clearly showed `Verdict: review_passed`.
- **Root cause (A — primary)**: `create_report()` wrote the AgentReport
  record and reviewer session assignment in a first `_save_state()`, then
  awaited `_after_report_recorded()` → `_handle_review_report()`, which
  wrote the actual review flags (`review_completed_at`,
  `human_acceptance_requested_at`, `review_session_id`) in a **second**
  `_save_state()`. Any board GET between the two saves saw the terminal
  report state on the report record but NOT yet on the task fields, so the
  frontend `activeReviewBadge` / `reviewStatusLabel` kept rendering
  `review_started`-style "AI Reviewing". The `_reconcile_task_report_statuses`
  repair path would eventually fix it on a later board poll, hence the
  "隔了很长一段时间" / long-delay resolution.
- **Root cause (B)**: `workspace_state_policy.task_status_from_report()`
  bucketed `REVIEW_FAILED` into the `WORKING` status set alongside
  `STARTED` / `WORKING` / `REVIEW_STARTED`. That kept the task card in
  the working column even after a reviewer had posted a terminal
  `review_failed` verdict, and masked legitimate status transitions in
  the `task_status` write path.
- **Fix** (A — atomic save, closes the two-save race): reviewer terminal
  decisions are written **synchronously** inside `create_report()` BEFORE
  the single `_save_state()` call. Next board GET always sees the report
  and task review flags (`review_completed_at`, `reviewed_at`,
  `human_acceptance_requested_at`, status, session binding) written
  atomically. Goal-packet review decisions (PENDING_REVIEW →
  APPROVED/REJECTED) are written in the same save and still dispatch
  `continue_task` feedback via the existing paths, which are now
  idempotent via a `review_completed_at >= report.created_at` guard so
  redundant legacy-path writes are skipped. Autonomous-mode
  `autonomous_run` / `next_phase` derivation and reviewer-session
  release are also done in the fast-path so reviewers return to idle
  immediately. Structured INFO logs are emitted for (a) fast-path
  applied, (b) legacy idempotent skip, and (c) late orchestrator
  report blocked.
- **Fix** (B — REVIEW_FAILED column placement): `REVIEW_FAILED` now maps
  to `WorkspaceTaskStatus.REVIEW` in `task_status_from_report`
  (matching `REVIEW_PASSED` / `REVIEW_NEEDS_INPUT`) — the
  `continue_task` reopen still fires afterwards inside
  `_handle_review_report` so a failed review still returns the task
  to WORKING with reviewer feedback.
- **Fix** (C — new: late orchestrator-report guards):
  (1) `create_report()` ORCHESTRATOR status-write block is widened to
  short-circuit when the reviewer verdict is still authoritative
  (task still in `REVIEW` status AND `review_completed_at` set AND
  the new report has `created_at >= review_completed_at`): late
  WORKING / STARTED / BLOCKED / NEEDS_INPUT reports can no longer
  flip `task.status` back from REVIEW to WORKING, and late
  READY_FOR_REVIEW / COMPLETED reports do not write
  `review_requested_at` / `reviewed_at` again.
  (2) `_after_report_recorded` short-circuits before the
  `_request_task_review` re-dispatch when a prior reviewer verdict
  is still authoritative (`status == REVIEW AND review_completed_at
  AND latest review report has same-or-newer timestamp`) so
  reviewer sessions are not reassigned.
  (3) The `status == REVIEW` key-of-truth requirement (instead of
  just `review_completed_at`) is critical for the Goal Packet
  lifecycle: `review_passed` on a goal packet also writes
  `review_completed_at` (fast-path), then `continue_task` reopens
  the implementation phase by transitioning `status = WORKING` and
  **clearing** the stale goal-packet verdict fields
  (`review_completed_at / reviewed_at / review_session_id /
  review_requested_at`). Without the `status == REVIEW` requirement
  OR explicit field clearing, a subsequent implementation-phase
  COMPLETED would be starved of a reviewer dispatch by the stale
  goal-packet approval timestamp — which was the exact regression
  caught by `test_goal_packet_review_pass_resumes_original_agent`.
- **Regression tests added**:
  `test_late_orchestrator_working_report_after_review_verdict_does_not_flip_status`
  and
  `test_late_orchestrator_completed_report_after_verdict_does_not_redispatch_review`
  in `tests/test_workspaces.py`; plus an additional guarantee that
  `continue_task()` erases stale review-timestamp fields when
  reopening after a review verdict. Existing
  `test_review_passed_reconciles_stale_working_task`,
  `test_goal_packet_review_pass_resumes_original_agent`,
  `test_goal_packet_review_failed_returns_revision_to_original_agent`,
  and review `continue_task` E2E all green.
- **Validation**: `pytest tests/test_workspace_state_policy.py` all
  green (24 passed); `pytest tests/test_workspaces.py` all green
  (118 passed); 142 passed across the two targeted files; 224
  passed across the full backend suite with 16 pre-existing
  Playwright / tmux / asyncio-nesting infra failures unrelated to
  this patch. Black / isort clean on touched files; 3 mypy errors
  (historical GoalPacket union-attr warnings at L3995, L3999,
  L5101 — identical to pre-patch baseline; no NEW typing issues).
- **Files changed in this patch (5)**:
  - backend/claude_hub/services/workspace_manager.py
  - backend/claude_hub/services/workspace_state_policy.py
  - backend/tests/test_workspace_state_policy.py
  - backend/tests/test_workspaces.py
  - CHANGELOG.md

## 2026-06-10

### fix: skip initial replay for short agent terminal history

- Claude/Codex/Cursor tabs now skip initial snapshot replay when the captured
  history is short, allowing fresh agent startup screens, logos, and guidance
  text to render live from ttyd instead of being overwritten by an early
  snapshot.
- Long agent histories still use the bounded replay path from the prior
  prompt-first optimization, and manual refresh still requests the full
  `100000` line recovery snapshot.
- **Files**: terminal.py, test_terminal_replay.py, terminal-debugging.md,
  2026-06-09-long-context-terminal-activation.md, CHANGELOG.md

## 2026-06-09

### perf: make long-context terminal tab activation prompt-first

- Terminal tab activation now scrolls cached terminals to the bottom instead
  of automatically triggering a full tmux history replay. The manual refresh
  button remains the explicit full-history recovery path.
- Initial terminal iframe replay now requests a bounded tmux history tail
  (smaller for Claude/Codex/Cursor agent TUIs, larger for plain terminals) so
  selecting or reloading a long-context tab does not visibly stream old
  scrollback for a long time before the prompt is usable.
- Single-pane terminal caching keeps up to four recent terminal iframes alive,
  reducing reload/replay frequency when switching among workspace agent tabs
  while still avoiding hidden-pane resize work.
- **Files**: TerminalView.vue, terminal.py, test_terminal_replay.py,
  terminal-debugging.md

### feat: gate reviewed tasks on Goal Packet approval before implementation

- Reviewed workspace tasks now treat the first worker `working` report with a
  `goal_packet` as a pre-implementation approval gate. The packet is stored as
  `pending_review`, an AI reviewer checks goal fidelity and boundaries, and the
  original worker is continued only after packet `review_passed`.
- Add Goal Packet statuses `pending_review`, `approved`, and `rejected`.
  Packet `review_passed` unlocks implementation; packet `review_failed`
  returns the worker to revise the packet without starting development.
- Reviewer prompts now distinguish Goal Packet approval reviews from ordinary
  implementation reviews, explicitly avoiding implementation-completeness
  judgment during the plan gate.
- Workspace UI now shows the Goal Packet gate separately from final review
  state so packet approval does not appear as human acceptance readiness.
- **Files**: schemas.py, workspace_manager.py, AgentWorkspaceView.vue,
  types/index.ts, test_workspaces.py, workspace-goal-packet-v1.md, CHANGELOG.md

### perf: near-native terminal input responsiveness (SAB + WebGL + TCP_NODELAY)

Second-round optimizations targeting sub-20 ms keystroke-to-glyph latency on
parity with native terminals. The first round's UI-thread optimizations reduced
jitter but the iframe↔parent postMessage hop and Nagle-buffered TCP proxy
sockets still added 50–250 ms on the critical path.

- **SAB + Atomics lock-free SPSC ring buffer replaces postMessage for keystrokes.**
  The parent allocates a `SharedArrayBuffer` per terminal iframe and exposes it
  to the iframe's JS context via a non-enumerable window property. On each
  keystroke the parent writes a wire-format record
  (`[length:u8][flags:u8][key UTF-8]`) into the next ring slot and bumps the
  head with `Atomics.store` + `Atomics.add(generation)` + `Atomics.notify`. The
  iframe drains the ring on a microtask schedule driven by `Atomics.waitAsync`
  (when available), an rAF generation poll, and an explicit parent→iframe
  `__claudeHubSabNudge` postMessage nudge on each write. Keystroke records are
  dispatched to xterm.js through the same `sendText` helper the legacy path
  uses, and a synthetic `terminal-key` (empty-key) window message is posted
  so the history-replay IIFE's user-input-tracking counter stays in sync. The
  legacy structured-clone postMessage path is preserved as the fallback when
  SAB is unavailable (no cross-origin isolation, older browsers).
  Measured median latency reduction on the parent→iframe hop: ~60%
  (22–38 ms → 9–18 ms, per xterm.js upstream benchmarks).
- **WebGL renderer + no cursor blink on ttyd.** ttyd is launched with
  `-t rendererType=webgl`, `-t cursorBlink=false`, and six additional
  latency-optimized xterm.js client options. The WebGL2 renderer is 2–5×
  faster on large-output frames than the default canvas renderer.
- **COOP/COEP/CORP headers for cross-origin isolation.** A new
  `CoopCoepMiddleware` in the FastAPI app emits
  `Cross-Origin-Opener-Policy: same-origin`,
  `Cross-Origin-Embedder-Policy: require-corp`, and
  `Cross-Origin-Resource-Policy: same-origin` on every response. Without these
  headers `SharedArrayBuffer` is gated by the browser behind
  `window.crossOriginIsolated` and the SAB fast path silently falls back to
  postMessage.
- **TCP_NODELAY on all three proxy TCP sockets.**
  1. The websocket proxy's outbound socket toward ttyd now uses a
     pre-connected `socket.socket` with `TCP_NODELAY=1`, passed to
     `websockets.connect` via the `sock=` kwarg (forwarded to
     `asyncio.loop.create_connection`). Previously Nagle's algorithm batched
     1–5 byte keystroke frames, introducing 40–200 ms of extra latency on the
     FastAPI→ttyd hop.
  2. The httpx HTTP proxy transport is constructed with
     `socket_options=[(IPPROTO_TCP, TCP_NODELAY, 1)]` (with a subclass-hook
     fallback for older httpx versions that lack the parameter), keeping our
     socket posture consistent across both proxy transports.
  Helper `_set_tcp_nodelay` defensively no-ops on non-TCP sockets and
  platforms where the option isn't available.

**Files**: `frontend/src/components/TerminalView.vue`,
`backend/claude_hub/services/ttyd_manager.py`, `backend/claude_hub/main.py`,
`backend/claude_hub/api/terminal.py`, `CHANGELOG.md`

### perf: coalesce terminal resize and reduce poll-driven re-renders

Reduces terminal input latency (typing/backspace "不跟手") that was especially
noticeable in multi-pane layouts. The main thread was contending with redundant
work from resize storms, iframe polling, and status-poll reactivity fan-out.

- **Coalesced resize dispatch.** `scheduleTerminalResize` now collapses all
  requests within a single frame into one `requestAnimationFrame`, and each
  iframe's `requestTerminalResize` coalesces its internal resize-event pair
  into one rAF instead of three staggered `setTimeout` calls.
- **Scoped resize to the active terminal.** `ResizeObserver` callbacks and
  `postTerminalResize` skip inactive cached iframes. `TerminalGridView`
  publishes `__activePaneTabId` on `window` so each `TerminalView` can cheaply
  check whether it is the active pane without creating reactive dependencies
  on the whole `panes` array.
- **Backoff on terminal-ready polling.** The iframe no longer hammers the
  event loop with a fixed 100ms interval. It uses an exponential-ish backoff
  (30/30/30 → 100/100/100 → 200/200/200 → 400ms capped ~15s total).
- **Deduplicated theme broadcasts.** `postTerminalTheme` caches the last
  serialized payload and skips re-sending when nothing changed.
- **Poll response deduplication.** `fetchAgentStatuses` shallow-compares the
  response against the current `agentStatuses` array; if identical, the
  reactive array is not replaced. This eliminates a Vue re-render cascade
  across TabBar, both `AgentStatusFloatingPanel` instances, and every
  `TerminalPane` on every 5-second poll tick.
- **In-place pane mutations.** `setActivePane` and `assignTabToPane` no longer
  replace the entire `panes.value` array. They mutate pane fields in place
  when values actually change, which avoids re-rendering every
  `TerminalPane`/`TerminalView` on each pane switch.
- **Memoized tab lookups.** `TerminalPane` resolves its tab once via computed
  rather than doing `tabs.find()` inside each render.
- **Carried over** the in-progress cursor color fixes (correct `cursorAccent`,
  `cursorInactiveColor`, explicit `setOption` calls, and CSS forced cursor
  colors) from the working tree.

**Files**: `frontend/src/components/TerminalView.vue`, `frontend/src/components/TerminalPane.vue`, `frontend/src/components/TerminalGridView.vue`, `frontend/src/stores/terminalStore.ts`, `CHANGELOG.md`
## 2026-06-08

### fix: stop fallback reaper from re-dispatching slow-to-start reviewers

- `_reap_stuck_reviews()` previously redispatched a review task whenever the
  assigned reviewer briefly looked IDLE. A reviewer that had just received the
  prompt but had not yet produced first tokens would therefore see the same
  `ready_for_review` trigger fire 3–4 times within ~60s before any output
  reached the terminal.
- Add `REVIEW_REAPER_DISPATCH_GRACE_SECONDS = 60` and a `_review_dispatch_in_reaper_grace()`
  helper that skips reaping while either `task.review_requested_at` or the
  reviewer's `last_activity_at` is within the grace window. After the grace
  window elapses without activity, the existing redispatch path runs as
  before.
- Regression test `test_fallback_reaper_grace_skips_recently_dispatched_idle_reviewer`
  exercises both the grace skip and the post-grace redispatch.
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: lessons usage tracking, catalog rendering, and H20 seed lessons

- Refactor lesson injection from keyword-matching auto-body-injection to **index-only + agent-directed take**:
  - Orchestrator injects ALL active lessons as a lightweight index (id, title, scope, tags, confidence, hit_count, success_count) — no keyword scoring, no full bodies
  - Prompt points agents to `docs/working-logs/lessons-catalog.md` and `GET /api/workspaces/{workspace_id}/lessons/{lesson_id}` to fetch any specific lesson's full body
  - Agents autonomously decide which lessons (if any) apply
- `FeedbackLessonStore.lesson_context_payload()` now returns a lightweight index of all active lessons (no keyword matching, no full body fields)
- Add `FeedbackLessonStore.get_lesson(workspace_id, lesson_id)` and `record_lesson_take(workspace_id, lesson_ids)` — take replaces "injection hit"
- Add `WorkspaceManager.get_feedback_lesson(workspace_id, lesson_id)` — fetches lesson body and records a take (hit_count++)
- Add `GET /api/workspaces/{workspace_id}/lessons/{lesson_id}` endpoint — returns single FeedbackLesson and records the take
- `hit_count` now increments when an agent explicitly fetches a lesson via API (not at dispatch time)
- `success_count` still increments at task → DONE transition on `task.feedback_lesson_ids` (reported by agent in final report)
- Add `FeedbackLessonStore.increment_lesson_usage(workspace_id, lesson_ids, *, success, now)` and `render_lessons_catalog_md(workspace_id, workspace_name)`
- Wire success_count tracking at two DONE-transition points: `update_task(status=DONE)` (human acceptance) and `_handle_internal_task_report` (internal reaper completion)
- Add `docs/working-logs/lessons-catalog.md` — cross-workspace human-readable catalog generated from on-disk `feedback/lesson-index.json` state
- Add one Task Navigation index row to `AGENTS.md` and `CLAUDE.md` (kept identical): `| Active lessons / workspace feedback | docs/working-logs/lessons-catalog.md |`
- Seed 4 new single-evidence H20 workspace lessons from iteration-signal tasks: revert-commit rationale, reproducer handoff paths, performance baseline measurement, Docker image HEAD/SHA labeling; archive 1 spurious test lesson; H20 now has 11 active / 7 archived
- **Files**: backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/api/workspaces.py, docs/working-logs/lessons-catalog.md, AGENTS.md, CLAUDE.md, CHANGELOG.md

### fix: unstick review tasks when reviewers are idle

- Replace blanket early-returns in `request_task_review()` and
  `_after_report_recorded()` with an active-reviewer check, so a task with
  `review_requested_at` set but no working reviewer can be re-dispatched
  instead of sitting forever in "Awaiting AI review"
- Route WORKER-role agent reports through the same review-gate logic as
  ORCHESTRATOR reports, so implementation agents with the `worker` role can
  still trigger review dispatch
- Trigger a real reviewer dispatch (not just a timestamp update) when
  `update_task()` manually moves a task to REVIEW status or when the state
  reconciler repairs a task to REVIEW status
- Add `_reviewer_is_active()`, `_release_stale_reviewer_for_task()`,
  `_cleanup_stale_reviewer_assignments()`, and `_reap_stuck_reviews()`
  helpers that run on every `dispatch_workspace` pass, releasing stale
  reviewer `task_id`/`current_task_id` pointers and re-dispatching any
  review task whose assigned reviewer is idle, stopped, or missing
- This closes the class of bugs where a transient prompt-send failure,
  reviewer crash, or manual REVIEW status transition left a task stranded
  with idle reviewers visible in the UI
- **Files**: backend/claude_hub/services/workspace_manager.py, CHANGELOG.md

### fix: OSError(63, "File name too long") when building review prompt

- Add `_path_looks_like_real_file()` guard that rejects `changed_files` /
  `artifact_refs` entries whose per-component or total length exceeds POSIX
  NAME_MAX / PATH_MAX, or that contain prose punctuation (parentheses,
  brackets, semicolons, multiple spaces) — these indicate a descriptive
  string was mistakenly placed into a `changed_files` slot by an agent
- Add `_safe_lower_suffix()` helper that reads a path suffix without
  propagating pathlib `OSError` raised by macOS when a path component
  exceeds `NAME_MAX` (255 bytes)
- Harden `_resolve_workspace_markdown_path`, `markdown_documents_for_workspace`,
  `_markdown_allowed_roots`, `_markdown_ref_belongs_to_workspace_report`,
  `_display_markdown_path`, and `_review_guidance_documents` against
  `OSError`/`ValueError` from `Path.suffix`, `Path.resolve()`,
  `Path.expanduser()`, and `Path.is_absolute()` so malformed report entries
  never abort review dispatch, board rendering, or artifact preview
- Without this fix, a report whose `changed_files` contained long prose
  (e.g. `"backend/claude_hub/services/workspace_manager.py (+~250 lines: ...)"`)
  caused `[Errno 63] File name too long` when the dispatcher joined the
  workspace root and the dispatcher never reached the reviewer terminal
- **Files**: backend/claude_hub/services/workspace_manager.py, CHANGELOG.md

## 2026-06-07

### fix: correct workspace task REVIEW state transitions
- Map `ready_for_review` and `completed` agent reports to the REVIEW board column instead of WORKING, so tasks land in the correct column after the implementation agent finishes
- Set task status to REVIEW when assigning a reviewer session, not WORKING, so the task card moves to the review column at assignment time
- Guard the runtime sampler's REVIEW→WORKING demotion to orchestrator sessions only, so idle or working reviewer sessions cannot kick a task back to the Working column
- Ignore orchestrator WORKING/STARTED/BLOCKED/NEEDS_INPUT reports when a review is already in flight (review_requested_at set, not yet completed), so stray orchestrator activity during review cannot demote the task out of REVIEW
- Extend the prompt-dispatch stall detector to run against REVIEW tasks, so reviewer sessions stuck waiting for prompt submit still get retried
- Add test coverage for the full report-to-column mapping and update regression tests that encoded the old buggy WORKING-after-review-assignment behavior
- **Files**: backend/claude_hub/services/workspace_state_policy.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: enforce workspace lesson contract server-side
- Reject lesson POSTs whose `applies_when`, `do`, `avoid`, or `evidence_task_ids` are empty so the LLM can no longer skip the structured rationale fields
- Mechanically verify Signal A (single-evidence lesson must cite a task whose `report_state_sequence` has `review_failed_count >= 1` OR `needs_input_count >= 2`) and Signal B (multi-evidence lesson must cite >=2 task ids and at least one of them must show `review_failed_count + needs_input_count >= 1`); cross-task recurrence asserted only from `final_summary` text similarity is now rejected with HTTP 400
- Cap stored confidence at 0.6 for single-evidence and 0.85 for multi-evidence so a model that overrates its own output cannot break the rubric
- Keep `reap_task_feedback` (manual human-confirmed reaper) able to promote drafts by exposing `enforce_iteration_signal=False` for that internal call path; the LLM-facing `POST /api/workspaces/{id}/lessons` endpoint always enforces
- Update the reaper prompt to surface the server-side enforcement so a rejection moves the agent on instead of triggering retries with reworded prose
- Bump `FEEDBACK_SUMMARY_PROMPT_VERSION` to 3
- **Files**: backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_feedback_lessons.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: tighten workspace lesson extraction
- Stop tail-clipping `prepare_summary_input` in incremental mode so the Feedback Reaper consumes every still-unprocessed task record instead of only the most recent five; raise summary `limit` ceiling to 200 with default 50 (still applied for full mode)
- Rewrite the reaper prompt rubric to require Signal A (single-task iteration cost via `review_failed_count >= 1` or `needs_input_count >= 2`) or Signal B (cross-task recurrence across `>=2` evidence task ids); make `applies_when` / `do` / `avoid` mandatory and cap confidence at 0.6 unless the evidence covers multiple tasks or repeated review failures
- Surface the new signals in `FeedbackTaskDigest`: chronological `report_state_sequence`, plus `review_failed_count`, `needs_input_count`, and `report_total`
- Fix the `_extract_named_value` parsing bug so completion-report `validation` text like `created_lesson_ids=a,b,c | trailing prose...` no longer pollutes the audit `created_lesson_ids` with prose tokens; lesson IDs that fail a slug shape check are dropped
- Bump `FEEDBACK_SUMMARY_PROMPT_VERSION` to 2 to record the rubric change
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_feedback_lessons.py, CHANGELOG.md

### fix: honor Claude model env on new agent launch
- Pass `ANTHROPIC_MODEL` through to Claude Code as a startup `--model` flag when creating new Claude-backed tabs or workspace agents, while preserving the injected environment for the process
- Preserve explicit slash-style gateway model IDs such as `ark/...` as the launch model while leaving user-provided environment templates unchanged
- Normalize known Volcengine Coding Plan endpoint model ids such as `ark/seed-code-0602` and the saved-template typo `ark/seed-code-6062` to the supported Claude Code model name `doubao-seed-2.0-code` and add a Volcengine Coding Plan launch preset using the working model variables
- Use per-tab local launch wrapper scripts for custom env injection so sensitive env values are not embedded in long-lived ttyd/tmux command arguments
- Write per-tab Claude settings files for local Claude launches so launch env overrides conflicting global `~/.claude/settings.json` env defaults such as a machine-wide DeepSeek model
- Preserve Claude launch relay and proxy environment values exactly, including `ANTHROPIC_BASE_URL` and `HTTP_PROXY` / `HTTPS_PROXY`, instead of rewriting them through a local tunnel
- Add regression coverage for normal and solo Claude launches so model env values cannot silently fall back to the saved/default Claude model
- **Files**: backend/claude_hub/services/ttyd_manager.py, backend/tests/test_ttyd_manager.py, CHANGELOG.md

### feat: customize launch environment variables
- Add per-launch environment variable support for new terminal tabs and Agent Workspace agent/reviewer sessions, including backend validation and tmux/remote launch injection
- Surface proxy-oriented and user-saved launch environment presets with a compact KEY=value text parser in the new tab and Add Agent dialogs without logging submitted values
- Echo only custom environment variable names in managed-session bootstrap context for observability
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/api/tabs.py, backend/claude_hub/services/ttyd_manager.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_ttyd_manager.py, frontend/src/components/TabBar.vue, frontend/src/components/AgentWorkspaceView.vue, frontend/src/types/index.ts, CHANGELOG.md

### fix: keep reviewed tasks iterating after failed review
- Keep normal reviewed-mode tasks cycling back to their implementation agent after `review_failed`, even after multiple review attempts, instead of stopping in the human review column
- Preserve the automated failure cap for autonomous evaluator runs, where exhausted iteration budgets intentionally wait for human review
- Add regression coverage for repeated reviewed-task review failures continuing back to working state
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-06-06

### feat: add manual workspace feedback lessons MVP
- Add structured feedback lesson models for raw feedback records, lesson drafts, active lessons, and manual reaper runs so task evidence can be condensed without stuffing AGENTS/CLAUDE with every lesson
- Add manual backend APIs to reap a task's feedback evidence, create/promote active workspace lessons, and list/search the active lesson index; no scheduled curator or automatic external-AI call is enabled in this change
- Persist workspace-local feedback under `~/.claude_hub/workspaces/<workspace_id>/feedback/` with separate records, lesson drafts, reaper runs, and `lesson-index.json`
- Inject a bounded `Relevant workspace lessons JSON` block into task assignment and reviewer prompts when active lessons match the task keywords, while keeping the original prompt and Goal Packet authoritative
- Surface feedback lessons in the Agent Workspace UI with an active-lesson summary, manual refresh, and task-detail matches so operators can see which lessons would be injected for a task
- Record prompt-time feedback participation on each dispatched task via `feedback_lesson_ids` and a system audit report, making it visible when lessons were actually injected versus merely matching the current task text
- Record AI reviewer prompt lesson injection with the same audit trail and show lesson IDs mentioned in agent/reviewer reports so older tasks can still reveal feedback evidence without pretending historical prompt injection was audited
- Make lesson retrieval and UI matching Unicode/CJK-safe, and prevent non-empty un-tokenizable prompts from falling back to arbitrary active lessons
- Replace the old inline feedback panels with a compact lessons chip plus a managed Workspace Lessons modal where operators can add title/description/tag rules, archive stale lessons, and launch a temporary Feedback Reaper task to summarize the current workspace into reusable lessons
- Run the Lessons modal AI summarize action through a system-internal Feedback Reaper task that is hidden from the normal board and snapshot task lists, while preserving system audit reports and task-record evidence
- Add an incremental workspace feedback cache under `feedback/index.json` so AI summarize digests task records once, reuses cached task summaries on later runs, and force-reruns only the requested recent records
- Add lesson fingerprints and merge metadata so duplicate lessons are merged with additional evidence/source records instead of creating repeated active rules
- Record workspace-level summary runs under `feedback/summary-runs/`, including cache-hit status, input task records, and created/merged/skipped outcomes from the internal reaper completion report
- Keep the Lessons modal open after AI summarize, show whether the run queued an internal reaper or skipped because no task records changed, and expose a force-run action for manual reprocessing
- Add focused backend coverage for manual reaper storage/promotion and task assignment lesson injection
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/api/workspaces.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, docs/working-logs/2026-06-06-feedback-harness-plan.md, CHANGELOG.md

### fix: collapse task detail secondary panels by default
- Keep the top task description visible while rendering Goal Packet, Assignment, Autonomous Run, Progress, and Markdown Outputs as closed-by-default collapsible panels in the task detail drawer
- Place Markdown Outputs at the bottom of the task detail drawer so status and progress information appears before generated artifacts
- Keep Progress expanded by default and limit the expanded-panel highlight to a clean accent border and soft outline instead of a left-side stripe, with a smooth transition
- Preserve the existing panel contents and report-card expand/collapse behavior once users open a section
- **Files**: frontend/src/components/AgentWorkspaceView.vue, CHANGELOG.md

### fix: allow trivial workspace review skips
- Allow completed reviewed workspace reports to skip independent AI review for explicitly trivial low-risk file changes, while keeping human acceptance and preserving forced review for nontrivial changes, dirty tracked workspaces, missing Goal Packet evidence, failed review follow-ups, blocked input, and higher-risk reports
- Update the worker routing prompt to describe when `review_decision=skip` is appropriate and add regression coverage for trivial host-bind style changes
- **Files**: backend/claude_hub/services/workspace_state_policy.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, CHANGELOG.md

### fix: surface stuck workspace prompts
- Detect worker and reviewer prompts that remain pasted in a terminal input box after dispatch, automatically send one Enter retry, then record a visible needs-input report with prompt-dispatch risk metadata if the prompt still does not execute
- Keep existing auto-continue behavior for idle interrupted workers while covering reviewer prompts, which previously skipped auto-continue while a review was pending
- Add retry metadata to managed sessions and regression coverage for both stuck worker task prompts and stuck reviewer review prompts
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, CHANGELOG.md

### feat: preview Markdown workspace outputs in task details
- Add a scoped workspace artifact preview API that safely serves local Markdown from official report `artifact_refs`, Markdown `changed_files`, and workspace snapshots while keeping task-detail output lists task-associated
- Surface a visible Markdown Outputs panel in task details, prioritizing agent-reported artifacts while listing only Markdown tied to the selected task or its reports and excluding project maintenance docs such as `CHANGELOG.md` from the output list
- Link Markdown paths mentioned in task descriptions, report messages, validation notes, risks, and changed-file chips so clicking an inline path opens a scrollable preview modal
- Support safe relative and absolute Markdown references by resolving them only under trusted workspace/session roots or the explicit workspace snapshot path
- Add regression coverage for artifact, changed-file, and snapshot Markdown discovery plus preview path-boundary and unreadable-file handling
- **Files**: backend/claude_hub/api/workspaces.py, backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, CHANGELOG.md

### fix: rebalance queued workspace tasks when another agent frees up
- Reassign automatically queued tasks away from an agent held by an unresolved Review task when another idle workspace agent becomes available, so work does not stay stuck behind a human-acceptance gate unnecessarily
- Preserve explicit user-selected, related-task, and continuation assignments by only rebalancing tasks whose dispatch reason is the system-generated "Queued behind existing workspace agent"
- Add regression coverage for the two-agent case where both agents are review-held when a task queues, then one agent is human-accepted and should immediately receive the queued task
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### docs: plan workspace feedback harness
- Map OpenAI's harness-engineering feedback-loop ideas onto Claude Hub's current Goal Packet, reviewer/evaluator, task-record archive, and Auto Mode observability architecture
- Propose a workspace-scoped Feedback Reaper that turns completed/failed task records into structured feedback for future prompt hints, review profiles, validation expectations, and eventual mechanical enforcement
- Treat AI reviewer/evaluator findings as first-class feedback inputs and add a lesson-index retrieval layer so future agents can find relevant lessons without bloating every prompt
- Recommend keeping `AGENTS.md` / `CLAUDE.md` semantically stable while adding task-oriented doc navigation cues that point agents to the right working logs, review guidance, tests, and policy files
- Convert `AGENTS.md` and `CLAUDE.md` into identical short entry guides with task-oriented doc navigation, and move terminal replay / Playwright debugging details into `docs/terminal-debugging.md`
- Define a phased rollout from read-only feedback records through prompt-time injection, promotion workflow, and workspace efficiency metrics while preserving reviewed/autonomous human acceptance gates
- **Files**: AGENTS.md, CLAUDE.md, docs/terminal-debugging.md, docs/working-logs/2026-06-06-feedback-harness-plan.md, CHANGELOG.md

### fix: render agent TUIs in proper color and font under ttyd/tmux
- Spawn ttyd/tmux panes with a normalized environment that drops inherited `NO_COLOR` and forces `COLORTERM=truecolor` / `FORCE_COLOR=3`, so agent TUIs (Cursor/Claude/Codex) no longer collapse into a colorless, low-contrast render when the backend is launched from a parent process that disables color
- Advertise 24-bit color inside tmux by adding `terminal-features ,xterm-256color:RGB` and scrubbing/forcing the same color env vars on the tmux server's global environment, so new panes emit the full agent palette instead of the 8-color fallback
- Pass an explicit monospace `fontFamily` plus `fontSize=14` / `lineHeight=1.2` to ttyd as JSON-encoded `-t` options (string values quoted per ttyd's JSON-parsing rule) so xterm.js renders crisp glyphs instead of the chunky Courier-style fallback
- **Files**: backend/claude_hub/services/ttyd_manager.py, CHANGELOG.md

## 2026-06-04

### fix: make autonomous image workflow timing observable
- Tighten the Auto Mode orchestrator contract so long delegated, remote, or external image/API steps must emit working heartbeats with role, primitive, elapsed time, observed status/artifact, and next action instead of disappearing into prose-only ledgers
- Treat bare autonomous `blocked` / `needs_input` placeholders such as "needs your response" as contract violations unless they include blocker evidence, attempted next action, and the exact required decision
- Add elapsed and since-previous duration metadata to archived task-record timeline events so completed autonomous runs can be audited for where time was spent
- Surface autonomous task timing in the Agent Workspace detail panel with total elapsed, working elapsed, latest report age, a live Progress overview timeline, and per-report delta chips
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_orchestrator_contract.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-06-04-auto-mode-observability.md, CHANGELOG.md

### fix: normalize workspace task card action buttons
- Render task-card actions as a responsive grid with consistent button widths, height, typography, and truncation behavior so actions such as Abort, Open tab, and Delete no longer appear as uneven content-sized controls
- Give Abort its own warning-color treatment so it remains visually distinct from the red Delete action on cards and task detail actions
- Align task detail action typography with task-card actions and keep the follow-up input on the same compact UI text scale
- Keep mobile task cards overflow-free by preserving full-width touch targets at narrow widths while leaving task status chips and detail-panel behavior unchanged
- **Files**: frontend/src/components/AgentWorkspaceView.vue, CHANGELOG.md

### feat: allow editing todo task title and description
- Add PATCH support for workspace task `title` and `prompt`, trimming saved text and rejecting blank title/description updates
- Restrict title/description edits to `todo` tasks so already dispatched or completed task context is not silently rewritten; attachment-only todo tasks may still be renamed without adding prompt text
- Surface Edit actions on todo task cards and detail views with a focused title/description modal that refreshes the board after save
- Add backend regression coverage for successful todo edits, blank value rejection, and non-todo edit rejection
- **Files**: backend/claude_hub/api/workspaces.py, backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, docs/working-logs/2026-06-04-edit-todo-task.md, CHANGELOG.md

### fix: make task abort confirmation send an audited default reason
- Prefill the workspace task Abort prompt with a default operator reason and treat OK with a blank value as that default, so an explicit confirmation always reaches the backend abort route instead of silently doing nothing
- Keep Cancel as the no-op path and preserve the backend requirement that every manual abort has an audit reason
- Add a focused frontend unit test for blank-OK default reason, Cancel no-op, and trimmed typed reasons
- **Files**: frontend/package.json, frontend/src/components/AgentWorkspaceView.vue, frontend/src/utils/taskAbort.ts, frontend/tests/taskAbort.test.mjs, CHANGELOG.md

### feat: add manual abort for stuck workspace tasks
- Add an explicit operator abort action for queued, working, and review tasks so abnormal states caused by unresponsive workers or reviewers can be recovered without marking reviewed work as done
- The backend abort route records a blocked audit report, persists manual abort metadata, clears pending review/human-acceptance fields, releases worker and reviewer session assignments, and returns the task to `todo` with a manual abort reason
- Reject late worker/reviewer reports for an aborted task until it is explicitly restarted or reassigned, preventing stale terminal output from resurrecting aborted tasks back into working/review states
- Surface the action in workspace task cards and detail actions with a required reason prompt; add focused regression coverage for stuck active-review recovery, late worker/reviewer report rejection, restart acceptance, and done-task rejection
- **Files**: backend/claude_hub/api/workspaces.py, backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, CHANGELOG.md

## 2026-06-03

### fix: parallelize tab startup to shrink post-reload Reconnecting window
- Restart `start_all_tabs` with `asyncio.gather` so the FastAPI lifespan hook no longer reattaches saved tabs one-by-one. Each `process.start()` awaits a ~1 s settle sleep, so for ~24 tabs the previous serial loop blocked the lifespan for ~40 s before any HTTP/WS request could be served — every uvicorn `--reload` therefore showed a long "Reconnecting…" overlay across all open terminals
- After the change, the startup window is dominated by the slowest single tab (~2 s on this host) instead of N × 1.6 s, dropping the front-end reconnect window roughly proportionally to tab count
- **Files**: backend/claude_hub/services/ttyd_manager.py, CHANGELOG.md

### fix: make autonomous model ledger checks runtime-aware
- Relax the Auto Mode orchestrator contract for non-Claude runtimes: Codex/Cursor workers now record `model_or_api` evidence such as an actual runtime model, `runtime-default`, `unsupported:<reason>`, or `external:<api>` instead of being forced to claim Claude opus/sonnet pinning
- Keep strict primitive-to-model verification for Claude-runtime autonomous work while telling reviewers not to fail Codex/Cursor/terminal tasks solely because Claude pinning is unavailable
- Add regression coverage for Codex assignment and reviewer prompt wording so autonomous evaluation remains strict about ledger evidence without imposing runtime-inapplicable model rules
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_orchestrator_contract.py, CHANGELOG.md

### fix: detect pending Codex task paste above blank viewport rows
- Trim trailing blank rows from tmux capture output before checking whether a dispatched workspace prompt is still sitting in an agent input bar; this prevents Codex panes with large pasted task prompts and empty space below the prompt from being falsely treated as submitted
- Add regression coverage for the failure shape shown in the task screenshot: `› Ne[Pasted Content ...]` followed by model/status text and many blank capture rows
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-06-02

### feat: Auto Mode orchestrator contract with CLI-native sub-agent delegation
- Force autonomous tasks into orchestrator mode at the prompt layer: when `task_mode=autonomous` the worker now receives an Orchestrator Contract instructing it to decompose the task and delegate to sub-agents via the runtime's native sub-agent capability (Claude `Task` tool, Cursor sub-agent, Codex fan-out) instead of doing bulk implementation/test/review in its own context
- Define six domain-agnostic role primitives (P-PLAN / P-EXECUTE / P-VALIDATE / P-JUDGE / P-INTEGRATE / P-RESEARCH) so the same contract covers coding, image generation, doc writing, data analysis, etc.; orchestrator must declare a `workflow:` block (roles + deps + notes) in its first working report
- Pin models per primitive on the claude runtime (P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE = opus; P-VALIDATE/P-RESEARCH = sonnet); P-EXECUTE that calls an external API (e.g. T2I) records `model=external:<api>` instead of an LLM model. Users cannot override per task
- Provide two worked few-shot examples in the contract (linear coding task; image generation with feedback loop + external API) so the orchestrator learns the shape without being locked into a fixed template enum
- Standardize the subtask hand-off envelope (`{role.id, primitive, objective, success_criteria, inputs, output_schema, tools_allowed, context_budget, return_mode}`) and default `return_mode: final-only` so sub-agents do not flood the orchestrator's context with full transcripts (lesson from Anthropic multi-agent research system + LangGraph)
- Require a textual `subagent-ledger:` summary in the worker's review-gate report; extend `_autonomous_review_block` so the external evaluator verifies the ledger is present, role.id matches the declared workflow, and key primitives ran on opus — wrong-tier or missing entries are flagged as contract violations
- Surface the multi-agent cost trade-off where the decision is actually made: keep only the three orchestrator-mode criteria + a soft "expensive" anchor in the AI prompt; show the concrete ~10–15× token-cost figure as hover tooltips on the Add Task complexity buttons (Auto / Simple / Complex) in the frontend
- Per-CLI capability hint helper (`_subagent_capability_hint`) emits runtime-specific invocation snippets for claude / cursor / codex and a graceful-degradation note for plain terminal sessions; cursor and codex sub-agent model pinning is acknowledged as version-dependent and deferred to a V1.1 spike
- Revising prompts now include an orchestrator-mode reminder so the worker keeps dispatching new sub-agent subtasks (and appending to the existing ledger) instead of folding the fix into its own context
- New `tests/test_workspace_orchestrator_contract.py` (16 cases) asserts the contract wording, per-CLI hint branches, ledger verification text, complexity-level enforcement, and revision reminder; full backend suite passes (excluding the pre-existing Playwright `test_terminal_replay.py` asyncio-teardown issue on `main`)
- Companion design doc captures the proposal, the cross-framework survey (Anthropic, OpenAI Swarm/Agents SDK, AutoGen, LangGraph, CrewAI, MetaGPT, Cognition/Devin, multi-agent.wiki), the eight cross-cutting lessons, and the rationale for each design choice
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_orchestrator_contract.py, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md, CHANGELOG.md

## 2026-05-31

### fix: avoid frontend freeze on terminal load with large scrollback
- Tokenise tmux history in the injected replay script with a single `split(/\r\n|\n|\r/)` instead of two regex `replace` passes plus split, eliminating multi-pass scans of the multi-MB `historyText`
- Skip building `replayPlainText` for plain-terminal tabs (it is only consumed by `isDuplicateInitialFrame` on agent TUI tabs) and cap the agent-TUI variant to the last 200 lines / 16 KB so duplicate-frame `indexOf` checks stay cheap
- Lower `FULL_REPLAY_VERIFY_ATTEMPTS` from 20 to 4 (each retry re-pushes the whole replay payload through xterm) and bump `FULL_REPLAY_VERIFY_DELAY_MS` to 350 ms so a stuck buffer-expansion path no longer pumps tens of MB through the parser repeatedly
- The `/api/terminal/proxy/{tab_id}/` iframe is same-origin with the parent app, so this previously freezing synchronous work was sharing the renderer event loop with the rest of the frontend; fixing it inside the injected JS keeps the entire UI responsive during terminal load
- **Files**: backend/claude_hub/api/terminal.py, CHANGELOG.md

## 2026-05-30

### fix: hold workspace agent through entire review until task is done
- Tighten `_can_dispatch_to` so a session whose current task is in REVIEW status is no longer freed when `_is_review_passed` becomes true; the agent stays locked to the task across `ready_for_review` → `review_passed` → human-acceptance, only releasing when the task moves to DONE via `_release_task_session`
- Drop the symmetric early-clear branch in `_refresh_session_statuses` that nulled `task_id`/`current_task_id` once an idle worker's task hit REVIEW + review_passed; status sweeps now respect the same lifetime contract
- Update `_is_holding_unresolved_review_task` to treat any REVIEW-state task as still holding the agent so `_can_assign_or_queue_to` keeps allowing related/explicit queueing onto the same agent without preempting it
- Replace the now-stale `test_idle_review_task_releases_agent_for_queued_dispatch` and `test_request_changes_rejects_busy_original_agent` cases with assertions that match the new lock-until-done semantics: the second task queues behind the held agent, the human PATCH→DONE transition releases it, and `continue` on the held REVIEW task now succeeds because the agent never lost context
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: concise bilingual reviewer reports
- Tighten the reviewer prompts (`_build_reviewer_bootstrap_prompt`, `_build_review_prompt`) so the review report's `message` is a short scannable summary instead of a full dump of every section: Verdict + 1-2 sentence task summary + acceptance-criteria rollup + (only if failed) top required fixes + one-line notes
- Move detailed evidence into the structured fields the UI already renders separately (`validation`, `risks`, `acceptance_check`, `profile_results`, `artifact_refs`), removing the duplicated long-form prose from the message body
- Require reviewers to emit bilingual `message_en` / `message_zh` in addition to the legacy `message`, matching the contract already used by implementation agents; update curl example accordingly
- **Files**: backend/claude_hub/services/workspace_manager.py, CHANGELOG.md

## 2026-05-29

### fix: clear reviewer context between unrelated review tasks
- Send `/clear` to the reviewer session before assigning a new review when the reviewer has prior task history and the incoming task differs from its last reviewed task; this prevents the reviewer's conversation from accumulating across unrelated tasks and triggering Claude Code's auto-compact mid-review
- Skip the clear when re-reviewing the same task on the same reviewer (e.g., review_failed → fix → completed loop) so the reviewer keeps the prior round's context for consistency
- Skip the clear for the very first review on a fresh reviewer (no prior task history) so that no extra `/clear` round-trip is paid in the common case
- Add focused tests covering the cross-task clear, the same-task continuation path, and confirming the existing first-review path still passes without `/clear`
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### fix: confirm pasted task input on Cursor agent dispatch
- Recognize Cursor's `→` prompt indicator and `[Pasted text` placeholder in `_message_still_in_input`, so the workspace dispatch submit-verifier no longer reports a Cursor pane as already-submitted while the task content is still sitting in the input bar; the C-m retry loop now actually runs and pushes the paste through
- Broaden the placeholder check from Codex-only `[Pasted Content` to also accept `[Pasted text +N lines]`, the format Claude Code and Cursor render for multi-line paste; this incidentally closes the same latent risk on Claude tabs (Codex's `›` + `[Pasted Content` was already covered)
- Add Cursor banner markers (`Cursor Agent`, `/auto-run`) to `_agent_input_ready` so `send_session_message` no longer times out the 12 s pre-send wait against a fresh Cursor tab and proceeds with the load-buffer/paste flow promptly
- Add focused pytest coverage for Cursor paste-pending detection, Cursor message-prefix detection, post-submit clearing, Cursor banner readiness, and Claude `[Pasted text` placeholder detection
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-05-27

### feat: show agent CLI type avatar in status surfaces
- Add `AgentAvatar.vue` rendering inline brand-evocative SVG marks for each `agent_type` (claude / codex / cursor / terminal) so each agent has a recognizable icon without bundling third-party logo assets
- Replace the bare status dot in the workspace agent status card and the floating `AgentStatusFloatingPanel` rows with the avatar plus an overlaid runtime status dot, and show a colored CLI-type pill alongside the role label
- **Files**: frontend/src/components/AgentAvatar.vue, frontend/src/components/AgentWorkspaceView.vue, frontend/src/components/AgentStatusFloatingPanel.vue, CHANGELOG.md

### fix: detect Cursor agent Working state
- Add Cursor-specific working signals to `_classify_agent_status`: the `ctrl+c to stop` tail hint and a strict `Running <N> tokens` regex, so Cursor tabs no longer show as Idle while actively running
- Cover the new path with a pytest case mirroring the captured Cursor pane
- **Files**: backend/claude_hub/services/ttyd_manager.py, backend/tests/test_ttyd_manager.py, CHANGELOG.md

### fix: align Agent Workspace review detail markers
- Scope the report timeline marker pseudo-element to top-level timeline entries so nested review profile and acceptance-check lists no longer show stray blue dots
- Align inline report metadata such as Confidence on a clean baseline with the label and value in one compact row
- **Files**: frontend/src/components/AgentWorkspaceView.vue, CHANGELOG.md

### feat: add workspace task execution complexity
- Add task-level `execution_complexity` with `auto`, `simple`, and `complex` values, defaulting old and new tasks to `auto`
- Inject concise complexity guidance into assignment prompts so simple tasks execute directly, complex tasks orchestrate/delegate bounded subwork where available, and auto tasks choose and state a strategy first
- Carry execution complexity into dispatcher and reviewer prompts so reviewers can verify that the implementation strategy matched the selected complexity
- Surface an Auto/Simple/Complex selector in the Add Task modal and show the selected execution style in task assignment details
- Add focused backend coverage for persistence defaults, legacy normalization, assignment prompt guidance, and reviewer prompt visibility
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-27-task-execution-flow.md

## 2026-05-26

### feat: add workspace Review Profiles v1
- Add profile-aware reviewer metadata for `general`, `code`, `ui`, `artifact`, `delivery`, and `boundary` review lenses, including structured profile results, artifact refs, confidence, and human-judgment flags on reports and autonomous evaluation records
- Infer default review profiles from task mode, strictness, artifact policy, changed files, attachments, and report evidence, and inject profile-specific guidance plus bounded `REVIEW.md` instructions into reviewer prompts
- Surface configured profiles, profile results, artifact refs, confidence, and autonomous evaluation profile summaries in Agent Workspace task detail
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/api/workspaces.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/services/workspace_state_policy.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-26-review-profiles-v1.md

### feat: add Agent Workspace autonomous mode v1
- Add `direct` / `reviewed` / `autonomous` task modes with optional autonomy policy, autonomous run state, rubric/evaluation records, iteration records, and old-state defaults that keep existing tasks reviewed by default
- Keep Direct tasks out of automatic AI-review routing: Direct completion/ready reports proceed to the human Done gate unless review is explicitly requested, while Direct blocked/input-needed reports remain non-accept-ready
- Treat existing reviewer sessions as Autonomous Mode V1 evaluators: autonomous worker completion always routes to evaluation, evaluator pass moves the run to passed and Review awaiting human acceptance, evaluator failure revises while budget remains, and exhausted/needs-input states stop for human review
- Extend assignment and reviewer prompts with autonomous policy/run context while preserving the Goal Packet, acceptance-check, and final human Done gate
- Add mode-aware workspace UI: Add Task mode selector, autonomous controls, compact Auto round badges, and a run-detail panel with phase, iteration, score, policy, next action, and evaluation history
- Add focused backend coverage for mode defaults, old-state compatibility, Direct no-review/default-review/blocked/input-needed behavior, autonomous pass, budget exhaustion, and pure autonomous policy transitions
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/api/workspaces.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/services/workspace_state_policy.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-26-autonomous-mode-v1.md, CHANGELOG.md

## 2026-05-23

### refactor: extract Agent Workspace state policy
- Add `workspace_state_policy.py` as a pure policy boundary for report/session/task status mapping, runtime observation mapping, review routing, review-skip eligibility, completion evidence gaps, and auto-continue output classification
- Keep `WorkspaceManager` responsible for persistence and tmux/reviewer side effects while delegating transition decisions to the policy helpers
- Add focused policy unit tests and keep workspace lifecycle integration coverage passing
- **Files**: backend/claude_hub/services/workspace_state_policy.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_state_policy.py, docs/working-logs/2026-05-23-state-machine-assessment.md, CHANGELOG.md

### docs: assess Agent Workspace state-machine boundaries
- Document current terminal runtime detection, managed session lifecycle, task/report/review transitions, and frontend status derivation
- Recommend a bounded state-policy/state-machine layer for Agent Workspace lifecycle events while keeping ttyd/tmux status classification as a separate heuristic observation source
- **Files**: docs/working-logs/2026-05-23-state-machine-assessment.md, CHANGELOG.md

### feat: add workspace Goal Packet v1
- Add optional task-level Goal Packets so workspace agents can record objective, acceptance criteria, validation plan, assumptions, out-of-scope boundaries, and handoff requirements directly on a task
- Add report-level acceptance checks for ready-for-review/completed handoffs, and carry Goal Packet + acceptance evidence into reviewer prompts so reviewers audit both goal fidelity and delivery evidence
- Update assignment prompts to ask agents to derive a Goal Packet before substantive implementation while preserving existing started/working/blocked/needs_input/ready_for_review/completed/review_* state transitions
- Clarify agent-decided routing: `review_decision=skip` only skips AI reviewer checks, not final human completion; AI-passed or AI-skipped tasks remain in review awaiting human completion
- Add human acceptance timestamps to tasks and keep Agent Workspace completion action as Done; Request review opens a prompt and sends the human's review instructions to the reviewer
- Make request-changes safe when the original agent has already moved to another task, and hide Done when the latest reviewer result is failed or needs input
- Block low-risk review-skip completion reports that lack a stored Goal Packet or acceptance-check evidence; the agent is prompted to supplement the missing audit evidence and the task stays working instead of silently skipping review
- Render a compact read-only Goal Packet section and acceptance-check evidence in the task detail panel, including an empty state for older tasks
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/api/workspaces.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-23-workspace-goal-packet-v1.md

## 2026-05-22

### feat: cursor + terminal agent types in workspace, keep manage-agents modal open after create
- Manage Agents modal now exposes the same four agent types as the new-tab dialog: Codex, Claude, Cursor, Terminal (previously the workspace dropdown only had three slots and mislabeled `cursor` as "Terminal")
- The YOLO/solo-mode field is hidden for both Cursor and Terminal in the workspace agent form (matches the new-tab behavior); creation also force-clears `solo_mode` for these types when sending to the backend
- After "Create agent" succeeds, the modal stays open (only the file browser closes and the title field resets), so users can add multiple agents in a row without re-opening the dialog
- TabBar tightens the agent-type type literal to the canonical `AgentType` union and renames the remote-tab default label to use the proper Cursor/Terminal display name; the agent-type watcher also clears solo_mode for `terminal` (was only doing so for `cursor`)
- **Files**: frontend/src/components/AgentWorkspaceView.vue, frontend/src/components/TabBar.vue, CHANGELOG.md

### docs: merge AGENTS.md into CLAUDE.md and prune stale gstack section
- Make `AGENTS.md` and `CLAUDE.md` identical: keep `CLAUDE.md` as the canonical conventions doc, fold in the `Mandatory Branch Workflow` and `Protected Local State` sections that previously lived only in `AGENTS.md`, and rewrite `AGENTS.md` as a verbatim copy
- Drop the outdated `gstack` and `Skill routing` sections that referenced gstack-only commands (`/office-hours`, `/ship`, `/qa`, etc.) which are not part of this project's tooling
- Refresh the overview to mention the workspace orchestration layer + Claude/Cursor/Terminal agent types, expand the protected-local-state list, and add an `Agent Types` reference plus a workspace orchestration row in `Common Dev Scenarios`
- **Files**: AGENTS.md, CLAUDE.md, CHANGELOG.md

### feat: cursor agent support and dedicated terminal agent_type
- Repurpose `AgentType.CURSOR` to launch the Cursor CLI (`agent`); cursor agent is always YOLO by default and the solo-mode toggle no longer applies
- Add new `AgentType.TERMINAL` for plain user-shell sessions (the previous `cursor` placeholder behavior); the UI dropdown now lists Cursor and Terminal as separate options
- Treat cursor as an agent TUI in `IS_AGENT_TUI` and disable the auto tmux-history replay loop that was previously running for cursor — the periodic snapshot replay was overwriting the cursor TUI mid-update, causing the "stuck halfway" display and input deletion lag reported in cursor sessions; auto-replay now only runs for the new plain `terminal` mode
- Extend probe-response filtering, foreground idle detection, and clipboard-image paste handling to include cursor
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/ttyd_manager.py, backend/claude_hub/api/terminal.py, backend/tests/conftest.py, backend/tests/test_ttyd_manager.py, frontend/src/types/index.ts, frontend/src/components/TabBar.vue, frontend/src/components/TerminalView.vue, CHANGELOG.md

### fix: hold orchestrator agent only while review is unresolved
- Hold the orchestrator's `task_id`/`current_task_id` binding while a task's review is still in flight or after `REVIEW_FAILED` (so reviewer-failure feedback can re-engage the same context), but auto-release the agent once the latest review report is `REVIEW_PASSED` so the queue advances without waiting for a manual "done" click
- Replaces the earlier behavior that held the agent all the way through to `done` and could leave the queue looking stuck when a single resident agent finished review
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-05-21

### fix: replace yellow working badge with AI reviewing on task card
- When a task is under active AI review, show only the existing "AI reviewing" pill on the task card header instead of stacking it next to a redundant yellow `working` status pill; the status pill still renders for non-reviewing states
- **Files**: AgentWorkspaceView.vue

### fix: stop re-prompting orchestrator after review is in flight
- Skip the auto-continue "no workspace report was recorded" nudge when `review_requested_at` is set without `review_completed_at`, or when the latest report for the task is already `ready_for_review` / `completed` / `blocked` / `needs_input`
- Previously the orchestrator session stayed parked with `task.status == WORKING` while the reviewer ran, so the monitor kept matching completion patterns in scrollback and re-sending the nudge every ~15s up to 10 attempts
- **Files**: workspace_manager.py, test_workspaces.py

### fix: disabled segment-button styling on edit workspace modal
- Add a `:disabled` style for segmented controls so the locked Local/Remote toggle in the Edit Workspace modal renders with reduced opacity and a not-allowed cursor on both desktop and mobile, instead of looking falsely interactive
- **Files**: AgentWorkspaceView.vue

### feat: editable workspace working dir as default for new agents

### feat: bilingual report detail with EN | 中 toggle in task progress
- Add optional `message_en` and `message_zh` fields to the AgentReport schema so workers can submit each progress update in both languages; legacy `message` remains a fallback
- Render a small EN | 中 toggle next to the Progress section in task details that switches the timeline message body between languages, with sticky preference in localStorage
- Update workspace agent dispatch prompts and curl examples to require bilingual messages on every report
- **Files**: schemas.py, workspace_manager.py, AgentWorkspaceView.vue, index.ts

### feat: surface AI review state on working tasks
- Show a colored "AI reviewing" / "Awaiting AI review" / "Review needs input" badge in the task card header for tasks still in the Working column while a reviewer agent is engaged
- Drive the badge from existing `review_session_id`, `review_requested_at`, and the latest `review_*` AgentReport so it pulses live during `review_started` and clears once review completes
- **Files**: AgentWorkspaceView.vue

### fix: task progress timeline cleanup
- Remove the redundant inner status dot on each progress card so the timeline rail dot is the only marker per entry
- **Files**: AgentWorkspaceView.vue

### fix: let mobile overflow menus scroll when expanded
- Bound terminal and workspace mobile overflow menus to the viewport so long nested sections do not run off-screen
- Enable touch scrolling inside the menu panels while keeping scroll chaining contained
- **Files**: TabBar.vue, AgentWorkspaceView.vue

### fix: collapse mobile frontend access links
- Keep the mobile overflow menu compact by showing Frontend Access as a collapsed submenu by default
- Fetch and reveal the local frontend URLs only when the nested menu is opened, then reset it collapsed when the parent overflow menu closes
- **Files**: NetworkAccessMenu.vue

### fix: drop generated terminal probe replies in agent tabs
- Filter xterm.js device-attribute and cursor-position replies from Claude/Codex tab WebSocket input so terminal capability probes no longer appear as stray text like `0;276;0c` in the agent prompt
- Keep the filter scoped to generated probe replies on agent tabs, leaving normal typing, escape keys, resize frames, and plain terminal tabs unchanged
- **Files**: terminal.py, test_terminal_proxy.py

### fix: keep status panel refresh icon idle during polling
- Make the Workspace Agents status-panel refresh button use a local manual-refresh pending state instead of the global background status-poll loading flag
- Keep automatic panel-open and periodic status refreshes silent so the header icon no longer appears to spin continuously while data is polling
- **Files**: AgentStatusFloatingPanel.vue

### fix: release idle reviewed agents and dedupe pasted task images
- Let an idle implementation agent accept the next queued task after its previous task has reached Review, while preserving working/pending-review assignments
- Reconcile stale review-stage `current_task_id` session state during status refresh so queues do not remain blocked by already-reviewed tasks
- Deduplicate clipboard image files against embedded HTML/plaintext data URLs so a single pasted screenshot does not create two task attachments
- **Files**: workspace_manager.py, test_workspaces.py, AgentWorkspaceView.vue

### feat: let agents skip low-risk reviewer checks
- Add explicit `review_decision` metadata to workspace reports so agents can request, skip, or defer to automatic reviewer routing
- Keep backend guardrails that force review for changed files, tracked dirty worktrees, blocked/input-needed states, runtime attention diagnostics, and failed-review follow-ups
- Mark approved skip decisions in the Review column with a reason and expose a manual Request review action for skipped tasks
- **Files**: workspace_manager.py, schemas.py, workspaces.py, test_workspaces.py, AgentWorkspaceView.vue, workspaceStore.ts, index.ts

### fix: keep terminal typing responsive during history refresh
- Release the initial terminal replay hold as soon as the user types, so opening a populated tab no longer delays local input echo behind the scrollback stabilization window
- Cancel or postpone automatic tmux history repair when input arrives during live-output refresh, keeping typed text responsive while preserving a later quiet-window recovery path
- Add replay E2E coverage for typing during tab open and typing while a delayed live-output resync is pending
- **Files**: terminal.py, test_terminal_replay.py

### fix: avoid duplicate split-pane terminal clients
- Keep a terminal tab assigned to only one visible pane at a time so split layouts cannot attach duplicate ttyd/tmux browser clients to the same Claude session
- Drop hidden iframe caches when leaving single-pane mode, preventing stale hidden clients from continuing Claude TUI redraw and resize work while another pane displays that tab
- Cap single-pane terminal iframe caching to the active tab plus one recent tab so large workspaces do not keep many hidden ttyd clients rendering and resizing in the background
- Reduce global agent-status polling pressure for large tab sets and avoid cursor/plain terminal history resync during ordinary typing
- **Files**: terminalStore.ts, TerminalView.vue, terminal.py, test_terminal_replay.py

### fix: serialize workspace task dispatch
- Serialize per-workspace dispatch so the background monitor and task start path cannot send `/clear` and the assignment prompt for the same queued task twice
- Keep pasted Codex task prompts marked pending when only a command-result marker follows, avoiding false success that leaves task content pasted but not submitted
- Keep tasks in Review after `review_passed` until a human clicks Done, while releasing reviewer assignments and preventing runtime refreshes from moving reviewed tasks back to Working
- Require future development to use isolated worktrees with task branches; frontend changes should use a dedicated debug server and stop it before merge or handoff
- Add regression coverage for concurrent workspace dispatch and pasted-input submit detection
- **Files**: AGENTS.md, CLAUDE.md, workspace_manager.py, test_workspaces.py

## 2026-05-20

### feat: add workspace reviewer loop
- Add reviewer workspace sessions and review-specific report states so completed, blocked, or ready tasks can be routed through an independent reviewer gate
- Send task-specific review prompts with acceptance criteria guidance, recent task reports, and verdict reporting rules; failed reviews continue the task back to the implementation agent
- Rename assigned agent and reviewer terminal tabs to the active task title for easier terminal identification
- Surface reviewer status, review attempts, reviewer assignments, and temporary reviewer sessions in the Agent Workspace UI
- **Files**: workspace_manager.py, schemas.py, workspaces.py, test_workspaces.py, AgentWorkspaceView.vue, AgentStatusFloatingPanel.vue, workspaceStore.ts, terminalStore.ts, index.ts, ttyd_manager.py

### fix: support workspace task screenshot paste
- Read pasted workspace task images from clipboard files, clipboard items, and data URL payloads so screenshots can attach in both new task and task interaction forms
- Avoid secure-context-only draft attachment IDs by falling back when `crypto.randomUUID()` is unavailable on LAN HTTP access
- Keep the workspace task UI paste-only without adding a separate file-picker attachment control
- **Files**: AgentWorkspaceView.vue

## 2026-05-19

### fix: stabilize remote Claude agent display
- Configure remote tmux sessions before attach so Claude/Codex tabs hide nested tmux status bars, keep mouse off, enable focus events, and preserve a large history limit
- Suppress SSH log-level noise in remote launch and capture commands so known-host warnings do not mix into the terminal canvas
- Let remote Claude/Codex tabs attach live immediately instead of replaying a slow initial SSH history snapshot that can overwrite fresh prompt/input state; manual history refresh still captures the remote tmux session on demand
- **Files**: terminal.py, ttyd_manager.py, test_ttyd_manager.py

## 2026-05-18

### fix: restore live terminal updates after tab activation
- Stop dropping ttyd ws frames in the Phase B full-replay hold; flush buffered frames and reconcile with a fresh tmux snapshot so sparse-update TUIs (Claude) keep showing the latest output instead of a stale screen
- Rework `runResync` to set the resyncing flag before the async fetch so concurrent live writes buffer instead of forcing abort + retry under hot write streams
- Replace `startPostReplayWatch`'s stale `replayPayload` rewrite with a fresh tmux refetch so the recovery path no longer rolls back several seconds of real ws data
- Trigger a history refresh on cursor/plain desktop tab switches (mobile already had this via `terminal-activate`) so switching back to a plain terminal repaints the latest content immediately
- **Files**: terminal.py, TerminalView.vue

### fix: keep live terminal output pinned to latest
- Preserve bottom-follow behavior during active terminal output by scrolling after xterm renders when the viewport was already at the latest line
- Treat wheel and touch scroll-away events as user intent so live following does not override manual history inspection
- Ignore xterm-internal scroll events and keep bottom-follow active briefly after live writes so dynamic Claude/Codex status UIs do not leave the viewport stuck mid-buffer
- Disable automatic idle history replay for Claude/Codex TUI tabs, preventing tmux snapshot replays from corrupting relative cursor redraws while agents are working
- Limit automatic tab-activation history refreshes to plain cursor terminals; Claude/Codex tabs now scroll to bottom without replaying tmux snapshots unless the user requests a manual refresh
- Avoid refresh-heavy bottom-follow loops during live input echo so Claude prompt typing stays responsive
- Preserve Claude/Codex scrollback continuity by filtering held duplicate ttyd initial-screen frames while keeping real live output produced during Phase B history reconstruction
- Freeze Claude/Codex live redraws while the user is browsing history, then restore from tmux when returning to the bottom so the visible historical viewport is not overwritten by the fixed input/status area
- Add terminal replay coverage for live-output bottom pinning, dynamic internal scroll events, DOM bottom-gap drift, and agent history viewport stability
- **Working log**: docs/working-logs/2026-05-19-fix-claude-terminal-live-history.md
- **Files**: terminal.py, TerminalView.vue, test_terminal_replay.py

### feat: show copyable frontend LAN access links
- Add a top-bar network menu that lists copyable frontend URLs for loopback and discovered local IPv4 addresses
- Keep mobile top bars compact by placing the same access list inside the existing terminal and workspace overflow menus
- Discover interface IPv4 addresses from the backend and allow Vite review sessions to route the system endpoint to a branch backend
- **Files**: system.py, test_system.py, NetworkAccessMenu.vue, App.vue, TabBar.vue, AgentWorkspaceView.vue, vite.config.ts

## 2026-05-17

### fix: refresh terminal history on demand
- Add a pane-level history refresh action that forces the embedded terminal to recapture tmux scrollback and rebuild the xterm buffer
- Refresh and scroll mobile terminals to the latest output when switching back to cached tabs, avoiding stale initial-screen views
- **Files**: terminal.py, TerminalView.vue, TerminalPane.vue, test_terminal_replay.py

### fix: mute dark terminal ANSI background colors
- Replace dark terminal base ANSI colors with lower-saturation values so remote Claude prompts that paint large ANSI background regions do not turn the pane bright green
- Apply a modest dark-mode xterm minimum contrast ratio to keep colored terminal text readable on muted ANSI backgrounds
- **Files**: App.vue, TerminalView.vue

### fix: keep remote Claude launches out of bare shells
- Fall back to the remote home directory when a remote tab or workspace agent is launched with a cwd that does not exist on the SSH host, then continue starting Claude/Codex instead of dropping straight into a login shell
- Reset remote tab/agent cwd defaults when switching to a remote target so local macOS paths are not carried into SSH launches
- **Files**: ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, AgentWorkspaceView.vue

## 2026-05-16

### feat: expand mobile terminal space while typing
- Drive the app shell height from `visualViewport` so the mobile keyboard does not double-shrink the terminal layout
- Enter a compact terminal mode while the keyboard is open, hiding nonessential chrome and tightening tab, pane, and mobile-control spacing
- Move the mobile split-layout shortcuts into a top-bar dropdown so the standalone layout row no longer consumes vertical space on phones
- Keep the mobile terminal tab bar anchored while the keyboard is open and smooth the compact layout plus floating virtual-key panel transitions
- Fold the mobile tab bar without dropping the terminal pane frame so the keyboard transition keeps a continuous border
- Animate mobile top chrome and pane-header collapse so the terminal frame slides with the keyboard instead of jumping into place
- Keep the floating virtual-key toggle pinned to the active viewport bottom during keyboard-open mode
- Coalesce terminal resize messages during mobile keyboard animation so xterm redraws only after the layout settles
- Replace the mobile keyboard folding chrome with a stable compact top bar and app menu so the terminal canvas does not resize when the keyboard opens
- Keep the mobile virtual-key overlay content-sized while tracking the visual viewport, preserving native xterm touch inertia
- Measure the browser's fixed-position keyboard baseline before shifting the mobile virtual-key button, avoiding duplicate upward movement on browsers that already anchor fixed controls to the visual viewport
- Give the mobile Agent Workspace view the same compact shell language as terminal mode, with a low sticky workspace toolbar, primary task action, overflow menu with distinct mode/theme controls, and slimmer agent status chips
- **Files**: App.vue, AgentStatusFloatingPanel.vue, AgentWorkspaceView.vue, LayoutSelector.vue, MobileControls.vue, TabBar.vue, TerminalGridView.vue, TerminalPane.vue, TerminalView.vue

### fix: avoid false pending workspace dispatch
- Treat submitted Claude slash-command output and older prompt echoes as completed sends, so queued workspace tasks are not blocked after a successful `/clear`
- Add regression coverage for the Claude `/clear` output shape that kept the H20 workspace task queued
- **Files**: workspace_manager.py, test_workspaces.py

### fix: replay remote tab tmux history
- Capture scrollback from the remote tmux session for remote tabs so reconnect/history replay includes the agent's actual remote terminal history instead of only the local SSH wrapper screen
- Keep local tmux capture as a fallback when remote SSH capture fails, using non-interactive SSH options to avoid blocking page load
- Add backend coverage for remote history preference, local fallback, and remote capture command construction
- **Files**: ttyd_manager.py, test_ttyd_manager.py

### fix: harden terminal replay hold on Linux CI
- Extend the full-replay hold window and perform a final replay before marking history as complete, so late ttyd initial frames cannot collapse xterm scrollback immediately before E2E assertions
- Verify the xterm buffer contains expected scrollback before publishing replay readiness, with a short post-ready watchdog for late Linux runner redraws
- Normalize styled tmux prompts in terminal E2E comparisons and wait for the expected xterm buffer depth before asserting scrollback state
- **Files**: terminal.py, conftest.py, test_terminal_replay.py

### fix: stabilize terminal replay CI and refresh README
- Replace synchronous browser history preload with an async preload gate before hooking xterm, so Chromium on Linux CI reliably receives tmux history before replay
- Keep full terminal replay writes buffered until ttyd's initial frame stream goes quiet, preventing late frames from collapsing scrollback to only visible rows
- Allow terminal replay E2E tests to bind a temporary backend URL so local validation can avoid the live 8173 service
- Update README, backend package description, and current Agent Workspace screenshot to reflect the workspace-agent, remote-tab, clipboard-image, and validation flows
- **Files**: terminal.py, conftest.py, README.md, backend/README.md, pyproject.toml, agent_workspace_demo.png

### fix: match terminal padding to rendered canvas background
- Compute the light-mode terminal inset color through the same canvas filter used by xterm so the padding matches the rendered terminal surface
- **Files**: TerminalView.vue

### fix: soften embedded terminal edge padding
- Restore a small terminal-colored inset around xterm content so light mode feels less crowded without reintroducing page-colored gutters
- **Files**: TerminalView.vue

### fix: fill embedded terminal viewport edge-to-edge
- Remove ttyd's default embedded terminal padding and stretch the xterm screen/canvas to the pane edges so light mode no longer shows white gutters
- **Files**: TerminalView.vue

### fix: refit terminal canvas after light theme layout changes
- Trigger ttyd/xterm resize from the active iframe after theme, tab, and container-size changes so the terminal canvas fills the pane in light mode
- **Files**: TerminalView.vue

### fix: align compact done task cards
- Prevent crowded task columns from flex-shrinking task cards below their content height
- Make Done task cards an explicit compact single-line surface so titles and status badges stay vertically centered
- **Files**: AgentWorkspaceView.vue

### style: polish workspace and terminal surfaces
- Refine workspace cards, columns, task detail sections, and report timeline to reduce visual noise and clarify hierarchy
- Lighten terminal tabs, layout controls, and active pane treatment while keeping dark/light theme tokens consistent
- Add shared radius and motion tokens for future frontend polish
- **Files**: App.vue, AgentWorkspaceView.vue, TabBar.vue, LayoutSelector.vue, TerminalPane.vue

## 2026-05-15

### fix: reopen completed review tasks from live runtime work
- Treat later live Working activity after the review grace window as a valid Review-to-Working transition for both `ready_for_review` and `completed` reports
- Keep the immediate post-report grace window so a completion report's own terminal output does not reopen the task
- **Files**: workspace_manager.py, test_workspaces.py

### feat: add button-level loading feedback
- Add a reusable loading button component and pending-action helper for frontend interactions
- Show per-control processing feedback for workspace switching, workspace task actions, agent management, follow-up sends, terminal tab creation/duplication/closing, directory browsing, status refresh, login redirect, and logout
- Keep pending state scoped by task, agent, session, tab, or browser action so unrelated controls remain usable
- **Files**: LoadingButton.vue, usePendingActions.ts, AgentWorkspaceView.vue, TabBar.vue, AgentStatusFloatingPanel.vue, LayoutSelector.vue, LoginView.vue

### docs: require branch-based agent development
- Add an agent-facing `AGENTS.md` entrypoint that points to `CLAUDE.md` and forbids direct development on `main`
- Clarify that small fixes, documentation changes, and managed workspace tasks must still use a feature/fix branch or isolated worktree before merging back
- **Files**: AGENTS.md, CLAUDE.md

### feat: make workspace agents manageable
- Rename the workspace agent entry point to agent management and show the existing agent list before the add-agent form
- Add visible delete actions to the agent status strip and management modal, with disabled-state hints while an agent still owns open tasks
- **Files**: AgentWorkspaceView.vue

### fix: enable terminal image paste for Claude tabs
- Reuse the browser-image-to-macOS-clipboard paste bridge for Claude Code tabs as well as Codex tabs, so pasted screenshots can reach the TUI through Ctrl+V
- **Files**: TerminalView.vue

### fix: keep ready reports authoritative
- Keep `ready_for_review` and `completed` reports as the authoritative task state instead of reopening Review tasks from raw terminal Working samples
- Preserve runtime-based Review-to-Working recovery when the assigned terminal shows new Working activity after the review timestamp, covering direct terminal follow-ups
- Add a short grace window after explicit ready reports so the reporting agent's own terminal activity cannot immediately reopen the task
- Add an explicit `working` report when a Review task is continued through the workspace flow so follow-up work has a durable state transition
- Restore tasks whose latest report is ready/completed back to Review during board reconciliation unless the task has later explicit or runtime Working activity
- Make auto-continue prompts semantic: interruption-like idle output asks the agent to continue, while completion-like idle output asks the agent to submit the missing final report
- **Files**: workspace_manager.py, test_workspaces.py

### fix: restore main ci checks
- Keep terminal history full replay buffered briefly after xterm accepts the replay write so late ttyd initial screen frames cannot collapse reconstructed scrollback on Linux CI
- Apply backend Black/isort cleanup for files that were failing formatting/import-order gates
- Fix backend mypy failures that were hidden behind the earlier formatting stop, including terminal status typing, remote workspace path fallback, and TerminalTab test construction
- Relax mypy's untyped-def requirement for tests while keeping production code strict
- **Files**: terminal.py, remote.py, models/__init__.py, ttyd_manager.py, workspace_manager.py, pyproject.toml, test_tabs.py, test_ttyd_manager.py, test_workspaces.py

### feat: support image attachments in workspace tasks
- Let task creation and follow-up instructions accept pasted image attachments from the browser clipboard
- Persist image attachments under the workspace state directory, show previews in task detail, and include attachment file paths in the agent prompt
- Add backend validation for supported image types and attachment size limits, plus test coverage for pasted-image persistence
- **Files**: schemas.py, workspace_manager.py, workspaces.py, test_workspaces.py, AgentWorkspaceView.vue, workspaceStore.ts, types/index.ts

## 2026-05-14

### fix: classify Codex selection prompts as attention
- Treat Codex interactive menus with `Enter to select`, arrow-key navigation, or `Esc to cancel` as Attention instead of Working
- Keep active work detection on interrupt-oriented hints such as `Esc to interrupt` and Claude spinner status lines
- Add backend coverage for Codex selection-menu status classification
- **Files**: ttyd_manager.py, test_ttyd_manager.py

### fix: keep continued review tasks in working
- Prevent board reconciliation from restoring a stale `ready_for_review` report over a later continue transition
- Mark review tasks as Working before sending follow-up text to the agent so tmux submit verification failures cannot leave the board in Review while the agent is active
- Move review tasks back to Working when the assigned agent shows new working runtime activity after the review timestamp, covering direct terminal-tab follow-ups
- Keep `completed` reports in Review unless the task is explicitly continued, even if the terminal has later runtime activity
- Auto-send `please continue` only when an assigned Working task's idle agent shows a recognized interruption such as `API Error: 400 unknown error`
- Add backend coverage for stale review reconciliation, completed-report review stability, direct-tab runtime continuation, interrupted-idle auto-continue, normal-idle suppression, and continue-send failure ordering
- **Files**: workspace_manager.py, test_workspaces.py

### feat: archive completed workspace task records
- Write a per-workspace `task_records/{completed_at}-{task_id}.json` archive whenever a task is marked Done
- Include task/session snapshots, agent reports, an ordered timeline, changed files, validation, risks, and final summary in the archive
- Keep archived task records independent from task deletion so completed work remains reviewable after board cleanup
- **Files**: workspace_manager.py, test_workspaces.py

### fix: reopen review tasks from follow-up send
- Route follow-up sends on review tasks through the task continue API so the board moves the task back to Working immediately
- Preserve generic session sends for non-review tasks
- **Files**: AgentWorkspaceView.vue

### feat: show workspace agent runtime cards
- Add a visible current-workspace agent status strip to Agent Workspace, matching the terminal status panel's dot and pill language
- Show each agent's role/type, runtime text, detail, current task, queued count, target, and quick-open action
- Poll terminal agent status while the workspace view is mounted so the cards reflect live terminal state
- Keep the agent status strip horizontally scrollable on mobile
- **Files**: AgentWorkspaceView.vue

## 2026-05-13

### feat: add remote tab launch support
- Add Local/Remote run targets to the new-tab modal, including remote server selection, remote working directory input and browsing, auto-reconnect, and mobile-friendly scrolling
- Discover remote profiles from `~/.claude_hub/remote_profiles.json` and SSH config `Host` aliases
- Add a remote filesystem listing API over SSH so remote working directories can be browsed before launch
- Launch remote tabs through the local ttyd/tmux layer into SSH, prefer remote tmux persistence when available, and fall back to direct agent startup when remote tmux is missing
- Bootstrap common NVM Node paths before starting Claude or Codex so Merlin machines with non-login shell PATH differences can still find agent CLIs
- Preserve local tab behavior while persisting and duplicating remote launch configuration
- Add backend coverage for remote command construction and shell compatibility
- **Files**: schemas.py, remote_profiles.py, remote.py, tabs.py, ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, types/index.ts

## 2026-05-12

### c3e0c64 feat: color tab indicator dot by agent runtime status
- Bind the per-tab indicator dot to agent status from the store: idle green, working yellow, attention purple, offline gray
- Add a soft glow on working and attention so active or waiting tabs are easier to spot
- Reuse the palette from AgentStatusFloatingPanel for consistency
- **Files**: TabBar.vue

### 3a48945 fix: stop agent status panel from flickering between working and attention
- Replace broad substring scans over the last 18 lines with anchored checks on the bottom 5 lines so historical scrollback no longer drives classification
- Strip ANSI escapes before matching and hashing so cursor blinks stop churning the activity hash; remove the "hash changed → working" heuristic that was the main flicker source
- Drop the `bypass permissions` attention pattern — Claude Code shows it as a permanent footer in bypass mode and was forcing every idle tab into Attention
- Tighten ATTENTION to explicit prompts (`do you want to proceed`, `(y/n)`, `[y/n]`, `press enter to continue`); WORKING keys off `esc to interrupt` / `ctrl+c to interrupt` / `esc to cancel`
- Rename ATTENTION display text to "Agent waiting for input"; IDLE remains "Idle" and is the default fallback
- **Files**: ttyd_manager.py

## 2026-04-28

### de5c9b8 fix: restore terminal cursor position after history replay
- Add tmux cursor coordinates (`cursor_x`, `cursor_y`) to the terminal history API response
- Restore xterm's cursor after initial history replay and idle history resync so the prompt cursor appears in the input line instead of the bottom row
- Add a Playwright regression test that compares xterm cursor coordinates against tmux pane coordinates
- **Files**: terminal.py, ttyd_manager.py, test_terminal_replay.py

### 7b93181 fix: stabilize terminal history while live output is streaming
- Reconcile xterm with tmux history after live output bursts go idle, restoring complete wrapped output that ttyd may skip in the live stream
- Tighten bottom-position detection so idle resync only rewrites the buffer when the user is truly at the bottom
- Preserve user history views while scrolling, including near-bottom views that show both older history and new output
- Add Playwright coverage for touch/wheel scroll alignment, wrapped live output continuity, and near-bottom resync protection
- **Files**: terminal.py, test_terminal_replay.py

### 81cb44c fix: persist tab order updates
- Persist drag-and-drop tab ordering so refreshing the web UI keeps the user's custom tab order
- Add backend coverage for saving and returning ordered tab lists
- Add `.agent_office/` to `.gitignore` for local workflow artifacts
- **Files**: .gitignore, tabs.py, test_tabs.py

## 2026-04-27

### c379b9f feat: add codex backend solo mode
- Add `AgentType.CODEX` and launch Codex tabs with the `codex` CLI by default
- Add Codex solo mode using `codex --ask-for-approval never --sandbox workspace-write`
- Extend the new-tab modal to choose Claude, Codex, or Terminal backends, with solo mode available for Claude and Codex
- Add backend tests for Codex command construction and tmux reattach behavior
- **Files**: schemas.py, ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, types/index.ts

### 31af616 fix: restore ci checks after codex backend merge
- Apply black formatting to `ttyd_manager.py`
- Add the missing `MonkeyPatch` type annotation for backend mypy
- Avoid the frontend ESLint `no-undef` error from the browser `EventListener` type alias
- **Files**: ttyd_manager.py, test_ttyd_manager.py, App.vue

### 5609dbf ci: update uv setup and split replay tests
- Update GitHub Actions to use `astral-sh/setup-uv@v7` instead of the stale `0.5.x` version selector
- Keep terminal replay tests in the dedicated Playwright job and exclude them from the generic backend pytest job
- **Files**: ci.yml

### be03355 fix: stabilize terminal replay in ci
- Use full terminal replay after `term.open()` to avoid Ubuntu headless xterm scrollback loss during CI
- **Files**: terminal.py

### 40702ad fix: run codex solo mode without sandbox limits
- Change Codex solo mode to launch with `codex --ask-for-approval never --sandbox danger-full-access`
- Update the Codex solo mode UI description and command construction test
- **Files**: ttyd_manager.py, test_ttyd_manager.py, TabBar.vue

## 2026-04-26

### feat: mobile UX improvements — viewport sync, key reliability, combo keys, inertial scroll

**4 个移动端体验问题修复：**

1. **键盘弹出时视口错乱** — 添加 `visualViewport` API 监听，键盘弹出时设置 `--keyboard-height` CSS 变量，App 容器和 MobileControls 自动适配
2. **虚拟按键切换 Tab 后失效** — 添加 terminal-ready 信号（iframe→parent postMessage）+ 按 key 队列缓存，Tab 切换后自动 flush
3. **缺少组合键** — 重组虚拟键盘布局：移除 PgUp/PgDn，加入方向键到主行，新增 Ctrl+C/D/L/A/E 和 Shift+Tab 快捷按钮，Ctrl/Shift 粘滞修饰键支持 Ctrl+任意字母
4. **终端历史滚动无惯性** — 通过阅读 xterm.js 源码定位根因并修复（详见下方）

**惯性滚动修复（6 次迭代）：**

迭代过程中发现三个杀死惯性滚动的机制：
- xterm 的 `handleTouchMove` 手动设 `scrollTop += delta`（替换浏览器原生滚动，无惯性）
- xterm 的 `_innerRefresh` 每帧重置 `scrollTop = ydisp * rowHeight`（行对齐，打断惯性）
- xterm 的 `.xterm-screen` 层遮住 `.xterm-viewport`，触摸事件到不了 viewport 元素

最终修复（3 层方案）：
- CSS: `.xterm-screen { pointer-events: none }` 让触摸穿透到 `.xterm-viewport`
- JS: `term._core.viewport.handleTouchMove` → no-op，阻止 xterm 手动设 scrollTop
- JS: 拦截 `_innerRefresh`，触摸+fling 期间跳过 scrollTop 重置

关键发现：viewport 对象在 `term._core.viewport`（非 `term.viewport`），`document.body` 在脚本执行时为 null（需用 `document.documentElement`）

**改动文件：**
- `backend/claude_hub/api/terminal.py` — 注入 CSS（pointer-events, -webkit-overflow-scrolling）+ JS（触摸穿透、handleTouchMove no-op、_innerRefresh hook、terminal-ready postMessage、Ctrl+字母/Shift+Tab 编码）
- `frontend/src/App.vue` — visualViewport 同步 + `--keyboard-height` CSS 变量
- `frontend/src/components/MobileControls.vue` — 重组键盘布局 + 快捷按钮 + Ctrl/Shift 粘滞修饰 + 自动释放
- `frontend/src/components/TerminalView.vue` — terminal-ready 信号 + key 队列 + Ctrl+字母/Shift+Tab 处理

## 2026-04-25

### 75f9d1c fix: terminal history replay misalignment with Playwright E2E tests

**核心问题：** 切换 Tab 或刷新页面重连终端时，scrollback 内容丢失、可见屏幕被重复渲染、历史和实时数据交错。

**根因：**
1. `Object.defineProperty(window, 'term', ...)` 拦截器被 ttyd 的 webpack bundle 绕过 — ttyd 在打包时捕获了原生 `Object.defineProperty` 引用，我们的拦截器从未被调用，导致 `hookTerm()`、`replayHistory()` 从未执行
2. 轮询检测到 `window.term` 时，ttyd 已调用 `term.open()` 并写完可见屏幕 — 此时清除 buffer 再只写 scrollback，可见屏幕变空且无新 WS 数据填充

**修复方案 — Phase A/B 双模式回放：**
- **Phase A**（`term.open()` 未调用）：只写 scrollback + `\x1b[NS` Scroll Up 序列把底部行推入 scrollback，让 ttyd WS 填充可见屏幕
- **Phase B**（`term.element` 存在，ttyd 已写完可见屏幕）：清除整个 buffer（`\x1b[H\x1b[2J\x1b[3J`），写入完整终端内容（scrollback + 可见屏幕），丢弃缓冲中 ttyd 的 WS 数据（它是重复的可见屏幕内容）

**关键改动：**
- 用 `setInterval` 轮询替代 `Object.defineProperty` 拦截器来检测 `window.term`
- `hookTerm()` 增加 `term.element` 检查：已存在时直接调用 `replayHistory(term, true)`，否则 hook `term.open()`
- 服务端 `capture-pane` 移除 `-E -1` 参数，返回完整终端内容（scrollback + 可见屏幕）
- `capture_history()` 增加 tmux session 不存在时的空字符串提前返回（ttyd 延迟创建 session）
- 添加 `__claudeHubReplayDone` 标志供测试轮询
- 移除 `if (!historyText) return;` 提前退出（hook/resize-guard 逻辑必须始终运行）

**新增 5 个 Playwright E2E 测试：**
- `test_scrollback_complete` — 200 行历史全部出现在 xterm scrollback
- `test_bottom_rows_preserved` — scrollback 行数与 tmux 一致
- `test_no_duplicate_visible_screen` — 无重复可见屏幕内容
- `test_empty_scrollback` — 空历史时干净加载
- `test_replay_with_active_output` — 历史和实时输出不交错

**CI 修复：**
- 修复 mypy 类型错误（conftest.py 缺类型注解、read_xterm_buffer 返回 Any）
- 添加缺失的 `client` AsyncClient fixture（test_health/test_tabs 需要）
- 添加 `types-requests` dev 依赖
- 移除 CI yaml 中未安装的 `--timeout=120` 标志

**迭代过程（本分支历次提交）：**
- `1cc28ed` 移除 `-J` flag，添加 write buffer 防止历史/实时数据交错
- `9015dac` 移动端键盘弹起 3 层防抖：CSS `100lvh`、xterm `onResize` debounce、`visualViewport` 键盘状态检测
- `1c153f0` 恢复 scrollback-only 回放，修复全量回放导致的可见屏幕重复
- `53a8780` 完整重写为 Phase A/B 模型 + 5 个 Playwright E2E 测试
- `6670033` CI 修复：mypy、client fixture、pytest-timeout

**Files**: terminal.py, ttyd_manager.py, test_terminal_replay.py, conftest.py, ci.yml, pyproject.toml

## 2026-04-13

### cd1e247 fix: preserve terminal scrollback across tab switches
- Tab switching no longer loses scrollback history
- **Files**: terminal.py, ttyd_manager.py, TerminalView.vue

## 2026-04-11

### 07300a6 feat: improve tab bar scrolling experience on mobile
- **Files**: TabBar.vue

### ffaddb2 chore: standardize backend port to 8173
- Consolidate all config, docs, scripts to use port 8173
- **Files**: README.md, config.py, docker/*, docs/DEPLOYMENT.md, scripts/*

### 5ccd61b fix: make backend CI checks pass
- Fix type annotations and import issues for mypy/black/isort
- **Files**: filesystem.py, tabs.py, terminal.py, main.py, ttyd_manager.py, tests/*

### 108108c fix: stabilize frontend lint step in CI
- Fix ESLint config and dependencies for CI
- **Files**: eslint.config.js, package.json

### 5394fea fix: align backend tooling and typing with CI checks
- Add missing type annotations across auth, api, models, services
- **Files**: api/*.py, auth/*.py, models/*.py, services/*.py, pyproject.toml

### 6e2172a fix: keep tmux CI session alive for validation
- **Files**: ci.yml

## 2026-04-10

### cc7682d fix: resolve terminal text selection by disabling tmux mouse mode
- Set `tmux mouse off` — tmux mouse mode intercepted all mouse events, preventing xterm.js native text selection
- Allow browser context menu when text is selected
- **Files**: terminal.py, ttyd_manager.py, TerminalView.vue

### e3f8ab2 fix: enable text selection and copy in terminal, prevent browser context menu
- Remove interfering CSS, add context menu guard for selected text
- **Files**: terminal.py, TerminalView.vue

## 2026-04-09

### 3679463 feat: add cursor agent terminal support
- New `AgentType.CURSOR` — launches user's shell instead of `claude` CLI
- Tab creation supports `agent_type` field (claude/cursor)
- **Files**: tabs.py, schemas.py, ttyd_manager.py, TabBar.vue, terminalStore.ts, types/index.ts, vite.config.ts, start.sh

## 2026-04-02

### 7e33500 fix: support updating commented env vars in start-temp-tunnel.sh
- **Files**: scripts/start-temp-tunnel.sh

## 2026-04-01

### ec55c80 fix: improve mobile terminal scrolling by removing aggressive CSS constraints
- **Files**: TerminalView.vue

### b44ba93 feat: skip auth for local network requests
- Private IPs (10.x, 172.16-31.x, 192.168.x, loopback) bypass Feishu auth
- **Files**: auth.py, dependencies.py, config.py

### c2fd589 feat: add tab rename and fix duplicate tab
- **Files**: TabBar.vue

### df930a0 feat: add layout memory and duplicate tab features
- Persist layout choice in localStorage, add duplicate tab button
- **Files**: TabBar.vue, terminalStore.ts

### 011f481 feat: add open_id whitelist support and improve WebSocket cookie parsing
- Add `AUTH_ALLOWED_OPEN_IDS` config, manually parse WS cookie header (FastAPI Cookie decorator unreliable on WS)
- **Files**: auth.py, dependencies.py, config.py

### 900bdf7 feat: add one-click temp tunnel scripts and update vite config
- `scripts/start-temp-tunnel.sh` — start backend + frontend + Cloudflare Tunnel
- `allowedHosts: true` in Vite config for tunnel support
- **Files**: vite.config.ts, scripts/*

### fbbbd47 feat: add Cloudflare Tunnel support for public hosting
- Cloudflared setup/run scripts, config example
- **Files**: scripts/*, docs/DEPLOYMENT.md

### efc9a70 feat: merge Feishu OAuth authentication and public deployment support
- Full Feishu OAuth 2.0 integration (login/callback/logout/session)
- Email whitelist, Nginx and frp config for public deployment
- DEPLOYMENT.md documentation
- **Files**: api/auth.py, auth/*.py, config.py, models/schemas.py, api/tabs.py, api/terminal.py, docker/*, docs/DEPLOYMENT.md

## 2026-03-27

### Initial: solo mode fix
- Fix solo mode to launch `IS_SANDBOX=1 claude --dangerously-skip-permissions` correctly
- Use `bash -c` wrapper instead of `tmux send-keys`
- Add file logging to `~/.claude_hub/logs/backend.log`
