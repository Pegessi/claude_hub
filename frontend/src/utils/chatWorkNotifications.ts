import type { ChatWork } from '@/types/chatWork'

const STORAGE_KEY = 'claude_hub_seen_work_results_v1'
const MAX_SEEN = 200
const meaningful = new Set(['anomaly', 'completed', 'decision', 'failed'])

/** Keep only opaque result identities; no report text is persisted in the browser. */
export function createWorkResultTracker(storage: Pick<Storage, 'getItem' | 'setItem'> | null) {
  let ids: string[] = []
  try {
    const saved: unknown = JSON.parse(storage?.getItem(STORAGE_KEY) ?? '[]')
    if (Array.isArray(saved)) ids = saved.filter((value): value is string => typeof value === 'string').slice(-MAX_SEEN)
  } catch { /* Private browsing or invalid old preferences must not block Chat. */ }
  const seen = new Set(ids)

  return (work: ChatWork[]): ChatWork[] => {
    // Other cached Chat panes may have recorded results since this tracker was
    // created. Merge their IDs before persisting so switching Chats never erases
    // each other's dedupe history.
    try {
      const saved: unknown = JSON.parse(storage?.getItem(STORAGE_KEY) ?? '[]')
      if (Array.isArray(saved)) saved.slice(-MAX_SEEN).forEach(value => { if (typeof value === 'string') seen.add(value) })
    } catch { /* The in-memory set still dedupes this mounted pane. */ }
    const updates = work.filter(item => {
      const result = item.latest_result
      if (!result || !meaningful.has(result.kind)) return false
      const key = JSON.stringify([item.id, result.report_id ?? result.task_id, result.kind, result.created_at])
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    if (updates.length) {
      const retained = [...seen].slice(-MAX_SEEN)
      seen.clear()
      retained.forEach(key => seen.add(key))
      try { storage?.setItem(STORAGE_KEY, JSON.stringify(retained)) } catch { /* Keep in-memory dedupe. */ }
    }
    return updates
  }
}
