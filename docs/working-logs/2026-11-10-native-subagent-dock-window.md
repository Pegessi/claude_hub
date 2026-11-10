# Native sub-agents in the Agents dock (2026-11-10)

Branch `codex/native-subagent-dock`, delivery only — nothing merged or pushed.

## User report

> 希望原生 subagent 的显示是不是也能做成现在和经由 claude hub 一样的显示（有个悬浮窗，可展开，可点击看这种）？

Hub children (`claude-hub agent run`) already had the pane-chrome Agents pill
with a gutter rail / popover / phone sheet and one-click jump. Provider-native
children (Claude `Agent`, Cursor `Task`, TraeX `spawnAgent`) only rendered
inline inside their parent turn, so a reader running several delegations had no
pane-level entry point and no place to read one child on its own.

## What was already there (and what was missing)

- Hub side: `/api/subagents` polling (`useParentSubagents`) → `ChatSubagentDockPill`
  → rail (wide panes), anchored popover (narrow), bottom sheet (phones) → `jump`
  switches to the child's Chat tab.
- Native side: the reducer groups child events by `payload.subagent_thread`
  into one `subagents` part per parent turn; the reader can only expand it inside
  the transcript, and a folded historical turn hides it behind the process
  header.

The data for a dock row therefore already existed in `turn.parts`: the missing
pieces were a projection, a place to put the control, and somewhere to read the
child — a native child has no Chat tab to jump to.

## Changed (frontend only, no backend or protocol change)

- `presentation/nativeSubagentRows.ts`: `turns → NativeSubagentRow[]`
  (newest first, bounded at 30 like the Hub chips), plus `findNativeSubthread`
  for resolving a row back to the live child. Rows are read from `turn.parts`,
  never from presentation output, so a folded turn still lists its children; a
  per-turn memo keyed on `turn.renderRevision` (which the reducer bumps on every
  visible child mutation) keeps the projection O(changed turns) per stream batch.
- `subagentJump.ts`: one row vocabulary for both sources — `SubagentJumpChip`
  gained `kind: 'hub'` / `canOpen: false`, and `SubagentDockRow` is their union.
  `subagentDockCounts` only needed the shared `group` field.
- `ChatSubagentRow` / `ChatSubagentList` / `ChatSubagentSheet` /
  `ChatSubagentDockPill` render the union unchanged: same states, sections and
  dots. A native row's action is `View` (it opens the window) instead of
  `Open` / `Restore & open` (which jump to a tab), and it is never `disabled`
  merely because it has no tab.
- `controllers/nativeSubagentDock.ts`: the pane's small surface state
  (`rows`, `openRow`, `publish`, `open`, `close`). `TerminalPane` creates and
  provides it; `StructuredPane` (the only subtree that owns the timeline) fills
  the rows and renders the window. Kept as an injected object rather than a store
  because these rows are a projection of one pane's timeline and die with it.
- `ChatNativeSubagentOverlay.vue`: the read-only window. A gutter panel on
  desktop, a bottom sheet on phones; dialog semantics (`aria-modal`, Tab trap,
  Esc from inside *and* from `<body>`, backdrop tap, body scroll lock) and it
  closes by handing focus back to the pill. Content is `ChatSubthreadBody` —
  the rows the transcript shows — so the window cannot drift from the inline
  region. It also offers `Show in transcript`.
- `ChatSubthreadBody.vue` (extracted from `ChatSubthread`): the child's rows,
  shared by the inline `<details>` row and the window. `ChatSubthread` keeps only
  its header and the collapse control.
- `ChatSubagentDockPill`: the wide-pane gutter rail forwards `open` as well;
  it is the default desktop reveal, so leaving it out would have made the new
  `View` rows dead clicks on the largest panes. Focus returns to the pill only
  when the dock could not restore it (the rail stays mounted, so there the row
  keeps focus).
- `StructuredPane` publishes rows on `onActivated` and clears them on
  `onDeactivated`: the pill is one per pane while KeepAlive caches one Chat
  body per tab, so without it a tab switch listed the previous tab's children
  and left an open window pointing at a child the new tab could not resolve.
  The window is additionally gated on `isPaneActivated`, because it is
  teleported to `<body>` and a hidden cached tab would otherwise keep showing
  it over the tab the reader switched to.
- `useChatTimeline.revealSubthread`: opens the child's inline region —
  widens the rendered tail window when the turn is old, loads the turn's
  execution detail when it is still lazy, expands the fold, then opens every
  collapsed `<details>` between the child row and the transcript and scrolls to
  it. It waits a bounded number of frames for the freshly mounted region (a
  single frame was not enough), cancels any pending tail anchor and detaches
  from the tail when the target is not the newest turn (otherwise the next
  stream delta would yank the view back), captures the visit before its awaits,
  and gives up silently: the window already gave the reader the content, so
  nothing correctness-bearing depends on the jump.

## Findings from independent review

- BLOCKING (fixed): the gutter rail did not forward `open`, so on a wide pane
  (>= ~1408px) the row showed `View ›` and did nothing.
- BLOCKING (fixed): rows were published only when the projection changed, so a
  KeepAlive tab switch kept the previous tab's rows and its open window; the
  same review found the teleported window could outlive deactivation. Covered
  now by `onActivated`/`onDeactivated` publishing, the `isPaneActivated` gate,
  and a two-tab section in the browser scenario (a second cached Chat tab is
  exactly the shape that produced a second dialog before the gate).
- NON-BLOCKING (fixed): pill focus watcher could steal focus the dock had
  restored to a still-mounted rail row; the window's Tab trap counted only
  footer buttons although the body is full of tab stops (markdown links, tool
  `<details>`); `revealSubthread` could be undone by tail-following and captured
  its visit after its awaits; the extracted body lost the 2px/8px chip padding
  and left a duplicate selector behind.
- BLOCKING (fixed on re-review): the projection watcher was ungated, so a
  cached, deactivated pane could still flush one last stream batch and publish
  its own children onto the visible tab's pill. Publication is now gated on
  `isPaneActivated`, and `onActivated` is the single publisher on mount
  (`immediate: true` is gone).
- The scenario now switches tabs with the window **open** (dispatched through
  the DOM, since the modal covers the sidebar) and asserts the window and its
  scroll lock are gone, the hidden pane cannot republish rows, and the count
  stays zero across a quiet interval.

### Second cross-review (Gemini 4 Argon), both halves

- BLOCKING (fixed): `Show in transcript` skipped `detachFromTail()` for the
  newest turn, so the resize observer (opening the region changes content
  height) and the next stream batch re-anchored to the tail and undid the scroll.
  Following now always detaches there and rearms itself when the reader is
  already near the bottom.
- BLOCKING (fixed): the DOM lookup was not scoped to the clicked turn. A
  provider can address the same child thread id again in a later turn
  (`sendInput`), and the first match in document order is the earlier turn.
- BLOCKING (fixed): a lazy historical turn can already carry a stub
  failed/cancelled child from the shallow page (`conversation_event_visible`
  keeps that status), which projected a row whose window had nothing to show.
  The projection now takes a readiness predicate built from `historyDetails`,
  mirroring `presentTurnParts`. The scenario's reload phase carries exactly
  that shape (the failed child's status survives the shallow page, its prose
  and the active sibling do not), so the phase is load-bearing: with the
  predicate removed the "no row before the turn is opened" assertion fails.
- BLOCKING (fixed): a top-layer `<dialog>` (the image/attachment lightbox opened
  from these rows) lost Escape to the window's document-level handler. A
  `dialog[open]` now owns both Escape and Tab. Gate is non-vacuous: with the
  guard removed the scenario fails at the "window stays open" assertion.
- Fixed as well: memo identity (`IncrementalTimelineReducer.reset()` restarts
  revision counts, so the cached turn instance is checked too); the missing
  visit/disposed guard right after the detail read; focus restoration moved
  into the dock so only a dismissal moves focus; `onUnmounted` clearing gated
  on pane activation; document-level Tab refusal when a click dropped focus to
  `<body>`; the phone header no longer crushes the name or repeats
  "Native sub-agent"; missing `.tool-status.cancelled`; the scenario now
  asserts the dispatch result, Shift+Tab, and captures the settled wide-pane
  window.
- Not changed: a historical turn's children still appear only once that turn's
  execution detail is loaded (the backend keeps child events behind it).

### Mock fidelity note (corrected)

The first draft of the scenario read `since` from the history request, but
`useAgentStream` sends `since_sequence`, so every read was `-1`: after a tab
round trip the app applied the same page twice and the delivered answer
appeared twice in the transcript, and polling `next_sequence` never advanced
the cursor (4,642 requests in 10s). It also served child/process events in the
shallow conversation page, which the backend never does, so the lazy turn-detail
path was never exercised.

The fixture now: reads `since_sequence` (inclusive cursor) and echoes it; serves
`view=conversation` with only the visible events plus an
`AgentStreamHistoryDetail` descriptor; answers `turn_id`/`through_sequence`
detail reads with the hidden events; delivers the live turn batch by batch from
`/wait` (reading `since_sequence` from the POST body and holding each empty poll
250ms). Requests dropped to 112 and the reload phase proves the documented
boundary: no child row until that turn is opened, then the row and the window
appear with the same content.

## Deliberate boundaries

- **Historical children appear once their turn's detail is loaded.** The
  backend keeps child events behind the turn's detail stream
  (`conversation_event_visible`), so after a reload a folded, never-expanded
  turn contributes no rows yet. Listing them ahead of that would need a
  backend descriptor field (e.g. an `subagent_thread_count`) and placeholder
  rows; this change does not add it, and the rail never invents a row it cannot
  open.
- `Show in transcript` for a turn outside the rendered window detaches the
  timeline from the tail (`visibleHistoryStart`), the same as the existing
  "Load earlier messages" path; `Latest` re-attaches.
- Only providers that actually publish thread attribution can appear: Codex has
  no spawn tool in the captured history, so it contributes no native rows.
- No Hub child behaviour changed: the pill, jump, archive restore and workspace
  attribution are untouched.

## Validation

All commands ran in the feature worktree and on owned resources; the mocked
browser server used port 5391 and was stopped afterwards (no 5173/8173 access).

- `pnpm typecheck`, `pnpm lint:check`, `pnpm build:bundle` — clean.
- `pnpm test:unit` — see the delivery report for the final count.
- New `frontend/tests/nativeSubagentRows.test.mjs` (8 tests): row vocabulary,
  lifecycle grouping, identity/provider fallbacks, newest-first ordering,
  bounded list, memo reuse/refresh/prune, and row → live-child resolution.
- New `frontend/tests/browser_native_subagent_dock.py` (registered in
  `scripts/verify_browser_mocks.py`, all APIs mocked): pill with native rows and
  no Hub children → popover row (`data-kind="native"`, `View`) → window with the
  delegated prompt, child report and dispatch result → Tab trap, body lock,
  Done/Esc/backdrop dismissal with focus back on the pill → `Show in transcript`
  expands the folded turn and the child's own inline row → phones get the sheet
  then the window as a bottom sheet with no horizontal overflow.
- Existing gates re-run on the same worktree: `browser_chat_subagent_region.py`
  (inline region, unchanged PASS) and the dock/unit contracts
  (`subagentJumpCard.test.mjs`, `structuredPaneSubagentCard.test.mjs`,
  `toolPresentation.test.mjs`, `chatFeatureLifecycle.test.mjs`).

## Risks / not done

- The rail is capped at 30 rows and shows 5 before "more"; a fan-out heavier
  than that scrolls rather than paginates.
- The window replays the same content as the transcript, so a very long child
  transcript renders twice while it is open (the inline copy stays mounted
  behind the backdrop).
- Focus on dismissal goes to the row that opened the window while it is still
  mounted (the wide-pane rail stays open); when that reveal closed with it, to
  the chrome pill. A data-driven row clear (tab switch, history reset) never
  moves focus.
- Not reviewed for right-to-left or long localized thread names.
