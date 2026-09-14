import type { TerminalTab } from '@/types'

export interface ChatCwdGroup {
  /** Raw cwd path; the empty-cwd sentinel is the literal 'No directory'. */
  cwd: string
  tabs: TerminalTab[]
}

/**
 * Group chat tabs by their working directory.
 *
 * Tabs with no cwd are grouped under the 'No directory' sentinel. Within each
 * group the tabs are sorted by `created_at` descending (newest first), and the
 * groups themselves are ordered by their most recent tab's `created_at`
 * descending. Pure function — no side effects — so it can be unit tested and
 * memoized by a computed.
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

  const groups: ChatCwdGroup[] = []
  for (const [cwd, groupTabs] of byCwd) {
    const sorted = [...groupTabs].sort((a, b) =>
      (b.created_at || '').localeCompare(a.created_at || '')
    )
    groups.push({ cwd, tabs: sorted })
  }

  groups.sort((a, b) =>
    (b.tabs[0]?.created_at || '').localeCompare(a.tabs[0]?.created_at || '')
  )
  return groups
}

/** Short display label for a cwd path: the basename, or the sentinel itself. */
export function cwdLabel(cwd: string): string {
  if (!cwd || cwd === 'No directory') return 'No directory'
  const trimmed = cwd.replace(/\/+$/, '')
  const slash = trimmed.lastIndexOf('/')
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}
