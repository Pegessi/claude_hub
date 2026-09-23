# Chat agent self-guidance (Hub runtime / self-scheduling hint)

Date: 2026-09-22
Branch: `feat/chat-agent-self-guidance` (baseline `main` `e840303`)
Scope: backend only — native Chat transports; no frontend change.

## Why

A native Chat agent has the `claude-hub` CLI on `PATH`, and
`claude-hub schedule create --kind chat_turn` can enqueue a turn for the
**current** Chat conversation. Nothing tells the agent either fact, and it
does not reliably know its own tab id. Without a hint the agent either never
self-schedules or mistakes a Chat for a Terminal and uses
`--kind tab_message`, which types into a terminal pane rather than driving a
native Chat turn.

The fix (1) injects a short, one-time "Hub runtime" block so the agent knows
where it is and how to self-schedule correctly, reusing the existing
sentinel wrap/strip mechanism (same pattern as the question-protocol and
image-attachment guidance), and (2) makes the tab id the block references
actually present in every native Chat provider subprocess.

> **Reviewer MUST-FIX (cycle 1 → fixed cycle 2).** The original task premise
> said the agent "already runs with `CLAUDE_HUB_TAB_ID` in env". That was only
> true for the tmux-shell path (`TTYDProcess._child_env`) and, incidentally,
> for Claude (its per-tab `--settings launch_env/<tab>.settings.json` carries
> the id). A native Chat transport spawns the provider **directly** through
> `ProviderSession._build_env()` = `os.environ + session.env`; the live
> backend process has no tab id and `session.env = dict(tab.env)` deliberately
> excludes it. So Cursor / Codex / TraeX started **without** the var and the
> guidance's `"$CLAUDE_HUB_TAB_ID"` expanded empty →
> `schedule create --tab-id ""` → 400. Cycle 2 adds the overlay in
> `_build_env()` (see below) and per-provider tests.

## Injection design

- New `HUB_RUNTIME_GUIDANCE` + `wrap_hub_runtime_guidance()` /
  `strip_hub_runtime_guidance()` in
  `backend/claude_hub/services/agent_stream/native.py`, using the same
  `<<<HUB_…_V1>>>` / `<<<END_HUB_…_V1>>>` sentinel convention and the same
  fail-safe strip semantics (an open block without its close marker is left
  untouched).
- Injected **once per transport session**, on the first turn that carries
  text. A boolean `_hub_runtime_guidance_injected` lives on the base
  `ProviderSession` and is consulted/set inside `send_message` (under the send
  lock) via `_with_first_turn_hub_guidance(text)`, before the provider-specific
  `_send_text`. Because every native session (`ClaudeNativeSession`,
  `CursorNativeSession`, `CodexNativeSession`, `TraexNativeSession`) funnels
  user text through that base path, all four providers get it with one change;
  Terminal sessions never construct a `ProviderSession`
  (`create_native_session` rejects `AgentType.TERMINAL`), so the plain terminal
  is unaffected.
- `$CLAUDE_HUB_TAB_ID` is rendered **literally**; the model expands it from
  its own env. The backend never substitutes the concrete id.
- An image-only turn (empty text) defers injection to the first turn that
  carries text, so the SDK envelope of an image-only turn keeps its exact
  "image block + empty text block" shape and conversational guidance is never
  attached to an attachment-only delivery.
- The guidance is explicit that scheduling requires an explicit user request.

## Tab-id env overlay (cycle-2 MUST-FIX fix)

`ProviderSession._build_env()` now overlays the tab id for every native Chat
subprocess:

```python
tab_id = getattr(self.session, "tab_id", None)
if tab_id:
    env.setdefault("CLAUDE_HUB_TAB_ID", tab_id)
```

- Sits on the **base** `ProviderSession`, so Claude / Cursor / Codex / TraeX
  all inherit it (and Terminal, which never builds a `ProviderSession`, is
  unaffected).
- `setdefault`, after merging `session.env`, so an explicit value (parent
  process env or `session.env`) stays authoritative and the overlay never
  clobbers it.
- Process env only; it is not written back to `self.env` / `tabs.json`,
  preserving the existing "keep it out of persisted user config" invariant
  that `TTYDProcess._child_env` already follows for the tmux path.
- Claude still also receives it via its `--settings` file; the two agree
  (same tab id) and `setdefault` makes the explicit settings-derived value
  win if ever present in `session.env`.

## Strip / non-leak design

The injected block must reach neither the persisted transcript nor the Chat
UI. Three independent planes are covered:

1. **Authoritative live echo.** The tailer publishes the `turn_started`
   summary from the clean composer `text` *before* calling
   `transport.send_message(...)` (`tailer.py`), so the live user bubble never
   contains the block regardless of provider.
2. **Provider transcript / snapshot replay.** Each provider normalizer strips
   the block when reading a user message from the provider's own transcript:
   - Claude — `claude_jsonl.py` `_normalize_user` (string-content branch).
   - Cursor — `cursor_cli_transcript.py` (alongside question/image strips).
   - Codex/TraeX — `codex_jsonl.py` `_normalize_event_msg` (`user_message`).
3. **edit-resend transcript fork.** `transcript_fork._extract_user_text`
   strips question/image/**hub** blocks before exact-content matching.

## Pitfalls

- **The tab id was not actually in the native Chat provider's env (cycle-1
  blocker).** Guidance text asserting `$CLAUDE_HUB_TAB_ID` exists is not
  enough — the var has to be in the spawned provider's environment. The tmux
  shell overlays it (`_child_env`), but a native Chat transport spawns the
  provider directly via `_build_env()` (`os.environ + session.env`), bypassing
  that overlay; the backend process has no tab id and `tab.env` omits it.
  Claude was covered only by its per-tab `--settings` file; Cursor / Codex /
  TraeX had no carrier at all. Tests that assert only the literal `$VAR` in
  the guidance text miss this — assert the var really lands in
  `_build_env()` per provider.
- **First-turn wrap breaks edit-resend unless stripped for the fork.**
  `fork_transcript` matches the edited turn to a provider user message by
  *exact* normalized text (with ordinal disambiguation). The pre-existing
  question/image blocks are injected on *every* Cursor turn and are
  incidentally tolerated; wrapping only the **first** turn would make editing
  a first turn unmappable (clean Hub text vs. wrapped provider text). The fork
  extractor now strips all three blocks so matching compares clean text to
  clean text.
- **Claude stores the first turn as a list of content blocks** (the
  `SDKUserMessage` envelope), and `_normalize_user` surfaces user text only for
  the string-content form; list text blocks are skipped (tool_result blocks
  are the list case it handles). The strip is still applied to the string
  branch for defense in depth, and a test asserts the list form never surfaces
  the block.
- **`schedule create` requires `--name`.** The hint includes `--name
  "<short name>"` so the command it tells the agent to run actually succeeds
  (the CLI marks `--name` required).
- **`--kind tab_message` is Terminal-only.** The hint names it only to steer
  Chat agents away from it.
- In normal streaming the user message comes from the Hub's authoritative
  echo, not from provider stdout; the transcript strips matter for backfill /
  replay / fork rather than the live bubble.

## Files

- `backend/claude_hub/services/agent_stream/native.py` — guidance text,
  wrap/strip, once-flag, base `send_message` injection, and the
  `_build_env()` tab-id overlay.
- `backend/claude_hub/services/agent_stream/claude_jsonl.py`,
  `codex_jsonl.py`, `cursor_cli_transcript.py` — transcript strip.
- `backend/claude_hub/services/agent_stream/transcript_fork.py` — fork-match strip.
- `backend/tests/test_agent_stream_native.py` — unit tests.

## Tests

See the task report for exact commands/results. Coverage:

- wrap→strip round trip; no-op without block; malformed open block untouched.
- literal `$CLAUDE_HUB_TAB_ID` + `chat_turn` guidance, no baked-in id.
- first-turn-only injection for Claude, Cursor, Codex via `send_message`
  (second turn is bare text); helper injects once.
- Terminal never constructs a native transport.
- each provider normalizer strips the block (clean summary, no sentinel);
  Claude list-content form never surfaces it.
- image + question + hub blocks all strip back to the original text.
- transcript-fork extraction strips the block so edit-resend still matches.

## Not covered / possible next steps

- No runtime/end-to-end Chat launch was exercised (per task boundaries: pytest
  is the source of truth; no live-Hub restart). The behavior is verified at the
  transport-construction and normalization boundaries with mocked
  spawn/JSON-RPC.
- Injection is per-process-session; a cold restart that builds a fresh
  transport before any resume will re-inject once. This is intentional and
  cheap (one block) but could later be keyed off a persisted turn count if
  strict once-per-conversation is ever required.
