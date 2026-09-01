# 2026-09-01 — Paseo Native Provider Transports

## System overview

Claude Hub's previous structured surface observed provider transcript files
through a 1s-polling `SessionTailer`. This delivered turn/block-level updates,
not true incremental output. Paseo instead owns each provider session and
consumes its native event stream. The new transports apply that same boundary:
provider deltas reach the structured pane as they are emitted.

Terminal sessions keep tmux+ttyd. Chat sessions use a `ProviderSession` that
owns the provider subprocess, parses its native stream, and feeds normalized
`AgentStreamEvent`s into the existing store + SSE fanout pipeline.

## ProviderSession contract

```text
ProviderSession
├── start()              launch subprocess with native streaming flags
├── send_message(...)    atomically submit text + provider-supported images
├── acknowledge_turn_complete()
│                        release the busy guard after persistence/fanout
├── stop()               terminate subprocess
├── read_line()          await one parsed JSON record from stdout (or None on EOF)
└── capabilities         StreamCapabilities for this transport
```

The tailer stamps the frontend-generated `client_turn_id` onto the authoritative
`turn_started` record and every provider event. A turn remains busy until its
terminal signal has been persisted and fanned out, preventing a second send
from stealing the active turn identity.

## Selected transports

| Provider | Command | Delta source |
| --- | --- | --- |
| Claude | `claude --print --verbose --input-format stream-json --output-format stream-json --include-partial-messages` | `stream_event.content_block_delta` (`thinking_delta`, `text_delta`) |
| Codex | `codex app-server --stdio` (JSON-RPC) | `item/agentMessage/delta`, `item/reasoning/textDelta` |
| Cursor | `agent --trust --print --output-format stream-json --stream-partial-output` | `thinking/delta`, `assistant` partial text chunks |

### Claude event shape (verified)

```json
{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Hello"}}}
{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"The user..."}}}
```

`message_start` / `content_block_start` / `content_block_stop` /
`message_delta` / `message_stop` bracket the deltas. `assistant` snapshots
and `result` are also emitted.

### Codex app-server notification shape (from generated schema)

```json
{"method":"item/agentMessage/delta","params":{"delta":"Hello","itemId":"...","threadId":"...","turnId":"..."}}
{"method":"item/reasoning/textDelta","params":{"delta":"...","itemId":"...","threadId":"...","turnId":"...","contentIndex":0}}
```

`ItemStarted` / `ItemCompleted` bracket items. `turn/start` carries the user
prompt; the response streams as `AgentMessageDelta` notifications.

### Cursor event shape (verified)

```json
{"type":"thinking","subtype":"delta","text":"Preparing a three-word"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hello"}]}}
```

Each `assistant` line is a growing text chunk; the final line is the complete
message. `thinking/completed` ends the reasoning block.

The installed executable was probed directly as
`agent --version` → `2026.08.25-3e8eec8` and `agent --help`. That build exposes
`--output-format stream-json`, `--stream-partial-output`, and `--resume`, but no
ACP server command and no image-input option. The native Cursor adapter
therefore uses the executable's supported partial-event protocol rather than
PTY/transcript scraping; image capability is explicitly false instead of
inventing an ACP or attachment path that this binary cannot provide.

## Phased file list

### Phase 1 — ProviderSession + Claude transport (RED→GREEN)

- `backend/claude_hub/services/agent_stream/native.py` — `ProviderSession`
  base, `ClaudeNativeSession`, `CodexNativeSession`, `CursorNativeSession`
  stubs; line reader + stdin writer.
- `backend/tests/test_agent_stream_native.py` — RED tests for Claude event
  parsing (thinking_delta → `THINKING_DELTA`, text_delta → `TEXT_DELTA`,
  message_start → `TURN_STARTED`, result → `TURN_COMPLETED`).

### Phase 2 — Tailer integration

- `backend/claude_hub/services/agent_stream/tailer.py` — accept an optional
  `native_transport`; when set, read from `read_line()` instead of polling a
  file. Reuse `normalize_line`, store append, fanout.
- `backend/claude_hub/services/agent_stream/registry.py` — return a native
  transport for `session_kind == chat`; keep transcript adapters for
  terminal sessions.

### Phase 3 — Codex app-server + Cursor transports

- `native.py` — implement `CodexNativeSession` JSON-RPC handshake
  (`initialize` → `thread/start` → `turn/start`) and notification parsing.
- `native.py` — implement `CursorNativeSession` (thinking/delta + assistant
  partial chunks).
- Tests for both.

### Phase 4 — ttyd_manager launch + send path

- `ttyd_manager.py` — for `session_kind == chat`, keep only an inert lifecycle
  shell in tmux; the `ProviderSession` is the sole owner of the provider CLI.
- `agent_stream/tailer.py` — route each composer turn through the single atomic
  `send_message(text, images)` path and acknowledge completion only after the
  terminal event is durably appended and fanned out.
- `api/agent_stream.py` — `POST /sessions/{id}/stream/send` for text + images.

### Phase 5 — Frontend incremental rendering

- `frontend/src/utils/agentStreamSequence.ts` — merge SSE and long-poll into a
  contiguous, exactly-once visible sequence; future events wait for gaps.
- `frontend/src/utils/agentStreamTimeline.ts` — reconcile by stable turn/tool
  identity rather than current position or user-text equality.
- `frontend/src/utils/textReveal.ts` — Paseo-compatible 60 Hz / 150 ms paced
  reveal over the authoritative accumulated text, flushed at turn completion.
- `frontend/src/components/StructuredPane.vue` — optimistic composer turns use
  `client_turn_id`, sticky-to-bottom preserves deliberate scroll detachment,
  and image capability is provider-specific.

## Control-plane boundary

User-created `SessionKind.CHAT` tabs are native and structured. Hub-managed
orchestrator/reviewer/worker sessions are control-plane runners and must remain
raw TUI sessions; converting them to inert Chat lifecycle shells prevents task
dispatch and recovery. Regression tests cover both creation paths.

### Phase 6 — Validation

- Backend: `pytest`, `black`, `isort`, `mypy`.
- Frontend: `lint`, `typecheck`, `build`.
- E2E: long reply streaming for all three providers.

## Browser and provider acceptance evidence

The isolated preview (`18173` backend / `5275` frontend) was exercised against
the installed real CLIs rather than a mocked stream:

- Claude emitted a contiguous 600-event long turn (376 thinking deltas and 222
  text deltas), accepted a real UI screenshot, and retained enough provider
  history across a cold backend restart to answer that the original request
  contained 12 numbered lines.
- Codex emitted 97 text deltas for the initial long turn. After the lifecycle
  race below was fixed, the same resumed thread emitted 11 more text deltas and
  accepted a real 1 MB screenshot through `localImage`, answering `YES` before
  one completed signal.
- Cursor emitted 37 thinking deltas and 43 text deltas. Its installed CLI has
  no image-input contract, so an attachment is rejected with HTTP 400 and the
  composer does not expose a misleading image control.
- A browser-side 100--150 ms sampler observed a Codex response grow through 28
  distinct DOM lengths over about 4.4 seconds. The conversation viewport ended
  with `distanceFromBottom == 0`, proving paced reveal and sticky-tail behavior
  in the rendered UI rather than only at the API boundary.

Final focused validation: 203 backend tests, 133 frontend tests, backend Black
and isort checks, mypy over 15 touched source files, frontend ESLint, typecheck,
and production build.

## Codex first-touch lifecycle race

On a cold tab, the native push loop and the first composer send can both call
`CodexNativeSession.start()`. Before serialization, each call could spawn an
app-server and then both reader tasks would dereference the later overwritten
`self._process`. Python consequently raised `readuntil() called while another
coroutine is already waiting for incoming data`, severing the structured
stream.

Codex startup and shutdown now share a lifecycle lock. The handshake is
single-flight, a second caller reuses the healthy reader, and a half-started
process is terminated and reset before retry. A concurrent-start regression
test asserts exactly one subprocess spawn and one live stdout reader.

## Fail-closed rules

- If the provider binary is missing, the Chat surface fails closed instead of
  mounting a raw-terminal fallback. A per-turn non-zero exit is persisted as an
  error plus exactly one failed completion; Codex app-server EOF hard-fails the
  structured session.
- Codex app-server handshake failure (no `initialize` response within 5s)
  fails closed.
- Cursor without `--trust` or unsupported `cursor_transport` fails closed.
