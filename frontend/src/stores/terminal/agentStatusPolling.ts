import type { Ref } from 'vue'
import type { TerminalAgentStatus, TerminalTab } from '@/types'
import { API_BASE, STATUS_POLL_INTERVAL_MS, statusesEqual } from './helpers'

interface AgentStatusPollingRuntime {
  tabs: Ref<TerminalTab[]>
  statuses: Ref<TerminalAgentStatus[]>
  isLoading: Ref<boolean>
  fetchTabs: () => Promise<void>
}

export function createAgentStatusPolling(runtime: AgentStatusPollingRuntime) {
  let timer: number | null = null
  let consumers = 0

  /**
   * A turn that just finished is what makes a row "needs you" while unread.
   * The unread flag lives on the tab list (not the status payload), so refresh
   * it once per completion — not on every poll, which would rescan every
   * session's history for tabs that did not change.
   */
  function completedTurns(previous: readonly TerminalAgentStatus[], next: readonly TerminalAgentStatus[]) {
    if (previous.length === 0) return false
    const priorStatus = new Map(previous.map(status => [status.tab_id, status.status]))
    return next.some(
      status => priorStatus.get(status.tab_id) === 'working' && status.status !== 'working',
    )
  }

  async function fetchAgentStatuses() {
    if (runtime.isLoading.value) return
    runtime.isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/status`)
      if (!response.ok) throw new Error('Failed to fetch agent statuses')
      const statuses: TerminalAgentStatus[] = await response.json()
      const finished = completedTurns(runtime.statuses.value, statuses)
      if (!statusesEqual(runtime.statuses.value, statuses)) {
        runtime.statuses.value = statuses
      }
      const knownTabIds = new Set(runtime.tabs.value.map(tab => tab.id))
      if (finished || statuses.some(status => !knownTabIds.has(status.tab_id))) {
        void runtime.fetchTabs()
      }
    } catch (error) {
      console.error('Error fetching agent statuses:', error)
    } finally {
      runtime.isLoading.value = false
    }
  }

  function startAgentStatusPolling() {
    consumers += 1
    if (timer !== null) return
    void fetchAgentStatuses()
    timer = window.setInterval(fetchAgentStatuses, STATUS_POLL_INTERVAL_MS)
  }

  function stopAgentStatusPolling() {
    if (consumers > 0) consumers -= 1
    if (consumers > 0 || timer === null) return
    window.clearInterval(timer)
    timer = null
  }

  return { fetchAgentStatuses, startAgentStatusPolling, stopAgentStatusPolling }
}
