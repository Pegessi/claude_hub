# 2026-09-01 — Paseo Agent / Terminal Session Separation

## System overview

Claude Hub now follows Paseo's product boundary: **Agent** and **Terminal** are
different session types, not two presentation modes for the same tab.

- Agent: Claude, Codex, or Cursor provider; fixed structured conversation UI;
  text and image composer; normalized transcript-backed timeline.
- Terminal: Claude, Codex, Cursor, or a plain shell profile; fixed native PTY
  UI with complete keyboard/TUI behavior.

Both kinds may use the same workspace and cwd. They do not promise identical
conversation state or token-by-token rendering.

## Module design

`SessionKind` is persisted on every tab as `agent` or `terminal`. It is
orthogonal to `AgentType`, which still chooses the executable/profile.

```text
new session
├── Agent + Claude/Codex/Cursor -> StructuredPane
│   └── hidden TerminalView keeps the current CLI transport/input bridge alive
└── Terminal + Claude/Codex/Cursor/plain shell -> visible TerminalView
```

`TabBar.vue` owns the explicit creation choice. `TerminalPane.vue` has no view
toggle and selects exactly one user-facing surface from `session_kind`.
`ttyd_manager.py` persists the choice, preserves it on duplicate, and migrates
legacy state:

- standalone rows without `session_kind` -> `terminal`;
- managed workspace rows with a non-terminal provider -> `agent`.

The terminal manager classifies managed non-shell workspace tabs as Agent at
its persistence boundary. Direct local Cursor Agent creation also pins the
verified Cursor version, data root, conversation
ID, transcript path, and schema needed by the existing transcript adapter.
Cursor Terminal creation deliberately leaves the transport as native terminal.

## Current update semantics

The structured plane continues to observe provider transcript files and emits
normalized blocks through the existing sequence-based stream. This is suitable
for turn/block-level updates but is not a promise of native per-token streaming.
Claude Agent SDK, Codex app-server, and Cursor ACP are deferred transport work.
They can replace the observation source later without changing the Agent /
Terminal product model introduced here.

## Key issues / pitfalls

- Do not infer the surface from `agent_type`: a Claude/Codex/Cursor executable
  is valid in both Agent and Terminal sessions.
- Do not reintroduce a per-pane Paseo toggle. A user chooses the session kind
  when creating the session.
- Keep the hidden Agent PTY mounted until native provider transports replace
  it; the current composer sends ordered input through that owner.
- Unsupported structured provenance must show an Agent-surface error. It must
  not silently reveal the raw terminal, because that changes the selected
  product contract.
- Legacy standalone tabs remain Terminal to avoid surprising upgrades.
