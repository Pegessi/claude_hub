/** Minimal reducer projection needed to decide whether the next-turn mode may change. */
export interface ChatTurnLifecycle {
  completed: boolean
  errors: readonly unknown[]
}

/** Minimal stream event projection used for one-shot runtime-status refreshes. */
export interface ChatStreamLifecycleEvent {
  type: string
  stream_sequence: number
  session_id?: string | null
  tab_id?: string | null
}

const STATUS_REFRESH_BOUNDARY_TYPES = new Set([
  'turn_started',
  'turn_completed',
  'error',
])

/**
 * Keep next-turn mode changes locked from the optimistic submit boundary until
 * the latest authoritative turn terminalizes.  An ``error`` is terminal even
 * when a provider does not emit a following ``turn_completed`` event.
 */
export function isChatModeLocked(
  hasPendingSubmission: boolean,
  turns: readonly ChatTurnLifecycle[],
): boolean {
  if (hasPendingSubmission) return true
  const latest = turns[turns.length - 1]
  if (!latest) return false
  return !latest.completed && latest.errors.length === 0
}

function eventIdentity(event: ChatStreamLifecycleEvent): string {
  return [
    event.session_id ?? '',
    event.tab_id ?? '',
    event.stream_sequence,
    event.type,
  ].join('\u0000')
}

/**
 * Return true only when the newly committed suffix crosses a native lifecycle
 * boundary. Streaming deltas intentionally return false so the status endpoint
 * is refreshed once per edge, not once per render batch.
 */
export function hasChatStatusRefreshBoundary(
  previous: readonly ChatStreamLifecycleEvent[],
  latest: readonly ChatStreamLifecycleEvent[],
): boolean {
  if (latest.length === 0) return false

  const isAppend = previous.length <= latest.length && (
    previous.length === 0 ||
    eventIdentity(previous[previous.length - 1]) === eventIdentity(latest[previous.length - 1])
  )
  const startIndex = isAppend ? previous.length : 0

  for (let index = startIndex; index < latest.length; index += 1) {
    if (STATUS_REFRESH_BOUNDARY_TYPES.has(latest[index].type)) return true
  }
  return false
}
