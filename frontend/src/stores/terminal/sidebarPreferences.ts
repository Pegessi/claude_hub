import type { ComputedRef, Ref } from 'vue'
import type { StoreNotification, TerminalTab } from '@/types'
import { parsePinnedChatIds } from '@/utils/chatGroups'
import { API_BASE, STORAGE_KEY_PINNED_CHATS } from './helpers'

interface SidebarPreferencesRuntime {
  chatTabs: ComputedRef<TerminalTab[]>
  pinnedChatIds: Ref<Set<string>>
  pushNotification: (notification: Omit<StoreNotification, 'id'>) => void
}

export function createSidebarPreferences(runtime: SidebarPreferencesRuntime) {
  const { chatTabs, pinnedChatIds, pushNotification } = runtime
  let pinMutationTail: Promise<unknown> = Promise.resolve()
  let requestSequence = 0
  let appliedSequence = 0
  let fetchPromise: Promise<void> | null = null
  let legacyPinsMigrated = false
  let failureNotified = false

  function applyPinnedChatIds(value: unknown, sequence: number) {
    if (sequence < appliedSequence) return
    appliedSequence = sequence
    const ids = Array.isArray(value) ? value : []
    pinnedChatIds.value = new Set(ids.filter(
      (id): id is string => typeof id === 'string' && id.length > 0,
    ))
  }

  function setChatPinned(tabId: string, pinned: boolean): Promise<boolean> {
    if (pinned && !chatTabs.value.some(tab => tab.id === tabId)) {
      return Promise.resolve(false)
    }
    const precedingSync = fetchPromise
    const mutate = async () => {
      if (precedingSync) await precedingSync
      const sequence = ++requestSequence
      try {
        const response = await fetch(`${API_BASE}/tabs/sidebar-preferences/pins/${tabId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pinned }),
        })
        if (!response.ok) throw new Error('Failed to save pin preference')
        const payload: { pinned_tab_ids?: unknown } = await response.json()
        applyPinnedChatIds(payload.pinned_tab_ids, sequence)
        return true
      } catch (error) {
        pushNotification({
          type: 'warning',
          message: error instanceof Error ? error.message : 'Pin preference could not be saved on the server.',
          autoDismissMs: 6000,
        })
        return false
      }
    }
    const result = pinMutationTail.then(mutate, mutate)
    pinMutationTail = result
    return result
  }

  function sync(): Promise<void> {
    if (fetchPromise) return fetchPromise
    const run = async () => {
      await pinMutationTail
      const sequence = ++requestSequence
      try {
        let response: Response
        let legacyPinnedIds: string[] = []
        if (!legacyPinsMigrated) {
          try {
            legacyPinnedIds = [...parsePinnedChatIds(localStorage.getItem(STORAGE_KEY_PINNED_CHATS))]
          } catch {
            // Storage can be disabled; an empty migration still establishes server ownership.
          }
          response = await fetch(`${API_BASE}/tabs/sidebar-preferences/migrate-browser-pins`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned_tab_ids: legacyPinnedIds }),
          })
        } else {
          response = await fetch(`${API_BASE}/tabs/sidebar-preferences`)
        }
        if (!response.ok) throw new Error('Failed to load server pin preferences')
        const payload: { pinned_tab_ids?: unknown } = await response.json()
        applyPinnedChatIds(payload.pinned_tab_ids, sequence)
        if (!legacyPinsMigrated) {
          try {
            localStorage.removeItem(STORAGE_KEY_PINNED_CHATS)
          } catch {
            // The server migration is durable even if browser cleanup is blocked.
          }
          legacyPinsMigrated = true
        }
        failureNotified = false
      } catch (error) {
        if (!failureNotified) {
          pushNotification({
            type: 'warning',
            message: error instanceof Error ? error.message : 'Failed to load server pin preferences',
            autoDismissMs: 6000,
          })
          failureNotified = true
        }
      }
    }
    fetchPromise = run().finally(() => {
      fetchPromise = null
    })
    return fetchPromise
  }

  return { setChatPinned, sync }
}
