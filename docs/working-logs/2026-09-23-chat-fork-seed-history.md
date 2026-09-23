# 2026-09-23 — Chat Fork: seed truncated history into the new provider session

## Symptom

In structured Chat, "Fork from here" at a message copied the conversation into
a new tab. The new tab's structured pane showed the copied history, but the
model behaved as if it had none: referencing earlier content ("the code I gave
you", "as decided above") made it answer that it couldn't find it. **UI had the
history; the model's context did not.**

## Root cause (confirmed by the read-only investigation; not revisited)

`POST /{tab}/fork` → `ttyd_manager.fork_tab(tab_id, ordinal)`
(`backend/claude_hub/services/ttyd_manager.py:3613`) does two things:

1. Reads the source `AgentStreamStore`, assigns each event a turn ordinal by
   first appearance of its `turn_id`, keeps events with `ordinal <= k`
   (inclusive — correct), and `replace_all`s them into the **new** tab's store.
2. `create_tab(...)`s the forked tab with the source's launch config but a
   **fresh provider conversation** (new constructive `agent_session_id`,
   deliberately omitting the source transcript/session).

Step 1 only seeds the Hub UI event log. The new native provider session is
started later, lazily, by `TailerManager._get_or_create`
(`agent_stream/tailer.py`) when the user first sends a message — and at that
point nothing injects the copied history. The ordinal boundary was never the
problem; there was simply no handoff of the prefix to the provider.

## Why provider-native resume is not the answer

- Claude: turns spawn `claude --print … [--resume <sid> | --session-id <sid>]`.
  `--resume` re-enters an *entire* prior conversation; there is no CLI surface
  to resume "up to turn k".
- Codex/TraeX: the persistent app-server resumes a whole thread via
  `thread/resume`; likewise no truncation.
- Cursor: `agent --resume <sid>` is constructive but again whole-conversation.

Resuming the source session would either leak turns after `k` (violating the
inclusive ordinal) or alias two tabs to one provider conversation. So the seed
must be carried as content rather than as a session reference.

## Chosen solution: one-shot sentinel prefill on the first turn

Same proven pattern as the first-turn Hub-runtime/self-scheduling guidance
(see `native.wrap_hub_runtime_guidance` and the `<<<HUB_RUNTIME_V1>>>` block):
wrap the copied transcript in sentinels, prepend it **once** to the new
session's first user turn, and strip it everywhere it would otherwise be read
back so it never pollutes the persisted timeline or the UI. The authoritative
Hub echo (`turn_started`) is published with the clean user text before the
transport ever wraps it.

New module `agent_stream/fork_seed.py`:

- `build_seed_body(events)` groups the copied events by `turn_id` in
  first-appearance order (matching the fork ordinal grouping, so interleaved
  turns render coherently), takes the first non-empty `turn_started.summary`
  as `User:` and concatenates that turn's `text_delta` fragments as
  `Assistant:` (deltas are stream fragments and concatenate — see the
  coalescer). Tool calls/results, approvals, status and thinking are ignored;
  returns `None` for a text-less prefix.
- `wrap_fork_seed_history` / `strip_fork_seed_history` — sentinel block
  `<<<FORK_SEED_HISTORY_V1>>> … <<<END_FORK_SEED_HISTORY_V1>>>`. The strip is
  malformed-safe (an open block without its close marker is left untouched).
- Sidecar persistence: `write/read/discard_seed_sidecar` use
  `{STATE_ROOT}/{workspace}/agent_streams/{session_id}.fork-seed.json`, written
  atomically (tmp + replace). This bridges fork time → first send, which can
  be far apart (and across a restart).

Wiring:

- `fork_tab` (`ttyd_manager.py:3717`) computes `build_seed_body(rewritten)`
  after `replace_all` and writes the sidecar. The existing orphan-cleanup
  (delete the new tab on any later failure) covers a failed seed write too;
  deleting the tab runs `discard_session_stream`, which also removes the
  sidecar.
- `TailerManager._get_or_create` (`tailer.py:2227`) reads the sidecar when
  constructing a chat transport and passes `seed_history` + an
  `on_seed_consumed` callback (delete the sidecar) into
  `create_native_session` → every provider session class.
- `ProviderSession` (`native.py`):
  - `_with_first_turn_prefixes` (async) composes the first turn: optional seed
    block + the once-only Hub-runtime block + the real user text.
  - The seed is committed only in `_mark_seed_delivered`, called **after**
    `_send_text` succeeds. On a failed first spawn the seed stays pending and
    is retried; after success the in-memory seed is cleared and the sidecar
    callback deletes the durable copy → at-most-once.
  - Applied uniformly to Claude (SDKUserMessage stdin), Codex/TraeX
    (`turn/start` input text item) and Cursor (stdin prompt). All accept plain
    prompt text, so there is no provider-specific prefill field and no silent
    "UI has history, model does not" gap for any of them.
- Strip on read-back: `claude_jsonl._normalize_user`, codex
  `_normalize_event_msg` (user_message), `cursor_cli_transcript` user rows, and
  the edit-resend matcher `transcript_fork._extract_user_text` all call
  `strip_fork_seed_history` alongside the existing runtime-guidance strip.
- `discard_session_stream` (tab delete / id reuse) removes an unconsumed
  sidecar.

## Scope deliberately unchanged

- Fork ordinal **inclusive** semantics, the per-source fork cap, and launch
  config copy are untouched (no counting changes).
- Terminal (non-Chat) fork is unchanged — no native transport is built.
- Seed is **text-only**. Tool call/results and images are provider-format
  specific and omitted; a text-less prefix seeds nothing (behaves as before).

## Pitfalls / decisions

- **Inject at most once AND retry-on-failure.** Marking the seed injected
  before spawn would lose it on a transient launch failure; never clearing it
  would re-send a large transcript every turn. Compose without committing,
  commit only after `_send_text` returns, delete the sidecar in the same commit
  (guarded so a callback failure doesn't throw the turn).
- **Deltas concatenate.** Assistant text must be the concatenation of a turn's
  `text_delta` payloads, not a join with separators and not one block per delta.
- **Interleaved turns.** Fork ordinal grouping is by first appearance; the seed
  renderer groups the same way (`seen` + first-appearance `order`) so A/B/A
  forks stay coherent.
- **Strip must precede UI and edit-resend matching**, or the first real user
  message in the forked tab would render with the transcript prefixed and the
  edit-resend content match would miss.
- Sidecar uses the same `workspace_id`/`session_id` the tailer passes to the
  `AgentStreamStore` (`"terminal-tabs"`, `terminal-tab-<id>`), resolved through
  the call-time `STATE_ROOT` indirection so tests can monkeypatch it.

## Verification

- New `tests/test_chat_fork_seed_history.py` (15 tests), including the
  end-to-end chain `fork_tab(ordinal=1)` → sidecar → real
  `create_native_session` → first `send_message`, capturing the **actual**
  Claude SDKUserMessage stdin: it contains turns 0–1 user+assistant text and
  does NOT contain turn 2; the sidecar is consumed and the UI event log is
  unchanged with no sentinel leaked. Injection itself is not stubbed — only
  the subprocess boundary is faked.
- Provider coverage: first-turn-only injection asserted for Claude, Cursor and
  Codex; failed-first-send retry asserts the seed is retained and the consumed
  callback fires exactly once; normalizer strip asserts both sentinel blocks
  are removed from a provider user line.
- Regression: existing fork ordinal/cap/cleanup/interleave tests and the
  native-guidance/self-scheduling/image-strip suites stay green.
- `black`, `isort`, `mypy` clean on all touched source files.
