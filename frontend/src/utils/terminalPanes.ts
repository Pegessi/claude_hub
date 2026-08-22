/**
 * Terminal pane visibility helpers.
 *
 * When the user switches back to Terminal mode, we only need to re-fit the
 * iframes whose tab is currently assigned to a visible pane. Cached iframes
 * for tabs that are not in any pane are hidden and their containers are not
 * laid out, so resizing them is a no-op (or worse, races against a stale
 * size). These helpers isolate the "which tab IDs are visible" logic so it
 * can be unit-tested for 1x1 and split layouts.
 */

export interface PaneLike {
  tabId: string | null
}

/**
 * Return the set of tab IDs currently assigned to a visible pane.
 *
 * In a 1x1 layout this is a single tab ID (or empty if the pane has no tab).
 * In a split layout this is the union of all pane tab IDs. Null tab IDs
 * (empty panes) are skipped.
 */
export function visiblePaneTabIds(panes: PaneLike[]): Set<string> {
  const ids = new Set<string>()
  for (const pane of panes) {
    const tabId = pane.tabId
    if (typeof tabId === 'string' && tabId !== null) {
      ids.add(tabId)
    }
  }
  return ids
}

/**
 * Given a map of all registered iframes and the current visible panes,
 * return only the iframes that belong to a visible pane.
 */
export function visibleIframes<T>(
  iframes: Record<string, T | null>,
  panes: PaneLike[],
): Record<string, T> {
  const visible = visiblePaneTabIds(panes)
  const result: Record<string, T> = {}
  for (const tabId of visible) {
    const iframe = iframes[tabId]
    if (iframe !== null && iframe !== undefined) {
      result[tabId] = iframe
    }
  }
  return result
}
