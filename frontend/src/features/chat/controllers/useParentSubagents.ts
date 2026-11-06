import {
  computed,
  onMounted,
  onUnmounted,
  ref,
  watch,
  type Ref,
} from 'vue'

import { API_BASE } from '@/stores/terminal/helpers'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAppStore } from '@/stores/appStore'
import {
  buildSubagentJumpChips,
  subagentPollIntervalMs,
  type SubagentJumpChip,
  type SubagentSummary,
} from '../presentation/subagentJump'

/**
 * Lists Hub subagents (`claude-hub agent run`) attached to a parent Chat tab
 * and exposes the one-click jump used by the pane's Agents dock. Owned by
 * TerminalPane (one poller per pane) and shared via `subagentDockKey`.
 *
 * The stream of a parent Chat never carries the child's Hub tab id, so the
 * source of truth is the durable /api/subagents record (includes archived
 * children). Polling is best-effort: request failures keep the last rows.
 */
export function useParentSubagents(tabId: Ref<string>) {
  const terminalStore = useTerminalStore()
  const appStore = useAppStore()
  const summaries = ref<SubagentSummary[]>([])
  const failed = ref(false)
  let timer: ReturnType<typeof setInterval> | null = null
  let disposed = false

  const chips = computed<SubagentJumpChip[]>(() =>
    buildSubagentJumpChips(
      summaries.value,
      [...terminalStore.tabs, ...terminalStore.archivedTabs],
      terminalStore.agentStatuses,
    ),
  )

  async function refresh(): Promise<void> {
    const requestedTabId = tabId.value
    // An empty id means "this pane has no Chat tab": nothing to list.
    if (!requestedTabId) return
    try {
      const response = await fetch(
        `${API_BASE}/subagents?parent_tab_id=${encodeURIComponent(requestedTabId)}&include_closed=true`,
      )
      if (!response.ok) throw new Error(`subagents list failed: ${response.status}`)
      const rows = (await response.json()) as SubagentSummary[]
      // Drop responses for a tab the pane has since switched away from.
      if (!disposed && tabId.value === requestedTabId) {
        summaries.value = Array.isArray(rows) ? rows : []
        failed.value = false
      }
    } catch {
      // Non-fatal: the card stays useful with the last successful rows.
      if (!disposed && tabId.value === requestedTabId) failed.value = true
    }
  }

  async function jump(chip: SubagentJumpChip): Promise<void> {
    if (!chip.tabId) return
    if (chip.archived) {
      // unarchiveTab refreshes the tab list and loads the tab into the pane.
      const restored = await terminalStore.unarchiveTab(chip.tabId)
      if (!restored) return
      appStore.setMode('terminal')
      return
    }
    await terminalStore.fetchTabs()
    appStore.setMode('terminal')
    terminalStore.setActiveTab(chip.tabId)
  }

  function startPolling() {
    if (timer !== null) return
    void refresh()
    timer = setInterval(() => void refresh(), subagentPollIntervalMs())
  }

  function stopPolling() {
    if (timer === null) return
    clearInterval(timer)
    timer = null
  }

  // The owner (TerminalPane) is never deactivated: other app modes hide the
  // terminal shell with v-show. Pause while that shell or the page is hidden,
  // and refetch at once on resume so the dock is not a poll interval stale.
  const pageVisible = ref(typeof document === 'undefined' || !document.hidden)
  const shouldPoll = computed(
    () => pageVisible.value && appStore.mode === 'terminal',
  )

  function onVisibilityChange() {
    pageVisible.value = !document.hidden
  }

  watch(shouldPoll, poll => (poll ? startPolling() : stopPolling()))

  onMounted(() => {
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibilityChange)
    }
    if (shouldPoll.value) startPolling()
  })

  // The pane component stays mounted while the user switches its tab; a
  // tabId change means this pane is the active one, so refetch immediately.
  watch(tabId, () => {
    summaries.value = []
    failed.value = false
    void refresh()
  })

  onUnmounted(() => {
    disposed = true
    stopPolling()
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  })

  return { chips, failed, refresh, jump }
}
