import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type {
  TerminalAgentStatus,
  TerminalTab,
  TerminalTabCreate,
  TerminalTabUpdate,
  SwitchEnvRequest,
  LayoutType,
  Pane,
  StoreNotification,
  NotificationType,
} from '@/types'
import { groupChatsByCwd, moveTabById, parsePinnedChatIds } from '@/utils/chatGroups'

const API_BASE = '/api'
const STORAGE_KEY_LAYOUT = 'claude_hub_layout_type'
const STORAGE_KEY_SIDEBAR = 'claude_hub_sidebar_collapsed'
// Browser + origin scoped, like the sidebar/layout preferences. Store IDs only;
// archived pins stay dormant and reappear if that session is restored.
const STORAGE_KEY_PINNED_CHATS = 'claude_hub_pinned_chat_ids'
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

// Shallow equality check for two status arrays. Returns true when both arrays have
// the same length and each corresponding entries have identical serialized JSON.
// Used to avoid triggering a reactivity cascade when a poll returns unchanged data.
function statusesEqual(a: TerminalAgentStatus[], b: TerminalAgentStatus[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const x = a[i]
    const y = b[i]
    if (
      x.tab_id !== y.tab_id ||
      x.tab_name !== y.tab_name ||
      x.agent_type !== y.agent_type ||
      x.status !== y.status ||
      x.status_text !== y.status_text ||
      x.detail !== y.detail ||
      x.tmux_session !== y.tmux_session ||
      x.last_changed_at !== y.last_changed_at ||
      x.sampled_at !== y.sampled_at
    ) {
      return false
    }
  }
  return true
}

export const useTerminalStore = defineStore('terminal', () => {
  const tabs = ref<TerminalTab[]>([])
  const agentStatuses = ref<TerminalAgentStatus[]>([])
  const activeTabId = ref<string | null>(null)
  const isLoading = ref(false)
  const isStatusLoading = ref(false)
  // ---- Notification / toast queue (F5: replaces single error string) ----
  const notifications = ref<StoreNotification[]>([])
  let _notifIdSeq = 0

  function pushNotification(partial: Omit<StoreNotification, 'id'>) {
    const id = `n-${Date.now().toString(36)}-${(_notifIdSeq++).toString(36)}`
    const n: StoreNotification = { id, ...partial }
    notifications.value.push(n)
    if (n.autoDismissMs && n.autoDismissMs > 0) {
      window.setTimeout(() => dismissNotification(id), n.autoDismissMs)
    }
  }

  function dismissNotification(id: string) {
    const i = notifications.value.findIndex(n => n.id === id)
    if (i >= 0) notifications.value.splice(i, 1)
  }

  function notifyError(message: string) {
    pushNotification({ type: 'error', message, autoDismissMs: 8000 })
  }

  // Backward compat: the most recent error-like message. Callers should prefer
  // notifications + the toast UI rendered by TabBar.
  const error = computed<string | null>(() =>
    notifications.value.find(n => n.type === ('error' as NotificationType))?.message ?? null
  )
  let statusPollTimer: number | null = null
  let statusPollConsumers = 0

  // Layout and panes
  const layoutType = ref<LayoutType>(
    (localStorage.getItem(STORAGE_KEY_LAYOUT) as LayoutType) || '1x1'
  )
  const panes = ref<Pane[]>([])
  const activePaneId = ref<string | null>(null)

  // Left chat sidebar collapse state, persisted across reloads.
  const sidebarCollapsed = ref<boolean>(
    localStorage.getItem(STORAGE_KEY_SIDEBAR) === '1'
  )
  const pinnedChatIds = ref<Set<string>>((() => {
    try {
      return parsePinnedChatIds(localStorage.getItem(STORAGE_KEY_PINNED_CHATS))
    } catch {
      return new Set<string>()
    }
  })())

  function setChatPinned(tabId: string, pinned: boolean) {
    if (!chatTabs.value.some(tab => tab.id === tabId)) return
    const next = new Set(pinnedChatIds.value)
    if (pinned) next.add(tabId)
    else next.delete(tabId)
    pinnedChatIds.value = next
    try {
      localStorage.setItem(STORAGE_KEY_PINNED_CHATS, JSON.stringify([...next]))
    } catch {
      pushNotification({ type: 'warning', message: 'Pin preference could not be saved in this browser.', autoDismissMs: 6000 })
    }
  }
  // Mobile slide-out session drawer visibility. Not persisted — it defaults to
  // closed on load. Set directly from components (TabBar opens, App closes).
  const mobileDrawerOpen = ref(false)
  // Soft-deleted tabs. Kept separately from `tabs` (which only holds active
  // tabs) so the archive browser can list them without polluting the grid.
  const archivedTabs = ref<TerminalTab[]>([])
  const isLoadingArchived = ref(false)

  const activeTab = computed(() => tabs.value.find(tab => tab.id === activeTabId.value) || null)
  const manualTabs = computed(() => tabs.value.filter(tab => !tab.workspace_id))
  const managedTabs = computed(() => tabs.value.filter(tab => Boolean(tab.workspace_id)))
  // Chat sessions only (excludes raw terminal tabs), for the sidebar.
  const chatTabs = computed(() =>
    tabs.value.filter(tab => !tab.workspace_id && tab.session_kind === 'chat')
  )
  const chatTabsByCwd = computed(() => groupChatsByCwd(chatTabs.value))

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
    // Mutate pane state in-place instead of replacing the whole array.
    // Creating a brand new array caused every TerminalPane and its TerminalView
    // to fully re-render whenever the user switched panes.
    let changed = false
    for (const pane of panes.value) {
      const shouldBeActive = pane.id === paneId
      if (pane.isActive !== shouldBeActive) {
        pane.isActive = shouldBeActive
        changed = true
      }
    }
    if (!changed && activePaneId.value === paneId) {
      return
    }
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
    for (const pane of panes.value) {
      if (pane.id === targetPaneId) {
        if (pane.tabId !== tabId) {
          pane.tabId = tabId
        }
        assigned = true
      } else if (pane.tabId === tabId) {
        // Avoid assigning the same tab to two panes simultaneously
        pane.tabId = null
      }
    }

    if (assigned) {
      activeTabId.value = tabId
    }
  }

  function getPaneCountForTab(tabId: string): number {
    return panes.value.filter(p => p.tabId === tabId).length
  }

  // Get the active pane (for mobile controls to know which terminal to send keys to)
  const activePane = computed(() => panes.value.find(p => p.id === activePaneId.value) || null)

  // True when the active pane hosts a native structured Chat session rather
  // than a raw PTY terminal. Mirrors TerminalPane's isChatSession boundary:
  // a top-level chat session owns the StructuredPane surface, and workspace
  // runners (which carry a workspace_role) always stay on their Terminal
  // surface. Mobile-only affordances that inject terminal keys (the floating
  // keyboard ball) are useless on the chat surface and must stay hidden there.
  const activePaneIsChat = computed(() => {
    const pane = activePane.value
    if (!pane || !pane.tabId) return false
    const tab = tabs.value.find(t => t.id === pane.tabId)
    if (!tab) return false
    return tab.session_kind === 'chat' && !tab.workspace_role
  })

  async function fetchTabs() {
    isLoading.value = true
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
      notifyError(e instanceof Error ? e.message : 'Unknown error')
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
      // Only update when the data actually changed — this avoids a full Vue
      // re-render cascade (TabBar, both AgentStatusFloatingPanels, all
      // TerminalPanes) every 5 seconds when the poll returns identical data.
      if (!statusesEqual(agentStatuses.value, statuses)) {
        agentStatuses.value = statuses
      }
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
    try {
      const response = await fetch(`${API_BASE}/tabs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!response.ok) throw new Error('Failed to create tab')
      const newTab = await response.json()
      tabs.value.unshift(newTab)
      activeTabId.value = newTab.id
      // Auto-assign new tab to active pane
      if (activePaneId.value) {
        assignTabToPane(newTab.id, activePaneId.value)
      }
      return newTab
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      isLoading.value = false
    }
  }

  async function duplicateTab(tabId: string) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}/duplicate`, {
        method: 'POST',
      })
      if (!response.ok) throw new Error('Failed to duplicate tab')
      const newTab = await response.json()
      tabs.value.unshift(newTab)
      activeTabId.value = newTab.id
      if (activePaneId.value) {
        assignTabToPane(newTab.id, activePaneId.value)
      }
      return newTab
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      isLoading.value = false
    }
  }

  const FORK_TIMEOUT_MS = 30000

  async function forkTab(tabId: string, ordinal: number) {
    isLoading.value = true
    // Capture the source tab BEFORE the await. The fork is a background job:
    // if the user switches to another tab/pane while it is in flight, we must
    // not yank them back to the fork when it resolves (their explicit switch
    // would be silently overridden).
    const sourceTabId = tabId
    // Bound the request so a hung connection cannot leave forkingOrdinal (and
    // the fork buttons) stuck forever.
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), FORK_TIMEOUT_MS)
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ordinal }),
        signal: controller.signal,
      })
      if (!response.ok) throw new Error('Failed to fork tab')
      const newTab = await response.json()
      tabs.value.unshift(newTab)
      // Is the user still looking at the source tab? The active pane's tab is
      // the ground truth for what is on screen right now. If yes → auto-switch
      // to the fork (legacy behavior). If they switched away → leave them
      // where they are and just notify.
      const activePane = panes.value.find(p => p.id === activePaneId.value)
      const stillOnSource = activePane?.tabId === sourceTabId
      if (stillOnSource) {
        activeTabId.value = newTab.id
        if (activePaneId.value) {
          assignTabToPane(newTab.id, activePaneId.value)
        }
      } else {
        pushNotification({
          type: 'success',
          message: `Fork created: ${newTab.name ?? 'new fork'}`,
          autoDismissMs: 5000,
        })
      }
      return newTab
    } catch (e) {
      // We only abort from the timeout timer above, so an AbortError here is a
      // timeout — surface a clearer message than the generic abort text.
      if (e instanceof Error && e.name === 'AbortError') {
        notifyError('Fork timed out — please try again')
      } else {
        notifyError(e instanceof Error ? e.message : 'Unknown error')
      }
    } finally {
      window.clearTimeout(timer)
      isLoading.value = false
    }
  }

  async function updateTab(tabId: string, data: TerminalTabUpdate) {
    isLoading.value = true
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
      notifyError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      isLoading.value = false
    }
  }

  async function deleteTab(tabId: string) {
    isLoading.value = true
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
      notifyError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      isLoading.value = false
    }
  }

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
    localStorage.setItem(STORAGE_KEY_SIDEBAR, sidebarCollapsed.value ? '1' : '0')
  }

  async function fetchArchivedTabs() {
    isLoadingArchived.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/archived`)
      if (!response.ok) throw new Error('Failed to fetch archived tabs')
      archivedTabs.value = await response.json()
    } catch (e) {
      console.error('Error fetching archived tabs:', e)
    } finally {
      isLoadingArchived.value = false
    }
  }

  // Archive (soft-delete) a tab: release its runtime but keep its JSONL history.
  // Mirrors deleteTab's pane/active cleanup, then refreshes the archived list.
  async function archiveTab(tabId: string) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}/archive`, {
        method: 'POST',
      })
      if (!response.ok) throw new Error('Failed to archive tab')
      tabs.value = tabs.value.filter(tab => tab.id !== tabId)
      // Remove tab from all panes, remembering the first pane that lost it so
      // the fallback tab can take its slot.
      let orphanedPaneId: string | null = null
      for (const pane of panes.value) {
        if (pane.tabId === tabId) {
          pane.tabId = null
          if (!orphanedPaneId) orphanedPaneId = pane.id
        }
      }
      if (activeTabId.value === tabId) {
        const fallback = manualTabs.value[0]
        if (fallback) {
          if (orphanedPaneId) {
            assignTabToPane(fallback.id, orphanedPaneId)
          } else {
            activeTabId.value = fallback.id
          }
        } else {
          activeTabId.value = null
        }
      }
      void fetchArchivedTabs()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      isLoading.value = false
    }
  }

  // Restore an archived tab and load it into the active pane. Returns true on
  // success, false on failure (callers can gate success feedback on it).
  async function unarchiveTab(tabId: string): Promise<boolean> {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}/unarchive`, {
        method: 'POST',
      })
      if (!response.ok) throw new Error('Failed to unarchive tab')
      await fetchTabs()
      setActiveTab(tabId)
      void fetchArchivedTabs()
      return true
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Unknown error')
      return false
    } finally {
      isLoading.value = false
    }
  }

  // Permanently delete an archived tab (reuses the hard-delete path, then
  // refreshes the archived list so the row disappears).
  async function permanentDeleteTab(tabId: string) {
    await deleteTab(tabId)
    void fetchArchivedTabs()
  }

  async function switchEnv(tabId: string, data: SwitchEnvRequest) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs/${tabId}/switch-env`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!response.ok) {
        const errText = await response.text().catch(() => '')
        throw new Error(errText || `Failed to switch env (${response.status})`)
      }
      const updatedTab = await response.json()
      const index = tabs.value.findIndex(t => t.id === tabId)
      if (index !== -1) {
        tabs.value[index] = updatedTab
      }
      // Also re-fetch to make sure env/solo_mode are in sync with the backend.
      await fetchTabs()
      return updatedTab
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Unknown error')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  // Best-effort: tell the backend the user opened this tab so its unread flag
  // clears. Also clear it locally for immediate feedback; the next poll re-syncs.
  async function markTabViewed(tabId: string) {
    const tab = tabs.value.find(t => t.id === tabId)
    if (tab) tab.is_unread = false
    try {
      await fetch(`${API_BASE}/tabs/${tabId}/view`, { method: 'POST' })
    } catch {
      // transient; the unread state re-syncs on the next tab-list poll
    }
  }

  function setActiveTab(tabId: string) {
    if (tabs.value.some(tab => tab.id === tabId)) {
      activeTabId.value = tabId
      // Also assign to active pane
      assignTabToPane(tabId)
      markTabViewed(tabId)
    }
  }

  let tabOrderSave: Promise<void> = Promise.resolve()

  function saveTabOrder(): Promise<void> {
    const tabIds = tabs.value.map(tab => tab.id)
    // Serialize writes: a slower earlier drag must not overwrite a later one.
    tabOrderSave = tabOrderSave.then(() => persistTabOrder(tabIds))
    return tabOrderSave
  }

  async function persistTabOrder(tabIds: string[]) {
    try {
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
    } catch (e) {
      console.error('Error saving tab order:', e)
      // (F5) Re-enabled — use pushNotification so transient tab-order save
      // failures surface without overwriting other concurrent errors.
      pushNotification({
        type: 'warning',
        message: e instanceof Error ? `保存标签页顺序失败：${e.message}` : '保存标签页顺序失败',
        autoDismissMs: 6000,
      })
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

  function reorderTabById(sourceId: string, targetId: string, position: 'before' | 'after') {
    const source = manualTabs.value.find(tab => tab.id === sourceId)
    const target = manualTabs.value.find(tab => tab.id === targetId)
    if (!source || !target) return
    const next = moveTabById(tabs.value, sourceId, targetId, position)
    if (next === tabs.value) return
    tabs.value = next
    void saveTabOrder()
  }

  return {
    tabs,
    manualTabs,
    managedTabs,
    chatTabs,
    chatTabsByCwd,
    pinnedChatIds,
    setChatPinned,
    agentStatuses,
    activeTabId,
    activeTab,
    isLoading,
    isStatusLoading,
    error,
    notifications,
    pushNotification,
    dismissNotification,
    layoutType,
    panes,
    activePaneId,
    activePane,
    activePaneIsChat,
    sidebarCollapsed,
    mobileDrawerOpen,
    archivedTabs,
    isLoadingArchived,
    fetchTabs,
    fetchAgentStatuses,
    startAgentStatusPolling,
    stopAgentStatusPolling,
    createTab,
    duplicateTab,
    forkTab,
    updateTab,
    deleteTab,
    switchEnv,
    setActiveTab,
    reorderTabs,
    reorderTabById,
    setLayout,
    setActivePane,
    assignTabToPane,
    getPaneCountForTab,
    initializePanes,
    saveTabOrder,
    toggleSidebar,
    fetchArchivedTabs,
    archiveTab,
    unarchiveTab,
    permanentDeleteTab,
  }
})
