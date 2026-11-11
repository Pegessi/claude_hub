import { type Ref, type ComputedRef, computed, nextTick, onMounted, onUnmounted, ref, shallowReactive, shallowRef, watch } from 'vue'
import { type AgentStreamEvent } from '@/types'
import { type AgentStreamHistoryDetailEntry } from '@/utils/agentStreamHistoryCache'
import { type TimelineTurn } from '../timeline/types'
import { type ChatPaneProps, type PendingTurn } from './types'
import { type useQuestionAnswers } from '@/composables/useQuestionAnswers'
import { IncrementalTimelineReducer } from '../timeline/reducer'
import { isQuietDotTurn } from '@/utils/dotApi'
import { isTimelineNearBottom } from '@/utils/timelineFollow'
import { createTimelineActivation, type TimelinePhase } from '@/utils/timelineActivation'
import { TIMELINE_PAGE_SIZE, selectTimelineWindow, timelineWindowStart } from '@/utils/timelineWindow'
import { parseTimestampMs } from '@/utils/duration'
import { withOptimisticCancelled } from '@/utils/agentStreamStopReset'
import { createSubthreadThinkingPresenter } from '@/utils/thinkingPresentation'
import { isTurnProcessExpanded, presentTurnParts, turnProcessFoldSignature } from '@/features/chat/presentation/chatTurnPresentation'

interface Dependencies {
  historyReady: Ref<boolean>
  connectionState: Ref<'idle' | 'hydrating' | 'live' | 'reconciling' | 'failed'>
  events: Ref<AgentStreamEvent[]>
  historyDetails: Ref<Record<string, AgentStreamHistoryDetailEntry>>
  historyDetailRevision: Ref<number>
  /** Read a turn's detail. ``force`` re-reads a range that is already loaded,
   *  which only a gap repair has a reason to do. */
  loadHistoryDetail: (turnId: string, options?: { force?: boolean }) => Promise<void>
  isApprovalResolved: ReturnType<typeof useQuestionAnswers>['isApprovalResolved']
  isEditingTurn: (turn: TimelineTurn) => boolean
  optimisticCancelledTurnId: Ref<string | null>
  pendingTurns: ComputedRef<PendingTurn[]>
  props: ChatPaneProps
}

/** A backfill read may be retired by a growing descriptor; this many tries is
 *  enough to land one and few enough that a permanent failure stays quiet. */
const BACKFILL_MAX_ATTEMPTS = 3

export function useChatTimeline(deps: Dependencies) {
  // ── Timeline grouping ───────────────────────────────────────────────────────
  // The flat event stream is grouped into turns by the
  // ``IncrementalTimelineReducer`` (see agentStreamTimeline.ts).
  //
  // ``groupEventsIntoTurns`` re-scans the entire event list on every call. For
  // a session with thousands of historical events, each incoming delta re-runs
  // the full O(n) reduction and dominates the long-task budget. The incremental
  // reducer keeps state across calls and only processes the unseen suffix, so
  // each batch costs O(new events) regardless of history length.

  const timelineReducer = new IncrementalTimelineReducer()

  const timelineEvents = computed<AgentStreamEvent[]>(() => {
    const base = deps.events.value
    const turnId = deps.optimisticCancelledTurnId.value
    if (!turnId) return base
    return withOptimisticCancelled(base, turnId, {
      tabId: deps.props.tabId,
      now: new Date().toISOString(),
    })
  })

  const authoritativeTurns = computed(() => timelineReducer.reduce(timelineEvents.value))

  // Assistant text streams directly from the batched event stream (backend
  // 60ms coalescer + frontend rAF/48ms batcher). No second-stage character
  // reveal: each committed batch updates the visible text once. On turn
  // completion MarkdownContent caches the final block and exposes the exact
  // final text synchronously.
  const turns = authoritativeTurns

  // null follows a bounded tail window; detaching freezes its start so incoming
  // turns cannot remove the message being read. Expanding never trims history.
  const visibleHistoryStart = ref<number | null>(null)

  const isLoadingEarlier = ref(false)

  const historyWindowStart = computed(() => timelineWindowStart(turns.value.length, visibleHistoryStart.value))

  const visibleTurns = computed(() => selectTimelineWindow(
    turns.value,
    historyWindowStart.value,
    turn => deps.isEditingTurn(turn) || turn.approvals.some(approval => !deps.isApprovalResolved(approval)),
  ).filter(({ turn }) => !isQuietDotTurn(turn)).map(({ turn, ordinal }) => ({
    ordinal,
    turn: {
      ...turn,
      // The turn exists but the provider has reported nothing yet; the live
      // footer names that window from `currentStep`. The copy also keeps each
      // recompute handing consumers a fresh object over an in-place-mutated turn.
      awaitingAgentActivity: !turn.completed &&
        turn.parts.length === 0 &&
        turn.errors.length === 0 &&
        turn.statuses.length === 0,
    },
  })))

  const timelineEl = ref<HTMLElement | null>(null)

  const timelineContentEl = ref<HTMLElement | null>(null)

  // Activation gate: the timeline is not revealed until authoritative history
  // has been hydrated and the tail has been synchronously pinned. This prevents
  // the visible "scroll replay" where history paints at the top and then jumps
  // to the bottom. See timelineActivation.ts for the state machine contract.
  const activation = createTimelineActivation()

  const timelinePhase = ref<TimelinePhase>(activation.phase)

  const isFollowingLatest = ref(activation.followOutput)

  function syncActivation() {
    timelinePhase.value = activation.phase
    isFollowingLatest.value = activation.followOutput
  }

  function markHistoryReady() {
    activation.markHistoryReady()
    syncActivation()
  }

  function confirmTailPinned() {
    activation.confirmTailPinned()
    syncActivation()
  }

  function detachFromTail() {
    if (visibleHistoryStart.value === null) visibleHistoryStart.value = historyWindowStart.value
    activation.detachFromTail()
    syncActivation()
  }

  function rearmFollow() {
    activation.rearmFollow()
    syncActivation()
  }

  function resetActivation() {
    activation.reset()
    syncActivation()
  }

  let timelineResizeObserver: ResizeObserver | null = null

  let timelineVerificationFrame: number | null = null

  let timelineDisposed = false

  let timelineVisit = 0

  const timelineLoadingMessage = computed(() => {
    if (deps.connectionState.value === 'failed') return ''
    if (deps.connectionState.value === 'reconciling') return 'Updating conversation…'
    if (deps.connectionState.value === 'idle' || deps.connectionState.value === 'hydrating') return 'Loading conversation…'
    return timelinePhase.value !== 'revealed' ? 'Opening latest messages…' : ''
  })

  /**
   * Which turns the viewer has explicitly opened or closed.
   *
   * Fold state is per-viewer UI state, not part of the durable timeline, so it
   * lives here rather than on the turn the shared reducer produces. Same
   * ``v-memo`` constraint as ``turnApprovalSignature``: a turn whose deps do not
   * change will not re-render, so the fold decision has to reach the deps array
   * via ``turnFoldSignature`` below or a click would do nothing.
   */
  const processExpandedOverrides = ref(new Map<string, boolean>())

  /**
   * Key of the newest turn, or ``null`` while none has started.
   *
   * That turn stays open: it is the one being read, and folding it the instant
   * it finished would collapse the process out from under someone mid-glance.
   * Older turns are history and fold themselves. The newest turn is the one
   * that stays open even while it is still running — so a finished turn folds
   * as soon as a newer turn starts, rather than staying expanded for the whole
   * time the newer turn is in flight.
   */
  const latestTurnKey = computed(() => {
    const list = turns.value
    return list.length > 0 ? list[list.length - 1].key : null
  })

  async function loadAndExpandHistoryDetail(turn: TimelineTurn): Promise<void> {
    const turnId = turn.turnId
    if (!turnId) return
    const visit = timelineVisit
    await deps.loadHistoryDetail(turnId)
    if (timelineDisposed || visit !== timelineVisit) return
    if (deps.historyDetails.value[turnId]?.status !== 'loaded') return
    const next = new Map(processExpandedOverrides.value)
    next.set(turn.key, true)
    processExpandedOverrides.value = next
  }

  function toggleTurnProcess(turn: TimelineTurn): void {
    const detail = turn.turnId ? deps.historyDetails.value[turn.turnId] : undefined
    if (detail && detail.status !== 'loaded') {
      void loadAndExpandHistoryDetail(turn)
      return
    }
    const next = new Map(processExpandedOverrides.value)
    next.set(turn.key, !isTurnProcessExpanded(turn, latestTurnKey.value, processExpandedOverrides.value))
    processExpandedOverrides.value = next
  }

  /** Attribute-value escape for a provider thread id in a query selector. */
  function attributeValue(value: string): string {
    return value.replace(/["\\]/g, character => `\\${character}`)
  }

  /** One animation frame, or a macrotask where rAF is unavailable (tests). */
  function nextFrame(): Promise<void> {
    if (typeof requestAnimationFrame === 'function') {
      return new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    }
    return new Promise<void>(resolve => setTimeout(resolve, 16))
  }

  /**
   * Take the reader to a native child's inline region.
   *
   * The dock's detail window can show any child the timeline holds, but the
   * transcript is where the conversation continues, so the window offers a
   * jump. Three things must hold before the row exists in the DOM: the turn has
   * to be inside the rendered tail window, its process has to be expanded, and
   * a historical turn's child rows have to be requested from its detail
   * stream. This attempts all three and gives up silently — the window already
   * gave the reader the content, so nothing correctness-bearing depends on it.
   */
  async function revealSubthread(turnKey: string, threadId: string): Promise<void> {
    const visit = timelineVisit
    const list = turns.value
    const ordinal = list.findIndex(turn => turn.key === turnKey)
    if (ordinal < 0) return
    const turn = list[ordinal]
    const wasFollowing = isFollowingLatest.value
    const widenedWindow = ordinal < historyWindowStart.value
    if (widenedWindow) {
      detachFromTail()
      visibleHistoryStart.value = ordinal
      await nextTick()
    }
    if (timelineDisposed || visit !== timelineVisit) return
    const detail = turn.turnId ? deps.historyDetails.value[turn.turnId] : undefined
    if (detail && detail.status !== 'loaded') {
      await loadAndExpandHistoryDetail(turn)
      // The read may have crossed a deactivation or failed outright; either way
      // there is nothing in the DOM to reveal and the new visit owns the tail.
      if (timelineDisposed || visit !== timelineVisit) return
      if (turn.turnId && deps.historyDetails.value[turn.turnId]?.status !== 'loaded') return
    } else if (!isTurnProcessExpanded(turn, latestTurnKey.value, processExpandedOverrides.value)) {
      const expanded = new Map(processExpandedOverrides.value)
      expanded.set(turn.key, true)
      processExpandedOverrides.value = expanded
    }
    // The scroll below is deliberate, and both the resize observer (opening the
    // region changes the content height) and the next stream batch would undo
    // it while the pane still follows the tail. Following rearms itself as soon
    // as the reader is near the bottom again (`handleTimelineScroll`).
    cancelScheduledTimelineScroll()
    if (wasFollowing) detachFromTail()
    await nextTick()
    if (timelineDisposed || visit !== timelineVisit) return
    const viewport = timelineEl.value
    if (!viewport) return
    // The expanded region mounts a turn's worth of children (markdown, tool
    // groups), so the row can arrive a frame or two after the fold changed.
    // A bounded wait keeps this honest instead of guessing one frame. The
    // lookup is scoped to this turn: a provider can address the same child
    // thread id again in a later turn, and the first match would be that one.
    let row: HTMLElement | null = null
    for (let attempt = 0; attempt < 10 && !row; attempt += 1) {
      row = viewport.querySelector<HTMLElement>(
        `[data-turn-key="${attributeValue(turnKey)}"] [data-subagent-thread-id="${attributeValue(threadId)}"]`,
      )
      if (row || attempt === 9) break
      await nextFrame()
      if (timelineDisposed || visit !== timelineVisit) return
    }
    // A stepping stone for the settle below: the reveal detached the tail
    // above, and scrolling is what normally rearms it (`handleTimelineScroll`).
    const settleTail = () => {
      if (!wasFollowing || widenedWindow) return
      const el = timelineEl.value
      if (!el || !isTimelineNearBottom(el)) return
      // A short turn leaves nothing to scroll (`scrollIntoView` is a no-op), so
      // no scroll event ever fires and nothing would rearm: put the reader back
      // on the live tail instead of leaving a `Latest` pill over a footer they
      // are already looking at.
      visibleHistoryStart.value = null
      rearmFollow()
    }
    if (!row) {
      settleTail()
      return
    }
    // Every collapsed <details> between the child row and the transcript
    // (its inline region and, if the provider nests one, its child row).
    let node: HTMLElement | null = row
    while (node && node !== viewport) {
      if (node.tagName === 'DETAILS') (node as HTMLDetailsElement).open = true
      node = node.parentElement
    }
    row.scrollIntoView({ block: 'center' })
    settleTail()
  }

  /** Parts to render for a turn, with its process folded away once it is history. */
  function turnPartsFor(turn: TimelineTurn) {
    const detail = turn.turnId ? deps.historyDetails.value[turn.turnId] : undefined
    return presentTurnParts(turn, latestTurnKey.value, processExpandedOverrides.value, detail)
  }

  /** How many events the turn has produced so far, as the pane's descriptor
   *  reports it. This is the one magnitude that exists before the detail is
   *  read and keeps growing while a turn streams: without it a long working
   *  turn shows a stopwatch and nothing about how much has happened. */
  function turnEventCount(turn: TimelineTurn): number | null {
    if (!turn.turnId) return null
    const detail = deps.historyDetails.value[turn.turnId]
    const described = detail?.descriptor.event_count
    const seen = observedTurnCounts.value.get(turn.turnId)?.count ?? 0
    // The higher of the two: a hydration descriptor counts the whole turn, and
    // the pane's own tally keeps growing for a turn that began here (no page
    // describes it) — or for one whose descriptor was written before the events
    // this pane watched arrive.
    const count = Math.max(typeof described === 'number' ? described : 0, seen)
    return count > 0 ? count : null
  }

  /** Output events per turn, counted as they arrive.
   *
   *  ``turn_started``/``turn_completed`` are the turn's punctuation, not output:
   *  counting them would make a turn with nothing on screen look like it had
   *  something to show, and the footer would stop saying it is still waiting for
   *  a first response. Sequences already counted are skipped, so a replay or a
   *  history-detail insertion cannot count the same event twice. */
  /** Per turn: how many output events this pane watched, and the highest
   *  sequence it has counted for that turn.
   *
   *  The mark is per turn, not global, so a different turn's events are never
   *  mistaken for a replay — and a detail read that replays a turn's middle
   *  (sequences below its mark) adds no count, because those events are already
   *  represented by the summary the stream showed.
   *
   *  ``shallowRef`` + copy-on-write: a plain Map mutated in a watcher is invisible
   *  to Vue, and a footer that reads the count would keep showing the number it
   *  saw first. */
  const observedTurnCounts = shallowRef(new Map<string, { count: number; through: number }>())

  function countObservedTurnEvents(): void {
    const counted = new Map(observedTurnCounts.value)
    let changed = false
    for (const event of deps.events.value) {
      const turnId = event.turn_id
      if (!turnId) continue
      const seen = counted.get(turnId)
      if (seen && event.stream_sequence <= seen.through) continue
      // The turn's opening and closing markers are punctuation, not output:
      // counting them would make a turn with nothing on screen look productive
      // and hide the waiting state.
      const produced = event.type === 'turn_started' || event.type === 'turn_completed' ? 0 : 1
      counted.set(turnId, {
        count: (seen?.count ?? 0) + produced,
        through: event.stream_sequence,
      })
      changed = true
    }
    if (changed) observedTurnCounts.value = counted
  }

  const subthreadPartsFor = createSubthreadThinkingPresenter()

  function retryHistoryDetail(turn: TimelineTurn): void {
    void loadAndExpandHistoryDetail(turn)
  }

  function turnFoldSignature(turn: TimelineTurn): string {
    void deps.historyDetailRevision.value
    const detail = turn.turnId ? deps.historyDetails.value[turn.turnId] : undefined
    return turnProcessFoldSignature(turn, latestTurnKey.value, processExpandedOverrides.value, detail)
  }

  // visibleTurns creates a display copy on each stream update. Key folds by the
  // durable turn key (as process folds do), not that temporary object identity.
  // Only small group-key Sets are retained; no tool payloads or turn objects.
  const expandedToolGroups = shallowReactive(new Map<string, ReadonlySet<string>>())

  function onToolGroupToggle(turn: TimelineTurn, groupKey: string, event: Event): void {
    const details = event.currentTarget
    if (!(details instanceof HTMLDetailsElement)) return
    const previous = expandedToolGroups.get(turn.key)
    if (Boolean(previous?.has(groupKey)) === details.open) return
    const next = new Set(previous)
    if (details.open) next.add(groupKey)
    else next.delete(groupKey)
    if (next.size > 0) expandedToolGroups.set(turn.key, next)
    else expandedToolGroups.delete(turn.key)
  }

  /** Collapse through the native control so tool toggle events also unmount the
   *  lazy body. Tool open state is mirrored for remounts after process folding;
   *  thinking and other details continue to use browser-owned state. */
  function collapseDetails(event: MouseEvent): void {
    const target = event.currentTarget
    if (target instanceof HTMLElement) {
      target.closest('details')?.removeAttribute('open')
    }
  }

  function cancelScheduledTimelineScroll() {
    if (timelineVerificationFrame !== null) {
      cancelAnimationFrame(timelineVerificationFrame)
      timelineVerificationFrame = null
    }
  }

  /**
   * Pin the timeline to the latest turn.
   *
   * The initial reveal is handled by the activation gate (``markHistoryReady``
   * → synchronous ``scrollTop = scrollHeight`` in ``nextTick`` →
   * ``confirmTailPinned``). This function only drives live updates once the
   * timeline is revealed and the user has not detached.
   *
   * The scroll is applied synchronously inside ``nextTick`` (after Vue's DOM
   * commit, before the browser paints) so a growing assistant message never
   * paints above the fold and then jumps down. A single verification rAF
   * re-sticks if Markdown / image / font layout changed the scroll height.
   */
  function requestLatestAnchor(force = false) {
    if (force) rearmFollow()
    if (!isFollowingLatest.value || timelineDisposed) return
    // While hidden or pinning, the activation gate owns the scroll position.
    if (timelinePhase.value !== 'revealed') return
    cancelScheduledTimelineScroll()
    const visit = timelineVisit
    void nextTick(() => {
      if (timelineDisposed || visit !== timelineVisit) return
      const el = timelineEl.value
      if (!el || !isFollowingLatest.value || timelineDisposed) return
      el.scrollTop = el.scrollHeight
      timelineVerificationFrame = requestAnimationFrame(() => {
        timelineVerificationFrame = null
        const current = timelineEl.value
        if (!current || !isFollowingLatest.value || timelineDisposed || visit !== timelineVisit) return
        if (!isTimelineNearBottom(current)) current.scrollTop = current.scrollHeight
      })
    })
  }

  function handleTimelineScroll() {
    const el = timelineEl.value
    if (!el) return
    // Scroll events during hydration are not user-driven and must not detach.
    if (timelineDisposed || timelinePhase.value !== 'revealed' || isLoadingEarlier.value) return
    if (isTimelineNearBottom(el)) {
      if (!isFollowingLatest.value) rearmFollow()
    } else {
      if (isFollowingLatest.value) detachFromTail()
    }
  }

  function jumpToLatest() {
    visibleHistoryStart.value = null
    rearmFollow()
    requestLatestAnchor(true)
  }

  async function loadEarlierTurns() {
    const viewport = timelineEl.value
    if (!viewport || isLoadingEarlier.value || historyWindowStart.value === 0) return
    detachFromTail()
    cancelScheduledTimelineScroll()
    const visit = timelineVisit
    const top = viewport.getBoundingClientRect().top
    // Keep a real visible row as anchor: older pending approvals may already be
    // rendered above the window, so scrollHeight differences alone are unsafe.
    const anchor = Array.from(viewport.querySelectorAll<HTMLElement>('.structured-turn'))
      .find(row => row.getBoundingClientRect().bottom > top)
    const offset = anchor?.getBoundingClientRect().top
    isLoadingEarlier.value = true
    try {
      // Let the disabled loading affordance paint before mounting the next page.
      await new Promise<void>(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)))
      if (timelineDisposed || visit !== timelineVisit) return
      visibleHistoryStart.value = Math.max(0, historyWindowStart.value - TIMELINE_PAGE_SIZE)
      await nextTick()
      if (timelineDisposed || visit !== timelineVisit) return
      if (anchor?.isConnected && offset !== undefined) {
        viewport.scrollTop += anchor.getBoundingClientRect().top - offset
      }
    } finally {
      isLoadingEarlier.value = false
    }
  }

  function observeTimelineGeometry() {
    timelineResizeObserver?.disconnect()
    if (typeof ResizeObserver === 'undefined') return
    const viewport = timelineEl.value
    const content = timelineContentEl.value
    if (!viewport || !content) return
    timelineResizeObserver = new ResizeObserver(() => {
      // Resize only re-sticks while the user is following the tail. A detached
      // viewport must never be hijacked by a layout change.
      if (activation.shouldHandleResize()) requestLatestAnchor()
    })
    timelineResizeObserver.observe(viewport)
    timelineResizeObserver.observe(content)
  }

  function activateViewport() {
    timelineDisposed = false
    paneActiveSince = Date.now()
    timelineVisit++
    visibleHistoryStart.value = null
    resetActivation()
    // Switching to the tab is what the reader asked for: backfill its live turn
    // once the reactivation has settled.
    void nextTick(() => void backfillNewestTurn())
  }

  function deactivateViewport() {
    timelineDisposed = true
    timelineVisit++
    timelineResizeObserver?.disconnect()
    timelineResizeObserver = null
    cancelScheduledTimelineScroll()
  }

  /** Turns whose detail this pane has already asked for on its own behalf.
   *
   *  A descriptor keeps changing while a turn streams, and re-reading a growing
   *  turn on every batch would be quadratic. One read per turn is enough: from
   *  then on the live stream appends to the parts it produced. */
  /** When this pane last became visible. A turn that started before this
   *  moment cannot have all of its parts here — only events from after it were
   *  reduced — which is what makes a mid-flight join recognisable. It is
   *  refreshed on activation, not fixed at construction: a turn that started
   *  while the tab was hidden is joined mid-flight too. */
  let paneActiveSince = Date.now()

  /** Reads this pane has already made on a turn's behalf, by reason: one to
   *  restore a turn it joined mid-flight, one more to repair a gap. Two separate
   *  flags, because a turn may owe both and neither may repeat. */
  const backfillReads = new Map<string, { join: number; repair: number }>()
  /** Every attempt by reason, landed or not, so a read that keeps being aborted
   *  cannot loop and a repair is not starved by a spent join budget. */
  const backfillAttempts = new Map<string, { join: number; repair: number }>()
  /** Turns with a read in flight right now. */
  const backfillInFlight = new Set<string>()
  /** Turns whose stream dropped while they were live. Their parts may be
   *  missing whatever the backend accepted during the gap. */
  const interruptedTurns = new Set<string>()

  /** Read the newest turn's detail so a turn the pane joined mid-flight has
   *  parts, without touching the fold's expansion state.
   *
   *  Events that arrived before the pane existed are not reduced into anything
   *  (there was no turn to reduce them into), so such a turn renders as a fold
   *  with nothing inside and a footer that has no step to name. The detail is
   *  the record of those events, so one read restores them. It sets no
   *  expansion override — the reader still decides whether to open a completed
   *  turn's fold. A *live* turn is never foldable in the first place, so there
   *  the read has the same end state as clicking its activity header today:
   *  the steps render inline and the answer keeps streaming under them.
   *
   *  Scoped to the newest turn on purpose: it is the one whose state the reader
   *  is looking at, and backfilling every historical turn would read thousands
   *  of events for transcripts nobody has opened. */
  async function backfillNewestTurn(): Promise<void> {
    if (timelineDisposed) return
    const turn = turns.value[turns.value.length - 1]
    const turnId = turn?.turnId
    if (!turnId) return
    const detail = deps.historyDetails.value[turnId]
    if (!detail || detail.status === 'loading') return
    const reads = backfillReads.get(turnId) ?? { join: 0, repair: 0 }
    // One read restores a turn the pane joined with nothing reduced. One more
    // repairs a turn whose stream dropped while it was live — and only once that
    // turn has stopped moving, because before then the read would chase the very
    // stream it is trying to repair. Neither may repeat: a descriptor keeps
    // changing as a turn streams, so an uncapped read is a polling loop.
    // "Nothing reduced" is too strict: a turn in flight almost always emits some
    // narration after this pane mounts, so those parts exist while the hundreds
    // of steps from before the mount never will. A live turn that started before
    // this pane was created is missing them by construction.
    const startedMs = parseTimestampMs(turn.startedAt)
    const joinedMidTurn = startedMs !== null && startedMs <= paneActiveSince
    const joinOwed = reads.join === 0 && (turn.parts.length === 0 || (!turn.completed && joinedMidTurn))
    // A repair re-reads a range that is already ``loaded`` on purpose: the loaded
    // detail was fetched before the gap, so it cannot contain what the backend
    // accepted while the stream was down.
    const repairOwed = interruptedTurns.has(turnId) && turn.completed && reads.repair === 0
    if (!joinOwed && !repairOwed) return
    // Attempts are bounded per reason and separately from successes: a read that
    // is aborted by a newer descriptor (or fails) must be retried once, but a
    // read that never lands must not become a loop.
    const attempts = backfillAttempts.get(turnId) ?? { join: 0, repair: 0 }
    const reason = repairOwed ? 'repair' : 'join'
    if (attempts[reason] >= BACKFILL_MAX_ATTEMPTS) return
    // Two watcher ticks can land before the first read resolves; without this the
    // budget would be spent twice on one request.
    if (backfillInFlight.has(turnId)) return
    backfillAttempts.set(turnId, { ...attempts, [reason]: attempts[reason] + 1 })
    backfillInFlight.add(turnId)
    const visit = timelineVisit
    await deps.loadHistoryDetail(turnId, { force: repairOwed })
    backfillInFlight.delete(turnId)
    if (timelineDisposed || visit !== timelineVisit) return
    // Charge the budget only once the range actually landed. A request retired
    // because the turn grew underneath it leaves the entry unloaded, and the
    // next activation deserves another try.
    const landed = deps.historyDetails.value[turnId]?.status === 'loaded'
    if (landed) {
      backfillReads.set(turnId, { ...reads, [reason]: reads[reason] + 1 })
    }
  }

  function registerWatchers() {
    watch(() => deps.events.value, countObservedTurnEvents, { immediate: true, deep: false })

    watch(
      () => [deps.events.value.length, deps.pendingTurns.value.length],
      () => requestLatestAnchor(),
    )

    // A drop from ``live`` is the only thing that means "events may be missing".
    // The initial ``hydrating`` state is the pane starting up, not a gap.
    watch(
      () => deps.connectionState.value,
      (state, previous) => {
        if (previous !== 'live' || state === 'live') return
        const turnId = turns.value[turns.value.length - 1]?.turnId
        if (turnId) interruptedTurns.add(turnId)
      },
    )

    // Ask again whenever the newest turn's own read state could have changed:
    // a descriptor can arrive after activation, reset to ``idle`` after a retired
    // read, grow, and the turn can complete — and ``backfillNewestTurn`` owns the
    // budget that makes a per-batch watcher safe.
    watch(
      () => {
        const turn = turns.value[turns.value.length - 1]
        const detail = turn?.turnId ? deps.historyDetails.value[turn.turnId] : undefined
        return [
          turns.value.length,
          turn?.completed === true ? 1 : 0,
          detail?.status ?? '',
          detail?.descriptor.last_sequence ?? -1,
          deps.historyDetailRevision.value,
        ].join('|')
      },
      () => void backfillNewestTurn(),
    )

    // Activation gate: when cached or authoritative history becomes renderable,
    // pin the tail synchronously (after Vue's DOM commit, before paint) and only
    // then reveal the timeline. This is the Paseo
    // ``isAuthoritativeHistoryReady`` pattern — the first painted frame is already
    // at the tail.
    //
    // ``immediate`` covers a quick tab switch-back: cached history enters
    // ``reconciling`` immediately, without waiting for network hydration.
    watch([deps.historyReady, deps.connectionState], ([ready, state]) => {
      if (ready) {
        // Reconciliation finishing within this visit must not interrupt reading.
        // Each activation resets the gate before starting reconciliation.
        if (timelinePhase.value === 'revealed') return
        markHistoryReady()
        const visit = timelineVisit
        void nextTick(() => {
          if (timelineDisposed || visit !== timelineVisit) return
          const el = timelineEl.value
          if (el) el.scrollTop = el.scrollHeight
          confirmTailPinned()
          // Removing the loading banner changes viewport height. Re-pin after
          // the reveal DOM commit as well, then verify delayed Markdown layout.
          requestLatestAnchor()
        })
      } else if (state === 'hydrating') {
        resetActivation()
      }
      // A capability failure does not hide successfully loaded history.
      // A failed history read stays hidden until Retry hydrates it.
    }, { immediate: true })

    onMounted(() => {
      timelineDisposed = false
      void nextTick(() => {
        if (timelineDisposed) return
        observeTimelineGeometry()
      })
    })

    onUnmounted(() => {
      timelineDisposed = true
      timelineResizeObserver?.disconnect()
      timelineResizeObserver = null
      cancelScheduledTimelineScroll()
    })
  }

  return {
    timelineReducer,
    timelineEvents,
    authoritativeTurns,
    turns,
    visibleHistoryStart,
    isLoadingEarlier,
    historyWindowStart,
    visibleTurns,
    timelineEl,
    timelineContentEl,
    activation,
    timelinePhase,
    isFollowingLatest,
    syncActivation,
    markHistoryReady,
    confirmTailPinned,
    detachFromTail,
    rearmFollow,
    resetActivation,
    get timelineDisposed() { return timelineDisposed },
    timelineLoadingMessage,
    processExpandedOverrides,
    latestTurnKey,
    toggleTurnProcess,
    revealSubthread,
    turnPartsFor,
    turnEventCount,
    retryHistoryDetail,
    subthreadPartsFor,
    turnFoldSignature,
    expandedToolGroups,
    onToolGroupToggle,
    collapseDetails,
    cancelScheduledTimelineScroll,
    requestLatestAnchor,
    handleTimelineScroll,
    jumpToLatest,
    loadEarlierTurns,
    observeTimelineGeometry,
    activateViewport,
    deactivateViewport,
    registerWatchers,
  }
}
