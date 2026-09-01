# 2026-09-01 — Paseo Chat / Terminal Session Separation

## System overview

Claude Hub follows Paseo's product boundary: **Chat** and **Terminal** are
different session types, not presentation modes for the same tab.

- Chat: a Claude, Codex, or Cursor provider with a fixed structured timeline,
  text/image composer, and provider-native event stream.
- Terminal: a Claude, Codex, Cursor, or plain shell profile with a fixed native
  PTY and complete keyboard/TUI behavior.

The persisted `SessionKind` contract is `chat | terminal`. `AgentType` remains
orthogonal and selects the provider executable. The retired persisted value
`agent` is accepted only by state-load migration and is normalized to `chat`;
new API, CLI, and frontend requests never emit it.

```text
new session
├── Chat + Claude/Codex/Cursor -> StructuredPane + native ProviderSession
└── Terminal + provider/plain shell -> TerminalView + ttyd/tmux
```

Workspace orchestrator/reviewer/worker sessions remain Terminal control-plane
runners even when their provider is Claude, Codex, or Cursor. A provider type
must never imply a Chat surface.

## Chat identity and persistence

Each Chat owns an independent durable identity:

- Hub tab/session id and provider type;
- provider conversation/thread id once verified;
- append-only structured event history with monotonic sequence numbers;
- bounded attachment preview cache.

Deleting a Chat stops its provider/tailer and removes its structured history,
cursor, and bounded previews. Merely switching tabs does not delete anything.

## Connection and runtime lifecycle

The browser does not keep every open Chat connected forever.

1. Each visible `StructuredPane` hydrates persisted history, opens SSE as a
   latency accelerator, and holds one authoritative long poll. Both paths are
   sequence-deduplicated.
2. Switching away or unmounting the pane closes its EventSource, aborts the
   long poll, and aborts any outstanding hydration. Multi-pane layouts keep one
   pair of subscriptions per visible Chat.
3. The backend shares one `SessionTailer` per Chat across all subscribers. Once
   the last subscriber leaves, the tailer receives a five-minute grace period
   before its provider transport is stopped.
4. Returning to the Chat restarts the tailer if needed, rehydrates the durable
   event log, and resumes the verified provider conversation id.

Provider process ownership differs by CLI:

- Claude and Cursor use a streaming subprocess per submitted turn and resume
  subsequent turns from the persisted conversation id.
- Codex uses a persistent `codex app-server --stdio` process while the backend
  tailer is warm; the five-minute zero-subscriber reaper releases it.
- Terminal sessions keep their tmux session independently of the browser;
  their raw WebSocket exists only while a Terminal view is mounted.

This is therefore an active-window resource model with durable recovery, not
one permanent browser/provider connection per tab. Rapid Chat switching may
rehydrate history again; a future in-memory timeline cache can optimize that
without changing provider ownership.

Current limitation: idle reaping checks only subscriber count and elapsed
time. If every pane leaves a Chat while an unusually long provider turn is
still running, the five-minute reaper can stop that transport. Protecting an
in-flight turn from zero-subscriber reaping is a follow-up; it is not part of
the current lifecycle guarantee.

## Key issues / pitfalls

- Do not infer `session_kind` from `agent_type`.
- Do not reintroduce a per-pane Chat/Terminal toggle; the choice is fixed at
  creation.
- A Chat must fail closed when its native structured transport is unavailable;
  it must not silently reveal a raw Terminal.
- State migration may read the retired string `agent`, but all new persistence
  must write `chat`.
- The current zero-subscriber reaper can terminate a turn that remains in
  flight beyond the five-minute grace period. A future fix must make idle
  eligibility depend on both subscriber count and provider turn state.
- Provider credentials remain in per-tab mode-0600 launch wrappers/settings,
  never in tmux's global environment.
