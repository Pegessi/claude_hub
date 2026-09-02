# 2026-08-31 — Paseo v2 direct Terminal tabs

> **Superseded product boundary (2026-09-02):** this note records the former
> same-tab Terminal/Paseo design. Current Chat creation exists only in the
> top-level Terminal area as a fixed `session_kind=chat` surface. Agent
> Workspace managed sessions remain Terminal control-plane runners and never
> mount `StructuredPane`. See
> `2026-09-01-paseo-agent-terminal-session-separation.md`.

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
5. **The shared input ring must not be decoded in place.** Chromium refuses a
   `TextDecoder.decode()` view backed by `SharedArrayBuffer`. That exception
   occurred before the ring tail advanced, permanently blocking the first
   structured key and every key behind it. Decode a copied, ordinary
   `Uint8Array` record instead; the `sendTerminalKey` boundary returns whether
   it has a target so the composer can retain a failed draft.
6. **EventSource availability is not a freshness guarantee.** In the isolated
   preview, Claude and the backend event store both had a response within two
   seconds while the mounted Structured view remained on its optimistic
   pending turn. The old client started long-poll only when `EventSource` was
   absent, so a connected-but-buffered SSE path had no reconciliation reader.
   Long-poll now remains authoritative while SSE is an optional accelerator;
   both paths deduplicate by `stream_sequence`.
7. **Prompt delivery must be ordered as a unit.** A long Chinese prompt was
   visible in the Raw Claude input box while neither the Claude transcript nor
   the normalized stream advanced beyond the prior turn. The per-character
   SAB path had rendered the prompt but left its final Enter behind. Direct
   structured text now uses a dedicated bulk terminal frame containing the
   prompt and submit carriage return, explicitly targeted to the owning tab.
8. **Following output is layout state, not event-count state.** The initial UI
   scrolled only when `events.length` changed. It missed optimistic pending
   turns, same-row text growth, and Markdown reflow. Paseo commit
   `7ae5133ed272dd6c48f0324682898e0d489f1161` was used as an external design
   reference: its web timeline separates sticky-bottom from detached reading,
   observes content geometry, anchors on send, and verifies the post-layout
   position. Claude Hub independently implements the same interaction contract
   with a 64 px tail threshold, `ResizeObserver`, a two-frame settle check,
   lifecycle cancellation, and an explicit `Latest` button. No Paseo source or
   types are copied (Paseo is AGPLv3).

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
- End-to-end isolated preview: a Structured text submission was observed in
  the corresponding Raw Claude terminal; after the timeline poll it also
  appeared in Structured. An image pasted into the Structured composer reached
  Claude's native input as `[Image #1]`. These are terminal-ingress checks;
  agent-provider response availability is intentionally a separate concern.
- Freshness reproduction: the original direct-tab store persisted its latest
  assistant text at `14:19:09Z`, while a screenshot captured at `14:19:20Z`
  still showed only the optimistic pending turn. After enabling concurrent
  long-poll reconciliation, a pre-armed `/stream/wait` request received the
  next test event in 1.746 seconds, including a deliberate one-second delay
  before submission (about 0.75 seconds of source-to-client latency).
- Frontend validation after the freshness fix: `pnpm lint:check`, 118 unit
  tests, `pnpm build`, and `git diff --check` pass. A real long-answer provider
  run was attempted separately but day1 remained in upstream capacity retries;
  this does not weaken the measured stream-reader result.
- Long-prompt diagnosis: tmux showed the complete latest prompt still inside
  Claude's input editor, while the pinned transcript remained byte-for-byte at
  the prior turn and `/stream/events` contained only that prior turn. This
  proves the reported absence was ingress failure rather than response
  truncation in the Structured renderer.
- Frontend validation after ordered prompt delivery and timeline anchoring:
  `pnpm lint:check`, 122 unit tests, `pnpm build`, and `git diff --check` pass.
  Browser-driven validation remains bounded because the connected Edge
  extension timed out while claiming the dedicated isolated test tab; no
  fallback browser automation was used and the user's active Tab 1 was not
  modified.
