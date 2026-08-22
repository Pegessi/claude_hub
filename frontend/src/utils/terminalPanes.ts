/**
 * Terminal mode-return dispatch helpers.
 *
 * When the user switches back to Terminal mode, the parent frame must ask
 * every visible terminal iframe to re-fit and scroll to bottom. These
 * helpers isolate the dispatch logic (which tab IDs to target, which
 * messages to send) so it can be unit-tested for 1x1 and split layouts,
 * hidden-cache exclusion, and rapid re-toggle safety.
 */

export interface PaneLike {
  tabId: string | null
}

export interface IframeLike {
  contentWindow: { postMessage: (message: unknown, targetOrigin: string) => void } | null
}

export interface TerminalReturnMessage {
  type: 'terminal-resize' | 'terminal-scroll-bottom'
  tabId: string
}

/**
 * Return the set of tab IDs currently assigned to a visible pane.
 * Null tab IDs (empty panes) are skipped.
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
 * Dispatch terminal-resize and terminal-scroll-bottom messages to every
 * iframe whose tab ID is assigned to a currently visible pane.
 *
 * Cached/hidden iframes (tabs not in any pane) are skipped because their
 * containers are not laid out and a fit there would be a no-op or race
 * against a stale size.
 *
 * @param panes - the current visible panes
 * @param iframes - map of tab ID -> iframe element
 * @param postMessage - optional override for the postMessage call (used in tests)
 * @returns the list of messages that were dispatched
 */
export function dispatchTerminalReturnResize(
  panes: PaneLike[],
  iframes: Record<string, IframeLike | null>,
  postMessage?: (iframe: IframeLike, message: TerminalReturnMessage) => void,
): TerminalReturnMessage[] {
  const visible = visiblePaneTabIds(panes)
  const dispatched: TerminalReturnMessage[] = []
  for (const tabId of visible) {
    const iframe = iframes[tabId]
    if (!iframe || !iframe.contentWindow) continue
    const resizeMsg: TerminalReturnMessage = { type: 'terminal-resize', tabId }
    const scrollMsg: TerminalReturnMessage = { type: 'terminal-scroll-bottom', tabId }
    if (postMessage) {
      postMessage(iframe, resizeMsg)
      postMessage(iframe, scrollMsg)
    } else {
      iframe.contentWindow.postMessage(resizeMsg, '*')
      iframe.contentWindow.postMessage(scrollMsg, '*')
    }
    dispatched.push(resizeMsg, scrollMsg)
  }
  return dispatched
}
