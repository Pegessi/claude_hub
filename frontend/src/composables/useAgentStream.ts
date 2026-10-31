import { ref, shallowRef, onUnmounted, type Ref, type ShallowRef } from 'vue'
import type {
  AgentStreamEvent,
  AgentStreamEventPage,
  AgentStreamHistoryDetail,
  StreamCapabilities,
} from '@/types'
import { validateImageAttachment, fileToDataUrl, generatePreviewDataUrl } from '@/utils/agentStreamAttachments'
import { createContiguousEventBuffer } from '@/utils/agentStreamSequence'
import { AgentStreamBatcher } from '@/utils/agentStreamBatcher'
import { StreamConnectionStateMachine } from '@/utils/streamConnectionStateMachine'
import { agentStreamCapabilitiesCache } from '@/utils/agentStreamCapabilitiesCache'
import {
  agentStreamHistoryCache,
  type AgentStreamHistoryDetailEntry,
  type AgentStreamHistorySnapshot,
} from '@/utils/agentStreamHistoryCache'

export { validateImageAttachment, fileToDataUrl, generatePreviewDataUrl }

const API_BASE = '/api'

/**
 * Connection lifecycle for the structured observation plane.
 *
 * - `idle`: not started.
 * - `hydrating`: fetching capabilities + initial event page.
 * - `reconciling`: cached history is visible while missed events are fetched.
 * - `live`: long-poll reconciliation is active; SSE may accelerate delivery.
 * - `failed`: hard failure — Chat surface stays visible and offers retry.
 */
export type StreamConnectionState = 'idle' | 'hydrating' | 'reconciling' | 'live' | 'failed'
export type StreamSource = 'managed-session' | 'terminal-tab'

/**
 * Hard upper bound on a single hydration fetch (capabilities or one events
 * page). If the backend does not respond within this window the request is
 * aborted and the stream fails closed rather than hanging on ``hydrating``.
 */
const HYDRATION_FETCH_TIMEOUT_MS = 15_000
/** Large enough to avoid hundreds of serial round trips for delta-heavy history. */
const HYDRATION_PAGE_LIMIT = 5_000
/**
 * The authoritative ``/wait`` long-poll tolerates transient transport blips
 * (a proxy 502, a brief backend reload during a long queue) instead of failing
 * closed on the first error: it retries with capped exponential backoff while
 * staying ``live`` (so Stop/Send stay armed and no reconnect banner flashes).
 * Only after the budget is exhausted does it surface ``failed``. A genuine
 * session loss therefore still fails, but a single dropped poll never does.
 */
// A planned service restart can outlast a short network blip. These retries
// only observe the existing session; they never replace or resend a provider turn.
const LONG_POLL_MAX_RETRIES = 60
const LONG_POLL_RETRY_BASE_MS = 1_000
const LONG_POLL_RETRY_MAX_MS = 5_000

class StreamWaitError extends Error {
  constructor(readonly status: number, detail?: string) {
    super(`wait HTTP ${status}${detail ? `: ${detail}` : ''}`)
  }
}

export interface UseAgentStreamApi {
  capabilities: ShallowRef<StreamCapabilities | null>
  events: ShallowRef<AgentStreamEvent[]>
  historyDetails: ShallowRef<Record<string, AgentStreamHistoryDetailEntry>>
  attributedToolIds: ShallowRef<ReadonlySet<string>>
  historyDetailRevision: Ref<number>
  connectionState: Ref<StreamConnectionState>
  errorMessage: Ref<string | null>
  /** Durable history can be shown before runtime controls are ready. */
  historyReady: Ref<boolean>
  /** Start (or restart) a managed-session or direct Chat-tab stream. */
  start: (sourceId: string, source?: StreamSource) => Promise<void>
  /** Fetch a folded historical turn's full process without advancing the live cursor. */
  loadHistoryDetail: (turnId: string) => Promise<void>
  /** Replace a failed provider transport, then hydrate its resumed stream. */
  retry: (sourceId: string, source?: StreamSource) => Promise<void>
  /** Refresh active-session capabilities without restarting timeline delivery. */
  refreshCapabilities: () => Promise<void>
  /** Update the active provider mode; the new mode applies to the next turn. */
  setMode: (mode: string) => Promise<void>
  /** Tear down the stream (SSE / long-poll). Safe to call repeatedly. */
  stop: () => void
  /** Re-issue the in-flight /wait immediately (e.g. right after Stop). */
  nudge: () => void
  /** Clear in-memory state and optionally restore a cached history snapshot. */
  reset: (snapshot?: AgentStreamHistorySnapshot) => void
}

/**
 * Structured Chat-stream client.
 *
 * Hydration contract (sequence-safe):
 *   1. GET persisted history and prepare capabilities concurrently.
 *   2. Reveal history independently; enable live controls only with valid capabilities.
 *   3. POST /stream/wait — authoritative live reconciliation loop.
 *   4. GET /stream/live (SSE) — optional low-latency accelerator.
 *
 * Both live paths are sequence-deduplicated. Keeping long-poll active even
 * when EventSource exists prevents a proxy-buffered or silently stale SSE
 * connection from freezing the visible timeline.
 *
 * Generation ownership
 * --------------------
 * Every ``start`` (and ``stop``) advances an internal generation counter on
 * the connection state machine. State transitions (``hydrating`` /
 * ``reconciling`` → ``live`` / ``failed``) are only honoured when the
 * caller's generation id matches the current one. This prevents a superseded
 * hydration that resolves late from flipping a newer generation's
 * ``hydrating`` to ``live`` — the root cause of the permanent "Loading
 * structured view" stall when switching tabs.
 *
 * Stale in-flight hydration fetches are cancelled via an ``AbortController``
 * owned by the current generation, and ``applyPage`` is guarded by generation
 * so a stale page cannot advance the shared sequence cursor.
 *
 * Chat sessions never silently fall back to raw; the composable reports an
 * explicit retryable failure to StructuredPane.
 */
export function useAgentStream(configuration: () => string = () => ''): UseAgentStreamApi {
  const capabilities = shallowRef<StreamCapabilities | null>(null)
  // Batches replace the array; event payloads are immutable. Deep proxies
  // make replaying a long transcript substantially more expensive.
  const events = shallowRef<AgentStreamEvent[]>([])
  const historyDetails = shallowRef<Record<string, AgentStreamHistoryDetailEntry>>({})
  const attributedToolIds = shallowRef<ReadonlySet<string>>(new Set())
  const historyDetailRevision = ref(0)
  const connectionState = ref<StreamConnectionState>('idle')
  const errorMessage = ref<string | null>(null)
  const historyReady = ref(false)
  let capabilitiesRevision = 0

  const stateMachine = new StreamConnectionStateMachine()

  let currentSessionId: string | null = null
  let currentStreamPath: string | null = null
  let eventSource: EventSource | null = null
  let longPollAbort: AbortController | null = null
  /**
   * Aborts ONLY the in-flight ``/wait`` request without stopping the loop, so
   * an explicit nudge (e.g. after Stop) makes the loop re-issue immediately
   * and fetch the just-persisted terminal edge in one RTT, instead of waiting
   * for the previous 30s long-poll to time out. ``longPollAbort`` above is the
   * generation-level signal: aborting it tears the whole loop down.
   */
  let waitRequestAbort: AbortController | null = null
  /** Aborts the in-flight capabilities / events hydration fetches. */
  let hydrationAbort: AbortController | null = null
  /** Latest-wins owner for requests that replace active capabilities. */
  let capabilitiesUpdateAbort: AbortController | null = null
  let pendingCapabilitiesUpdate: Promise<void> | null = null
  let nextHistoryDetailRequestId = 1
  const historyDetailRequests = new Map<
    string,
    { id: number; descriptor: AgentStreamHistoryDetail; abort: AbortController }
  >()
  // Stream sequences are zero-based and cursors are exclusive. SSE is only an
  // accelerator: future events stay buffered until long-poll fills every gap.
  const sequenceBuffer = createContiguousEventBuffer<AgentStreamEvent>()
  let stopped = false

  // ── event micro-batching ───────────────────────────────────────────────
  // Incoming committed events are accumulated and flushed to `events.value`
  // on a rAF / 48ms timer (whichever fires first). This caps the number of
  // reactive timeline re-renders during high-throughput streams (long
  // Thinking bursts) without increasing end-to-end latency beyond one
  // frame. Terminal events bypass the window and flush immediately.
  const batcher = new AgentStreamBatcher((batch) => {
    events.value = [...events.value, ...batch]
  })

  function normalizeHistoryDetailsForCache(
    details: Record<string, AgentStreamHistoryDetailEntry>,
  ): Record<string, AgentStreamHistoryDetailEntry> {
    const normalized: Record<string, AgentStreamHistoryDetailEntry> = {}
    for (const [turnId, entry] of Object.entries(details)) {
      normalized[turnId] = entry.status === 'loading'
        ? { descriptor: entry.descriptor, status: 'idle' }
        : entry
    }
    return normalized
  }

  function enqueueEvents(committed: AgentStreamEvent[]) {
    batcher.enqueue(committed)
  }

  function abortHistoryDetailRequests() {
    for (const request of historyDetailRequests.values()) request.abort.abort()
    historyDetailRequests.clear()
  }

  function reset(snapshot?: AgentStreamHistorySnapshot) {
    // Flush any pending events before clearing so they are not lost.
    batcher.flushAndCancel()
    if (!snapshot && currentStreamPath) agentStreamHistoryCache.delete(currentStreamPath)
    abortHistoryDetailRequests()
    capabilitiesRevision += 1
    historyReady.value = Boolean(snapshot)
    capabilities.value = snapshot?.capabilities ?? null
    events.value = snapshot?.events ?? []
    historyDetails.value = snapshot?.historyDetails
      ? normalizeHistoryDetailsForCache(snapshot.historyDetails)
      : {}
    attributedToolIds.value = snapshot?.attributedToolIds ? new Set(snapshot.attributedToolIds) : new Set()
    historyDetailRevision.value = snapshot?.detailRevision ?? 0
    errorMessage.value = null
    sequenceBuffer.reset(snapshot?.cursor ?? -1)
  }

  function currentHistorySnapshot(): AgentStreamHistorySnapshot {
    batcher.flushAndCancel()
    return {
      capabilities: capabilities.value,
      events: events.value,
      cursor: sequenceBuffer.cursor,
      historyDetails: normalizeHistoryDetailsForCache(historyDetails.value),
      attributedToolIds: new Set(attributedToolIds.value),
      detailRevision: historyDetailRevision.value,
    }
  }

  function cacheCurrentHistory() {
    const snapshot = currentHistorySnapshot()
    if (!currentStreamPath || !capabilities.value?.structured) return
    agentStreamHistoryCache.set(currentStreamPath, snapshot)
  }

  function closeSse() {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
  }

  function abortLongPoll() {
    if (longPollAbort) {
      longPollAbort.abort()
      longPollAbort = null
    }
    waitRequestAbort = null
  }

  /**
   * Re-issue the in-flight ``/wait`` immediately (coalesced; abort is
   * idempotent). Used after a successful Stop so the cancelled terminal edge
   * arrives in one RTT even while the surface is off-live and the previous
   * long-poll still has up to 30s left on the server.
   */
  function nudge() {
    waitRequestAbort?.abort()
  }

  function abortHydration() {
    if (hydrationAbort) {
      hydrationAbort.abort()
      hydrationAbort = null
    }
  }

  function abortCapabilitiesUpdate() {
    pendingCapabilitiesUpdate = null
    if (capabilitiesUpdateAbort) {
      capabilitiesUpdateAbort.abort()
      capabilitiesUpdateAbort = null
    }
  }

  function replayIdentity(event: AgentStreamEvent): string | null {
    if (![
      'tool_call_started',
      'tool_call_completed',
      'approval_required',
      'approval_resolved',
    ].includes(event.type)) return null
    const fromPayload = event.payload.tool_call_id
    if (typeof fromPayload === 'string' && fromPayload) return fromPayload
    return typeof event.call_id === 'string' && event.call_id ? event.call_id : null
  }

  function filterLiveCommittedEvents(committed: AgentStreamEvent[]): AgentStreamEvent[] {
    const nextIds = new Set(attributedToolIds.value)
    const accepted: AgentStreamEvent[] = []
    let changedIds = false
    for (const event of committed) {
      const identity = replayIdentity(event)
      if (!identity) {
        accepted.push(event)
        continue
      }
      if (!event.turn_id && nextIds.has(identity)) {
        changedIds = true
        continue
      }
      accepted.push(event)
      if (event.turn_id && !nextIds.has(identity)) {
        nextIds.add(identity)
        changedIds = true
      }
    }
    if (changedIds) attributedToolIds.value = nextIds
    return accepted
  }

  function applyPage(page: AgentStreamEventPage, generationId: number) {
    // A stale page (from a superseded generation) must not advance the shared
    // sequence cursor; otherwise the current generation's events would be
    // skipped because their stream_sequence is <= the stale cursor.
    if (!stateMachine.isCurrent(generationId)) return
    const committed = sequenceBuffer.push(page.events)
    if (committed.length) enqueueEvents(filterLiveCommittedEvents(committed))
  }

  function detailChanged(previous: AgentStreamHistoryDetail, next: AgentStreamHistoryDetail): boolean {
    return previous.first_sequence !== next.first_sequence ||
      previous.last_sequence !== next.last_sequence ||
      previous.event_count !== next.event_count
  }

  function sameHistoryDetailDescriptor(
    previous: AgentStreamHistoryDetail,
    next: AgentStreamHistoryDetail,
  ): boolean {
    return previous.turn_id === next.turn_id && !detailChanged(previous, next)
  }

  function currentHistoryDetailMatches(turnId: string, descriptor: AgentStreamHistoryDetail): boolean {
    const current = historyDetails.value[turnId]
    return Boolean(current && sameHistoryDetailDescriptor(current.descriptor, descriptor))
  }

  function recordHistoryDetails(details: AgentStreamHistoryDetail[] | undefined) {
    if (!details || details.length === 0) return
    const next = { ...historyDetails.value }
    let changed = false
    for (const descriptor of details) {
      const existing = next[descriptor.turn_id]
      if (!existing) {
        next[descriptor.turn_id] = { descriptor, status: 'idle' }
        changed = true
      } else if (detailChanged(existing.descriptor, descriptor)) {
        // A newer summary covers a different raw range. Previously fetched
        // detail is no longer complete for this descriptor; make the next
        // expansion read the fixed, current range and retire any in-flight
        // request for the older range.
        const request = historyDetailRequests.get(descriptor.turn_id)
        request?.abort.abort()
        historyDetailRequests.delete(descriptor.turn_id)
        next[descriptor.turn_id] = { descriptor, status: 'idle' }
        changed = true
      }
    }
    if (changed) historyDetails.value = next
  }

  function markHistoryDetail(
    turnId: string,
    patch: Partial<AgentStreamHistoryDetailEntry>,
    expectedDescriptor?: AgentStreamHistoryDetail,
  ) {
    const existing = historyDetails.value[turnId]
    if (!existing) return
    if (expectedDescriptor && !sameHistoryDetailDescriptor(existing.descriptor, expectedDescriptor)) return
    historyDetails.value = {
      ...historyDetails.value,
      [turnId]: { ...existing, ...patch },
    }
  }

  function replaceHistoryDetailEvents(turnId: string, descriptor: AgentStreamHistoryDetail, detailEvents: AgentStreamEvent[]) {
    const lower = descriptor.first_sequence
    const upper = descriptor.last_sequence
    const retained = events.value.filter(event => {
      if (event.turn_id !== turnId) return true
      return event.stream_sequence < lower || event.stream_sequence > upper
    })
    const orderedDetail = [...detailEvents].sort((a, b) => a.stream_sequence - b.stream_sequence)
    events.value = [...retained, ...orderedDetail].sort((a, b) => a.stream_sequence - b.stream_sequence)
    historyDetailRevision.value += 1
  }

  function recordAttributedToolIds(ids: string[] | undefined) {
    if (!ids || ids.length === 0) return
    const next = new Set(attributedToolIds.value)
    let changed = false
    for (const id of ids) {
      if (!next.has(id)) {
        next.add(id)
        changed = true
      }
    }
    if (changed) attributedToolIds.value = next
  }

  function applyCompactedHistoryPage(page: AgentStreamEventPage, generationId: number) {
    if (!stateMachine.isCurrent(generationId)) return
    recordHistoryDetails(page.history_details)
    recordAttributedToolIds(page.attributed_tool_ids)
    // Compacted history intentionally has sequence gaps: adjacent durable
    // deltas are represented by one event.  Commit those presentation events
    // directly, then advance the correctness cursor to the raw page cursor so
    // long-poll/SSE resume after every durable row covered by the response.
    if (page.events.length) enqueueEvents(page.events)
    sequenceBuffer.reset(page.next_sequence)
  }

  function streamBasePath(sourceId: string, source: StreamSource): string {
    return source === 'terminal-tab'
      ? `${API_BASE}/workspaces/tabs/${sourceId}/stream`
      : `${API_BASE}/workspaces/sessions/${sourceId}/stream`
  }

  /**
   * Fetch with a hard timeout. The request is aborted if it does not resolve
   * within ``timeoutMs`` so hydration can never hang indefinitely.
   */
  async function fetchWithTimeout(
    input: string,
    init: RequestInit & { signal?: AbortSignal } = {},
    timeoutMs: number,
  ): Promise<Response> {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    // Chain the caller's signal (if any) so an explicit abort also cancels.
    const upstreamSignal = init.signal
    const onUpstreamAbort = () => controller.abort()
    if (upstreamSignal) {
      if (upstreamSignal.aborted) controller.abort()
      else upstreamSignal.addEventListener('abort', onUpstreamAbort, { once: true })
    }
    try {
      return await fetch(input, { ...init, signal: controller.signal })
    } finally {
      clearTimeout(timer)
      if (upstreamSignal) upstreamSignal.removeEventListener('abort', onUpstreamAbort)
    }
  }

  async function fetchCapabilities(
    streamPath: string,
    signal: AbortSignal,
    refresh = false,
  ): Promise<StreamCapabilities> {
    const suffix = refresh ? '?refresh=true' : ''
    const res = await fetchWithTimeout(`${streamPath}/capabilities${suffix}`, { signal }, HYDRATION_FETCH_TIMEOUT_MS)
    if (!res.ok) throw new Error(`capabilities HTTP ${res.status}`)
    return (await res.json()) as StreamCapabilities
  }

  async function fetchEvents(
    streamPath: string,
    since: number,
    signal: AbortSignal,
    options: { view?: 'conversation'; turnId?: string; throughSequence?: number } = {},
  ): Promise<AgentStreamEventPage> {
    const params = new URLSearchParams({
      since_sequence: String(since),
      limit: String(HYDRATION_PAGE_LIMIT),
      compact: 'true',
      persisted_only: 'true',
    })
    if (options.view) params.set('view', options.view)
    if (options.turnId) params.set('turn_id', options.turnId)
    if (options.throughSequence !== undefined) params.set('through_sequence', String(options.throughSequence))
    const res = await fetchWithTimeout(
      `${streamPath}/events?${params.toString()}`,
      { signal },
      HYDRATION_FETCH_TIMEOUT_MS,
    )
    if (!res.ok) throw new Error(`events HTTP ${res.status}`)
    return (await res.json()) as AgentStreamEventPage
  }

  async function waitEvents(streamPath: string, since: number, signal?: AbortSignal): Promise<AgentStreamEventPage> {
    const res = await fetch(`${streamPath}/wait`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ since_sequence: since, timeout_seconds: 30 }),
      signal: signal ?? longPollAbort?.signal,
    })
    if (!res.ok) {
      let detail: string | undefined
      try {
        const body: unknown = await res.json()
        if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') {
          detail = body.detail.trim()
        }
      } catch {
        // Proxy error pages are not provider diagnostics; keep the HTTP status.
      }
      throw new StreamWaitError(res.status, detail)
    }
    return (await res.json()) as AgentStreamEventPage
  }

  /** Authoritative live reconciliation loop; SSE is only an accelerator. */
  async function longPollLoop(sourceId: string, streamPath: string, generationId: number) {
    let consecutiveFailures = 0
    while (!stopped && currentSessionId === sourceId && stateMachine.isCurrent(generationId)) {
      // One short-lived controller per request lets an explicit nudge abort
      // just this /wait (so it re-issues immediately) while the generation's
      // longPollAbort still tears the whole loop down.
      const reqController = new AbortController()
      waitRequestAbort = reqController
      const parentSignal = longPollAbort?.signal
      const onParentAbort = () => reqController.abort()
      if (parentSignal) {
        if (parentSignal.aborted) reqController.abort()
        else parentSignal.addEventListener('abort', onParentAbort, { once: true })
      }
      try {
        const page = await waitEvents(streamPath, sequenceBuffer.cursor, reqController.signal)
        applyPage(page, generationId)
        consecutiveFailures = 0
      } catch (err) {
        if (stopped || currentSessionId !== sourceId || !stateMachine.isCurrent(generationId)) return
        if (longPollAbort?.signal.aborted) return
        // An explicit nudge (e.g. right after Stop): re-issue /wait at once so
        // the just-persisted terminal edge lands in one RTT instead of waiting
        // out the previous 30s poll. Not counted as a transport failure.
        if (reqController.signal.aborted) {
          continue
        }
        // Transient blip: stay live and retry with capped backoff rather than
        // failing on one dropped poll (which used to disable Stop and flash a
        // reconnect banner during a long provider queue).
        const retryable = !(err instanceof StreamWaitError) || err.status === 408 || err.status === 429 || err.status >= 500
        if (retryable && consecutiveFailures < LONG_POLL_MAX_RETRIES) {
          consecutiveFailures += 1
          const delay = Math.min(
            LONG_POLL_RETRY_BASE_MS * 2 ** (consecutiveFailures - 1),
            LONG_POLL_RETRY_MAX_MS,
          )
          await new Promise<void>(resolve => {
            const timer = setTimeout(resolve, delay)
            reqController.signal.addEventListener(
              'abort',
              () => {
                clearTimeout(timer)
                resolve()
              },
              { once: true },
            )
          })
          continue
        }
        // Budget exhausted — surface the failure and stop; fail closed.
        agentStreamCapabilitiesCache.delete(streamPath)
        const message = err instanceof Error ? err.message : 'stream wait failed'
        if (stateMachine.fail(generationId, message)) {
          errorMessage.value = message
          connectionState.value = 'failed'
        }
        return
      } finally {
        parentSignal?.removeEventListener('abort', onParentAbort)
        if (waitRequestAbort === reqController) waitRequestAbort = null
      }
    }
  }

  function startSse(sourceId: string, streamPath: string, generationId: number) {
    const url = `${streamPath}/live?since_sequence=${sequenceBuffer.cursor}`
    eventSource = new EventSource(url)
    const helloRevision = capabilitiesRevision
    const helloConfiguration = configuration()

    eventSource.addEventListener('hello', (ev: MessageEvent) => {
      if (!stateMachine.isCurrent(generationId) || capabilitiesRevision !== helloRevision) return
      if (configuration() !== helloConfiguration) return
      try {
        const caps = JSON.parse(ev.data) as StreamCapabilities
        capabilities.value = caps
        agentStreamCapabilitiesCache.set(streamPath, helloConfiguration, caps)
        if (!caps.structured) {
          const message = caps.unavailable_reason || 'structured observation unavailable for this session'
          if (stateMachine.fail(generationId, message)) {
            errorMessage.value = message
            connectionState.value = 'failed'
          }
          stop()
          // stop() retires delivery ownership; retain the failure and readable
          // history rather than replacing the error with an idle loading banner.
          connectionState.value = 'failed'
        }
      } catch {
        // ignore malformed hello
      }
    })

    eventSource.addEventListener('agent-stream', (ev: MessageEvent) => {
      if (!stateMachine.isCurrent(generationId)) return
      try {
        const evt = JSON.parse(ev.data) as AgentStreamEvent
        const committed = sequenceBuffer.push([evt])
        if (committed.length) enqueueEvents(filterLiveCommittedEvents(committed))
      } catch {
        // ignore malformed event
      }
    })

    eventSource.addEventListener('error', (ev: MessageEvent) => {
      if (stopped || currentSessionId !== sourceId || !stateMachine.isCurrent(generationId)) return
      // Long-poll remains authoritative. Retire a broken SSE connection rather
      // than waiting for a browser/proxy reconnect that may stay silently
      // buffered; the wait loop will surface a real session failure.
      const data = (ev as MessageEvent).data
      if (data) {
        try {
          const parsed = JSON.parse(data) as { message?: string }
          errorMessage.value = parsed.message || 'structured stream error'
        } catch {
          errorMessage.value = 'structured stream error'
        }
      }
      closeSse()
    })
  }

  async function start(sourceId: string, source: StreamSource = 'managed-session') {
    stop()
    stopped = false
    currentSessionId = sourceId

    const streamPath = streamBasePath(sourceId, source)
    currentStreamPath = streamPath
    const cached = agentStreamHistoryCache.get(streamPath)
    reset(cached)

    const generationId = stateMachine.start()
    // Cached history is immediately renderable. Keep input disabled while a
    // background capabilities check and incremental catch-up reconcile it.
    connectionState.value = cached ? 'reconciling' : 'hydrating'

    // Abort controller for the current generation's hydration fetches. A newer
    // start() (or stop()) will abort these, so a stale fetch cannot resolve
    // against the current generation's state.
    hydrationAbort = new AbortController()
    const signal = hydrationAbort.signal

    const requestConfiguration = configuration()
    const requestRevision = capabilitiesRevision
    const cachedCapabilities = agentStreamCapabilitiesCache.get(streamPath, requestConfiguration)
    // Handle rejection immediately: history may finish after a failed capability
    // request, and remains useful even when the execution backend is unavailable.
    const prepared = (cachedCapabilities
      ? Promise.resolve(cachedCapabilities)
      : fetchCapabilities(streamPath, signal)
    ).then(value => ({ value }), error => ({ error }))

    try {
      // A cold mount pulls full history. A remount resumes at the cached
      // contiguous cursor and fetches only events published while hidden.
      let since = sequenceBuffer.cursor

      while (true) {
        if (stopped || !stateMachine.isCurrent(generationId)) return
        const page = await fetchEvents(streamPath, since, signal, { view: 'conversation' })
        if (stopped || !stateMachine.isCurrent(generationId)) return
        applyCompactedHistoryPage(page, generationId)
        since = page.next_sequence
        if (!page.has_more) break
      }

      if (stopped || !stateMachine.isCurrent(generationId)) return

      // The activation gate may reveal immediately on live. Commit the final
      // delta-only page before that transition, even with rAF suspended.
      batcher.flushAndCancel()
      historyReady.value = true
      connectionState.value = 'reconciling'
      const result = await prepared
      if (stopped || !stateMachine.isCurrent(generationId)) return
      if (configuration() !== requestConfiguration) {
        await start(sourceId, source)
        return
      }
      // A settings request may supersede preparation while history is loading.
      // Wait for its result rather than admitting input with the older snapshot.
      let pending = pendingCapabilitiesUpdate
      while (pending) {
        try {
          await pending
        } catch (error) {
          if (pending === pendingCapabilitiesUpdate) throw error
        }
        if (stopped || !stateMachine.isCurrent(generationId)) return
        if (pending === pendingCapabilitiesUpdate) break
        pending = pendingCapabilitiesUpdate
      }
      // A settings response in this generation takes precedence over the older
      // hydration response, even when a mocked/remote fetch ignores cancellation.
      if (capabilitiesRevision === requestRevision) {
        if ('error' in result) throw result.error
        capabilities.value = result.value
        if (!cachedCapabilities) agentStreamCapabilitiesCache.set(streamPath, requestConfiguration, result.value)
      }
      const caps = capabilities.value
      if (!caps?.structured) {
        agentStreamCapabilitiesCache.delete(streamPath)
        throw new Error(caps?.unavailable_reason || 'structured observation unavailable for this session')
      }
      // A legacy transcript can be imported during first-time preparation.
      // Preserve summary/detail folding instead of replaying that whole import
      // through the raw live path when the initial durable view was empty.
      if (!cachedCapabilities && events.value.length === 0) {
        while (true) {
          const page = await fetchEvents(streamPath, since, signal, { view: 'conversation' })
          if (stopped || !stateMachine.isCurrent(generationId)) return
          applyCompactedHistoryPage(page, generationId)
          since = page.next_sequence
          if (!page.has_more) break
        }
        batcher.flushAndCancel()
      }
      if (stateMachine.success(generationId)) {
        connectionState.value = 'live'
      }

      // Long-poll is the correctness path and wakes as soon as the backend
      // tailer publishes an event. SSE runs alongside it when available for
      // lower latency; applyPage/sequence checks deduplicate both paths.
      longPollAbort = new AbortController()
      void longPollLoop(sourceId, streamPath, generationId)

      if (typeof EventSource !== 'undefined') {
        startSse(sourceId, streamPath, generationId)
      }
    } catch (err) {
      if (stopped || !stateMachine.isCurrent(generationId)) return
      agentStreamCapabilitiesCache.delete(streamPath)
      const message = err instanceof Error ? err.message : 'stream start failed'
      if (stateMachine.fail(generationId, message)) {
        errorMessage.value = message
        connectionState.value = 'failed'
      }
    }
  }

  async function loadHistoryDetail(turnId: string) {
    const sourceId = currentSessionId
    const streamPath = currentStreamPath
    const entry = historyDetails.value[turnId]
    if (stopped || !sourceId || !streamPath || !entry || entry.status === 'loaded' || entry.status === 'loading') return

    const requestId = nextHistoryDetailRequestId++
    const descriptor = entry.descriptor
    const controller = new AbortController()
    historyDetailRequests.set(turnId, { id: requestId, descriptor, abort: controller })
    markHistoryDetail(turnId, { status: 'loading', errorMessage: undefined }, descriptor)

    const detailGeneration = stateMachine.getGeneration()
    const since = Math.max(-1, descriptor.first_sequence - 1)
    const through = descriptor.last_sequence
    const detailEvents: AgentStreamEvent[] = []
    try {
      let cursor = since
      while (true) {
        if (
          stopped ||
          currentSessionId !== sourceId ||
          currentStreamPath !== streamPath ||
          !stateMachine.isCurrent(detailGeneration)
        ) return
        const page = await fetchEvents(streamPath, cursor, controller.signal, {
          turnId,
          throughSequence: through,
        })
        if (
          stopped ||
          currentSessionId !== sourceId ||
          currentStreamPath !== streamPath ||
          !stateMachine.isCurrent(detailGeneration) ||
          historyDetailRequests.get(turnId)?.id !== requestId ||
          !currentHistoryDetailMatches(turnId, descriptor)
        ) return
        detailEvents.push(...page.events)
        cursor = page.next_sequence
        if (!page.has_more) break
      }
      if (historyDetailRequests.get(turnId)?.id !== requestId || !currentHistoryDetailMatches(turnId, descriptor)) return
      replaceHistoryDetailEvents(turnId, descriptor, detailEvents)
      markHistoryDetail(turnId, { status: 'loaded', errorMessage: undefined }, descriptor)
    } catch (err) {
      if (
        stopped ||
        currentSessionId !== sourceId ||
        currentStreamPath !== streamPath ||
        !stateMachine.isCurrent(detailGeneration)
      ) return
      if (historyDetailRequests.get(turnId)?.id !== requestId || !currentHistoryDetailMatches(turnId, descriptor)) return
      markHistoryDetail(turnId, {
        status: 'error',
        errorMessage: err instanceof Error ? err.message : 'Failed to load execution details',
      }, descriptor)
    } finally {
      if (historyDetailRequests.get(turnId)?.id === requestId) historyDetailRequests.delete(turnId)
    }
  }

  async function retry(sourceId: string, source: StreamSource = 'managed-session') {
    const streamPath = streamBasePath(sourceId, source)
    agentStreamCapabilitiesCache.delete(streamPath)
    const retryHistory = currentStreamPath === streamPath && historyReady.value
      ? currentHistorySnapshot()
      : undefined
    stop()
    stopped = false
    currentSessionId = sourceId
    currentStreamPath = streamPath
    reset(retryHistory)

    const generationId = stateMachine.start()
    connectionState.value = 'hydrating'

    hydrationAbort = new AbortController()
    const signal = hydrationAbort.signal

    try {
      const res = await fetchWithTimeout(
        `${streamPath}/retry`,
        { method: 'POST', credentials: 'same-origin', signal },
        HYDRATION_FETCH_TIMEOUT_MS,
      )
      if (!res.ok) {
        let detail = `retry HTTP ${res.status}`
        try {
          const body = await res.json() as { detail?: string }
          if (body.detail) detail = body.detail
        } catch {
          // Keep the bounded HTTP fallback for non-JSON failures.
        }
        throw new Error(detail)
      }
      // The retry request can outlive a KeepAlive deactivation. If this
      // instance has since stopped or started a newer generation, its late 200
      // must not restart a hidden stream or replace the reactivated one.
      if (
        stopped
        || !stateMachine.isCurrent(generationId)
        || currentSessionId !== sourceId
        || currentStreamPath !== streamPath
      ) return
      if (retryHistory) agentStreamHistoryCache.set(streamPath, currentHistorySnapshot())
      await start(sourceId, source)
    } catch (err) {
      if (stopped || !stateMachine.isCurrent(generationId)) return
      const message = err instanceof Error ? err.message : 'stream retry failed'
      if (stateMachine.fail(generationId, message)) {
        errorMessage.value = message
        connectionState.value = 'failed'
      }
    }
  }

  async function updateCapabilities(
    request: (streamPath: string, signal: AbortSignal) => Promise<StreamCapabilities>,
  ) {
    const sourceId = currentSessionId
    const streamPath = currentStreamPath
    if (stopped || !sourceId || !streamPath) throw new Error('Structured source is unavailable.')

    abortCapabilitiesUpdate()
    capabilitiesRevision += 1
    agentStreamCapabilitiesCache.delete(streamPath)
    const generationId = stateMachine.getGeneration()
    const requestConfiguration = configuration()
    const controller = new AbortController()
    capabilitiesUpdateAbort = controller
    const pending = (async () => {
      const nextCapabilities = await request(streamPath, controller.signal)
      if (
        stopped || currentSessionId !== sourceId || currentStreamPath !== streamPath
        || !stateMachine.isCurrent(generationId) || capabilitiesUpdateAbort !== controller
        || controller.signal.aborted || configuration() !== requestConfiguration
      ) return
      capabilities.value = nextCapabilities
      agentStreamCapabilitiesCache.set(streamPath, requestConfiguration, nextCapabilities)
      if (!nextCapabilities.structured) {
        const message = nextCapabilities.unavailable_reason || 'structured observation unavailable for this session'
        if (stateMachine.fail(generationId, message)) {
          errorMessage.value = message
          connectionState.value = 'failed'
          closeSse()
          abortLongPoll()
        }
        throw new Error(message)
      }
    })()
    pendingCapabilitiesUpdate = pending
    try {
      await pending
    } finally {
      if (capabilitiesUpdateAbort === controller) capabilitiesUpdateAbort = null
      // Retain the settled result until replaced/reset, so hydration can observe
      // a failure even if its original capability response finishes later.
    }
  }

  async function refreshCapabilities() {
    await updateCapabilities((streamPath, signal) => fetchCapabilities(streamPath, signal, true))
  }

  async function setMode(mode: string) {
    await updateCapabilities(async (streamPath, signal) => {
      const res = await fetchWithTimeout(
        `${streamPath}/mode`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ mode }),
          signal,
        },
        HYDRATION_FETCH_TIMEOUT_MS,
      )
      if (!res.ok) {
        let detail = `mode update HTTP ${res.status}`
        try {
          const body = await res.json() as { detail?: string }
          if (body.detail) detail = body.detail
        } catch {
          // Keep the bounded HTTP fallback for non-JSON failures.
        }
        throw new Error(detail)
      }
      return (await res.json()) as StreamCapabilities
    })
  }

  function stop() {
    cacheCurrentHistory()
    stopped = true
    currentSessionId = null
    currentStreamPath = null
    closeSse()
    abortLongPoll()
    abortHydration()
    abortCapabilitiesUpdate()
    abortHistoryDetailRequests()
    stateMachine.stop()
    connectionState.value = 'idle'
  }

  onUnmounted(() => {
    stop()
  })

  return {
    capabilities,
    events,
    historyDetails,
    attributedToolIds,
    historyDetailRevision,
    connectionState,
    errorMessage,
    historyReady,
    start,
    loadHistoryDetail,
    retry,
    refreshCapabilities,
    setMode,
    stop,
    nudge,
    reset,
  }
}
