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

A follow-up concern was that the `working` status classification itself was
inaccurate: sometimes the agent stops working or finishes but forgets to
report, and the status stays `working` for up to 3 minutes. This prevents the
auto-refresh (which depends on the `working` → `idle`/`attention` transition)
from firing promptly.

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

## Backend: making `working` status accurate

The frontend watcher depends on the backend `_classify_agent_status`
(`ttyd_manager.py`) correctly flipping from `working` to `idle`/`attention`
when the agent finishes. Two issues made this flip slow or unreliable:

### Issue 1: working markers took priority over a bare shell prompt

The classification order was ATTENTION → WORKING → IDLE. Working markers
(`esc to interrupt`, `running…`, the Claude spinner line, etc.) are matched
against the bottom 10 lines, while the bare shell prompt (`❯`, `$`, `#`, …)
was only checked on the last line — *after* the working patterns. So an agent
that had finished and returned to a shell prompt but still showed
`esc to interrupt` a few lines up stayed `working` instead of flipping to
`idle`.

**Fix:** reorder `_classify_agent_status` so a bare shell prompt on the last
line and the Claude/Codex idle hints (`? for shortcuts`, `/ for commands`)
are checked *before* the working patterns. ATTENTION still wins (an agent
asking for input is more specific than a prompt). The new order is:

1. ATTENTION tail patterns (bottom 5 lines)
2. Bare shell prompt (last line) → IDLE
3. IDLE tail hints (bottom 5 lines) → IDLE
4. Codex working (full frame)
5. WORKING tail patterns (bottom 10 lines)
6. Claude working status regex (bottom 10 lines)
7. Cursor working status regex (bottom 10 lines)
8. Foreground command is `claude`/`codex`/`agent` → IDLE
9. Default → IDLE

A shell prompt on the last line is the canonical "the turn is done" signal,
so it must take priority over working markers in the scrollback above.

### Issue 2: 180-second staleness window

When an agent stops working (frame freezes) without emitting a clean shell
prompt — e.g. it crashed or got stuck — `working_or_stale()` kept the status
as `working` for `_WORKING_FRAME_STALE_SECONDS = 180` (3 minutes) before
flipping to `attention`. During those 3 minutes the auto-refresh never fired.

**Fix:** reduce `_WORKING_FRAME_STALE_SECONDS` from 180.0 to 45.0. This is
still well above the ~1s spinner tick and the 5s frontend poll interval (so a
slow tool call that briefly stops repainting is not flagged prematurely), but
surfaces a genuinely stopped agent in ~45s instead of ~3min. The hash is
computed on ANSI-stripped output, so cursor blinks and color codes do not
reset the staleness timer — only real content changes do.

### Tests

Added to `tests/test_ttyd_manager.py`:

- `test_shell_prompt_takes_priority_over_working_markers` — a frame with
  `❯` on the last line and `esc to interrupt` in the bottom 10 → IDLE.
- `test_idle_hints_take_priority_over_working_markers` — a frame with
  `? for shortcuts` in the bottom 5 and `esc to interrupt` in the bottom
  10 → IDLE.
- `test_frozen_working_frame_45s_window_classifies_as_stuck` — a frame
  frozen past the 45s window → ATTENTION.
- `test_working_frame_within_45s_window_stays_working` — a frame frozen
  within the 45s window → WORKING (no false positive on slow tool calls).

All 16 classification tests and the full 74-test suite pass. black / isort /
mypy clean on the touched backend files; frontend lint + build clean.

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
- `backend/claude_hub/services/ttyd_manager.py` — reordered
  `_classify_agent_status` so a bare shell prompt and idle hints take priority
  over working markers; reduced `_WORKING_FRAME_STALE_SECONDS` from 180.0 to
  45.0 so a frozen working frame flips to `attention` faster.
- `backend/tests/test_ttyd_manager.py` — added 4 tests for the new
  classification behavior.
- `CHANGELOG.md` — added "Unreleased / feat: Auto-refresh visible terminal
  when agent finishes a turn" entry.
