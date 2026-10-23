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
  = chat`), created through `ttyd_manager.create_tab` with `solo_mode` on by
  default because no one can approve interactive prompts. The tab records
  `parent_tab_id` (the caller's Chat) and `subagent_id`. Older tab records do not
  have these keys, and empty or malformed values are normalized to `None`.
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
  backend owner) stores each subagent and every turn's status, final text
  (capped at 1M characters) and error. Closed records are pruned after 7 days,
  and at most 500 are kept. When the backend starts, a turn still marked
  `running` becomes `failed` ("Claude Hub restarted …"), so a waiter never hangs.
  Non-kept subagents whose turn ended this way are then closed.
- **Idempotency.** Each `call_id` is serialized by a keyed lock. Repeating a
  create or follow-up with the same `call_id` returns the original
  subagent/turn. If the request body differs, the API returns 409 and never
  creates a second tab or turn. The CLI generates one `call_id` per invocation and
  retries only transport failures, using the same body.
- **Lifecycle.**
  - `keep=false` (default): the tab is archived after the turn finishes, whether
    it completes, fails, times out or is cancelled. History remains restorable.
  - `keep=true`: the tab stays open for `POST /turns` until it is closed.
  - Closing cancels any running turn and archives the tab.

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

## API (`/api/subagents`, authenticated like other Hub routes)

| Method and path | Purpose |
| --- | --- |
| `POST /api/subagents` | Create the subagent and start its first turn; returns `SubagentTurnResult` (201, `status: running`) |
| `POST /api/subagents/{id}/turns` | Start a follow-up turn on a kept subagent. Returns 409 if the subagent is closed, not kept or busy |
| `GET /api/subagents/{id}/turns/{turn_id\|latest}?wait=S` | Long-poll the turn result (`wait` ≤ 60 s) |
| `GET /api/subagents?parent_tab_id=…&include_closed=…` | List subagent summaries |
| `GET /api/subagents/{id}` | Full record, including all turns |
| `POST /api/subagents/{id}/cancel` | Cancel the running turn |
| `POST /api/subagents/{id}/close` | Cancel the turn and archive the tab |

Create body: `agent_type` (`claude|codex|traex|cursor|maso`; `terminal` is
rejected), `prompt`, optional `model`, `effort`, `cwd`, `env_preset`, `env`,
`parent_tab_id`, `name`, `keep`, `call_id`, `timeout_seconds` (default 1800,
max 86400) and `solo_mode`.

- **Model and effort.** The model is mapped to the env key the Chat transport
  already reads (`ANTHROPIC_MODEL`, `CODEX_MODEL`, `CURSOR_MODEL`, `MASO_MODEL`).
  Only Codex and TraeX accept `effort` (`CODEX_REASONING_EFFORT`); other
  providers return 400 and should use a model variant instead.
- **cwd.** Defaults to the parent's cwd and must be an existing absolute
  directory.
- **Parent tab.** A missing parent returns 404; an archived parent returns 409.

Result: `{subagent_id, tab_id, turn_id, status: running|completed|failed|timeout|cancelled,
final_text, error, started_at, completed_at, keep, closed}`.

## CLI

```bash
claude-hub agent run --provider codex [--model M] [--effort E] [--cwd DIR] \
  [--env-preset P] [--keep] [--name N] [--timeout 30m] [--background] [--json] \
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
- **Interrupting.** Ctrl-C while blocking calls `cancel`.
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
the `agent run` help repeat the same recipe.

## Validation

- `tests/test_subagents.py` uses a faked Chat runtime and the real API router.
  It covers:
  - blocking and background results, idempotent retries, kept follow-ups, and
    non-kept archive;
  - timeouts (status `timeout`, with the turn cancelled), cancel and failure;
  - parent archive and delete, validation errors, restart recovery with orphan
    sweep, and tab-record round-trip.
- `tests/test_cli_subagents.py` covers CLI output, exit codes, options, stdin and
  file prompts, transport retries with the same call_id, Ctrl-C cancellation and
  the companion commands.
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
  a turn and the result is `failed` ("requires … Web Chat"), so subagents run in
  solo mode by default.
- After a backend restart, an interrupted turn is recorded as `failed`. For a
  kept subagent, Hub's own restart continuation may still resume that Chat turn
  in the tab, but its answer is not linked back to the failed record.
- Records are not scoped per user, just like tabs. Any authenticated Hub user can
  list, read and close subagents.
- Nested subagents (a subagent that calls `agent run`) work, and parent closure
  cascades through the listener. No depth limit is enforced.
