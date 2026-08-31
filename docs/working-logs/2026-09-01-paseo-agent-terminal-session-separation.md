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

## Claude startup interaction gate

Claude 2.1.159 displays a one-time responsibility disclaimer when
`--dangerously-skip-permissions` starts in a configuration domain that has not
accepted it. That prompt is visible in Terminal but is intentionally hidden by
the Agent surface. Sending a normal chat message at that point submits the
dialog's default `No, exit` choice, leaving the optimistic user bubble waiting
while Claude has already returned to the shell.

For a structured Agent, selecting Solo Mode is the explicit request to launch
with `--dangerously-skip-permissions`. Claude Hub therefore supplies a
secret-free inline settings acknowledgement for that combination only. It
does not modify the user's global `~/.claude.json`; Claude Terminal sessions
retain the native warning and interactive choice.

The live reproduction also exposed an independent persistence boundary: a
named tmux server retains the global environment of the process that first
created it. An integration test had left the preview server with a pytest
temporary `HOME` and `PATH`, so later real Agents launched in the wrong Claude
configuration domain. `_ensure_tmux_server` now refreshes stable launch keys
from the current backend and removes pytest ownership markers before any new
pane is created.

## Key issues / pitfalls

- Do not infer the surface from `agent_type`: a Claude/Codex/Cursor executable
  is valid in both Agent and Terminal sessions.
- Do not reintroduce a per-pane Paseo toggle. A user chooses the session kind
  when creating the session.
- Keep the hidden Agent PTY mounted until native provider transports replace
  it; the current composer sends ordered input through that owner.
- Refresh the named tmux server environment even when the server already
  exists. A process restart alone does not replace tmux's retained global
  environment.
- Never copy credentials into tmux's global environment. Provider credentials
  remain in per-tab mode-0600 launch wrappers/settings.
- Unsupported structured provenance must show an Agent-surface error. It must
  not silently reveal the raw terminal, because that changes the selected
  product contract.
- Legacy standalone tabs remain Terminal to avoid surprising upgrades.
