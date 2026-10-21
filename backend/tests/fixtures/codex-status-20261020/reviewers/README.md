# Managed reviewer status evidence (acceptance follow-up)

The four `evo-workspace-reviewer-*-idle.ansi` files are exact raw ANSI tmux
captures of existing managed reviewers 2 (Claude), 3/5/6 (Codex), obtained with
read-only capture-pane commands. Their real Workspace metadata identifies
role=reviewer, session_kind=terminal. Current reviewer-1 is session_kind=chat
(native Codex), so terminal content is not its status source.

`manifest.json` preserves source IDs, provider, kind, role, commands, UTC
capture timestamps and byte SHA-256. Initial shared board/status evidence is
in the handoff evidence/followup-board.json and followup-tab-status.json.
A 240-second read-only observation found no naturally busy shared reviewer.
No shared reviewer was sent input or resized.

The `isolated-managed-reviewer-busy.ansi` and `-idle.ansi` files are **actual
installed Codex 0.163.0 TUI bytes**, not reconstructed text. The task-owned
probe registers a real ManagedSession(role=reviewer, session_kind=terminal)
in an isolated WorkspaceManager/Hub home and binds its TTYDProcess to a private
tmux socket (`codex-status-reviewer-task5`). A loopback Responses mock holds
one turn open; Escape cancels only that private mock turn for idle capture.
This proves actual rendering/status plumbing, not substantive AI review or
external provider execution.

The normal tab-status and workspace-board endpoints were called through ASGI
against that private manager and both returned working then idle for the
reviewer role. Exact code, private config/state, busy/idle API JSON and cleanup
are preserved beside FINDINGS.md in reviewer-probe/. The sample waits 850ms
across cancellation to respect the existing 750ms terminal status cache;
instantaneous old status within that TTL is not claimed as a new bug.

All task-owned tmux/HTTP/Codex processes were stopped after capture, with
CODEX_HOME ownership checked before daemon cleanup. No primary Hub process,
config or running state was modified. Tests independently replay all six
screens through the classifier, board/tab APIs and real CLI formatters.

The Claude pane retains its real final blank line. Do not trim raw bytes to
satisfy source whitespace checks; hashes intentionally cover that trailing row.
