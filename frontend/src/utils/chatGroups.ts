import type { TerminalTab } from '@/types'

export interface ChatCwdGroup {
  /** Raw cwd path; the empty-cwd sentinel is the literal 'No directory'. */
  cwd: string
  tabs: TerminalTab[]
}

/**
 * Group chat tabs by their working directory.
 *
 * Preserve the server's persisted order, both within each directory and for
 * the first appearance of each directory. Sorting by creation time here would
 * undo every user reorder as soon as the computed runs.
 */
export function groupChatsByCwd(tabs: TerminalTab[]): ChatCwdGroup[] {
  const byCwd = new Map<string, TerminalTab[]>()
  for (const tab of tabs) {
    const cwd = tab.cwd || 'No directory'
    const bucket = byCwd.get(cwd)
    if (bucket) {
      bucket.push(tab)
    } else {
      byCwd.set(cwd, [tab])
    }
  }

  return [...byCwd].map(([cwd, groupTabs]) => ({ cwd, tabs: groupTabs }))
}

export const CHAT_GROUP_PREVIEW_SIZE = 6

export interface ChatSidebarGroup extends ChatCwdGroup {
  key: string
  pinned: boolean
  collapsed: boolean
  expanded: boolean
  visibleTabs: TerminalTab[]
  hiddenCount: number
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
  },
): ChatSidebarGroup[] {
  const query = options.query.trim().toLowerCase()
  const matches = (tab: TerminalTab) => !query ||
    (tab.name || '').toLowerCase().includes(query) ||
    (tab.cwd || '').toLowerCase().includes(query)
  const groups: ChatSidebarGroup[] = []
  const pinned = tabs.filter(tab => pinnedIds.has(tab.id) && matches(tab))
  if (pinned.length) {
    groups.push({
      key: 'pinned', cwd: '', pinned: true, tabs: pinned, visibleTabs: pinned,
      collapsed: false, expanded: true, hiddenCount: 0,
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
  group: Pick<ChatSidebarGroup, 'pinned' | 'cwd'>,
): boolean {
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
