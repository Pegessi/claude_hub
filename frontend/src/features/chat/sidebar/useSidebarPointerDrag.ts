import { onUnmounted, ref, type Ref } from 'vue'
import { canDropChatInGroup, type ChatSidebarGroup } from '@/utils/chatGroups'
import type { TerminalTab } from '@/types'

interface SidebarDragDependencies {
  chatTabs: Readonly<Ref<TerminalTab[]>>
  filteredGroups: Readonly<Ref<ChatSidebarGroup[]>>
  filterText: Readonly<Ref<string>>
  renamingTabId: Readonly<Ref<string | null>>
  isMobile: Readonly<Ref<boolean>>
  selectMobileTab: (tabId: string) => void
  setActiveTab: (tabId: string) => void
  setChatPinned: (tabId: string, pinned: boolean) => void
  reorderTabById: (sourceId: string, targetId: string, position: 'before' | 'after') => void
  reorderChatWorkspace: (sourceCwd: string, targetCwd: string, position: 'before' | 'after') => void
  toggleGroup: (cwd: string) => void
}

/** Row and workspace drags share hit-testing, suppression and listener lifetime. */
export function useSidebarPointerDrag(dependencies: SidebarDragDependencies) {
  const { chatTabs, filteredGroups, filterText, renamingTabId, isMobile, selectMobileTab, setActiveTab,
    setChatPinned, reorderTabById, reorderChatWorkspace, toggleGroup } = dependencies
  const draggedTabId = ref<string | null>(null)
  const dropTarget = ref<{ id: string; position: 'before' | 'after' } | null>(null)
  const dropGroupKey = ref<string | null>(null)
  const dropOnPinZone = ref(false)
  const dropAtGroupStart = ref(false)
  const draggedGroupCwd = ref<string | null>(null)
  const groupDropTarget = ref<{ cwd: string; position: 'before' | 'after' } | null>(null)

  function clearDrag() {
    draggedTabId.value = null
    dropTarget.value = null
    dropGroupKey.value = null
    dropOnPinZone.value = false
    dropAtGroupStart.value = false
    document.body.classList.remove('chat-sidebar-dragging')
  }

  interface PointerDragState {
    pointerId: number
    sourceId: string
    startX: number
    startY: number
    moved: boolean
  }

  const POINTER_DRAG_THRESHOLD_PX = 5
  let pointerDrag: PointerDragState | null = null
  let suppressNextRowClick = false
  let groupPointerDrag: PointerDragState | null = null
  let suppressNextGroupClick = false
  let rowClickTimer: ReturnType<typeof window.setTimeout> | null = null
  let groupClickTimer: ReturnType<typeof window.setTimeout> | null = null

  function draggedForGroup(group: ChatSidebarGroup) {
    const source = chatTabs.value.find(tab => tab.id === draggedTabId.value)
    return source && canDropChatInGroup(source, group) ? source : null
  }

  function groupFromElement(element: Element | null): ChatSidebarGroup | null {
    const key = element?.closest<HTMLElement>('.chat-sidebar__group')?.dataset.groupKey
    return key ? filteredGroups.value.find(group => group.key === key) ?? null : null
  }

  function updatePointerDrop(clientX: number, clientY: number) {
    const target = document.elementFromPoint(clientX, clientY)
    dropTarget.value = null
    dropGroupKey.value = null
    dropOnPinZone.value = false
    dropAtGroupStart.value = false
    if (target?.closest('[data-pointer-drop="pin"]')) {
      dropOnPinZone.value = true
      return
    }
    const row = target?.closest<HTMLElement>('.chat-sidebar__item')
    const group = groupFromElement(row ?? target)
    const tabId = row?.dataset.tabId
    if (!group || !draggedForGroup(group)) return
    dropGroupKey.value = group.key
    if (!row || !tabId) {
      dropAtGroupStart.value = true
      return
    }
    if (tabId === draggedTabId.value) return
    const rect = row.getBoundingClientRect()
    dropTarget.value = { id: tabId, position: clientY < rect.top + rect.height / 2 ? 'before' : 'after' }
  }

  function onPointerMove(event: PointerEvent) {
    const drag = pointerDrag
    if (!drag || event.pointerId !== drag.pointerId) return
    if (!drag.moved) {
      const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY)
      if (distance < POINTER_DRAG_THRESHOLD_PX) return
      drag.moved = true
      draggedTabId.value = drag.sourceId
      document.body.classList.add('chat-sidebar-dragging')
    }
    event.preventDefault()
    updatePointerDrop(event.clientX, event.clientY)
  }

  function finishPointerDrag(event: PointerEvent, cancelled = false) {
    const drag = pointerDrag
    if (!drag || event.pointerId !== drag.pointerId) return
    pointerDrag = null
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    window.removeEventListener('pointercancel', onPointerCancel)
    if (!drag.moved) return

    suppressNextRowClick = true
    if (rowClickTimer !== null) window.clearTimeout(rowClickTimer)
    rowClickTimer = window.setTimeout(() => { suppressNextRowClick = false; rowClickTimer = null }, 0)
    if (!cancelled) {
      // Use the last pointermove candidate that the user actually saw. Some
      // browsers retarget pointerup to the pressed button, and re-running hit
      // testing here can silently flip an `after` indicator back to `before`.
      const source = chatTabs.value.find(tab => tab.id === drag.sourceId)
      if (source && dropOnPinZone.value) {
        setChatPinned(source.id, true)
      } else {
        const group = filteredGroups.value.find(item => item.key === dropGroupKey.value)
        const targetId = dropTarget.value?.id
        if (source && group && canDropChatInGroup(source, group)) {
          setChatPinned(source.id, group.pinned)
          if (targetId && targetId !== source.id) {
            reorderTabById(source.id, targetId, dropTarget.value!.position)
          } else if (dropAtGroupStart.value) {
            const first = group.tabs.find(tab => tab.id !== source.id)
            if (first) reorderTabById(source.id, first.id, 'before')
          }
        }
      }
    }
    clearDrag()
  }

  function onPointerUp(event: PointerEvent) {
    finishPointerDrag(event)
  }

  function onPointerCancel(event: PointerEvent) {
    finishPointerDrag(event, true)
  }

  function onRowPointerDown(event: PointerEvent, tab: TerminalTab) {
    if (event.button !== 0 || !event.isPrimary || renamingTabId.value === tab.id) return
    const target = event.target
    if (!(target instanceof HTMLElement) || target.closest('.chat-sidebar__item-menu, .chat-sidebar__rename-input')) return
    pointerDrag = {
      pointerId: event.pointerId,
      sourceId: tab.id,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    }
    window.addEventListener('pointermove', onPointerMove, { passive: false })
    window.addEventListener('pointerup', onPointerUp)
    window.addEventListener('pointercancel', onPointerCancel)
  }

  function onRowClick(event: MouseEvent, tab: TerminalTab) {
    if (suppressNextRowClick) return
    const target = event.target
    if (!(target instanceof HTMLElement)) return
    if (target.closest('.chat-sidebar__item-menu, .chat-sidebar__rename-input')) return
    // On mobile the row lives inside the drawer: switch + auto-dismiss it so the
    // selected session is visible immediately; desktop leaves the sidebar open.
    if (isMobile.value) {
      selectMobileTab(tab.id)
    } else {
      setActiveTab(tab.id)
    }
  }

  function updateGroupPointerDrop(clientX: number, clientY: number) {
    groupDropTarget.value = null
    const target = document.elementFromPoint(clientX, clientY)
    const element = target?.closest<HTMLElement>('.chat-sidebar__group')
    const group = groupFromElement(element ?? null)
    if (!group || group.pinned || group.cwd === draggedGroupCwd.value) return
    const header = element!.querySelector<HTMLElement>('.chat-sidebar__group-header')
    const rect = header?.getBoundingClientRect() ?? element!.getBoundingClientRect()
    groupDropTarget.value = {
      cwd: group.cwd,
      position: clientY < rect.top + rect.height / 2 ? 'before' : 'after',
    }
  }

  function clearGroupDrag() {
    draggedGroupCwd.value = null
    groupDropTarget.value = null
    document.body.classList.remove('chat-sidebar-group-dragging')
  }

  function onGroupPointerMove(event: PointerEvent) {
    const drag = groupPointerDrag
    if (!drag || event.pointerId !== drag.pointerId) return
    if (!drag.moved) {
      const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY)
      if (distance < POINTER_DRAG_THRESHOLD_PX) return
      drag.moved = true
      draggedGroupCwd.value = drag.sourceId
      document.body.classList.add('chat-sidebar-group-dragging')
    }
    event.preventDefault()
    updateGroupPointerDrop(event.clientX, event.clientY)
  }

  function finishGroupPointerDrag(event: PointerEvent, cancelled = false) {
    const drag = groupPointerDrag
    if (!drag || event.pointerId !== drag.pointerId) return
    groupPointerDrag = null
    window.removeEventListener('pointermove', onGroupPointerMove)
    window.removeEventListener('pointerup', onGroupPointerUp)
    window.removeEventListener('pointercancel', onGroupPointerCancel)
    if (!drag.moved) return

    suppressNextGroupClick = true
    if (groupClickTimer !== null) window.clearTimeout(groupClickTimer)
    groupClickTimer = window.setTimeout(() => { suppressNextGroupClick = false; groupClickTimer = null }, 0)
    if (!cancelled && groupDropTarget.value) {
      reorderChatWorkspace(
        drag.sourceId, groupDropTarget.value.cwd, groupDropTarget.value.position,
      )
    }
    clearGroupDrag()
  }

  function onGroupPointerUp(event: PointerEvent) {
    finishGroupPointerDrag(event)
  }

  function onGroupPointerCancel(event: PointerEvent) {
    finishGroupPointerDrag(event, true)
  }

  function onGroupPointerDown(event: PointerEvent, group: ChatSidebarGroup) {
    if (
      event.button !== 0 || !event.isPrimary || group.pinned ||
      filterText.value.trim() || draggedTabId.value
    ) return
    groupPointerDrag = {
      pointerId: event.pointerId,
      sourceId: group.cwd,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    }
    window.addEventListener('pointermove', onGroupPointerMove, { passive: false })
    window.addEventListener('pointerup', onGroupPointerUp)
    window.addEventListener('pointercancel', onGroupPointerCancel)
  }

  function onGroupHeaderClick(cwd: string) {
    if (suppressNextGroupClick) return
    toggleGroup(cwd)
  }

  onUnmounted(() => {
    if (rowClickTimer !== null) window.clearTimeout(rowClickTimer)
    if (groupClickTimer !== null) window.clearTimeout(groupClickTimer)
    pointerDrag = null
    groupPointerDrag = null
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    window.removeEventListener('pointercancel', onPointerCancel)
    window.removeEventListener('pointermove', onGroupPointerMove)
    window.removeEventListener('pointerup', onGroupPointerUp)
    window.removeEventListener('pointercancel', onGroupPointerCancel)
    clearDrag()
    clearGroupDrag()
  })
  return { draggedTabId, draggedGroupCwd, dropTarget, groupDropTarget,
    onRowPointerDown, onRowClick, onGroupPointerDown, onGroupHeaderClick }
}
