import type { TerminalAgentStatus, TerminalTab } from '@/types'
import type { NativeSubagentRow } from './nativeSubagentRows'

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
  /** Discriminant for the shared dock list; native rows are ``'native'``. */
  kind: 'hub'
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
  /** Running children are "active"; everything else is history ("earlier"). */
  group: 'active' | 'earlier'
  createdAt: string
  /** Hub children open by jumping to their Chat tab, never in a window. */
  canOpen: false
}

/** One dock row: a Hub child Chat that jumps, or a native child that opens a
 *  detail window. The list, rail, popover and sheet render both identically. */
export type SubagentDockRow = SubagentJumpChip | NativeSubagentRow

export const SUBAGENT_JUMP_MAX_CHIPS = 30
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
    kind: 'hub',
    key: summary.id,
    subagentId: summary.id,
    tabId,
    name: summary.name,
    shortId: shortSubagentId(summary.id),
    agentType: summary.agent_type,
    state,
    stateLabel: STATE_LABELS[state],
    summary: cleanSubagentSummary(summary.last_summary) || FALLBACK_SUMMARY[state],
    archived,
    canJump: tabId !== null,
    canOpen: false,
    group: state === 'running' ? 'active' : 'earlier',
    createdAt: summary.created_at,
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

/** Hub names sub-agents "<type> subagent: <task>"; the type already has its
 *  own column, so list rows show just the task. */
export function subagentDisplayName(name: string): string {
  const task = name.replace(/^\S+ subagent:\s*/i, '').trim()
  return task || name
}

/** One plain line from a markdown-ish summary ('### Title', '**bold**', code). */
export function cleanSubagentSummary(text: string | null | undefined): string {
  if (!text) return ''
  const line = text
    .split('\n')
    .map(part => part.trim())
    .find(part => part.replace(/[#>*\-_`\s]/g, '').length > 0)
  if (!line) return ''
  return line
    .replace(/^#{1,6}\s+/, '')
    .replace(/^>\s?/, '')
    .replace(/^[-*]\s+/, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/(\*\*|__|\*|`)/g, '')
    .trim()
}

/** "just now" / "5m ago" / "3h ago" / "2d ago"; '' when the date is unusable. */
export function relativeSubagentTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso)
  if (!Number.isFinite(then)) return ''
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 45) return 'just now'
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export interface SubagentDockCounts {
  running: number
  history: number
  total: number
}

export function subagentDockCounts(chips: readonly SubagentDockRow[]): SubagentDockCounts {
  // Only the grouping matters here, so both row kinds count the same way.
  const running = chips.filter(chip => chip.group === 'active').length
  return { running, history: chips.length - running, total: chips.length }
}

const HUB_SUBAGENT_ID_RE = /\bsa-[0-9a-f]{12}\b/
const HUB_AGENT_RUN_RE = /claude-hub\s+agent\s+(?:run|send)\b/

/** Hub sub-agent id of a `claude-hub agent run` Bash call, only when the
 *  command is an agent run AND its output clearly names the id (background
 *  runs and `--json` print it; foreground runs may not, and then no link). */
export function hubSubagentIdFromTool(argsText: string, resultText: string): string | null {
  if (!HUB_AGENT_RUN_RE.test(argsText)) return null
  return HUB_SUBAGENT_ID_RE.exec(resultText)?.[0] ?? null
}
