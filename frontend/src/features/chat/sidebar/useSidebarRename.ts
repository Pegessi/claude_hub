import { nextTick, ref } from 'vue'
import type { TerminalTab } from '@/types'

/** Keep one edit/save owner even when a row is unmounted or another row is clicked. */
export function useSidebarRename(updateTab: (tabId: string, updates: { name: string }) => Promise<unknown>) {
  // Inline row rename (mirrors the TabBar's double-click rename).
  const renamingTabId = ref<string | null>(null)
  const renamingTabName = ref('')
  // Tab whose rename PUT is in flight, so a late Enter/blur cannot double-submit
  // or have one row's continuation cancel another row's rename.
  const savingRenameId = ref<string | null>(null)
  const renameInputRef = ref<HTMLInputElement | null>(null)

  function setRenameInputRef(el: unknown) {
    renameInputRef.value = el instanceof HTMLInputElement ? el : null
  }

  function startRename(tab: TerminalTab) {
    // A save is in flight on another row; ignore so its continuation cannot
    // unmount this input mid-edit (the window is one PUT, typically <100ms).
    if (savingRenameId.value) return
    renamingTabId.value = tab.id
    renamingTabName.value = tab.name
    nextTick(() => {
      renameInputRef.value?.focus()
      renameInputRef.value?.select()
    })
  }

  function cancelRename(tabId: string) {
    // Ignore Esc on a row that isn't actively renaming, or one whose save is
    // already in flight (the continuation will close it).
    if (renamingTabId.value !== tabId || savingRenameId.value) return
    renamingTabId.value = null
    renamingTabName.value = ''
  }

  async function handleRenameTab(tabId: string) {
    // A late blur/Enter from a different (already unmounted) row's input, or a
    // duplicate event while this row's PUT is in flight: do nothing.
    if (renamingTabId.value !== tabId || savingRenameId.value) return
    const name = renamingTabName.value.trim()
    // Blank name: just close without saving.
    if (!name) {
      renamingTabId.value = null
      renamingTabName.value = ''
      return
    }
    savingRenameId.value = tabId
    try {
      await updateTab(tabId, { name })
    } finally {
      if (renamingTabId.value === tabId) {
        renamingTabId.value = null
        renamingTabName.value = ''
      }
      savingRenameId.value = null
    }
  }

  return { renamingTabId, renamingTabName, setRenameInputRef, startRename, cancelRename, handleRenameTab }
}
