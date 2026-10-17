import type { Ref } from 'vue'
import type { TerminalTab } from '@/types'
import {
  createPaneReconnectRegistry,
  type PaneReconnectStatus,
} from '@/utils/terminalReconnect'

export function createPaneReconnectRuntime(tabs: Ref<TerminalTab[]>) {
  const registry = createPaneReconnectRegistry()

  function paneReconnectStatus(tabId: string): PaneReconnectStatus {
    return registry.get(tabId)
  }

  function requestPaneReconnect(tabId: string): boolean {
    if (!tabId || !tabs.value.some(tab => tab.id === tabId)) return false
    return !registry.request(tabId).deduped
  }

  function settlePaneReconnect(
    tabId: string,
    state: 'success' | 'error',
    nonce?: number,
  ): boolean {
    return registry.settle(tabId, state, nonce)
  }

  function clearPaneReconnect(tabId: string, nonce?: number): void {
    registry.clear(tabId, nonce)
  }

  return {
    paneReconnectStatus,
    requestPaneReconnect,
    settlePaneReconnect,
    clearPaneReconnect,
  }
}
