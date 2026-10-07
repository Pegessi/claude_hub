import { ref, watch, type Ref } from 'vue'
import type { ChatWork, ChatWorkUpdate } from '@/types/chatWork'

const POLL_MS = 15_000
const IDLE_POLL_MS = 60_000

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (Array.isArray(body.detail)) {
      return body.detail.map(item => item.msg ?? 'Invalid request').join('; ')
    }
  } catch { /* The proxy may return a non-JSON response. */ }
  return `HTTP ${response.status}`
}

/** One reader for the visible Chat. Never appends checks to the transcript. */
export function useChatWork(tabId: Ref<string>) {
  const work = ref<ChatWork[]>([])
  const error = ref<string | null>(null)
  const refreshing = ref(false)
  const busyId = ref<string | null>(null)
  const stale = ref(false)
  let active = false
  let disposed = false
  let scope = 0
  let readVersion = 0
  let readController: AbortController | null = null
  let mutationController: AbortController | null = null
  let poll: ReturnType<typeof setTimeout> | null = null

  function clearPoll() {
    if (poll !== null) clearTimeout(poll)
    poll = null
  }

  function schedule() {
    clearPoll()
    if (!active || disposed || busyId.value || refreshing.value) return
    const pending = work.value.some(item => ['running', 'waiting', 'review'].includes(item.status))
    poll = setTimeout(() => { void refresh() }, pending ? POLL_MS : IDLE_POLL_MS)
  }

  function invalidateRead() {
    readVersion++
    readController?.abort()
    readController = null
    refreshing.value = false
    clearPoll()
  }

  async function refresh(): Promise<void> {
    if (!active || disposed || busyId.value || refreshing.value) return
    clearPoll()
    const version = ++readVersion
    const tab = tabId.value
    const controller = new AbortController()
    readController = controller
    const timeout = setTimeout(() => controller.abort(), 15_000)
    refreshing.value = true
    try {
      const response = await fetch(`/api/tabs/${encodeURIComponent(tab)}/work`, {
        credentials: 'same-origin', signal: controller.signal,
      })
      if (!response.ok) throw new Error(await errorDetail(response))
      const next = await response.json() as ChatWork[]
      if (version !== readVersion || tab !== tabId.value) return
      // Do not render or offer controls for work belonging to another Chat.
      work.value = next.filter(item => item.source_tab_id === tab)
      stale.value = false
      error.value = null
    } catch (cause) {
      if (version !== readVersion) return
      stale.value = true
      error.value = cause instanceof Error ? cause.message : 'Could not refresh background work.'
    } finally {
      clearTimeout(timeout)
      if (version === readVersion) {
        refreshing.value = false
        readController = null
        schedule()
      }
    }
  }

  async function update(id: string, change: ChatWorkUpdate): Promise<boolean> {
    if (!change || typeof change !== 'object' || Array.isArray(change) ||
        Object.keys(change).length !== 1 || change.action !== 'stop') return false
    const item = work.value.find(candidate => candidate.id === id)
    if (!active || disposed || busyId.value || stale.value || !item ||
        item.status === 'stopped' || item.status === 'completed') return false
    invalidateRead()
    const context = scope
    const tab = tabId.value
    const controller = new AbortController()
    mutationController = controller
    const timeout = setTimeout(() => controller.abort(), 30_000)
    busyId.value = id
    error.value = null
    try {
      const response = await fetch(`/api/tabs/${encodeURIComponent(tab)}/work/${encodeURIComponent(id)}`, {
        method: 'PATCH', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(change),
      })
      if (!response.ok) throw new Error(await errorDetail(response))
      const next = await response.json() as ChatWork
      if (context !== scope) return false
      if (next.source_tab_id !== tab || next.id !== id) throw new Error('Unexpected background work response. Refresh to reconcile.')
      work.value = work.value.map(item => item.id === id ? next : item)
      stale.value = false
      return true
    } catch (cause) {
      if (context !== scope) return false
      const message = cause instanceof Error ? cause.message : 'Background work action failed.'
      // The server may have applied an action even when its response was lost.
      // Reconcile, and keep controls disabled if current state is still unknown.
      stale.value = true
      busyId.value = null
      await refresh()
      if (context === scope) error.value = message
      return false
    } finally {
      clearTimeout(timeout)
      if (context === scope) {
        busyId.value = null
        mutationController = null
        schedule()
      }
    }
  }

  async function start() {
    if (active || disposed) return
    active = true
    await refresh()
  }

  function stop() {
    active = false
    invalidateRead()
  }

  const unwatch = watch(tabId, () => {
    scope++
    invalidateRead()
    mutationController?.abort()
    mutationController = null
    busyId.value = null
    work.value = []
    stale.value = false
    error.value = null
    if (active) void refresh()
  }, { flush: 'sync' })

  function dispose() {
    disposed = true
    scope++
    stop()
    mutationController?.abort()
    unwatch()
  }

  return { work, error, refreshing, busyId, stale, refresh, update, start, stop, dispose }
}
