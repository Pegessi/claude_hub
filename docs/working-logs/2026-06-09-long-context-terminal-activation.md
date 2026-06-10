# Long-Context Terminal Activation

## System Overview

Terminal iframes are same-origin ttyd pages wrapped by `TerminalView.vue`. The
parent component controls iframe caching, tab activation, resize/theme messages,
and manual history refresh. The backend ttyd proxy injects the xterm replay
script that fetches tmux history and reconstructs scrollback.

## Module Design

- Tab activation is now a prompt-first action: cached iframes receive
  `terminal-scroll-bottom`, not a full tmux history refresh.
- Manual refresh remains the full-history recovery path and continues to fetch
  the full `FULL_HISTORY_LINES` snapshot.
- Initial iframe attach uses bounded history snapshots:
  - agent TUIs (`claude`, `codex`, `cursor`): smaller tail for fast prompt
    visibility.
  - plain terminals: larger tail for normal shell scrollback.
- Agent TUI initial replay is conditional: short captured histories skip replay
  so fresh Claude/Codex/Cursor startup screens can render live from ttyd, while
  genuinely long histories still take the bounded replay path.
- Single-pane mode keeps a small LRU cache of recent terminal iframes so
  switching among active workspace agents does not repeatedly reload ttyd and
  replay history.

## Key Issues / Pitfalls

- Do not reintroduce automatic full history refresh on tab activation. It makes
  long-context tabs visibly replay old scrollback before the prompt becomes
  usable.
- Keep hidden cached iframes out of resize/layout work. Session binding is by
  stable tab id, and resize messages should remain scoped to the active iframe.
- Agent TUIs should not receive automatic history resync during live redraws;
  cursor-relative screen updates can be corrupted by plain tmux snapshot replay.
- Do not replay tiny early agent snapshots. Fresh agent tabs may emit the logo
  and guidance after the first history capture; replaying the early snapshot
  can hide those live startup frames.
