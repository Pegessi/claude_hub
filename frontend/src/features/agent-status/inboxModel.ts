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

// The entry itself is called "Needs you", so its first group must not repeat
// that name: the panel title says what the surface is, and each group says what
// state those rows are in.
const GROUP_TITLES: Record<InboxGroup, string> = {
  needs: 'Waiting on you',
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

function isSubagentTab(tab: TerminalTab): boolean {
  return Boolean(tab.parent_tab_id || tab.subagent_id)
}

/** A manual top-level session created in the Terminal/Chat UI, not a Workspace
 *  managed session or a Hub sub-agent spawned from another Chat. */
export function isManualSessionTab(tab: TerminalTab): boolean {
  return (
    !tab.workspace_id &&
    !tab.parent_tab_id &&
    !tab.subagent_id &&
    !tab.managed_session_id &&
    !tab.workspace_role
  )
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
/** The dot a row wears, from the same fields the row's text uses.
 *
 * A card waiting on the user is its own state (`needs`), but only where the
 * provider is otherwise healthy: `pending_question` also rides on an
 * `attention`/`offline` snapshot (see `useTabStatus`), and painting that amber
 * would contradict the row's own reason and the sidebar light. */
export function rowDotState(reported: TerminalAgentStatus | null | undefined, group: string): string {
  const state = reported?.status
  if (reported?.pending_question && state !== 'attention' && state !== 'offline') return 'needs'
  return state ?? (group === 'working' ? 'working' : 'idle')
}

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

  if (isSubagentTab(tab)) {
    // A Hub sub-agent is owned by its parent Chat (which consumes its reply
    // and surfaces it in the pane-chrome Agents dock), so it never demands
    // human attention in "Waiting on you".
    if (runtimeStatus === 'working') {
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

  if (status?.pending_question) {
    // A blocked idle turn reports no activity clock; the newest reply is the
    // only real timestamp it has, so the row keeps a time either way. The
    // backend also keeps `pending_question` on an attention/offline snapshot,
    // where an answer can be refused, so the reason names the failure too
    // instead of hiding it behind "Waiting for your answer".
    const failure = runtimeStatus === 'attention' || runtimeStatus === 'offline'
    const failureReason = failure
      ? (status?.detail ?? '').trim() ||
        (runtimeStatus === 'attention' ? 'Needs attention' : 'Agent is offline')
      : null
    return row(
      'needs',
      failureReason ? `${failureReason} — a question is waiting` : 'Waiting for your answer',
      replyExcerpt,
      activitySinceMs ?? replyAtMs,
      4,
    )
  }

  // A finished turn the user has not looked at yet is the inbox's core case for
  // manual user Chats: the work is done and the result is sitting unread.
  // Workspace orchestrator/reviewer sessions report to the Task Graph rather
  // than waiting on the user to open their internal Chat tab.
  if (tab.is_unread && runtimeStatus !== 'working' && isManualSessionTab(tab)) {
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
  if (scope === 'workspace') return !isManualSessionTab(tab)
  return isManualSessionTab(tab)
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

export interface InboxWorkspaceSection {
  key: string
  /** Empty for rows that belong to no workspace (manual sessions). */
  title: string
  rows: InboxRow[]
}

/**
 * Split one bucket's rows by owning workspace.
 *
 * The Workspace Agents view keeps what the old floating panel showed: a
 * workspace-attributed Chat (including a sub-agent started from another Chat)
 * must be readable as "this belongs to Repo", not as a loose row. Ordering is
 * stable — workspaces by first appearance, rows in their bucket order — so the
 * header does not jump while statuses poll.
 */
export function groupRowsByWorkspace(rows: readonly InboxRow[]): InboxWorkspaceSection[] {
  const sections: InboxWorkspaceSection[] = []
  const byKey = new Map<string, InboxWorkspaceSection>()
  for (const row of rows) {
    const key = row.tab.workspace_id || ''
    let section = byKey.get(key)
    if (!section) {
      section = {
        key,
        title: key ? row.tab.workspace_name?.trim() || 'Workspace' : '',
        rows: [],
      }
      byKey.set(key, section)
      sections.push(section)
    }
    section.rows.push(row)
  }
  return sections
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

export type InboxKeyAction = 'move-down' | 'move-up' | 'expand' | 'collapse' | 'open' | 'close'

export interface InboxKeyInput {
  key: string
  /** The key came from one of the panel's own buttons. */
  fromButton: boolean
  /** The key came from a text field (reply input or any future editor). */
  fromTextEntry: boolean
  /** There is a row under the keyboard cursor to act on. */
  hasRow: boolean
}

/**
 * Resolve one key press to a panel action, or null to let the browser have it.
 *
 * The panel is a container of real controls — filters, refresh, the done
 * toggle, per-row expand, and the inline reply field. A container-level
 * keydown must never swallow what those controls do natively: Enter on a
 * button or in the reply field belongs to the control, and arrow keys inside a
 * text field belong to the caret. Only keys pressed on the panel itself (or on
 * non-interactive text such as a group header) drive the row cursor.
 *
 * Escape is the one exception: it closes the surface from anywhere inside it.
 */
export function resolveInboxKey(input: InboxKeyInput): InboxKeyAction | null {
  if (input.key === 'Escape') return 'close'
  if (input.fromTextEntry) return null
  if (input.key === 'Enter') return input.fromButton || !input.hasRow ? null : 'open'
  if (input.key === 'ArrowDown') return 'move-down'
  if (input.key === 'ArrowUp') return 'move-up'
  if (input.key === 'ArrowRight') return input.hasRow ? 'expand' : null
  if (input.key === 'ArrowLeft') return input.hasRow ? 'collapse' : null
  return null
}

/** Whether a keydown target is one of the panel's own interactive controls. */
export function keyTargetKind(target: unknown, container: unknown): {
  fromButton: boolean
  fromTextEntry: boolean
} {
  if (!target || target === container) return { fromButton: false, fromTextEntry: false }
  const element = target as {
    tagName?: string
    isContentEditable?: boolean
  }
  const tag = (element.tagName ?? '').toUpperCase()
  if (tag === 'BUTTON') return { fromButton: true, fromTextEntry: false }
  const textEntry =
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    element.isContentEditable === true
  return { fromButton: false, fromTextEntry: textEntry }
}
