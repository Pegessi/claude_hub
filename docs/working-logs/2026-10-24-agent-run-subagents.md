# Hub subagents: `claude-hub agent run`

## Why

An agent that wanted help from a different provider had to go through the
Workspace Task board: `workspace ensure`, `task run`, a `task wait` loop,
`task report`, `task accept` and cleanup (6-8 CLI calls). Each call needed a cold
start and a bootstrap turn. The result arrived only through a curl report, and the
agent stayed unavailable until acceptance. That workflow suits tracked, reviewed
work. It is too heavy when an agent only needs a second opinion or a delegated
sub-question.

`agent run` provides that smaller path. One blocking call starts a native Chat
with the chosen provider and returns its final answer. It creates no Task, board
entry, Goal Packet, review or acceptance step.

## Design

- **One subagent = one native Chat tab.** It is an ordinary Chat (`session_kind
  = chat`), created through `ttyd_manager.create_tab`. The tab records
  `parent_tab_id` (the caller's Chat) and `subagent_id`, and its env carries
  `CLAUDE_HUB_SUBAGENT_ID`. Older tab records do not have these keys, and empty
  or malformed values are normalized to `None`.
- **Approvals (solo mode).** By default a subagent inherits its parent Chat's
  `solo_mode`, so it never bypasses approvals the user turned on for that Chat.
  Without a parent it runs in solo mode, because nobody could approve. The
  request field `solo_mode` (CLI `--solo/--no-solo`) overrides either default.
  The record stores the effective value.
- **One turn = one server-owned asyncio task.** The task calls the existing
  external-conversation adapter `dispatch_tab_chat_and_wait` (also used by the
  Feishu bot) with `prefer_final_text=True`. This returns the text after the
  turn's last tool call, so the caller gets the answer rather than progress
  notes. The adapter cancels the provider turn on timeout and retires it when
  the awaiting task is cancelled.
- **No bootstrap turn.** The first prompt is wrapped in a short
  `<claude_hub_subagent>` preamble: you are a subagent, do the task, your final
  message is the answer, and ask only if you are truly blocked. The Chat UI shows
  only the caller's prompt (`visible_text`). Follow-up turns are sent unwrapped.
- **Durable results.** `<runtime home>/subagents.json` (atomic replace, single
  backend owner) stores each subagent and every turn's status, final text and
  error. Each final text is capped at 64k characters, with an inline note and
  `final_text_truncated=true`; the Chat tab keeps the full answer. Each record
  keeps its first turn (create idempotency), the last 20 turns and any running
  turn. Closed records are pruned after 7 days, and at most 500 are kept.
  - Saves serialize a snapshot on the event loop and write it (temp file,
    fsync, `os.replace`) in a worker thread. They run in call order under one
    lock, a save already covered by a newer write is skipped, and a cancelled
    caller still waits for its write to finish, so an older snapshot can never
    replace a newer one.
  - A new subagent record is saved before it becomes visible in memory. If that
    save fails, the request returns 503, the new tab is archived and no turn
    runs; a retry with the same `call_id` starts cleanly. A failed follow-up
    save removes the new turn and returns 503 without sending anything. When the backend starts, a turn still marked
  `running` becomes `failed` ("Claude Hub restarted …"), so a waiter never hangs.
  Non-kept subagents whose turn ended this way are then closed.
- **Idempotency.** Each `call_id` is serialized by a keyed lock. Repeating a
  create or follow-up with the same `call_id` returns the original
  subagent/turn. If the request body differs, the API returns 409 and never
  creates a second tab or turn. The CLI generates one `call_id` per invocation and
  retries only transport failures, using the same body.
- **Lifecycle.**
  - `keep=false` (default): the tab is archived about 3 s after the turn
    finishes, whether it completes, fails, times out or is cancelled. The grace
    period lets trailing provider events (usage, transcript flush) land before
    the runtime stops. History remains restorable.
  - `keep=true`: the tab stays open for `POST /turns` until it is closed.
  - Closing cancels any running turn and archives the tab. The record reports
    `closed` only after the tab is archived (an in-progress close is tracked
    separately, so a cancelled turn does not start a second close and a
    follow-up is refused); a close cut off by shutdown leaves the record open
    for startup reconciliation.
  - Every task the service spawns (turns, delayed and listener-triggered
    closes) is tracked; shutdown cancels and joins them. A non-kept subagent
    whose delayed close was cut off by shutdown is closed by the next startup
    reconciliation. After joining, shutdown saves once more so a result whose
    own save was interrupted is still persisted. A create that was in flight
    when shutdown began re-checks before publishing: it archives its tab,
    drops the unpublished record from disk and returns 503 instead of spawning
    an untracked turn.
- **Nesting.** A subagent may start one more level of subagents (max depth 2,
  counted through the tabs' `subagent_id`/`parent_tab_id` chain); a deeper
  create returns 400 with "Subagent nesting limit reached". The CLI always
  sends the caller's identity (`caller_tab_id` from `$CLAUDE_HUB_TAB_ID`,
  `caller_subagent_id` from `$CLAUDE_HUB_SUBAGENT_ID`) separately from
  `--parent-tab-id`/`--no-parent`. The server counts depth from the caller
  (the tab record wins over the env id), and when the caller is a subagent it
  refuses `--no-parent` (400) and links the new subagent to the caller's own
  Chat whatever parent the request named, so closing the caller cascades. An
  unknown or closed `caller_subagent_id` without a live caller tab returns 409.
  Caller fields are not part of the `call_id` request digest. Subagent Chats
  also get runtime guidance without the `agent run` recipe, so they do not fan
  out by default; that choice follows the tab's `subagent_id` (copied onto the
  terminal-tab stream session), not the env key.

### Parent binding: why a tab-retirement listener

Archiving or deleting a parent tab must close its subagents. Many code paths
call `archive_tab` or `delete_tab`, including the HTTP routes, Workspace session
lifecycle, task abort, review reaper and Dot. Hooking only the HTTP routes would
miss those paths, and the terminal manager should not import feature services.

The least invasive hook that covers every path is a small registry
(`services/terminal_runtime/retirement_listeners.py`):

- The manager calls `notify_tab_retired(tab_id)` after an archive is persisted,
  and after a delete is durably retired but before artifact cleanup.
- The subagent service registers a synchronous callback. That callback only
  schedules the async close: cancel the turn, then archive the child tab.

The same callback also closes a subagent when the user archives or deletes the
subagent's own tab. Retirements that happen while the backend is down produce no
notification. Startup reconciles them: if the parent or own tab is missing or
archived, the record is closed. A tab that has a `subagent_id` but no open record
(for example, after a crash between tab creation and record write) is archived.

Unarchiving a subagent tab through the normal tab unarchive path detaches it:
the manager clears its `subagent_id`, `parent_tab_id` and
`CLAUDE_HUB_SUBAGENT_ID`, so it becomes an ordinary Chat. Duplicating or forking
a subagent tab likewise yields an ordinary Chat: the copy gets no link and its
launch env drops `CLAUDE_HUB_SUBAGENT_ID`. Neither parent closure
nor startup reconciliation re-archives it, and a record that still pointed at it
is closed as "subagent tab detached" without touching the tab.

## API (`/api/subagents`, authenticated like other Hub routes)

| Method and path | Purpose |
| --- | --- |
| `POST /api/subagents` | Create the subagent and start its first turn; returns `SubagentTurnResult` (201, `status: running`) |
| `POST /api/subagents/{id}/turns` | Start a follow-up turn on a kept subagent. Returns 409 if the subagent is closed, not kept or busy |
| `GET /api/subagents/{id}/turns/{turn_id\|latest}?wait=S` | Long-poll the turn result (`wait` ≤ 60 s) |
| `GET /api/subagents?parent_tab_id=…&include_closed=…` | List subagent summaries |
| `GET /api/subagents/{id}` | Full record, including all turns |
| `POST /api/subagents/{id}/cancel[?turn_id=…]` | Cancel the running turn, or only `turn_id` (a finished turn is left as is) |
| `POST /api/subagents/{id}/close` | Cancel the turn and archive the tab |

Create body: `agent_type` (`claude|codex|traex|cursor|maso`; `terminal` is
rejected), `prompt`, optional `model`, `effort`, `cwd`, `env_preset`, `env`,
`parent_tab_id`, `caller_tab_id`, `caller_subagent_id`, `name`, `keep`, `call_id`, `timeout_seconds` (default 1800,
max 86400) and `solo_mode` (default: the parent's setting; `true` without a
parent).

- **Model and effort.** The model is mapped to the env key the Chat transport
  already reads (`ANTHROPIC_MODEL`, `CODEX_MODEL`, `CURSOR_MODEL`, `MASO_MODEL`).
  Only Codex and TraeX accept `effort` (`CODEX_REASONING_EFFORT`); other
  providers return 400 and should use a model variant instead.
- **cwd.** Defaults to the parent's cwd and must be an existing absolute
  directory.
- **Parent tab.** A missing parent returns 404; an archived parent returns 409.

Result: `{subagent_id, tab_id, turn_id, status: running|completed|failed|timeout|cancelled,
final_text, final_text_truncated, error, started_at, completed_at, keep, closed}`.

## CLI

```bash
claude-hub agent run --provider codex [--model M] [--effort E] [--cwd DIR] \
  [--env-preset P] [--keep] [--solo|--no-solo] [--name N] [--timeout 30m] \
  [--background] [--json] \
  (--prompt TEXT | --prompt-file FILE | --prompt-file - | stdin)
claude-hub agent result SUBAGENT_ID [TURN_ID] [--wait] [--json]
claude-hub agent send SUBAGENT_ID --prompt …          # kept subagents only
claude-hub agent subagents [--all] [--include-closed] # default parent=$CLAUDE_HUB_TAB_ID
claude-hub agent cancel SUBAGENT_ID
claude-hub agent close SUBAGENT_ID
```

- **Defaults.** `--parent-tab-id` comes from `$CLAUDE_HUB_TAB_ID` (use
  `--no-parent` to skip it), `--cwd` is the current directory, and `--env-preset`
  comes from the CLI config, as for `agent create`.
- **Output.** The CLI blocks with repeated 20 s long-polls, each within the
  HTTP client timeout. It prints only the final text on stdout; progress goes
  to stderr. `--json` prints the structured result instead.
- **Background.** `--background` prints `SUBAGENT_ID TURN_ID` and returns
  immediately.
- **Interrupting.** Ctrl-C in `run` or `send` cancels the turn that command
  started (`cancel?turn_id=…`). If it lands while the create/send POST is in
  flight, the CLI re-posts the same body and `call_id` (idempotent) to learn the
  turn, then cancels it; a second Ctrl-C prints how to find and cancel it.
  Ctrl-C in `agent result --wait` only stops watching (exit 130) and never
  cancels.
- **Exit codes.** 0 completed, 1 failed, 124 timeout (or still running for
  `result` without `--wait`), 130 cancelled, 2 usage error.

Example session:

```text
$ claude-hub agent run --provider codex --prompt-file - <<'EOF'
Review backend/claude_hub/services/subagents.py for cancellation races. Reply with findings only.
EOF
[agent] subagent sa-4e07a3a614ba (codex) started in tab 53c1cca6-…; turn subagent-turn-…
<final answer on stdout>

$ SA=$(claude-hub agent run --provider claude --keep --background --prompt "Draft a plan for X" | cut -d' ' -f1)
$ claude-hub agent result "$SA" --wait
$ claude-hub agent send "$SA" --prompt "Now shorten it to 5 bullets"
$ claude-hub agent close "$SA"
```

## Guidance

Chat runtime guidance gains one line: "For help from another provider:
`claude-hub agent run --provider X --prompt-file -` returns its final answer …
Use Workspace Tasks only for tracked, reviewed work." The `agent` group help and
the `agent run` help repeat the same recipe. A Chat whose tab is linked to a
subagent (`subagent_id`) gets the same guidance without that line.

## Review follow-ups (2026-10-24)

An independent review found eight issues; all were fixed in the follow-up
commit and are reflected above: (1) forced solo mode; (2) reconciliation
re-archiving a user-unarchived subagent tab; (3) unbounded nesting and fan-out;
(4) `subagents.json` growth and fsync on the event loop; (5) a failed save
during create leaving a running in-memory record with a live tab; (6) CLI
Ctrl-C during the create POST and `result --wait` cancelling; (7) archiving
before trailing provider events; (8) untracked post-turn close tasks at
shutdown.

## Re-review follow-ups (2026-10-24)

- M1: `--no-parent` / `--parent-tab-id OTHER` no longer escape the depth limit
  or the close cascade (caller identity, see Nesting).
- M2: `closed` is set after the tab is archived; the test that waited on
  `closed` and then asserted the archive is now stable (20/20 runs).
- L1: final guarded save in `stop()`.
- L3: shutdown re-check before a create publishes or spawns (503).
- L4: duplicate/fork strip `CLAUDE_HUB_SUBAGENT_ID`; reduced guidance follows
  the tab link.
- L5: trimmed turns lose `call_id` idempotency (see Limitations).
- L2 (not done): `_save` still re-raises a cancellation that arrived while its
  shielded write completed. Swallowing it would break `asyncio.timeout`/task
  cancellation semantics for callers. The written snapshot is current, and the
  remaining window (a caller's cleanup after a cancelled save, e.g. a follow-up
  turn removed in memory but present on disk) is corrected by the next save and,
  after a crash, by restart recovery marking the turn failed.

## Validation

- `tests/test_subagents.py` uses a faked Chat runtime and the real API router.
  It covers:
  - blocking and background results, idempotent retries, kept follow-ups, and
    non-kept archive;
  - timeouts (status `timeout`, with the turn cancelled), cancel and failure;
  - parent archive and delete, validation errors, restart recovery with orphan
    sweep, and tab-record round-trip.
  - Review follow-ups: solo inheritance and override, the approval failure
    message, the depth limit, subagent guidance (including the first-turn
    transport prefix), final-text and turn-history caps, saves in a worker
    thread and in call order, failed create/follow-up saves and `call_id`
    retry, concurrent same-`call_id` creates while the tab is starting,
    per-turn cancel, the archive grace period and shutdown joining a pending
    close, and `prefer_final_text` in `dispatch_tab_chat_and_wait`.
  - Re-review follow-ups: a subagent caller (by tab or subagent id) cannot use
    `--no-parent` or another parent, is linked as the parent, cannot exceed
    depth 2 and cascades its close; a top-level caller may still run unlinked;
    an unknown caller subagent is refused; `closed` is never reported while the
    tab is live; `stop()` persists in-memory state; a create in flight during
    shutdown returns 503 and spawns nothing; reduced guidance follows the tab
    link, not a stale env key.
  - `tests/test_ttyd_manager.py`: duplicate and fork of a subagent tab drop
    the link and `CLAUDE_HUB_SUBAGENT_ID` but keep other env.
  - With a bare real `TTYDManager`: parent archive and delete notify the
    service through the real retirement path, and an unarchived subagent tab is
    detached and not re-archived by parent closure or restart reconciliation.
    Reconciliation archives orphans but keeps detached tabs.
- `tests/test_cli_subagents.py` covers CLI output, exit codes, options, stdin and
  file prompts, transport retries with the same call_id, Ctrl-C cancellation
  (while waiting and during the start POST, by turn id), `result --wait` Ctrl-C
  without cancel, `--solo/--no-solo`, the companion commands, and caller
  identity being sent with `--no-parent` and `--parent-tab-id`.
- Manual smoke test on an isolated backend (port 18273, private
  `CLAUDE_HUB_HOME`, `tmux -L ar-test`):
  - A Codex `agent run` returned `PONG` and exited 0. Its tab was archived with
    `close_reason="turn completed"`.
  - A Claude run whose preset credentials were rejected reported `failed` with
    the provider's 401 message and exited 1.
  - A kept Codex subagent answered `OK`. A follow-up recalled `7` from the same
    conversation.
  - A 3 s `agent send` timed out with exit 124, and the provider turn was
    cancelled.
  - Archiving the parent Chat closed both linked subagents with
    `close_reason="parent tab archived or deleted"`.
  - Stopping the backend mid-turn recorded `failed` ("shut down …"). After
    restart, `agent result` reported it and exited 1, and the non-kept tab was
    archived.

## Limitations and risks

- The frontend does not yet group or hide subagent tabs. They appear as ordinary
  Chats (non-kept ones in the archive) and carry `parent_tab_id`/`subagent_id`
  for a later UI change.
- A provider approval or question cannot be answered. The adapter retires such
  a turn and the result is `failed`. The error says how to proceed: re-run with
  `--solo` when approvals were on, or re-run with `--keep` and answer in the
  subagent's Chat in Web Chat.
- After a backend restart, an interrupted turn is recorded as `failed`. For a
  kept subagent, Hub's own restart continuation may still resume that Chat turn
  in the tab, but its answer is not linked back to the failed record.
- Records are not scoped per user, just like tabs. Any authenticated Hub user can
  list, read and close subagents.
- Nested subagents work up to depth 2, and parent closure cascades through the
  listener. Caller identity comes from the calling process's env and is
  advisory, not a security boundary: a subagent that deliberately unsets
  `CLAUDE_HUB_TAB_ID`/`CLAUDE_HUB_SUBAGENT_ID` or calls the API directly can
  still start an unlinked subagent.
- With more than 20 follow-ups, older turns (except the first) are dropped from
  the record, so `agent result` no longer finds them and their `call_id` is no
  longer idempotent: a retry of a trimmed follow-up's `call_id` starts a new
  turn instead of returning the old result. The create `call_id` (first turn)
  stays replayable until the record is pruned.
- Records written before these follow-ups keep their stored final texts until
  they are pruned. A subagent tab unarchived before the detach change still
  carries its link and can be re-archived once by reconciliation.

## Cross-review fix (Codex, 2026-10-26)

`_close` used to swallow archive failures and still mark the record closed.
The API then reported success, later closes did nothing, and the retirement
that closes descendants never ran. Archive failure now leaves the record
open and retryable, and `close` returns 503. Background closes (parent
retirement, grace close) log the failure; the record stays open for an
explicit retry or startup reconciliation.

Follow-up from the Codex re-review: the terminal manager's archive sets the
tab's `archived` flag before runtime shutdown and persistence. A failure
after that leaves a flagged tab with no retirement callback, and a retry sees
"already archived". Closing a subagent therefore closes its child subagents
directly instead of relying only on that callback. The ordering inside the
terminal manager predates this work, affects every archive, and is not
changed here.
