import type { AgentStreamEvent, AgentType } from '@/types'

/**
 * Manual Stop local-reset helpers.
 *
 * A user Stop terminalizes the active Hub turn server-side, but its durable
 * ``turn_completed(cancelled)`` edge is delivered only over the long-poll/SSE
 * observation surface — which may itself be reconnecting or FAILED while the
 * turn is wedged. To unlock the composer and mark the round "Stopped"
 * immediately, StructuredPane appends one synthetic terminal edge for the
 * stopped turn and lets the real edge reconcile on the next poll.
 *
 * These are pure functions so the reset contract (including the idempotent
 * ``cancelled:false`` re-Stop that is the reported wedge) is unit-testable
 * without mounting the whole pane.
 */

/**
 * Synthetic stream_sequence for the local Stop edge. It never exists
 * server-side: a negative value cannot collide with a real (zero-based)
 * sequence, and appending it at the end forces the incremental timeline
 * reducer to rebuild (its append-prefix check fails) rather than treat it as a
 * genuine durable suffix.
 */
export const OPTIMISTIC_CANCEL_SEQUENCE = -1

/**
 * Build the synthetic ``turn_completed(cancelled)`` edge for a locally stopped
 * turn. ``tabId`` scopes it to this Chat tab; ``agentType`` is inherited from
 * the last known event (defaulting to ``traex``) only to satisfy the event
 * shape — it is never rendered from this edge.
 */
export function buildOptimisticCancelledEvent(
  turnId: string,
  options: { tabId: string; agentType?: AgentType; now: string },
): AgentStreamEvent {
  return {
    stream_sequence: OPTIMISTIC_CANCEL_SEQUENCE,
    session_id: '',
    tab_id: options.tabId,
    agent_type: options.agentType ?? 'traex',
    type: 'turn_completed',
    run_epoch: null,
    turn_id: turnId,
    message_id: turnId,
    call_id: null,
    payload: { status: 'cancelled' },
    created_at: options.now,
    redacted: false,
  }
}

/**
 * Append the optimistic cancelled edge for ``turnId`` to ``base`` unless one is
 * already present. Returns a new array (``base`` is untouched).
 */
export function withOptimisticCancelled(
  base: AgentStreamEvent[],
  turnId: string,
  options: { tabId: string; agentType?: AgentType; now: string },
): AgentStreamEvent[] {
  if (base.some(event => event.stream_sequence === OPTIMISTIC_CANCEL_SEQUENCE && event.turn_id === turnId)) {
    return base
  }
  const agentType = (base[base.length - 1]?.agent_type ?? options.agentType ?? 'traex') as AgentType
  return [...base, buildOptimisticCancelledEvent(turnId, { tabId: options.tabId, agentType, now: options.now })]
}

/**
 * True once the backend's own terminal edge for the optimistically-stopped turn
 * has arrived (any status — a natural completion winning the Stop race is just
 * as terminal). At that point the synthetic edge is dropped.
 */
export function hasAuthoritativeTerminalFor(
  events: AgentStreamEvent[],
  turnId: string,
): boolean {
  return events.some(
    event => event.type === 'turn_completed' && event.turn_id === turnId,
  )
}
