import type { AgentStreamEvent, StreamCapabilities } from '@/types'

export interface AgentStreamHistorySnapshot {
  capabilities: StreamCapabilities
  events: AgentStreamEvent[]
  cursor: number
}

export interface AgentStreamHistoryCache {
  get: (key: string) => AgentStreamHistorySnapshot | undefined
  set: (key: string, snapshot: AgentStreamHistorySnapshot) => void
  delete: (key: string) => void
  clear: () => void
  readonly size: number
}

/**
 * Small process-local LRU for completed Chat hydration snapshots.
 *
 * Event arrays are immutable-by-convention in ``useAgentStream``: live
 * batches replace the array instead of mutating it. Keeping the array by
 * reference therefore makes a tab switch O(1) without duplicating a long
 * conversation in browser memory.
 */
export function createAgentStreamHistoryCache(maxEntries: number): AgentStreamHistoryCache {
  const entries = new Map<string, AgentStreamHistorySnapshot>()
  const capacity = Math.max(0, Math.floor(maxEntries))

  return {
    get(key) {
      const snapshot = entries.get(key)
      if (!snapshot) return undefined
      // Map insertion order doubles as LRU order. A hit becomes MRU without
      // copying the potentially large event array.
      entries.delete(key)
      entries.set(key, snapshot)
      return snapshot
    },
    set(key, snapshot) {
      if (capacity === 0) return
      entries.delete(key)
      entries.set(key, snapshot)
      while (entries.size > capacity) {
        const oldest = entries.keys().next().value as string | undefined
        if (oldest === undefined) break
        entries.delete(oldest)
      }
    },
    delete(key) {
      entries.delete(key)
    },
    clear() {
      entries.clear()
    },
    get size() {
      return entries.size
    },
  }
}

export const agentStreamHistoryCache = createAgentStreamHistoryCache(3)
