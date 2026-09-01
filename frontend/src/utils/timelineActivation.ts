/**
 * Timeline activation gate.
 *
 * Mirrors the Paseo strategy-web ``isAuthoritativeHistoryReady`` activation
 * gate: the structured timeline is not revealed until the authoritative
 * history has been hydrated. While hidden, live updates and resize events
 * must not drive scroll behaviour — only the explicit activation transition
 * pins the tail, and only after that does normal follow-mode take over.
 *
 * Phases:
 *   - ``hidden``: history is not yet authoritative; timeline content is not
 *     revealed. Live updates / resize are ignored.
 *   - ``pinning``: history is ready; the consumer must synchronously pin the
 *     scroll container to the tail (before the next paint) and then call
 *     ``confirmTailPinned``.
 *   - ``revealed``: the timeline is visible and follows the tail while
 *     ``followOutput`` is true.
 */
export type TimelinePhase = 'hidden' | 'pinning' | 'revealed'

export interface TimelineActivationApi {
  readonly phase: TimelinePhase
  readonly followOutput: boolean
  /** Authoritative history has been hydrated; move to ``pinning``. */
  markHistoryReady(): void
  /** The tail has been synchronously pinned; move to ``revealed``. */
  confirmTailPinned(): void
  /** The user scrolled away from the tail; stop following. */
  detachFromTail(): void
  /** Explicitly re-enable tail following (jump-to-latest, tab switch). */
  rearmFollow(): void
  /** Reset for a new stream (tab switch / retry). */
  reset(): void
  /** Whether a live event update should scroll to the tail. */
  shouldFollowLiveUpdate(): boolean
  /** Whether a resize should re-stick to the tail. */
  shouldHandleResize(): boolean
}

export function createTimelineActivation(): TimelineActivationApi {
  let phase: TimelinePhase = 'hidden'
  let followOutput = true

  return {
    get phase() {
      return phase
    },
    get followOutput() {
      return followOutput
    },
    markHistoryReady() {
      phase = 'pinning'
      // Force follow for the initial reveal so the first painted frame is
      // already at the tail, regardless of any prior detach state.
      followOutput = true
    },
    confirmTailPinned() {
      if (phase === 'pinning') phase = 'revealed'
    },
    detachFromTail() {
      followOutput = false
    },
    rearmFollow() {
      followOutput = true
    },
    reset() {
      phase = 'hidden'
      followOutput = true
    },
    shouldFollowLiveUpdate() {
      // Live updates only drive scrolling once the timeline is revealed and
      // the user has not detached. They never rearm follow on their own.
      return phase === 'revealed' && followOutput
    },
    shouldHandleResize() {
      return phase === 'revealed' && followOutput
    },
  }
}
