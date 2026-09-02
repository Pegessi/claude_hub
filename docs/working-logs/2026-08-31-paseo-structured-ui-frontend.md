# Paseo v2 Structured Terminal UI + Image Composer (frontend)

Date: 2026-08-31

> **Superseded product boundary (2026-09-02):** this note records the initial
> two-view implementation stage. Current Chat creation exists only in the
> top-level Terminal area: `session_kind=chat` owns `StructuredPane`, while
> Agent Workspace orchestrator/reviewer/worker sessions remain Terminal
> control-plane runners and never expose this structured surface. See
> `2026-09-01-paseo-agent-terminal-session-separation.md`.

## Overview

Frontend Wave for the Paseo v2 structured observation plane. Delivers the
first-release product contract: exactly two reversible views of the SAME
managed-agent terminal session — Paseo-style **Structured** and the original
interactive **Raw** — with Raw as the default and always mounted.

## System overview

```
TerminalPane
 ├─ viewMode: 'raw' | 'structured'   (default 'raw')
 ├─ managedSession = sessionForTab(tabId)
 ├─ TerminalView (raw)   ← always mounted; CSS-hidden when structured
 └─ StructuredPane       ← rendered only when viewMode === 'structured'
      ├─ useAgentStream(sessionId)
      │    ├─ capabilities → structured=false → fail-closed (emit fallback-to-raw)
      │    ├─ events (paginated /events → SSE /live → long-poll /wait)
      │    └─ connectionState: idle | hydrating | live | failed
      ├─ timeline = groupEventsIntoTurns(events)
      └─ composer → sendMessage(sessionId, message, attachments)
```

## Module design

### `StructuredPane.vue`

- **Timeline**: `groupEventsIntoTurns` (pure, in `agentStreamTimeline.ts`)
  groups the flat event stream into turns keyed by `TURN_STARTED`. Renders
  user message, assistant text (`TEXT_DELTA`), collapsible thinking
  (`THINKING_DELTA`), tool calls (`TOOL_CALL_STARTED`/`COMPLETED` matched by
  `call_id`/`tool_call_id`), errors, and status events.
- **Connection state**: hydrating spinner, live (no banner), failed →
  `emit('fallback-to-raw')` so `TerminalPane` switches back to raw.
- **Composer**: textarea + image picker, drag-drop, paste. Attachments go
  through `validateImageAttachment` (mime + 8 MB) and `fileToDataUrl`. Send
  calls `workspaceStore.sendMessage(sessionId, message, attachments)`. On
  error, the draft message and attachments are retained.

### `agentStreamTimeline.ts`

Pure grouping function. Extracted from the component so it is unit-testable
without Vue. Handles:
- Turn boundaries (`TURN_STARTED`).
- `TURN_COMPLETED` flipping still-running tools to `completed`.
- Tool call matching by `call_id` (event-level) or `payload.tool_call_id`.
- Orphan `TOOL_CALL_COMPLETED` rendered as a standalone tool entry.
- `approval_required`/`approval_resolved` intentionally ignored (approval UI
  lands later).

### `agentStreamAttachments.ts`

Pure image validation + data-URL helpers. Extracted from `useAgentStream.ts`
for the same testability reason. Re-exported from `useAgentStream.ts` for
backward compatibility.

### `TerminalPane.vue`

- Adds `viewMode` ref and a toggle button (shown only when the tab has a
  managed session).
- **Raw stays mounted**: `TerminalView` is always rendered when a tab is
  assigned; the `.is-hidden` class applies `position: absolute; inset: 0;
  visibility: hidden; pointer-events: none;` — NOT `display: none` — so the
  ttyd iframe and its scrollback survive the switch.
- **Fail-closed**: if `managedSession` is null or `StructuredPane` emits
  `fallback-to-raw`, `viewMode` resets to `'raw'`.

### `useAgentStream.ts`

Stream client (already present from the backend foundation wave). Hydration:
`/capabilities` → fail-closed if `structured=false` → paginate `/events` →
SSE `/live` with `/wait` long-poll fallback.

## Key issues / pitfalls

- **Raw iframe must not unload**: `display: none` on an iframe can reload it
  in some browsers; `visibility: hidden` + `position: absolute` keeps the
  document (and xterm.js scrollback) alive.
- **Fail-closed is the only safe default**: `structured=false` from
  capabilities, a stream hard-failure, or a missing managed session all drop
  back to raw. The structured view is opt-in and never silently shows stale
  data.
- **No secrets/paths in the UI**: only `filename` is shown for attachments;
  `data_url` is used only for the thumbnail preview and the send payload.
  Tool args/results are rendered as-is from the (already redacted) event
  payload.
- **Composer error retention**: a failed `sendMessage` must not clear the
  draft so the user can retry. Only success clears the composer.
