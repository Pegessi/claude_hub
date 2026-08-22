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
  /**
   * Optional request-correlation nonce. When present, the terminal iframe
   * records it in `__claudeHubLastFitNonce` / `__claudeHubLastScrollNonce`
   * once the corresponding operation completes. The benchmark sends a unique
   * nonce per request and waits for the matching nonce — proving the
   * *current* request ran, not a delayed unrelated fit/scroll.
   */
  nonce?: string
}

/**
 * A minimal animation-frame scheduler interface so the scheduling logic can
 * be tested without a real browser (node --test provides no rAF).
 */
export interface RafScheduler {
  requestAnimationFrame: (cb: () => void) => number
  cancelAnimationFrame: (id: number) => void
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
 * Generate a short unique nonce for request-correlation.
 * Uses Math.random + timestamp; sufficient for distinguishing one
 * mode-return dispatch from another within the same session.
 */
function makeNonce(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * Dispatch terminal-resize and terminal-scroll-bottom messages to every
 * iframe whose tab ID is assigned to a currently visible pane.
 *
 * Cached/hidden iframes (tabs not in any pane) are skipped because their
 * containers are not laid out and a fit there would be a no-op or race
 * against a stale size.
 *
 * Each message carries a unique `nonce`. The terminal records the nonce in
 * `__claudeHubLastFitNonce` / `__claudeHubLastScrollNonce` once the
 * operation completes, so callers (notably the benchmark) can prove the
 * *current* request ran. The dispatched nonces are also stored on
 * `window.__claudeHubTerminalReturnNonces` keyed by tab ID so the benchmark
 * can read them after triggering a mode switch.
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
  const nonces: Record<string, { fit: string; scroll: string }> = {}
  for (const tabId of visible) {
    const iframe = iframes[tabId]
    if (!iframe || !iframe.contentWindow) continue
    const fitNonce = makeNonce()
    const scrollNonce = makeNonce()
    nonces[tabId] = { fit: fitNonce, scroll: scrollNonce }
    const resizeMsg: TerminalReturnMessage = { type: 'terminal-resize', tabId, nonce: fitNonce }
    const scrollMsg: TerminalReturnMessage = { type: 'terminal-scroll-bottom', tabId, nonce: scrollNonce }
    if (postMessage) {
      postMessage(iframe, resizeMsg)
      postMessage(iframe, scrollMsg)
    } else {
      iframe.contentWindow.postMessage(resizeMsg, '*')
      iframe.contentWindow.postMessage(scrollMsg, '*')
    }
    dispatched.push(resizeMsg, scrollMsg)
  }
  // Expose nonces to the benchmark so it can wait for the matching
  // __claudeHubLastFitNonce / __claudeHubLastScrollNonce in each iframe.
  if (typeof window !== 'undefined') {
    ;(window as unknown as { __claudeHubTerminalReturnNonces?: Record<string, { fit: string; scroll: string }> }).__claudeHubTerminalReturnNonces = nonces
  }
  return dispatched
}

// Module-level handle for the currently pending terminal-return resize
// callback. Only one such callback can be pending at a time — scheduling a
// new one cancels the previous. This mirrors how App.vue's mode watcher
// cancels the previous cleanup before scheduling a new dispatch.
let pendingTerminalReturnRafId: number | null = null
let pendingTerminalReturnCancel: (() => void) | null = null

/**
 * Schedule a deferred terminal-return resize dispatch.
 *
 * The dispatch is deferred to the next animation frame so the terminal
 * shell has left its absolute-positioned hidden state and its layout box
 * is final before iframes are asked to fit.
 *
 * To guard against stale callbacks (the user rapidly toggles away from
 * terminal mode before the frame fires), this function:
 *   1. cancels any previously scheduled callback that hasn't fired yet;
 *   2. re-checks `getMode()` inside the callback and skips the dispatch
 *      if the mode is no longer 'terminal'.
 *
 * @param getMode - returns the current app mode at callback time
 * @param panes - the current visible panes (captured at schedule time)
 * @param iframes - map of tab ID -> iframe element
 * @param scheduler - rAF implementation (defaults to the global one)
 * @returns a cleanup function that cancels the pending callback
 */
export function scheduleTerminalReturnResize(
  getMode: () => string,
  panes: PaneLike[],
  iframes: Record<string, IframeLike | null>,
  scheduler: RafScheduler = {
    requestAnimationFrame: globalThis.requestAnimationFrame?.bind(globalThis) as (cb: () => void) => number,
    cancelAnimationFrame: globalThis.cancelAnimationFrame?.bind(globalThis) as (id: number) => void,
  },
): () => void {
  // Cancel any previously scheduled callback from an earlier mode change
  // that hasn't fired yet.
  if (pendingTerminalReturnCancel) {
    pendingTerminalReturnCancel()
    pendingTerminalReturnCancel = null
  }

  pendingTerminalReturnRafId = scheduler.requestAnimationFrame(() => {
    pendingTerminalReturnRafId = null
    pendingTerminalReturnCancel = null
    // Stale-callback guard: if the mode has already left terminal by the
    // time this frame fires, skip the dispatch entirely.
    if (getMode() !== 'terminal') return
    dispatchTerminalReturnResize(panes, iframes)
  })

  const cancel = () => {
    if (pendingTerminalReturnRafId !== null) {
      scheduler.cancelAnimationFrame(pendingTerminalReturnRafId)
      pendingTerminalReturnRafId = null
    }
    pendingTerminalReturnCancel = null
  }

  pendingTerminalReturnCancel = cancel
  return cancel
}
