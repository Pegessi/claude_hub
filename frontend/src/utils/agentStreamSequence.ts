export interface SequencedEvent {
  stream_sequence: number
}

export interface ContiguousEventBuffer<T extends SequencedEvent> {
  readonly cursor: number
  readonly pendingCount: number
  push: (incoming: readonly T[]) => T[]
  reset: (cursor?: number) => void
}

/** Merge long-poll and SSE without ever exposing an out-of-order gap. */
export function createContiguousEventBuffer<T extends SequencedEvent>(
  initialCursor = -1,
): ContiguousEventBuffer<T> {
  let cursor = initialCursor
  const pending = new Map<number, T>()

  return {
    get cursor() {
      return cursor
    },
    get pendingCount() {
      return pending.size
    },
    push(incoming) {
      for (const event of incoming) {
        if (!Number.isInteger(event.stream_sequence) || event.stream_sequence <= cursor) continue
        if (!pending.has(event.stream_sequence)) pending.set(event.stream_sequence, event)
      }

      const committed: T[] = []
      while (pending.has(cursor + 1)) {
        const next = pending.get(cursor + 1)!
        pending.delete(cursor + 1)
        cursor += 1
        committed.push(next)
      }
      return committed
    },
    reset(nextCursor = -1) {
      cursor = nextCursor
      pending.clear()
    },
  }
}
