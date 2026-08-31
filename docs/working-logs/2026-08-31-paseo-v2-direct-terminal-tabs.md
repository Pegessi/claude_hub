# 2026-08-31 — Paseo v2 direct Terminal tabs

## System overview

Paseo is a second representation of the same agent conversation as Raw
Terminal. Agent Workspace sessions already have a managed-session id and a
durable message outbox, but a tab created from the Terminal `+` flow has only a
provider conversation id. It must not be forced into a visible Workspace just
to render the two-view UI.

The Terminal pane therefore exposes the two peers as labelled `Terminal` and
`Paseo` controls. Claude and Codex terminal tabs can enter Paseo immediately,
including before their provider has created the first transcript file. Plain
shell tabs remain Raw-only. Cursor remains available when its verified ACP or
terminal-transcript provenance is present; raw Cursor transport remains
fail-closed.

## Module design

- `api/agent_stream.py` derives an ephemeral `ManagedSession`-shaped stream
  descriptor from a live `TerminalTab`. Its id is namespaced as
  `terminal-tab-<tab-id>` and its append-only store lives under the isolated
  `terminal-tabs` namespace. It never appears in the Workspace board or
  participates in task scheduling.
- `/api/workspaces/tabs/{tab_id}/stream/{capabilities,events,wait,live}` uses a
  dedicated tailer manager whose session getter re-resolves the tab on every
  poll. Deleting the tab terminates a live stream rather than heartbeating
  forever.
- Claude and Codex source files are created lazily after the first prompt.
  Direct-tab capability therefore advertises a ready structured composer when
  an adapter is valid even with `sources=[]`; the tailer keeps the normal
  discovery grace window and fails closed if a source never arrives.
- For a local tab without an explicit cwd, the stream descriptor records the
  backend process cwd because that is the directory inherited by its launcher
  shell. This keeps Claude's first transcript lookup aligned with the running
  Raw tab.
- `useAgentStream` selects either the managed-session or direct-tab endpoint.
  `StructuredPane` preserves the durable Workspace send path for managed
  agents. A direct tab instead queues text through its still-mounted Raw
  terminal and transfers image attachments through the existing clipboard
  bridge before sending Ctrl-V, so both views retain one terminal owner.

## Key issues and pitfalls

1. **Provider id is not managed-session id.** `TerminalTab.agent_session_id`
   identifies a Claude/Codex/Cursor conversation; it cannot be passed to
   `/api/workspaces/sessions/{id}/stream/*`.
2. **The first empty state is valid.** Treating a missing source file as
   unsupported hides the composer before the only action that can create that
   file. Adapter availability and source discovery are separate checks for a
   direct tab.
3. **Do not unmount Raw on Paseo.** Direct composer input relies on the existing
   terminal iframe queue and image clipboard mechanism. The Raw view stays
   mounted and invisible, preserving scrollback and the terminal's single
   input owner.
   `visibility: hidden` alone is insufficient: ttyd's active iframe sets
   `visibility: visible` itself, which overrides the inherited hidden value.
   The Raw wrapper must therefore own an opacity and stacking boundary while
   Paseo is visible. The pane header remains above both surfaces and always
   exposes the labelled `Terminal | Paseo` control.
4. **Direct input has terminal semantics.** It intentionally does not claim
   the managed Workspace outbox's at-least-once receipt/ACK protocol; a direct
   terminal message is equivalent to a user typing into that Raw pane.

## Verification

- `pnpm lint:check` and `pnpm build` pass.
- `uv run pytest tests/test_agent_stream.py` passes (27 tests), including
  direct tab descriptor metadata and inherited-cwd coverage.
- `black`, `isort`, and `mypy` pass for `api/agent_stream.py`.
- Isolated preview: Vite `5275` proxies the direct-tab capability endpoint to
  backend `18173`, which returns `structured: true` for an empty Claude tab.
  The preview uses its own runtime home, `tmux -L ch-paseo-ui-preview`, and
  ttyd base port `19250`; live `5173/8173` and the default tmux server remain
  untouched.
- Frontend revalidation after the exclusive-surface fix: `pnpm lint` and
  `pnpm build` pass. The view has been checked statically against the confirmed
  direct-tab stream contract; the isolated preview remains available for the
  final user visual pass.
