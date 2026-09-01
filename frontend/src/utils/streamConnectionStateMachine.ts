/**
 * Connection lifecycle state machine for the structured observation plane.
 *
 * Owns the ``idle → hydrating → live | failed`` transitions and guards them
 * by generation so a stale ``start`` (one superseded by a newer one) can
 * never mutate the visible state.
 *
 * Generation contract
 * -------------------
 * Each ``start()`` increments an internal generation counter and returns the
 * new generation id. ``success`` / ``fail`` are no-ops unless the caller's
 * generation id matches the current one. This makes state transitions
 * generation-owned: a superseded hydration that resolves late cannot flip a
 * newer generation's ``hydrating`` to ``live`` or ``failed``.
 *
 * ``stop()`` advances the generation and moves the state to ``idle``. Any
 * in-flight work from before ``stop`` is therefore invalidated (its
 * generation id no longer matches), and the next ``start()`` produces a fresh
 * generation id.
 */
export type StreamConnectionState = 'idle' | 'hydrating' | 'live' | 'failed'

export class StreamConnectionStateMachine {
  private state: StreamConnectionState = 'idle'
  private generation = 0
  private errorMessage: string | null = null

  /** Begin a new hydration generation. State becomes ``hydrating``. */
  start(): number {
    this.generation += 1
    this.state = 'hydrating'
    this.errorMessage = null
    return this.generation
  }

  /** Transition to ``live``. No-op if ``generationId`` is not current. */
  success(generationId: number): boolean {
    if (generationId !== this.generation) return false
    this.state = 'live'
    return true
  }

  /** Transition to ``failed``. No-op if ``generationId`` is not current. */
  fail(generationId: number, message: string): boolean {
    if (generationId !== this.generation) return false
    this.state = 'failed'
    this.errorMessage = message
    return true
  }

  /** Tear down: advance the generation and return to ``idle``. */
  stop(): void {
    this.generation += 1
    this.state = 'idle'
    this.errorMessage = null
  }

  getState(): StreamConnectionState {
    return this.state
  }

  getErrorMessage(): string | null {
    return this.errorMessage
  }

  /** Current generation id. Exposed so callers can compare without mutating. */
  getGeneration(): number {
    return this.generation
  }

  /** Whether ``generationId`` is the active generation. */
  isCurrent(generationId: number): boolean {
    return generationId === this.generation
  }
}
