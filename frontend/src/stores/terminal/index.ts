import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import type {
  TerminalAgentStatus,
  TerminalTab,
  TerminalTabCreate,
  TerminalTabUpdate,
  SwitchEnvRequest,
  LayoutType,
  Pane,
} from '@/types'
import { groupChatsByCwd, moveCwdGroup, moveTabById } from '@/utils/chatGroups'
import {
  API_BASE,
  LAYOUT_CONFIGS,
  STORAGE_KEY_LAYOUT,
  STORAGE_KEY_SIDEBAR,
  generatePaneId,
  tabRequestError,
} from './helpers'
import { createAgentStatusPolling } from './agentStatusPolling'
import { createMobileDrawer } from './mobileDrawer'
import { createPaneReconnectRuntime } from './paneReconnect'
import { createQueuedChatRuntime } from './queuedChatRuntime'
import { createSidebarPreferences } from './sidebarPreferences'
import { createStoreNotifications } from '../notifications'
import type { QueuedChatActivationRequest, QueuedChatState } from './types'

export const useTerminalStore = defineStore('terminal', () => {
  const tabs = ref<TerminalTab[]>([])
  const agentStatuses = ref<TerminalAgentStatus[]>([])
  const activeTabId = ref<string | null>(null)
  const isLoading = ref(false)
  const isStatusLoading = ref(false)
  const {
    notifications,
    pushNotification,
    dismissNotification,
    notifyError,
    error,
  } = createStoreNotifications('n', 8000)
  // Layout and panes
  const layoutType = ref<LayoutType>(
    (localStorage.getItem(STORAGE_KEY_LAYOUT) as LayoutType) || '1x1'
  )
  const panes = ref<Pane[]>([])
  const activePaneId = ref<string | null>(null)
  // Queue state outlives the bounded StructuredPane KeepAlive cache. Hidden
  // completion observers and recreated panes share this small durable browser
  // record; full transcript DOM and stream state remain component-local.
  const queuedChatStates = ref<QueuedChatState[]>([])
  const chatPaneCacheEpochs = ref<Record<string, number>>({})
  const queuedChatActivationRequests = ref<QueuedChatActivationRequest[]>([])

  // Left chat sidebar collapse state, persisted across reloads.
  const sidebarCollapsed = ref<boolean>(
    localStorage.getItem(STORAGE_KEY_SIDEBAR) === '1'
  )
  const pinnedChatIds = ref<Set<string>>(new Set())
  const {
    mobileDrawerOpen,
    openMobileDrawer,
    closeMobileDrawer,
    toggleMobileDrawer,
    selectMobileTab,
  } = createMobileDrawer(setActiveTab)
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
  const { setChatPinned, sync: syncChatSidebarPreferences } = createSidebarPreferences({
    chatTabs,
    pinnedChatIds,
    pushNotification,
  })

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
    const nextConfig = LAYOUT_CONFIGS[type]
    const retainedPaneIds = new Set(
      panes.value.slice(0, nextConfig.rows * nextConfig.cols).map(pane => pane.id)
    )
    if (queuedChatStates.value.some(state =>
      queuedChatStateIsProtected(state) && !retainedPaneIds.has(state.paneId)
    )) {
      pushNotification({
        type: 'warning',
        message: 'Wait for queued Chat messages before reducing the pane layout.',
        autoDismissMs: 6000,
      })
      return
    }
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

  const queuedChat = createQueuedChatRuntime({
    tabs,
    panes,
    activePaneId,
    activeTabId,
    states: queuedChatStates,
    cacheEpochs: chatPaneCacheEpochs,
    requests: queuedChatActivationRequests,
    setActivePane,
    setActiveTab,
  })
  const {
    owner: queuedChatActivationOwner,
    stateIsProtected: queuedChatStateIsProtected,
    getState: getQueuedChatState,
    moveToPane: moveQueuedChatToPane,
    paneHasProtected: paneHasProtectedQueuedChat,
    protectedIds: protectedQueuedChatIds,
    cacheKey: chatPaneCacheKey,
    observedIds: observedQueuedChatIds,
    discard: discardQueuedChat,
    prune: pruneQueuedChatState,
    isOwner: isQueuedChatActivationOwner,
    hasRequest: hasQueuedChatActivationRequest,
    requestId: queuedChatActivationRequestId,
    requestActivation: requestQueuedChatActivation,
    releaseActivation: releaseQueuedChatActivation,
    settleActivation: settleQueuedChatActivation,
  } = queuedChat

  watch(
    [activePaneId, activeTabId, queuedChatActivationOwner],
    queuedChat.ensureActivation,
    { flush: 'post' },
  )

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
      // Moving a tab can reactivate an existing KeepAlive instance whose setup
      // will not rerun. Migrate store-owned queue/request ownership at the
      // assignment boundary so a completion cannot pull the Chat back to its
      // previous pane.
      moveQueuedChatToPane(tabId, targetPaneId)
      activeTabId.value = tabId
    }
  }

  function getPaneCountForTab(tabId: string): number {
    return panes.value.filter(p => p.tabId === tabId).length
  }

  // Get the active pane (for mobile controls to know which terminal to send keys to)
  const activePane = computed(() => panes.value.find(p => p.id === activePaneId.value) || null)

  // True when the active pane hosts a structured Chat session rather than a
  // raw PTY terminal. Managed Chat requires the managed session identity used
  // by its canonical stream endpoint; ordinary Chat remains tab-scoped.
  const activePaneIsChat = computed(() => {
    const pane = activePane.value
    if (!pane || !pane.tabId) return false
    const tab = tabs.value.find(t => t.id === pane.tabId)
    if (!tab) return false
    return tab.session_kind === 'chat'
  })

  const {
    paneReconnectStatus,
    requestPaneReconnect,
    settlePaneReconnect,
    clearPaneReconnect,
  } = createPaneReconnectRuntime(tabs)

  // Background refresh of the tab list. This must NOT drive ``isLoading``:
  // it runs every status-poll cycle (and on every mounted status panel), and
  // sharing that flag with the launcher makes the sidebar + button blink
  // into a disabled "..." every few seconds. ``isLoading`` is reserved for
  // explicit mutations (create / close / archive / ...). Overlapping calls
  // (the two floating panels mount at once, poll-driven refreshes overlap)
  // coalesce onto one in-flight request and share its result.
  let tabsFetchPromise: Promise<void> | null = null
  let tabSnapshotGeneration = 0
  function invalidateTabSnapshots(): number {
    tabSnapshotGeneration += 1
    return tabSnapshotGeneration
  }

  function fetchTabs(options: { force?: boolean } = {}): Promise<void> {
    // Mutation callers (restore, switch-env, create agent, ...) rely on the
    // awaited refresh reflecting the change they just made, so they bypass
    // coalescing — a poll-started fetch could have raced the server.
    if (tabsFetchPromise && !options.force) return tabsFetchPromise
    const generation = invalidateTabSnapshots()
    const run = async () => {
      try {
        const response = await fetch(`${API_BASE}/tabs`)
        if (!response.ok) throw new Error('Failed to fetch tabs')
        const snapshot: unknown = await response.json()
        if (!Array.isArray(snapshot)) throw new Error('Invalid full tab snapshot')
        // A newer fetch or a successful local mutation owns the current list.
        // Never let this older full snapshot erase a newly created/restored tab,
        // its pane assignment, or a protected queued draft.
        if (generation !== tabSnapshotGeneration) return
        const nextTabs = snapshot as TerminalTab[]
        const nextIds = new Set(nextTabs.map(tab => tab.id))
        const removedIds = tabs.value
          .map(tab => tab.id)
          .filter(tabId => !nextIds.has(tabId))
        tabs.value = nextTabs
        if (removedIds.length > 0) {
          const removed = new Set(removedIds)
          for (const pane of panes.value) {
            if (pane.tabId && removed.has(pane.tabId)) pane.tabId = null
          }
          for (const tabId of removedIds) discardQueuedChat(tabId)
          if (activeTabId.value && removed.has(activeTabId.value)) activeTabId.value = null
        }
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
        await syncChatSidebarPreferences()
      } catch (e) {
        notifyError(e instanceof Error ? e.message : 'Unknown error')
      }
    }
    const promise = run()
    const tracked = promise.finally(() => {
      if (tabsFetchPromise === tracked) tabsFetchPromise = null
    })
    // A forced request supersedes an older coalesced request. Its identity
    // check in finally prevents the older request from clearing this promise.
    tabsFetchPromise = tracked
    return tracked
  }

  const {
    fetchAgentStatuses,
    startAgentStatusPolling,
    stopAgentStatusPolling,
  } = createAgentStatusPolling({
    tabs,
    statuses: agentStatuses,
    isLoading: isStatusLoading,
    fetchTabs,
  })

  async function createTab(data: TerminalTabCreate) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/tabs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!response.ok) throw await tabRequestError(response, 'Failed to create tab')
      const newTab = await response.json()
      invalidateTabSnapshots()
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
      if (!response.ok) throw await tabRequestError(response, 'Failed to duplicate tab')
      const newTab = await response.json()
      invalidateTabSnapshots()
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
      if (!response.ok) throw await tabRequestError(response, 'Failed to fork tab')
      const newTab = await response.json()
      invalidateTabSnapshots()
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
      invalidateTabSnapshots()
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
      invalidateTabSnapshots()
      tabs.value = tabs.value.filter(tab => tab.id !== tabId)
      discardQueuedChat(tabId)
      // Remove tab from all panes
      panes.value.forEach(pane => {
        if (pane.tabId === tabId) {
          pane.tabId = null
        }
      })
      if (activeTabId.value === tabId) {
        activeTabId.value = manualTabs.value.length ? manualTabs.value[0].id : null
      }
      const nextPinned = new Set(pinnedChatIds.value)
      nextPinned.delete(tabId)
      pinnedChatIds.value = nextPinned
      return true
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Unknown error')
      return false
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
      invalidateTabSnapshots()
      tabs.value = tabs.value.filter(tab => tab.id !== tabId)
      discardQueuedChat(tabId)
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
      if (!response.ok) throw await tabRequestError(response, 'Failed to unarchive tab')
      await fetchTabs({ force: true })
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
    const deleted = await deleteTab(tabId)
    if (deleted) void fetchArchivedTabs()
    return deleted
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
      invalidateTabSnapshots()
      const index = tabs.value.findIndex(t => t.id === tabId)
      if (index !== -1) {
        tabs.value[index] = updatedTab
      }
      // Also re-fetch to make sure env/solo_mode are in sync with the backend.
      await fetchTabs({ force: true })
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
    invalidateTabSnapshots()
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
    invalidateTabSnapshots()
    tabs.value = next
    void saveTabOrder()
  }

  function reorderChatWorkspace(
    sourceCwd: string, targetCwd: string, position: 'before' | 'after',
  ) {
    const chatCwds = new Set(chatTabsByCwd.value.map(group => group.cwd))
    if (!chatCwds.has(sourceCwd) || !chatCwds.has(targetCwd)) return
    // Move every manual tab in the directory, including raw terminals. This
    // makes the persisted workspace order apply to both Chat and Terminal
    // Status while leaving Workspace-managed agents in their own tail block.
    // Pinned Chats keep their slots because the pinned section follows tab
    // order, which a workspace drag must not rearrange.
    const pinned = pinnedChatIds.value
    const movableTabs = manualTabs.value.filter(tab => !pinned.has(tab.id))
    const movedTabs = moveCwdGroup(movableTabs, sourceCwd, targetCwd, position)
    if (movedTabs === movableTabs) return
    let movedIndex = 0
    const reorderedManualTabs = manualTabs.value.map(
      tab => (pinned.has(tab.id) ? tab : movedTabs[movedIndex++]),
    )
    invalidateTabSnapshots()
    tabs.value = [...reorderedManualTabs, ...managedTabs.value]
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
    queuedChatActivationOwner,
    sidebarCollapsed,
    mobileDrawerOpen,
    openMobileDrawer,
    closeMobileDrawer,
    toggleMobileDrawer,
    selectMobileTab,
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
    reorderChatWorkspace,
    setLayout,
    setActivePane,
    getQueuedChatState,
    chatPaneCacheKey,
    paneHasProtectedQueuedChat,
    protectedQueuedChatIds,
    observedQueuedChatIds,
    discardQueuedChat,
    pruneQueuedChatState,
    isQueuedChatActivationOwner,
    hasQueuedChatActivationRequest,
    queuedChatActivationRequestId,
    requestQueuedChatActivation,
    releaseQueuedChatActivation,
    settleQueuedChatActivation,
    assignTabToPane,
    getPaneCountForTab,
    initializePanes,
    paneReconnectStatus,
    requestPaneReconnect,
    settlePaneReconnect,
    clearPaneReconnect,
    saveTabOrder,
    toggleSidebar,
    fetchArchivedTabs,
    archiveTab,
    unarchiveTab,
    permanentDeleteTab,
  }
})
