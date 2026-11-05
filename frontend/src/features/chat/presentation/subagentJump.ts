import type { TerminalAgentStatus, TerminalTab } from '@/types'

/** `GET /api/subagents` row (fields this card consumes). */
export interface SubagentSummary {
  id: string
  tab_id: string | null
  parent_tab_id: string | null
  agent_type: string
  name: string
  keep: boolean
  closed: boolean
  close_reason: string | null
  created_at: string
  turn_count: number
  last_turn_id: string | null
  last_status: 'running' | 'completed' | 'failed' | 'timeout' | 'cancelled' | null
  last_summary: string | null
}

export interface SubagentJumpChip {
  key: string
  subagentId: string
  tabId: string | null
  name: string
  shortId: string
  agentType: string
  /** Terminal lifecycle state used for label and pill styling. */
  state: 'running' | 'completed' | 'failed' | 'timeout' | 'cancelled' | 'closed'
  stateLabel: string
  summary: string
  archived: boolean
  canJump: boolean
}

export const SUBAGENT_JUMP_MAX_CHIPS = 10
const POLL_INTERVAL_MS = 5000

export function subagentPollIntervalMs(): number {
  return POLL_INTERVAL_MS
}

export function shortSubagentId(subagentId: string): string {
  // sa-0123456789ab -> sa…89ab keeps both the prefix and a stable tail.
  if (subagentId.length <= 9) return subagentId
  return `${subagentId.slice(0, 3)}…${subagentId.slice(-4)}`
}

const STATE_LABELS: Record<SubagentJumpChip['state'], string> = {
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  timeout: 'Timeout',
  cancelled: 'Cancelled',
  closed: 'Closed',
}

const FALLBACK_SUMMARY: Record<SubagentJumpChip['state'], string> = {
  running: 'Working…',
  completed: 'Completed',
  failed: 'Turn failed',
  timeout: 'Timed out',
  cancelled: 'Cancelled',
  closed: 'Subagent closed',
}

function chipState(summary: SubagentSummary, live: boolean): SubagentJumpChip['state'] {
  if (summary.closed) return 'closed'
  switch (summary.last_status) {
    case 'running':
      return 'running'
    case 'completed':
    case 'failed':
    case 'timeout':
    case 'cancelled':
      // A finished non-kept tab leaves the live list once it is archived.
      return live ? summary.last_status : 'closed'
    default:
      return 'running'
  }
}

export function toSubagentJumpChip(
  summary: SubagentSummary,
  options: { liveTabIds: Set<string>; archivedTabIds: Set<string>; statusByTabId: Record<string, TerminalAgentStatus> },
): SubagentJumpChip {
  const live = summary.tab_id !== null && options.liveTabIds.has(summary.tab_id)
  const state = chipState(summary, live)
  const archived = summary.tab_id !== null && options.archivedTabIds.has(summary.tab_id)
  const tabId = summary.tab_id
  return {
    key: summary.id,
    subagentId: summary.id,
    tabId,
    name: summary.name,
    shortId: shortSubagentId(summary.id),
    agentType: summary.agent_type,
    state,
    stateLabel: STATE_LABELS[state],
    summary: summary.last_summary || FALLBACK_SUMMARY[state],
    archived,
    canJump: tabId !== null,
  }
}

export function buildSubagentJumpChips(
  summaries: SubagentSummary[],
  tabs: TerminalTab[],
  statuses: TerminalAgentStatus[],
): SubagentJumpChip[] {
  const liveTabIds = new Set(tabs.filter(tab => !tab.archived).map(tab => tab.id))
  const archivedTabIds = new Set(tabs.filter(tab => Boolean(tab.archived)).map(tab => tab.id))
  const statusByTabId: Record<string, TerminalAgentStatus> = {}
  for (const status of statuses) statusByTabId[status.tab_id] = status
  return summaries.slice(0, SUBAGENT_JUMP_MAX_CHIPS).map(summary =>
    toSubagentJumpChip(summary, { liveTabIds, archivedTabIds, statusByTabId }),
  )
}
