import {
  computed,
  onActivated,
  onDeactivated,
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
 * and exposes the one-click jump used by the Codex-style tips card.
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

  onMounted(startPolling)
  // KeepAlive keeps the pane alive across tab visits; do not poll hidden panes.
  onActivated(startPolling)
  onDeactivated(stopPolling)

  // The pane component stays mounted while the user switches its tab; a
  // tabId change means this pane is the active one, so refetch immediately.
  watch(tabId, () => {
    summaries.value = []
    void refresh()
  })

  onUnmounted(() => {
    disposed = true
    stopPolling()
  })

  return { chips, failed, refresh, jump }
}
