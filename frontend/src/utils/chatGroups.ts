import type { TerminalAgentStatus, TerminalTab } from '@/types'

export type ChatStatusBucket = 'needs_you' | 'working' | 'review' | 'done'

const STATUS_BUCKET_META: readonly { key: ChatStatusBucket; label: string }[] = [
  { key: 'needs_you', label: 'Needs you' },
  { key: 'working', label: 'Working' },
  { key: 'review', label: 'Review' },
  { key: 'done', label: 'Done' },
]

export interface ChatCwdGroup {
  /** Raw cwd path; the empty-cwd sentinel is the literal 'No directory'. */
  cwd: string
  tabs: TerminalTab[]
}

function cwdKey(tab: Pick<TerminalTab, 'cwd'>): string {
  return tab.cwd || 'No directory'
}

/**
 * Group chat tabs by their working directory.
 *
 * Preserve the server's persisted order, both within each directory and for
 * the first appearance of each directory. Sorting by creation time here would
 * undo every user reorder as soon as the computed runs.
 */
export function groupChatsByCwd(
  tabs: TerminalTab[],
  preferredCwds: readonly string[] = [],
): ChatCwdGroup[] {
  const byCwd = new Map<string, TerminalTab[]>()
  for (const tab of tabs) {
    const cwd = cwdKey(tab)
    const bucket = byCwd.get(cwd)
    if (bucket) {
      bucket.push(tab)
    } else {
      byCwd.set(cwd, [tab])
    }
  }

  const orderedCwds: string[] = []
  for (const cwd of preferredCwds) {
    if (byCwd.has(cwd) && !orderedCwds.includes(cwd)) orderedCwds.push(cwd)
  }
  for (const cwd of byCwd.keys()) {
    if (!orderedCwds.includes(cwd)) orderedCwds.push(cwd)
  }
  return orderedCwds.map(cwd => ({ cwd, tabs: byCwd.get(cwd)! }))
}

export const CHAT_GROUP_PREVIEW_SIZE = 6

export interface ChatSidebarGroup extends ChatCwdGroup {
  key: string
  label?: string
  statusBucket?: ChatStatusBucket
  pinned: boolean
  collapsed: boolean
  expanded: boolean
  visibleTabs: TerminalTab[]
  hiddenCount: number
}

export function classifyChatStatusBucket(
  tab: TerminalTab,
  status?: TerminalAgentStatus,
): ChatStatusBucket {
  if (status?.pending_question) return 'needs_you'
  const runtime = status?.status ?? (tab.session_kind === 'chat' ? 'offline' : tab.is_active ? 'idle' : 'offline')
  if (runtime === 'working') return 'working'
  if (tab.is_unread || runtime === 'attention') return 'needs_you'
  if (status?.review_state || status?.review_label) return 'review'
  return 'done'
}

function activityTimestampMs(tab: TerminalTab, status?: TerminalAgentStatus): number {
  const raw =
    status?.last_activity_at ||
    status?.last_reply_at ||
    status?.activity_since ||
    tab.created_at
  if (!raw) return 0
  const parsed = Date.parse(raw)
  return Number.isNaN(parsed) ? 0 : parsed
}

/** Group chats into Pinned + Needs you / Working / Review / Done status buckets. */
export function buildChatSidebarStatusGroups(
  tabs: TerminalTab[],
  pinnedIds: ReadonlySet<string>,
  statusesByTabId: Readonly<Record<string, TerminalAgentStatus | undefined>>,
  options: {
    query: string
    activeTabId: string | null
    collapsed: ReadonlySet<string>
    expanded: ReadonlySet<string>
    pinnedCollapsed?: boolean
  },
): ChatSidebarGroup[] {
  const query = options.query.trim().toLowerCase()
  const matches = (tab: TerminalTab) => !query ||
    (tab.name || '').toLowerCase().includes(query) ||
    (tab.cwd || '').toLowerCase().includes(query)

  const groups: ChatSidebarGroup[] = []
  const pinned = tabs.filter(tab => pinnedIds.has(tab.id) && matches(tab))
  if (pinned.length) {
    const collapsed = !query && Boolean(options.pinnedCollapsed)
    const active = pinned.find(tab => tab.id === options.activeTabId)
    const visibleTabs = collapsed ? (active ? [active] : []) : pinned
    groups.push({
      key: 'pinned',
      cwd: '',
      label: 'Pinned',
      pinned: true,
      tabs: pinned,
      visibleTabs,
      collapsed,
      expanded: true,
      hiddenCount: pinned.length - visibleTabs.length,
    })
  }

  const buckets: Record<ChatStatusBucket, TerminalTab[]> = {
    needs_you: [],
    working: [],
    review: [],
    done: [],
  }
  for (const tab of tabs) {
    if (pinnedIds.has(tab.id) || !matches(tab)) continue
    const bucket = classifyChatStatusBucket(tab, statusesByTabId[tab.id])
    buckets[bucket].push(tab)
  }

  for (const meta of STATUS_BUCKET_META) {
    const bucketTabs = buckets[meta.key]
    if (!bucketTabs.length) continue
    bucketTabs.sort(
      (a, b) =>
        activityTimestampMs(b, statusesByTabId[b.id]) -
        activityTimestampMs(a, statusesByTabId[a.id]),
    )
    const groupId = `status:${meta.key}`
    const collapsed = !query && options.collapsed.has(groupId)
    const expanded = Boolean(query) || meta.key !== 'done' || options.expanded.has(groupId)
    const active = bucketTabs.find(tab => tab.id === options.activeTabId)
    const previewCap = meta.key === 'done' ? CHAT_GROUP_PREVIEW_SIZE : bucketTabs.length
    let visibleTabs = collapsed
      ? []
      : expanded
        ? bucketTabs
        : bucketTabs.slice(0, previewCap)
    if (active && !visibleTabs.includes(active)) {
      visibleTabs = collapsed
        ? [active]
        : [...visibleTabs.slice(0, Math.max(0, previewCap - 1)), active]
    }
    groups.push({
      key: groupId,
      cwd: groupId,
      label: meta.label,
      statusBucket: meta.key,
      pinned: false,
      tabs: bucketTabs,
      visibleTabs,
      collapsed,
      expanded,
      hiddenCount: bucketTabs.length - visibleTabs.length,
    })
  }
  return groups
}

/** Search the full collection before applying directory folds or row limits. */
export function buildChatSidebarGroups(
  tabs: TerminalTab[],
  pinnedIds: ReadonlySet<string>,
  options: {
    query: string
    activeTabId: string | null
    collapsed: ReadonlySet<string>
    expanded: ReadonlySet<string>
    pinnedCollapsed?: boolean
  },
): ChatSidebarGroup[] {
  const query = options.query.trim().toLowerCase()
  const matches = (tab: TerminalTab) => !query ||
    (tab.name || '').toLowerCase().includes(query) ||
    (tab.cwd || '').toLowerCase().includes(query)
  const groups: ChatSidebarGroup[] = []
  const pinned = tabs.filter(tab => pinnedIds.has(tab.id) && matches(tab))
  if (pinned.length) {
    const collapsed = !query && Boolean(options.pinnedCollapsed)
    const active = pinned.find(tab => tab.id === options.activeTabId)
    const visibleTabs = collapsed ? (active ? [active] : []) : pinned
    groups.push({
      key: 'pinned', cwd: '', pinned: true, tabs: pinned, visibleTabs,
      collapsed, expanded: true, hiddenCount: pinned.length - visibleTabs.length,
    })
  }
  for (const group of groupChatsByCwd(tabs.filter(tab => !pinnedIds.has(tab.id)))) {
    const matchingTabs = group.tabs.filter(matches)
    if (!matchingTabs.length) continue
    const collapsed = !query && options.collapsed.has(group.cwd)
    const expanded = Boolean(query) || options.expanded.has(group.cwd)
    const active = matchingTabs.find(tab => tab.id === options.activeTabId)
    let visibleTabs = collapsed ? [] : expanded
      ? matchingTabs
      : matchingTabs.slice(0, CHAT_GROUP_PREVIEW_SIZE)
    // Keep six rows at most, substituting the active row for the last preview
    // row when needed. A manually collapsed active directory retains that row.
    if (active && !visibleTabs.includes(active)) {
      visibleTabs = [...visibleTabs.slice(0, CHAT_GROUP_PREVIEW_SIZE - 1), active]
    }
    groups.push({
      key: `cwd:${group.cwd}`, cwd: group.cwd, tabs: matchingTabs, pinned: false,
      collapsed, expanded, visibleTabs, hiddenCount: matchingTabs.length - visibleTabs.length,
    })
  }
  return groups
}

/** Never interpret a cross-directory drag as changing a session's cwd. */
export function canDropChatInGroup(
  tab: TerminalTab,
  group: Pick<ChatSidebarGroup, 'pinned' | 'cwd' | 'statusBucket'>,
): boolean {
  if (group.statusBucket && !group.pinned) return false
  return !tab.workspace_id && tab.session_kind === 'chat' &&
    (group.pinned || (tab.cwd || 'No directory') === group.cwd)
}

/** Resolve positions at drop time so filtered lists and concurrent inserts are safe. */
export function moveTabById<T extends { id: string }>(
  tabs: T[], sourceId: string, targetId: string, position: 'before' | 'after',
): T[] {
  const source = tabs.find(tab => tab.id === sourceId)
  if (!source || sourceId === targetId || !tabs.some(tab => tab.id === targetId)) return tabs
  const next = tabs.filter(tab => tab.id !== sourceId)
  const targetIndex = next.findIndex(tab => tab.id === targetId)
  next.splice(targetIndex + (position === 'after' ? 1 : 0), 0, source)
  return next.every((tab, index) => tab === tabs[index]) ? tabs : next
}

/** Move every manual tab in one cwd as a block relative to another cwd. */
export function moveCwdGroup<T extends Pick<TerminalTab, 'cwd'>>(
  tabs: T[], sourceCwd: string, targetCwd: string, position: 'before' | 'after',
): T[] {
  if (sourceCwd === targetCwd) return tabs
  const groups = new Map<string, T[]>()
  for (const tab of tabs) {
    const cwd = cwdKey(tab)
    const group = groups.get(cwd)
    if (group) group.push(tab)
    else groups.set(cwd, [tab])
  }
  if (!groups.has(sourceCwd) || !groups.has(targetCwd)) return tabs

  const orderedCwds = [...groups.keys()].filter(cwd => cwd !== sourceCwd)
  const targetIndex = orderedCwds.indexOf(targetCwd)
  orderedCwds.splice(targetIndex + (position === 'after' ? 1 : 0), 0, sourceCwd)
  const next = orderedCwds.flatMap(cwd => groups.get(cwd)!)
  return next.every((tab, index) => tab === tabs[index]) ? tabs : next
}

export function parsePinnedChatIds(raw: string | null): Set<string> {
  try {
    const value: unknown = JSON.parse(raw || '[]')
    return new Set(Array.isArray(value)
      ? value.filter((id): id is string => typeof id === 'string' && id.length > 0)
      : [])
  } catch {
    return new Set()
  }
}

/** Short display label for a cwd path: the basename, or the sentinel itself. */
export function cwdLabel(cwd: string): string {
  if (!cwd || cwd === 'No directory') return 'No directory'
  const trimmed = cwd.replace(/\/+$/, '')
  const slash = trimmed.lastIndexOf('/')
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}
