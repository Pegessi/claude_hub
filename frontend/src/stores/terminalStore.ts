import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type {
  TerminalAgentStatus,
  TerminalTab,
  TerminalTabCreate,
  TerminalTabUpdate,
  LayoutType,
  Pane,
} from '@/types'

const API_BASE = '/api'
const STORAGE_KEY_LAYOUT = 'claude_hub_layout_type'
const STATUS_POLL_INTERVAL_MS = 5000

function generatePaneId(): string {
  return 'pane-' + Math.random().toString(36).substr(2, 9)
}

const LAYOUT_CONFIGS: Record<LayoutType, { rows: number; cols: number }> = {
  '1x1': { rows: 1, cols: 1 },
  '2x1': { rows: 1, cols: 2 },
  '1x2': { rows: 2, cols: 1 },
  '3x1': { rows: 1, cols: 3 },
  '1x3': { rows: 3, cols: 1 },
  '2x2': { rows: 2, cols: 2 },
  '3x3': { rows: 3, cols: 3 },
}

export const useTerminalStore = defineStore('terminal', () => {
  const tabs = ref<TerminalTab[]>([])
  const agentStatuses = ref<TerminalAgentStatus[]>([])
  const activeTabId = ref<string | null>(null)
  const isLoading = ref(false)
  const isStatusLoading = ref(false)
  const error = ref<string | null>(null)
  let statusPollTimer: number | null = null
  let statusPollConsumers = 0

  // Layout and panes
  const layoutType = ref<LayoutType>(
    (localStorage.getItem(STORAGE_KEY_LAYOUT) as LayoutType) || '1x1'
  )
  const panes = ref<Pane[]>([])
  const activePaneId = ref<string | null>(null)

  const activeTab = computed(() => tabs.value.find(tab => tab.id === activeTabId.value) || null)
  const manualTabs = computed(() => tabs.value.filter(tab => !tab.workspace_id))
  const managedTabs = computed(() => tabs.value.filter(tab => Boolean(tab.workspace_id)))

  function initializePanes() {
    const config = LAYOUT_CONFIGS[layoutType.value]
    const paneCount = config.rows * config.cols
    const newPanes: Pane[] = []
    const assignedTabIds = new Set<string>()

    let foundActive = false
    for (let i = 0; i < paneCount; i++) {
      const existingPane = panes.value[i]
      const id = existingPane?.id || generatePaneId()
      let tabId = existingPane?.tabId || null
      if (tabId && assignedTabIds.has(tabId)) {
        tabId = null
      }
      if (tabId) {
        assignedTabIds.add(tabId)
      }
      const isActive = existingPane?.isActive && !foundActive ? true : i === 0
      if (isActive) foundActive = true

      newPanes.push({
        id,
        tabId,
        isActive,
      })
    }

    panes.value = newPanes

    const activePane = newPanes.find(p => p.isActive)
    if (activePane) {
      activePaneId.value = activePane.id
      if (activePane.tabId) {
        activeTabId.value = activePane.tabId
      }
    }
  }

  function setLayout(type: LayoutType) {
    layoutType.value = type
    localStorage.setItem(STORAGE_KEY_LAYOUT, type)
    initializePanes()
  }

  function setActivePane(paneId: string) {
    // Create a new array to ensure reactivity
    panes.value = panes.value.map(pane => ({
      ...pane,
      isActive: pane.id === paneId
    }))
    activePaneId.value = paneId

    // If the pane has a tab, make it the active tab too
    const pane = panes.value.find(p => p.id === paneId)
    if (pane?.tabId) {
      activeTabId.value = pane.tabId
    }
  }

  function assignTabToPane(tabId: string, paneId?: string) {
    const targetPaneId = paneId || activePaneId.value
    if (!targetPaneId) return

    let assigned = false
    panes.value = panes.value.map(pane => {
      if (pane.id === targetPaneId) {
        assigned = true
        return { ...pane, tabId }
      }
      if (pane.tabId === tabId) {
        return { ...pane, tabId: null }
      }
      return pane
    })

    if (assigned) {
      activeTabId.value = tabId
    }
  }

  function getPaneCountForTab(tabId: string): number {
    return panes.value.filter(p => p.tabId === tabId).length
  }

  // Get the active pane (for mobile controls to know which terminal to send keys to)
  const activePane = computed(() => panes.value.find(p => p.id === activePaneId.value) || null)

  async function fetchTabs() {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/tabs`)
      if (!response.ok) throw new Error('Failed to fetch tabs')
      tabs.value = await response.json()
      if (manualTabs.value.length && !activeTabId.value) {
        activeTabId.value = manualTabs.value[0].id
      }
      // Initialize panes after fetching tabs
      if (panes.value.length === 0) {
        initializePanes()
      }
      // Auto-assign first tab to first pane if available
      if (manualTabs.value.length > 0 && panes.value.length > 0) {
        const firstPane = panes.value[0]
        if (!firstPane.tabId) {
          firstPane.tabId = manualTabs.value[0].id
        }
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Unknown error'
    } finally {
      isLoading.value = false
    }
  }

  async function fetchAgentStatuses() {
    if (isStatusLoading.value) return
    isStatusLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/status`)
      if (!response.ok) throw new Error('Failed to fetch agent statuses')
      const statuses: TerminalAgentStatus[] = await response.json()
      agentStatuses.value = statuses
      const knownTabIds = new Set(tabs.value.map(tab => tab.id))
      if (statuses.some(status => !knownTabIds.has(status.tab_id))) {
        void fetchTabs()
      }
    } catch (e) {
      console.error('Error fetching agent statuses:', e)
    } finally {
      isStatusLoading.value = false
    }
  }

  function startAgentStatusPolling() {
    statusPollConsumers += 1
    if (statusPollTimer !== null) return
    fetchAgentStatuses()
    statusPollTimer = window.setInterval(fetchAgentStatuses, STATUS_POLL_INTERVAL_MS)
  }

  function stopAgentStatusPolling() {
    if (statusPollConsumers > 0) {
      statusPollConsumers -= 1
    }
    if (statusPollConsumers > 0) return
    if (statusPollTimer === null) return
    window.clearInterval(statusPollTimer)
    statusPollTimer = null
  }

  async function createTab(data: TerminalTabCreate) {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/tabs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!response.ok) throw new Error('Failed to create tab')
      const newTab = await response.json()
      tabs.value.push(newTab)
      activeTabId.value = newTab.id
      // Auto-assign new tab to active pane
      if (activePaneId.value) {
        assignTabToPane(newTab.id, activePaneId.value)
      }
      return newTab
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Unknown error'
    } finally {
      isLoading.value = false
    }
  }

  async function duplicateTab(tabId: string) {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}/duplicate`, {
        method: 'POST',
      })
      if (!response.ok) throw new Error('Failed to duplicate tab')
      const newTab = await response.json()
      tabs.value.push(newTab)
      activeTabId.value = newTab.id
      if (activePaneId.value) {
        assignTabToPane(newTab.id, activePaneId.value)
      }
      return newTab
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Unknown error'
    } finally {
      isLoading.value = false
    }
  }

  async function updateTab(tabId: string, data: TerminalTabUpdate) {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!response.ok) throw new Error('Failed to update tab')
      const updatedTab = await response.json()
      const index = tabs.value.findIndex(tab => tab.id === tabId)
      if (index !== -1) {
        tabs.value[index] = updatedTab
      }
      return updatedTab
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Unknown error'
    } finally {
      isLoading.value = false
    }
  }

  async function deleteTab(tabId: string) {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error('Failed to delete tab')
      tabs.value = tabs.value.filter(tab => tab.id !== tabId)
      // Remove tab from all panes
      panes.value.forEach(pane => {
        if (pane.tabId === tabId) {
          pane.tabId = null
        }
      })
      if (activeTabId.value === tabId) {
        activeTabId.value = manualTabs.value.length ? manualTabs.value[0].id : null
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Unknown error'
    } finally {
      isLoading.value = false
    }
  }

  function setActiveTab(tabId: string) {
    if (tabs.value.some(tab => tab.id === tabId)) {
      activeTabId.value = tabId
      // Also assign to active pane
      assignTabToPane(tabId)
    }
  }

  async function saveTabOrder() {
    try {
      const tabIds = tabs.value.map(t => t.id)
      console.log('Saving tab order:', tabIds)
      const response = await fetch(`${API_BASE}/tabs/order`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tab_ids: tabIds }),
      })
      if (!response.ok) {
        const errText = await response.text()
        console.error('Failed to save tab order:', response.status, errText)
        throw new Error(`Failed to save tab order: ${response.status}`)
      }
      const result = await response.json()
      console.log('Tab order saved:', result)
    } catch (e) {
      console.error('Error saving tab order:', e)
      // Don't set global error for this - it's non-critical
      // error.value = e instanceof Error ? e.message : 'Unknown error'
    }
  }

  function reorderTabs(fromIndex: number, toIndex: number) {
    const visibleTabs = manualTabs.value
    if (fromIndex < 0 || fromIndex >= visibleTabs.length) return
    if (toIndex < 0 || toIndex >= visibleTabs.length) return
    if (fromIndex === toIndex) return

    const reorderedManualTabs = [...visibleTabs]
    const [removed] = reorderedManualTabs.splice(fromIndex, 1)
    reorderedManualTabs.splice(toIndex, 0, removed)
    tabs.value = [...reorderedManualTabs, ...managedTabs.value]
    // Save the new order to backend
    saveTabOrder()
  }

  return {
    tabs,
    manualTabs,
    managedTabs,
    agentStatuses,
    activeTabId,
    activeTab,
    isLoading,
    isStatusLoading,
    error,
    layoutType,
    panes,
    activePaneId,
    activePane,
    fetchTabs,
    fetchAgentStatuses,
    startAgentStatusPolling,
    stopAgentStatusPolling,
    createTab,
    duplicateTab,
    updateTab,
    deleteTab,
    setActiveTab,
    reorderTabs,
    setLayout,
    setActivePane,
    assignTabToPane,
    getPaneCountForTab,
    initializePanes,
    saveTabOrder,
  }
})
