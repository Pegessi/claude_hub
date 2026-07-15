# Terminal Auto-Refresh On Agent Turn Completion

Date: 2026-07-15
Scope: frontend (`frontend/src/components/TerminalView.vue`)

## Problem

After an agent (Claude / Codex / Cursor) finishes a round of conversation
processing, the terminal display can end up visually misaligned — the prompt
line may not be at the bottom of the viewport, or the rendered output may not
match the actual buffer state. The existing manual refresh button (in
`TerminalPane.vue`) and the tab-switch scroll-bottom logic both fix this, but
they require explicit user action or a tab switch. The user requested a small
optimization: when a turn completes, if the terminal is currently displayed,
automatically trigger a refresh to avoid the misalignment.

## Solution

The fix is a single watcher in `TerminalView.vue` that reacts to the agent's
runtime status transitioning from `working` to `idle` or `attention`.

### Status source

`terminalStore.agentStatuses` is polled every 5 seconds by
`fetchAgentStatuses()` and exposed via `storeToRefs`. Each entry is a
`TerminalAgentStatus` with `tab_id` and `status` (`AgentRuntimeStatus`). The
`_classify_agent_status` backend logic maps `working` → `idle` when the shell
prompt becomes visible and `working` → `attention` when the agent is waiting
for input — both represent "the turn is done producing output."

### Displayed-terminal-only

`TerminalView.vue` renders one iframe per cached tab, but only the iframe
matching `props.tabId` is visible (`:class="{ active: cachedTabId === tabId }"`).
Therefore resolving the status by `props.tabId` and refreshing only
`props.tabId` naturally limits the behavior to the displayed terminal.
Hidden/cached iframes and terminals in other panes are never touched.

### The watcher

```ts
const lastAgentStatus = ref<AgentRuntimeStatus | null>(null)

const currentAgentStatus = computed<AgentRuntimeStatus | null>(
  () => agentStatuses.value.find(s => s.tab_id === props.tabId)?.status ?? null
)

watch(currentAgentStatus, (newStatus) => {
  if (
    lastAgentStatus.value === 'working' &&
    (newStatus === 'idle' || newStatus === 'attention')
  ) {
    postTerminalHistoryRefresh(props.tabId, {
      reason: 'auto-round-complete',
      scrollToBottom: true,
    })
  }
  lastAgentStatus.value = newStatus
})
```

The component-local `postTerminalHistoryRefresh(tabId, { reason, scrollToBottom })`
is used (not the global `window.__claudeHub.refreshTerminalHistory`) to avoid
split-pane targeting issues, per the reviewer note. The `reason:
'auto-round-complete'` distinguishes auto refreshes from manual ones in logs.

### Tab-switch guard

The existing `props.tabId` watcher resets `lastAgentStatus.value = null` on
every tab change. This prevents a status transition observed on tab A from
triggering a refresh after the user switches to tab B (acceptance criterion 3).
The reset happens before any `currentAgentStatus` reaction, so the edge
detection always compares statuses belonging to the same tab.

## Key issues / pitfalls

- **Flicker is acceptable.** The existing tab-switch code deliberately avoids a
  full history replay because of visible flicker
  (`TerminalView.vue:403-406`). The auto-refresh introduces the same flicker
  on turn completion; this is acceptable per the user's stated intent and the
  Goal Packet.
- **Use the local refresh, not the global.** `window.__claudeHub.refreshTerminalHistory`
  resolves the target via `activePaneTabId`, which can point at a different
  pane in split layouts. The component-local `postTerminalHistoryRefresh(props.tabId)`
  always refreshes exactly this component's displayed terminal.
- **No extra debounce.** `agentStatuses` only changes when the backend poll
  returns a genuinely different status (`statusesEqual` guard in the store), so
  the watcher fires at most once per real transition. No additional debounce is
  needed.
- **`offline` is ignored.** The edge check only matches `working` → `idle` or
  `working` → `attention`; `offline` (and any other transition) does nothing,
  per out-of-scope.

## Validation

- `cd frontend && pnpm lint` — clean, no errors.
- `cd frontend && pnpm build` (`vue-tsc` + `vite build`) — clean, no type or
  build errors.
- Manual test 1: open an agent terminal, send a query, wait for the agent to
  finish processing; the terminal auto-refreshes and scrolls to the bottom once
  the status goes `working` → `idle`/`attention`.
- Manual test 2: switch to a second tab while the first tab's agent is still
  `working`; the first (hidden) terminal does not trigger a visible refresh,
  and switching back does not fire a spurious refresh.

## Files changed

- `frontend/src/components/TerminalView.vue` — added `computed` +
  `AgentRuntimeStatus` imports, `agentStatuses` from `storeToRefs`,
  `lastAgentStatus` ref, `currentAgentStatus` computed, the status-transition
  watcher, and the `lastAgentStatus` reset in the existing `props.tabId`
  watcher.
- `CHANGELOG.md` — added "Unreleased / feat: Auto-refresh visible terminal
  when agent finishes a turn" entry.
