import type { AgentStreamEvent, AgentStreamEventType } from '@/types'

/**
 * Flush interval for the event micro-batch scheduler.
 *
 * rAF fires ~60fps (~16.7ms) but can be throttled by the browser when the tab
 * is backgrounded. A 48ms setTimeout fallback guarantees a bounded maximum
 * latency even when rAF is suppressed. The scheduler uses whichever fires
 * first.
 */
export const FLUSH_INTERVAL_MS = 48

/**
 * Event types that act as semantic barriers and must flush the pending batch
 * immediately rather than waiting for the rAF / 48ms window.
 *
 * These are events that mark the completion of a logical unit (turn, tool
 * call) or require immediate user visibility (errors, approvals, status
 * changes). High-frequency delta events (`text_delta`, `thinking_delta`) and
 * start events are batched to reduce reactive re-renders.
 */
export const IMMEDIATE_FLUSH_TYPES: ReadonlySet<AgentStreamEventType> = new Set([
  'turn_completed',
  'tool_call_completed',
  'approval_required',
  'approval_resolved',
  'error',
  'status',
])

/**
 * Event micro-batch scheduler.
 *
 * Incoming committed events are accumulated and flushed to a commit callback
 * on a rAF / 48ms timer (whichever fires first). This caps the number of
 * reactive timeline re-renders during high-throughput streams (long Thinking
 * bursts) without increasing end-to-end latency beyond one frame.
 *
 * Semantic-barrier events (turn/tool completion, errors, approvals, status)
 * bypass the batching window and flush immediately so user-visible state
 * changes are never delayed.
 */
export class AgentStreamBatcher {
  private pending: AgentStreamEvent[] = []
  private rafId: number | null = null
  private timerId: ReturnType<typeof setTimeout> | null = null
  private scheduled = false

  constructor(
    private readonly commit: (events: AgentStreamEvent[]) => void,
  ) {}

  /**
   * Enqueue events for batched flush. If any event is a semantic-barrier
   * type, the whole batch is flushed immediately so the barrier and all
   * preceding pending deltas are committed in order.
   */
  enqueue(events: AgentStreamEvent[]): void {
    if (events.length === 0) return
    this.pending.push(...events)
    if (events.some((e) => IMMEDIATE_FLUSH_TYPES.has(e.type))) {
      this.flushNow()
    } else {
      this.scheduleFlush()
    }
  }

  /** Flush any pending events immediately and cancel scheduled timers. */
  flushNow(): void {
    this.cancelScheduled()
    this.commitPending()
  }

  /** Cancel any scheduled flush without committing pending events. */
  cancel(): void {
    this.cancelScheduled()
  }

  /**
   * Flush pending events (so they are not lost) then cancel timers.
   * Used on reset/stop/unmount.
   */
  flushAndCancel(): void {
    this.commitPending()
    this.cancelScheduled()
  }

  /** Number of events currently buffered. */
  get pendingCount(): number {
    return this.pending.length
  }

  /** Whether a flush is currently scheduled. */
  get isScheduled(): boolean {
    return this.scheduled
  }

  private scheduleFlush(): void {
    if (this.scheduled) return
    this.scheduled = true

    const doFlush = () => {
      this.cancelScheduled()
      this.commitPending()
    }

    if (typeof requestAnimationFrame !== 'undefined') {
      this.rafId = requestAnimationFrame(doFlush)
    }
    // Fallback timer guarantees a flush even if rAF is throttled (e.g.
    // background tab).
    this.timerId = setTimeout(doFlush, FLUSH_INTERVAL_MS)
  }

  private cancelScheduled(): void {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId)
      this.rafId = null
    }
    if (this.timerId !== null) {
      clearTimeout(this.timerId)
      this.timerId = null
    }
    this.scheduled = false
  }

  private commitPending(): void {
    if (this.pending.length === 0) return
    const batch = this.pending
    this.pending = []
    this.commit(batch)
  }
}
