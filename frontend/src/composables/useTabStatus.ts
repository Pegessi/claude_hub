import { computed } from 'vue'
import type { Ref } from 'vue'
import type { AgentRuntimeStatus, TerminalAgentStatus, TerminalTab } from '@/types'

const TAB_STATUS_LABELS: Record<AgentRuntimeStatus, string> = {
  idle: 'Idle',
  working: 'Working',
  attention: 'Needs attention',
  offline: 'Offline',
}

/**
 * Shared tab-status logic: index the backend-reported agent statuses by tab id,
 * resolve a tab's runtime status (with a fallback), and build a human label.
 * Used by both the TabBar and the session sidebar so the logic lives in one
 * place and cannot drift between the two.
 */
export function useTabStatus(agentStatuses: Ref<readonly TerminalAgentStatus[]>) {
  const tabStatusById = computed<Record<string, TerminalAgentStatus>>(() => {
    const map: Record<string, TerminalAgentStatus> = {}
    for (const s of agentStatuses.value) {
      map[s.tab_id] = s
    }
    return map
  })

  function getTabStatus(tab: TerminalTab): AgentRuntimeStatus {
    return tabStatusById.value[tab.id]?.status ?? (
      tab.session_kind === 'chat' ? 'offline' : tab.is_active ? 'idle' : 'offline'
    )
  }

  // Returns the status label, plus any backend `status_text` detail:
  // "Working", or "Working — <detail>".
  function getTabStatusLabel(tab: TerminalTab): string {
    const status = getTabStatus(tab)
    const statusLabel = TAB_STATUS_LABELS[status]
    const detail = tabStatusById.value[tab.id]?.status_text?.trim()
    if (!detail || detail.toLocaleLowerCase() === statusLabel.toLocaleLowerCase()) {
      return statusLabel
    }
    return `${statusLabel} — ${detail}`
  }

  return { getTabStatus, getTabStatusLabel }
}
