import { ref, shallowRef, onUnmounted, type Ref, type ShallowRef } from 'vue'
import type {
  AgentStreamEvent,
  AgentStreamEventPage,
  StreamCapabilities,
} from '@/types'
import { validateImageAttachment, fileToDataUrl, generatePreviewDataUrl } from '@/utils/agentStreamAttachments'
import { createContiguousEventBuffer } from '@/utils/agentStreamSequence'
import { AgentStreamBatcher } from '@/utils/agentStreamBatcher'
import { StreamConnectionStateMachine } from '@/utils/streamConnectionStateMachine'

export { validateImageAttachment, fileToDataUrl, generatePreviewDataUrl }

const API_BASE = '/api'

/**
 * Connection lifecycle for the structured observation plane.
 *
 * - `idle`: not started.
 * - `hydrating`: fetching capabilities + initial event page.
 * - `live`: long-poll reconciliation is active; SSE may accelerate delivery.
 * - `failed`: hard failure — Chat surface stays visible and offers retry.
 */
export type StreamConnectionState = 'idle' | 'hydrating' | 'live' | 'failed'
export type StreamSource = 'managed-session' | 'terminal-tab'

/**
 * Hard upper bound on a single hydration fetch (capabilities or one events
 * page). If the backend does not respond within this window the request is
 * aborted and the stream fails closed rather than hanging on ``hydrating``.
 */
const HYDRATION_FETCH_TIMEOUT_MS = 15_000

export interface UseAgentStreamApi {
  capabilities: ShallowRef<StreamCapabilities | null>
  events: Ref<AgentStreamEvent[]>
  connectionState: Ref<StreamConnectionState>
  errorMessage: Ref<string | null>
  /** Start (or restart) a managed-session or direct Chat-tab stream. */
  start: (sourceId: string, source?: StreamSource) => Promise<void>
  /** Replace a failed provider transport, then hydrate its resumed stream. */
  retry: (sourceId: string, source?: StreamSource) => Promise<void>
  /** Tear down the stream (SSE / long-poll). Safe to call repeatedly. */
  stop: () => void
}

/**
 * Structured Chat-stream client.
 *
 * Hydration contract (sequence-safe):
 *   1. GET /stream/capabilities — fail closed if ``structured=false``.
 *   2. GET /stream/events?since_sequence=-1 — backfill the timeline.
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
 * the connection state machine. State transitions (``hydrating`` → ``live`` /
 * ``failed``) are only honoured when the caller's generation id matches the
 * current one. This prevents a superseded hydration that resolves late from
 * flipping a newer generation's ``hydrating`` to ``live`` — the root cause of
 * the permanent "Loading structured view" stall when switching tabs.
 *
 * Stale in-flight hydration fetches are cancelled via an ``AbortController``
 * owned by the current generation, and ``applyPage`` is guarded by generation
 * so a stale page cannot advance the shared sequence cursor.
 *
 * Chat sessions never silently fall back to raw; the composable reports an
 * explicit retryable failure to StructuredPane.
 */
export function useAgentStream(): UseAgentStreamApi {
  const capabilities = shallowRef<StreamCapabilities | null>(null)
  const events = ref<AgentStreamEvent[]>([])
  const connectionState = ref<StreamConnectionState>('idle')
  const errorMessage = ref<string | null>(null)

  const stateMachine = new StreamConnectionStateMachine()

  let currentSessionId: string | null = null
  let eventSource: EventSource | null = null
  let longPollAbort: AbortController | null = null
  /** Aborts the in-flight capabilities / events hydration fetches. */
  let hydrationAbort: AbortController | null = null
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

  function enqueueEvents(committed: AgentStreamEvent[]) {
    batcher.enqueue(committed)
  }

  function reset() {
    // Flush any pending events before clearing so they are not lost.
    batcher.flushAndCancel()
    capabilities.value = null
    events.value = []
    errorMessage.value = null
    sequenceBuffer.reset()
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
  }

  function abortHydration() {
    if (hydrationAbort) {
      hydrationAbort.abort()
      hydrationAbort = null
    }
  }

  function applyPage(page: AgentStreamEventPage, generationId: number) {
    // A stale page (from a superseded generation) must not advance the shared
    // sequence cursor; otherwise the current generation's events would be
    // skipped because their stream_sequence is <= the stale cursor.
    if (!stateMachine.isCurrent(generationId)) return
    const committed = sequenceBuffer.push(page.events)
    if (committed.length) enqueueEvents(committed)
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
  ): Promise<StreamCapabilities> {
    const res = await fetchWithTimeout(`${streamPath}/capabilities`, { signal }, HYDRATION_FETCH_TIMEOUT_MS)
    if (!res.ok) throw new Error(`capabilities HTTP ${res.status}`)
    return (await res.json()) as StreamCapabilities
  }

  async function fetchEvents(
    streamPath: string,
    since: number,
    signal: AbortSignal,
  ): Promise<AgentStreamEventPage> {
    const res = await fetchWithTimeout(
      `${streamPath}/events?since_sequence=${since}&limit=200`,
      { signal },
      HYDRATION_FETCH_TIMEOUT_MS,
    )
    if (!res.ok) throw new Error(`events HTTP ${res.status}`)
    return (await res.json()) as AgentStreamEventPage
  }

  async function waitEvents(streamPath: string, since: number): Promise<AgentStreamEventPage> {
    const res = await fetch(`${streamPath}/wait`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ since_sequence: since, timeout_seconds: 30 }),
      signal: longPollAbort?.signal,
    })
    if (!res.ok) throw new Error(`wait HTTP ${res.status}`)
    return (await res.json()) as AgentStreamEventPage
  }

  /** Authoritative live reconciliation loop; SSE is only an accelerator. */
  async function longPollLoop(sourceId: string, streamPath: string, generationId: number) {
    while (!stopped && currentSessionId === sourceId && stateMachine.isCurrent(generationId)) {
      try {
        const page = await waitEvents(streamPath, sequenceBuffer.cursor)
        applyPage(page, generationId)
      } catch (err) {
        if (stopped || currentSessionId !== sourceId || !stateMachine.isCurrent(generationId)) return
        // Surface the failure and stop; the Chat surface stays fail-closed.
        const message = err instanceof Error ? err.message : 'stream wait failed'
        if (stateMachine.fail(generationId, message)) {
          errorMessage.value = message
          connectionState.value = 'failed'
        }
        return
      }
    }
  }

  function startSse(sourceId: string, streamPath: string, generationId: number) {
    const url = `${streamPath}/live?since_sequence=${sequenceBuffer.cursor}`
    eventSource = new EventSource(url)

    eventSource.addEventListener('hello', (ev: MessageEvent) => {
      if (!stateMachine.isCurrent(generationId)) return
      try {
        const caps = JSON.parse(ev.data) as StreamCapabilities
        capabilities.value = caps
        if (!caps.structured) {
          const message = 'structured observation unavailable for this session'
          if (stateMachine.fail(generationId, message)) {
            errorMessage.value = message
            connectionState.value = 'failed'
          }
          stop()
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
        if (committed.length) enqueueEvents(committed)
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
    reset()

    const generationId = stateMachine.start()
    connectionState.value = 'hydrating'

    // Abort controller for the current generation's hydration fetches. A newer
    // start() (or stop()) will abort these, so a stale fetch cannot resolve
    // against the current generation's state.
    hydrationAbort = new AbortController()
    const signal = hydrationAbort.signal

    const streamPath = streamBasePath(sourceId, source)

    try {
      const caps = await fetchCapabilities(streamPath, signal)
      if (!stateMachine.isCurrent(generationId)) return
      capabilities.value = caps
      if (!caps.structured) {
        const message = 'structured observation unavailable for this session'
        if (stateMachine.fail(generationId, message)) {
          errorMessage.value = message
          connectionState.value = 'failed'
        }
        return
      }

      // Hydrate: pull the full history before going live.
      let since = -1

      while (true) {
        if (stopped || !stateMachine.isCurrent(generationId)) return
        const page = await fetchEvents(streamPath, since, signal)
        if (stopped || !stateMachine.isCurrent(generationId)) return
        applyPage(page, generationId)
        since = page.next_sequence
        if (!page.has_more) break
      }

      if (stopped || !stateMachine.isCurrent(generationId)) return

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
      const message = err instanceof Error ? err.message : 'stream start failed'
      if (stateMachine.fail(generationId, message)) {
        errorMessage.value = message
        connectionState.value = 'failed'
      }
    }
  }

  async function retry(sourceId: string, source: StreamSource = 'managed-session') {
    stop()
    stopped = false
    currentSessionId = sourceId
    reset()

    const generationId = stateMachine.start()
    connectionState.value = 'hydrating'

    hydrationAbort = new AbortController()
    const signal = hydrationAbort.signal

    const streamPath = streamBasePath(sourceId, source)

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

  function stop() {
    stopped = true
    currentSessionId = null
    closeSse()
    abortLongPoll()
    abortHydration()
    stateMachine.stop()
    connectionState.value = 'idle'
    // Flush any buffered events so the final state is committed before
    // teardown; cancel any pending flush timers.
    batcher.flushAndCancel()
  }

  onUnmounted(() => {
    stop()
  })

  return {
    capabilities,
    events,
    connectionState,
    errorMessage,
    start,
    retry,
    stop,
  }
}
