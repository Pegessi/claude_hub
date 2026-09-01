/**
 * Connection lifecycle state machine for the structured observation plane.
 *
 * Owns the ``idle → hydrating → live | failed`` transitions.
 *
 * NOTE: This initial extraction mirrors the existing ``useAgentStream``
 * behaviour where any in-flight hydration can mutate ``connectionState``
 * regardless of whether it has been superseded. The generation parameter is
 * accepted for API compatibility but not yet enforced — that is the fix.
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

  /** Transition to ``live``. */
  success(_generationId: number): boolean {
    // BUG: generation not checked — a stale hydration can flip state to live.
    this.state = 'live'
    return true
  }

  /** Transition to ``failed``. */
  fail(_generationId: number, message: string): boolean {
    // BUG: generation not checked — a stale failure can overwrite live.
    this.state = 'failed'
    this.errorMessage = message
    return true
  }

  /** Tear down: state returns to ``idle``. */
  stop(): void {
    this.state = 'idle'
    this.errorMessage = null
  }

  getState(): StreamConnectionState {
    return this.state
  }

  getErrorMessage(): string | null {
    return this.errorMessage
  }

  getGeneration(): number {
    return this.generation
  }

  isCurrent(generationId: number): boolean {
    return generationId === this.generation
  }
}
