import { type Ref, type ComputedRef, computed, nextTick, onMounted, onUnmounted, ref, shallowReactive, watch } from 'vue'
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
import { withOptimisticCancelled } from '@/utils/agentStreamStopReset'
import { createSubthreadThinkingPresenter } from '@/utils/thinkingPresentation'
import { isTurnProcessExpanded, presentTurnParts, turnProcessFoldSignature } from '@/features/chat/presentation/chatTurnPresentation'

interface Dependencies {
  historyReady: Ref<boolean>
  connectionState: Ref<'idle' | 'hydrating' | 'live' | 'reconciling' | 'failed'>
  events: Ref<AgentStreamEvent[]>
  historyDetails: Ref<Record<string, AgentStreamHistoryDetailEntry>>
  historyDetailRevision: Ref<number>
  loadHistoryDetail: (turnId: string) => Promise<void>
  isApprovalResolved: ReturnType<typeof useQuestionAnswers>['isApprovalResolved']
  isEditingTurn: (turn: TimelineTurn) => boolean
  optimisticCancelledTurnId: Ref<string | null>
  pendingTurns: ComputedRef<PendingTurn[]>
  props: ChatPaneProps
}

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

  /** Parts to render for a turn, with its process folded away once it is history. */
  function turnPartsFor(turn: TimelineTurn) {
    const detail = turn.turnId ? deps.historyDetails.value[turn.turnId] : undefined
    return presentTurnParts(turn, latestTurnKey.value, processExpandedOverrides.value, detail)
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
    timelineVisit++
    visibleHistoryStart.value = null
    resetActivation()
  }

  function deactivateViewport() {
    timelineDisposed = true
    timelineVisit++
    timelineResizeObserver?.disconnect()
    timelineResizeObserver = null
    cancelScheduledTimelineScroll()
  }

  function registerWatchers() {
    watch(
      () => [deps.events.value.length, deps.pendingTurns.value.length],
      () => requestLatestAnchor(),
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
    turnPartsFor,
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
