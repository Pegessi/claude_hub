import { reactive } from 'vue'

/**
 * Per-tab bookkeeping for the manual Terminal-pane reconnect button.
 *
 * This registry is intentionally transport-agnostic: it owns only the
 * request/settle state machine and the in-flight dedup guarantee. The actual
 * re-attach is performed by TerminalView (iframe reload → ttyd re-attaches the
 * same tmux session), which watches `nonce` and settles the request once the
 * iframe finishes loading (or fails/times out).
 *
 * State machine per tab:
 *   idle ──request()──▶ connecting ──settle(true)──▶ success ──(auto clear)──▶ idle
 *                           │
 *                           └──settle(false)──▶ error ──(auto clear)──▶ idle
 *
 * A second request while `connecting` is coalesced (same nonce, deduped=true)
 * so repeated clicks cannot trigger concurrent reconnects of the same pane.
 */
export type PaneReconnectState = 'idle' | 'connecting' | 'success' | 'error'

export interface PaneReconnectStatus {
  state: PaneReconnectState
  /** Monotonic per-tab counter; bumps on every accepted (non-deduped) request. */
  nonce: number
}

export interface PaneReconnectRequest {
  nonce: number
  /** True when an in-flight reconnect for this tab absorbed this request. */
  deduped: boolean
}

const IDLE_STATUS: PaneReconnectStatus = Object.freeze({ state: 'idle', nonce: 0 })

export interface PaneReconnectRegistryApi {
  get(tabId: string): PaneReconnectStatus
  isConnecting(tabId: string): boolean
  request(tabId: string): PaneReconnectRequest
  settle(tabId: string, state: 'success' | 'error', nonce?: number): boolean
  clear(tabId: string, nonce?: number): void
}

export class PaneReconnectRegistry implements PaneReconnectRegistryApi {
  private readonly statuses = new Map<string, PaneReconnectStatus>()

  get(tabId: string): PaneReconnectStatus {
    return this.statuses.get(tabId) ?? IDLE_STATUS
  }

  isConnecting(tabId: string): boolean {
    return this.get(tabId).state === 'connecting'
  }

  /**
   * Register a manual reconnect request for a tab. Returns the request nonce
   * and whether the request was coalesced onto an in-flight reconnect.
   */
  request(tabId: string): PaneReconnectRequest {
    const current = this.statuses.get(tabId)
    if (current?.state === 'connecting') {
      return { nonce: current.nonce, deduped: true }
    }
    const nonce = (current?.nonce ?? 0) + 1
    this.statuses.set(tabId, { state: 'connecting', nonce })
    return { nonce, deduped: false }
  }

  /**
   * Settle an in-flight request. A stale settle (nonce from an older request,
   * or no request at all) is ignored so a late failure cannot overwrite a
   * newer attempt's status.
   */
  settle(tabId: string, state: 'success' | 'error', nonce?: number): boolean {
    const current = this.statuses.get(tabId)
    if (!current) return false
    if (nonce !== undefined && current.nonce !== nonce) return false
    if (current.state !== 'connecting') return false
    current.state = state
    return true
  }

  /** Return a tab to idle, ignoring stale clears from an older request. */
  clear(tabId: string, nonce?: number): void {
    const current = this.statuses.get(tabId)
    if (!current) return
    if (nonce !== undefined && current.nonce !== nonce) return
    current.state = 'idle'
  }
}

/** Vue-reactive registry used by the terminal store. */
export function createPaneReconnectRegistry(): PaneReconnectRegistryApi {
  return reactive(new PaneReconnectRegistry())
}
