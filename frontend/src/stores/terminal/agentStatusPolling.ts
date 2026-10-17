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

  async function fetchAgentStatuses() {
    if (runtime.isLoading.value) return
    runtime.isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/status`)
      if (!response.ok) throw new Error('Failed to fetch agent statuses')
      const statuses: TerminalAgentStatus[] = await response.json()
      if (!statusesEqual(runtime.statuses.value, statuses)) {
        runtime.statuses.value = statuses
      }
      const knownTabIds = new Set(runtime.tabs.value.map(tab => tab.id))
      if (statuses.some(status => !knownTabIds.has(status.tab_id))) {
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
