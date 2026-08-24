import { ref, type Ref } from 'vue'

/**
 * Bounded connecting-state management for terminal iframes.
 *
 * Each cached tab's iframe starts in the 'connecting' state. If it fires its
 * load event within CONNECTING_TIMEOUT_MS it moves to 'loaded' (overlay
 * hidden). If the timeout fires first it moves to 'timeout' (slow — the
 * iframe may still load, so we show "Taking longer than expected" and a
 * Retry action). If the iframe fires an error event it moves to 'error'
 * (proven failure — "Terminal failed to connect" + Retry).
 *
 * This module owns only the state sets + timers; the consumer (TerminalView)
 * wires them to the iframe lifecycle (load/error handlers, cache/evict,
 * unmount). The xterm canvas / palette are never touched.
 */

/** Time before a still-loading iframe is considered slow (not failed). */
export const CONNECTING_TIMEOUT_MS = 8000

export type TerminalConnectingState = 'connecting' | 'timeout' | 'error' | 'loaded'

export interface TerminalConnectingApi {
  loadedTabIds: Ref<Set<string>>
  timeoutTabIds: Ref<Set<string>>
  errorTabIds: Ref<Set<string>>
  /** Arm the connecting timeout for a tab. No-op if already loaded/errored/timed-out. */
  startConnectingTimer: (tabId: string) => void
  /** Cancel a pending connecting timeout for a tab. */
  clearConnectingTimer: (tabId: string) => void
  /** Clear timer + loaded/timeout/error state for a tab (used on eviction / retry). */
  resetTabConnectingState: (tabId: string) => void
  /** Mark a tab as loaded (called from the iframe load handler). */
  markLoaded: (tabId: string) => void
  /** Mark a tab as a hard failure (called from the iframe error handler). */
  markError: (tabId: string) => void
  /** Reset state and reload the tab's iframe via the provided reload callback. */
  retryTab: (tabId: string) => void
  /** Cancel all pending timers (called on unmount). */
  clearAllTimers: () => void
}

/**
 * @param reloadIframe callback that reloads the DOM iframe for a tab (the
 *   composable does not touch the DOM directly so it stays testable).
 */
export function useTerminalConnecting(
  reloadIframe: (tabId: string) => void,
): TerminalConnectingApi {
  const loadedTabIds = ref<Set<string>>(new Set())
  const timeoutTabIds = ref<Set<string>>(new Set())
  const errorTabIds = ref<Set<string>>(new Set())
  const connectingTimers = new Map<string, number>()

  function clearConnectingTimer(tabId: string) {
    const handle = connectingTimers.get(tabId)
    if (handle !== undefined) {
      clearTimeout(handle)
      connectingTimers.delete(tabId)
    }
  }

  function startConnectingTimer(tabId: string) {
    if (
      loadedTabIds.value.has(tabId) ||
      timeoutTabIds.value.has(tabId) ||
      errorTabIds.value.has(tabId)
    ) {
      return
    }
    clearConnectingTimer(tabId)
    const handle = setTimeout(() => {
      connectingTimers.delete(tabId)
      if (loadedTabIds.value.has(tabId) || errorTabIds.value.has(tabId)) return
      if (!timeoutTabIds.value.has(tabId)) {
        timeoutTabIds.value = new Set(timeoutTabIds.value).add(tabId)
      }
    }, CONNECTING_TIMEOUT_MS)
    connectingTimers.set(tabId, handle)
  }

  function resetTabConnectingState(tabId: string) {
    clearConnectingTimer(tabId)
    if (loadedTabIds.value.has(tabId)) {
      const next = new Set(loadedTabIds.value)
      next.delete(tabId)
      loadedTabIds.value = next
    }
    if (timeoutTabIds.value.has(tabId)) {
      const next = new Set(timeoutTabIds.value)
      next.delete(tabId)
      timeoutTabIds.value = next
    }
    if (errorTabIds.value.has(tabId)) {
      const next = new Set(errorTabIds.value)
      next.delete(tabId)
      errorTabIds.value = next
    }
  }

  function markLoaded(tabId: string) {
    clearConnectingTimer(tabId)
    if (!loadedTabIds.value.has(tabId)) {
      loadedTabIds.value = new Set(loadedTabIds.value).add(tabId)
    }
    if (timeoutTabIds.value.has(tabId)) {
      const next = new Set(timeoutTabIds.value)
      next.delete(tabId)
      timeoutTabIds.value = next
    }
    if (errorTabIds.value.has(tabId)) {
      const next = new Set(errorTabIds.value)
      next.delete(tabId)
      errorTabIds.value = next
    }
  }

  function markError(tabId: string) {
    clearConnectingTimer(tabId)
    if (timeoutTabIds.value.has(tabId)) {
      const next = new Set(timeoutTabIds.value)
      next.delete(tabId)
      timeoutTabIds.value = next
    }
    if (!errorTabIds.value.has(tabId)) {
      errorTabIds.value = new Set(errorTabIds.value).add(tabId)
    }
  }

  function retryTab(tabId: string) {
    resetTabConnectingState(tabId)
    reloadIframe(tabId)
    startConnectingTimer(tabId)
  }

  function clearAllTimers() {
    connectingTimers.forEach(handle => clearTimeout(handle))
    connectingTimers.clear()
  }

  return {
    loadedTabIds,
    timeoutTabIds,
    errorTabIds,
    startConnectingTimer,
    clearConnectingTimer,
    resetTabConnectingState,
    markLoaded,
    markError,
    retryTab,
    clearAllTimers,
  }
}

/** Derive the connecting state for a tab from the loaded/timeout/error sets. */
export function getConnectingState(
  tabId: string,
  loaded: Set<string>,
  timeout: Set<string>,
  errored: Set<string>,
): TerminalConnectingState {
  if (loaded.has(tabId)) return 'loaded'
  if (errored.has(tabId)) return 'error'
  if (timeout.has(tabId)) return 'timeout'
  return 'connecting'
}
