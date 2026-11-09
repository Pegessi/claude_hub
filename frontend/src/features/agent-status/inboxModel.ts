/**
 * Inbox model: turn the per-tab status poll into the three questions a user
 * actually asks — "does anything need me?", "what is still working?", "what
 * just finished?".
 *
 * Everything here is a pure function of (tab, status, now) so the grouping
 * rules, the badge count and the keyboard cursor can be unit-tested without a
 * browser. The panel component only renders this model.
 */
import type { TerminalAgentStatus, TerminalTab } from '@/types'

/** Row buckets, in display order. */
export type InboxGroup = 'needs' | 'working' | 'done'

/** Which tab population a row belongs to; the panel's segment filter. */
export type InboxScope = 'all' | 'manual' | 'workspace'

export interface InboxRow {
  tab: TerminalTab
  status?: TerminalAgentStatus
  group: InboxGroup
  /** Short sentence explaining why this row is where it is. */
  reason: string
  /** Preview text: the last reply, else the current activity. */
  excerpt: string | null
  /** Elapsed-time anchor for working rows. */
  sinceMs: number | null
  /** Sort key inside the group (larger = newer/more urgent). */
  sortKey: number
}

export interface InboxGroupData {
  key: InboxGroup
  title: string
  rows: InboxRow[]
}

const GROUP_TITLES: Record<InboxGroup, string> = {
  needs: 'Needs you',
  working: 'Working',
  done: 'Recently done',
}

/** A short reply is a preview; the full text lives in the Chat itself. */
const MAX_EXCERPT_CHARS = 160

/** Finished rows stop being "recent" after this long; older ones are noise. */
export const RECENT_DONE_WINDOW_MS = 12 * 60 * 60 * 1000

/** Idle rows that only exist because a shell is open are not inbox items. */
function isAgentTab(tab: TerminalTab): boolean {
  return tab.session_kind === 'chat' || Boolean(tab.agent_type)
}

function isWorkspaceTab(tab: TerminalTab): boolean {
  return Boolean(tab.workspace_id)
}

function parseMs(value?: string | null): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

function cleanExcerpt(value?: string | null): string | null {
  const collapsed = (value ?? '').replace(/\s+/g, ' ').trim()
  if (!collapsed) return null
  if (collapsed.length <= MAX_EXCERPT_CHARS) return collapsed
  return `${collapsed.slice(0, MAX_EXCERPT_CHARS - 1).trimEnd()}…`
}

function activityLabel(status?: TerminalAgentStatus): string {
  const activity = (status?.activity ?? '').trim()
  if (activity) return activity
  // A terminal session has no provider activity; the classifier's own detail
  // ("Waiting for input") is the honest fallback.
  const detail = (status?.detail ?? '').trim()
  if (detail) return detail
  return status?.status_text?.trim() || 'Working'
}

/**
 * Decide the bucket for one row. The order is deliberate: a pending question
 * outranks everything (a turn can keep running while a card waits), then an
 * unread finished turn, then failures, then plain work.
 */
export function classifyRow(
  tab: TerminalTab,
  status: TerminalAgentStatus | undefined,
  nowMs: number,
): InboxRow {
  const runtimeStatus = status?.status ?? (tab.is_active ? 'idle' : 'offline')
  const replyExcerpt = cleanExcerpt(status?.last_reply_excerpt)
  const replyAtMs = parseMs(status?.last_reply_at) ?? parseMs(status?.last_changed_at)
  const activitySinceMs = parseMs(status?.activity_since)

  const row = (
    group: InboxGroup,
    reason: string,
    excerpt: string | null,
    sinceMs: number | null,
    sortKey: number,
  ): InboxRow => ({
    tab,
    status,
    group,
    reason,
    // A one-line row must not print the same sentence twice: the old payloads
    // repeated their detail as the preview, and a reply that echoes the reason
    // is not a preview either.
    excerpt: excerpt && excerpt !== reason ? excerpt : null,
    sinceMs,
    sortKey,
  })

  if (status?.pending_question) {
    return row('needs', 'Waiting for your answer', replyExcerpt, activitySinceMs, 4)
  }

  // A finished turn the user has not looked at yet is the inbox's core case:
  // the work is done and the result is sitting unread.
  if (tab.is_unread && runtimeStatus !== 'working') {
    return row('needs', 'New reply', replyExcerpt, replyAtMs, 3)
  }

  if (runtimeStatus === 'attention') {
    return row(
      'needs',
      (status?.detail ?? '').trim() || 'Needs attention',
      replyExcerpt,
      replyAtMs,
      2,
    )
  }

  // Offline matters for agent sessions (the provider stopped or errored);
  // a bare shell tab whose tmux session disappeared is not something the user
  // can act on from the inbox.
  if (runtimeStatus === 'offline' && isAgentTab(tab)) {
    return row('needs', (status?.detail ?? '').trim() || 'Agent is offline', replyExcerpt, replyAtMs, 1)
  }

  if (runtimeStatus === 'working') {
    // A running row shows what is happening now; its previous reply is not a
    // useful preview, and the state sentence would just repeat the reason.
    return row(
      'working',
      activityLabel(status),
      null,
      activitySinceMs,
      nowMs - (activitySinceMs ?? nowMs),
    )
  }

  return row(
    'done',
    status?.detail?.trim() || (runtimeStatus === 'idle' ? 'Idle' : 'Offline'),
    replyExcerpt,
    replyAtMs,
    -(replyAtMs ?? 0),
  )
}

export function inScope(tab: TerminalTab, scope: InboxScope): boolean {
  if (scope === 'all') return true
  if (scope === 'workspace') return isWorkspaceTab(tab)
  return !isWorkspaceTab(tab)
}

/**
 * Build the full panel model: filtered, classified, grouped and ordered.
 *
 * `needsYouCount` keeps the trigger badge honest — the count it shows is
 * exactly the number of "Needs you" rows, so a quiet Hub shows no number.
 */
export function buildInbox(
  tabs: readonly TerminalTab[],
  statuses: readonly TerminalAgentStatus[],
  options: { scope?: InboxScope; nowMs: number; includeDone?: boolean },
): InboxGroupData[] {
  const scope = options.scope ?? 'all'
  const statusByTabId = new Map<string, TerminalAgentStatus>()
  for (const status of statuses) statusByTabId.set(status.tab_id, status)

  const rows: InboxRow[] = []
  for (const tab of tabs) {
    if (tab.archived) continue
    if (!inScope(tab, scope)) continue
    const status = statusByTabId.get(tab.id)
    const row = classifyRow(tab, status, options.nowMs)
    if (row.group === 'done') {
      if (options.includeDone === false) continue
      const at = row.sinceMs
      if (at !== null && options.nowMs - at > RECENT_DONE_WINDOW_MS) continue
    }
    rows.push(row)
  }

  const groups: InboxGroupData[] = (['needs', 'working', 'done'] as InboxGroup[]).map(key => ({
    key,
    title: GROUP_TITLES[key],
    rows: rows
      .filter(row => row.group === key)
      .sort((a, b) => (key === 'done' ? a.sortKey - b.sortKey : b.sortKey - a.sortKey)),
  }))
  return groups
}

/** The badge count: needs-you rows only, never the total. */
export function needsYouCount(groups: readonly InboxGroupData[]): number {
  return groups.find(group => group.key === 'needs')?.rows.length ?? 0
}

/** Flattened rows in render order, for ↑/↓ cursor movement. */
export function flattenRows(groups: readonly InboxGroupData[]): InboxRow[] {
  return groups.flatMap(group => group.rows)
}

/**
 * Move the keyboard cursor by one row, clamped at both ends.
 *
 * A hidden (collapsed) "Recently done" group is passed in already removed by
 * the caller, so the cursor can never land on something not on screen.
 */
export function moveSelection(current: number, delta: number, length: number): number {
  if (length <= 0) return -1
  if (current < 0) return delta > 0 ? 0 : length - 1
  const next = current + delta
  if (next < 0) return 0
  if (next >= length) return length - 1
  return next
}

/** "3m", "2h", "just now" — compact, tabular, mobile friendly. */
export function formatElapsed(sinceMs: number | null, nowMs: number): string {
  if (sinceMs === null || Number.isNaN(sinceMs)) return ''
  const seconds = Math.max(0, Math.round((nowMs - sinceMs) / 1000))
  if (seconds < 45) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

/** Short state dot label for assistive tech and the row tooltip. */
export function statusLabel(row: InboxRow): string {
  if (row.group === 'working') return 'Working'
  if (row.status?.status === 'attention') return 'Needs attention'
  if (row.status?.status === 'offline') return 'Offline'
  if (row.group === 'needs') return 'Waiting for you'
  return 'Idle'
}
