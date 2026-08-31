import { ref, shallowRef, onUnmounted, type Ref, type ShallowRef } from 'vue'
import type {
  AgentStreamEvent,
  AgentStreamEventPage,
  StreamCapabilities,
} from '@/types'
import { validateImageAttachment, fileToDataUrl } from '@/utils/agentStreamAttachments'

export { validateImageAttachment, fileToDataUrl }

const API_BASE = '/api'

/**
 * Connection lifecycle for the structured observation plane.
 *
 * - `idle`: not started (raw terminal is the only view).
 * - `hydrating`: fetching capabilities + initial event page.
 * - `live`: SSE (or long-poll fallback) is streaming new events.
 * - `failed`: hard failure — consumer should fall back to raw.
 */
export type StreamConnectionState = 'idle' | 'hydrating' | 'live' | 'failed'
export type StreamSource = 'managed-session' | 'terminal-tab'

export interface UseAgentStreamApi {
  capabilities: ShallowRef<StreamCapabilities | null>
  events: Ref<AgentStreamEvent[]>
  connectionState: Ref<StreamConnectionState>
  errorMessage: Ref<string | null>
  /** Start (or restart) a managed-session or Terminal-tab stream. */
  start: (sourceId: string, source?: StreamSource) => Promise<void>
  /** Tear down the stream (SSE / long-poll). Safe to call repeatedly. */
  stop: () => void
}

/**
 * Structured agent-stream client.
 *
 * Hydration contract (sequence-safe):
 *   1. GET /stream/capabilities — fail-closed to raw if ``structured=false``.
 *   2. GET /stream/events?since_sequence=-1 — backfill the timeline.
 *   3. GET /stream/live (SSE) — stream new events.
 *      On SSE failure (non-OK, parse error, or browser without EventSource),
 *      fall back to POST /stream/wait long-polling.
 *
 * The consumer (StructuredPane) owns the decision of whether to show raw or
 * structured; this composable only reports capabilities + connection state.
 */
export function useAgentStream(): UseAgentStreamApi {
  const capabilities = shallowRef<StreamCapabilities | null>(null)
  const events = ref<AgentStreamEvent[]>([])
  const connectionState = ref<StreamConnectionState>('idle')
  const errorMessage = ref<string | null>(null)

  let currentSessionId: string | null = null
  let currentSource: StreamSource = 'managed-session'
  let eventSource: EventSource | null = null
  let longPollAbort: AbortController | null = null
  // Stream sequences are zero-based and cursors are exclusive.
  let deliveredSequence = -1
  let stopped = false

  function reset() {
    capabilities.value = null
    events.value = []
    connectionState.value = 'idle'
    errorMessage.value = null
    deliveredSequence = -1
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

  function applyPage(page: AgentStreamEventPage) {
    if (page.events.length === 0) return
    // Deduplicate by stream_sequence; events are append-only and ordered.
    const existing = new Set(events.value.map(e => e.stream_sequence))
    const fresh = page.events.filter(e => !existing.has(e.stream_sequence))
    if (fresh.length) {
      events.value = [...events.value, ...fresh]
    }
    if (page.next_sequence > deliveredSequence) {
      deliveredSequence = page.next_sequence
    }
  }

  function streamBasePath(sourceId: string, source: StreamSource): string {
    return source === 'terminal-tab'
      ? `${API_BASE}/workspaces/tabs/${sourceId}/stream`
      : `${API_BASE}/workspaces/sessions/${sourceId}/stream`
  }

  async function fetchCapabilities(streamPath: string): Promise<StreamCapabilities> {
    const res = await fetch(`${streamPath}/capabilities`)
    if (!res.ok) throw new Error(`capabilities HTTP ${res.status}`)
    return (await res.json()) as StreamCapabilities
  }

  async function fetchEvents(streamPath: string, since: number): Promise<AgentStreamEventPage> {
    const res = await fetch(
      `${streamPath}/events?since_sequence=${since}&limit=200`,
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

  /** Long-poll fallback loop used when SSE is unavailable or fails. */
  async function longPollLoop(sourceId: string, streamPath: string) {
    while (!stopped && currentSessionId === sourceId) {
      try {
        const page = await waitEvents(streamPath, deliveredSequence)
        applyPage(page)
      } catch (err) {
        if (stopped || currentSessionId !== sourceId) return
        // Surface the failure and stop; the consumer falls back to raw.
        errorMessage.value = err instanceof Error ? err.message : 'stream wait failed'
        connectionState.value = 'failed'
        return
      }
    }
  }

  function startSse(sourceId: string, streamPath: string) {
    const url = `${streamPath}/live?since_sequence=${deliveredSequence}`
    eventSource = new EventSource(url)

    eventSource.addEventListener('hello', (ev: MessageEvent) => {
      try {
        const caps = JSON.parse(ev.data) as StreamCapabilities
        capabilities.value = caps
        if (!caps.structured) {
          errorMessage.value = 'structured observation unavailable for this session'
          connectionState.value = 'failed'
          stop()
        }
      } catch {
        // ignore malformed hello
      }
    })

    eventSource.addEventListener('agent-stream', (ev: MessageEvent) => {
      try {
        const evt = JSON.parse(ev.data) as AgentStreamEvent
        if (evt.stream_sequence > deliveredSequence) {
          events.value = [...events.value, evt]
          deliveredSequence = evt.stream_sequence
        }
      } catch {
        // ignore malformed event
      }
    })

    eventSource.addEventListener('error', (ev: MessageEvent) => {
      if (stopped || currentSessionId !== sourceId) return
      // EventSource auto-reconnects on transient errors; only fall back to
      // long-poll when the stream explicitly errors or the connection is
      // permanently closed.
      const data = (ev as MessageEvent).data
      if (data) {
        try {
          const parsed = JSON.parse(data) as { message?: string }
          errorMessage.value = parsed.message || 'structured stream error'
        } catch {
          errorMessage.value = 'structured stream error'
        }
        connectionState.value = 'failed'
        stop()
      }
    })
  }

  async function start(sourceId: string, source: StreamSource = 'managed-session') {
    stop()
    stopped = false
    currentSessionId = sourceId
    currentSource = source
    reset()
    connectionState.value = 'hydrating'
    const streamPath = streamBasePath(sourceId, source)

    try {
      const caps = await fetchCapabilities(streamPath)
      capabilities.value = caps
      if (!caps.structured) {
        errorMessage.value = 'structured observation unavailable for this session'
        connectionState.value = 'failed'
        return
      }

      // Hydrate: pull the full history before going live.
      let since = -1

      while (true) {
        const page = await fetchEvents(streamPath, since)
        applyPage(page)
        since = page.next_sequence
        if (!page.has_more) break
      }

      connectionState.value = 'live'

      // Prefer SSE; fall back to long-poll if EventSource is unavailable.
      if (typeof EventSource !== 'undefined') {
        startSse(sourceId, streamPath)
      } else {
        longPollAbort = new AbortController()
        void longPollLoop(sourceId, streamPath)
      }
    } catch (err) {
      if (stopped || currentSessionId !== sourceId || currentSource !== source) return
      errorMessage.value = err instanceof Error ? err.message : 'stream start failed'
      connectionState.value = 'failed'
    }
  }

  function stop() {
    stopped = true
    currentSessionId = null
    closeSse()
    abortLongPoll()
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
    stop,
  }
}
