<template>
  <aside
    class="chat-sidebar"
    :class="{ collapsed: sidebarCollapsed, resizing: isResizing }"
    :style="sidebarStyle"
    aria-label="Chat sessions"
  >
    <!-- Header: title + collapse toggle -->
    <div class="chat-sidebar__header">
      <span
        v-if="!sidebarCollapsed"
        class="chat-sidebar__title"
      >Chats</span>
      <button
        type="button"
        class="chat-sidebar__icon-btn"
        :title="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        :aria-label="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        @click="toggleSidebar"
      >
        <svg
          class="chat-sidebar__chevron"
          :class="{ flipped: sidebarCollapsed }"
          viewBox="0 0 16 16"
          width="16"
          height="16"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M10 12.5 5.5 8 10 3.5l.9.9L7.3 8l3.6 3.6z"
          />
        </svg>
      </button>
    </div>

    <!-- Expanded body -->
    <template v-if="!sidebarCollapsed">
      <div class="chat-sidebar__search">
        <svg
          viewBox="0 0 16 16"
          width="13"
          height="13"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M11.4 10.6a5.5 5.5 0 1 0-.8.8l3 3 .8-.8-3-3zM7 11.5a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"
          />
        </svg>
        <input
          v-model="filterText"
          type="text"
          class="chat-sidebar__search-input"
          placeholder="Filter chats..."
          aria-label="Filter chats"
        >
      </div>

      <div class="chat-sidebar__body">
        <div
          v-if="draggedTabId && !filteredGroups.some(group => group.pinned)"
          class="chat-sidebar__pin-drop"
          data-pointer-drop="pin"
        >
          Drop here to pin
        </div>
        <div
          v-for="group in filteredGroups"
          :key="group.key"
          class="chat-sidebar__group"
          :class="{ 'chat-sidebar__group--pinned': group.pinned }"
          :data-group-key="group.key"
        >
          <div
            v-if="group.pinned"
            class="chat-sidebar__group-header chat-sidebar__pinned-header"
            title="Pins are saved in this browser"
            data-pointer-drop="group"
          >
            <span aria-hidden="true">⌖</span>
            <span class="chat-sidebar__group-label">Pinned</span>
            <span class="chat-sidebar__group-count">{{ group.tabs.length }}</span>
          </div>
          <button
            v-else
            type="button"
            class="chat-sidebar__group-header"
            data-pointer-drop="group"
            :aria-expanded="!group.collapsed"
            :aria-controls="`chat-group-${group.key}`"
            @click="toggleGroup(group.cwd)"
          >
            <svg
              viewBox="0 0 16 16"
              width="14"
              height="14"
              aria-hidden="true"
            >
              <path
                fill="currentColor"
                d="M1.5 3.5v9h13v-7h-6l-1.5-2h-5.5zm1 1h3.7l1.5 2h5.3v5h-10.5v-7z"
              />
            </svg>
            <span
              class="chat-sidebar__group-label"
              :title="group.cwd"
            >{{ cwdLabel(group.cwd) }}</span>
            <span class="chat-sidebar__group-count">{{ group.tabs.length }}</span>
            <svg
              class="chat-sidebar__group-chevron"
              :class="{ collapsed: group.collapsed }"
              viewBox="0 0 16 16"
              width="12"
              height="12"
              aria-hidden="true"
            >
              <path
                fill="currentColor"
                d="M4.5 6 8 9.5 11.5 6l.9.9L8 11.3 3.6 6.9z"
              />
            </svg>
          </button>

          <div
            :id="`chat-group-${group.key}`"
            class="chat-sidebar__group-items"
          >
            <div
              v-for="tab in group.visibleTabs"
              :key="tab.id"
              class="chat-sidebar__item"
              :class="{ active: tab.id === activeTabId, dragging: tab.id === draggedTabId }"
              :data-tab-id="tab.id"
              :data-drop-position="dropTarget?.id === tab.id ? dropTarget.position : undefined"
              @pointerdown="onRowPointerDown($event, tab)"
              @click="onRowClick($event, tab)"
            >
              <button
                v-if="renamingTabId !== tab.id"
                type="button"
                class="chat-sidebar__item-main"
                :aria-current="tab.id === activeTabId ? 'page' : undefined"
                :title="`${tab.name || 'Untitled'} — ${tab.cwd || 'No directory'}. Alt+↑/↓ to reorder`"
                @click.stop="onRowClick($event, tab)"
                @keydown.alt.up.prevent="moveInGroup(tab, group, -1)"
                @keydown.alt.down.prevent="moveInGroup(tab, group, 1)"
              >
                <span
                  class="chat-sidebar__item-status"
                  :data-status="getTabStatus(tab)"
                  :data-unread="tab.is_unread ? 'true' : 'false'"
                  role="img"
                  :aria-label="`Session status: ${getTabStatusLabel(tab)}`"
                  :title="`Session status: ${getTabStatusLabel(tab)}`"
                />
                <span class="chat-sidebar__item-name">{{ tab.name || 'Untitled' }}</span>
                <span class="chat-sidebar__item-time">{{ relativeTime(tab.created_at) }}</span>
              </button>
              <input
                v-else
                :ref="setRenameInputRef"
                v-model="renamingTabName"
                type="text"
                class="chat-sidebar__rename-input"
                aria-label="Rename session"
                @blur="handleRenameTab(tab.id)"
                @keyup.enter="handleRenameTab(tab.id)"
                @keyup.escape="cancelRename(tab.id)"
              >
              <!-- Same ⋯ actions as the TabBar (rename / duplicate / archive /
                   copy link / switch env). Hover-revealed like the old archive. -->
              <TabActionsMenu
                class="chat-sidebar__item-menu"
                :tab="tab"
                variant="sidebar"
                :move-up-disabled="group.tabs[0]?.id === tab.id"
                :move-down-disabled="group.tabs[group.tabs.length - 1]?.id === tab.id"
                @rename="startRename"
                @move="direction => moveInGroup(tab, group, direction)"
              />
            </div>
            <button
              v-if="!group.pinned && !group.collapsed && !filterText.trim() && group.tabs.length > CHAT_GROUP_PREVIEW_SIZE"
              type="button"
              class="chat-sidebar__show-more"
              :aria-expanded="group.expanded"
              @click="toggleExpanded(group.cwd)"
            >
              {{ group.expanded ? 'Show less' : `Show more (${group.hiddenCount})` }}
            </button>
          </div>
        </div>

        <div
          v-if="filteredGroups.length === 0"
          class="chat-sidebar__empty"
        >
          {{ filterText ? 'No matching chats' : 'No chat sessions yet' }}
        </div>
      </div>
    </template>

    <!-- Collapsed body: icon-only quick access -->
    <div
      v-else
      class="chat-sidebar__body chat-sidebar__body--icons"
    >
      <button
        type="button"
        class="chat-sidebar__icon-btn chat-sidebar__icon-btn--big"
        title="Show chats"
        aria-label="Show chats"
        @click="toggleSidebar"
      >
        <svg
          viewBox="0 0 16 16"
          width="18"
          height="18"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M8 2a6.5 6.5 0 0 0-6.5 6.5c0 1.2.3 2.3.9 3.3L2 14l2.4-.5c1.1.6 2.3 1 3.6 1A6.5 6.5 0 1 0 8 2zm0 1a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11zM5.5 7a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm5 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2z"
          />
        </svg>
      </button>
    </div>

    <!-- Footer: archived sessions browser entry -->
    <div class="chat-sidebar__footer">
      <button
        type="button"
        class="chat-sidebar__archived-btn"
        :title="sidebarCollapsed ? 'View archived sessions' : ''"
        @click="$emit('open-archive')"
      >
        <svg
          viewBox="0 0 16 16"
          width="15"
          height="15"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M2 3.5h12v2h-.6l-.8 7.5H3.4L2.6 5.5H2v-2zm1.7 2 .6 6.5h7.4l.6-6.5H3.7zM6 7h1v4H6V7zm3 0h1v4H9V7zM4 4.5h8v-1H4v1z"
          />
        </svg>
        <span
          v-if="!sidebarCollapsed"
          class="chat-sidebar__archived-label"
        >Archived ({{ archivedTabs.length }})</span>
      </button>
    </div>

    <div
      v-if="!sidebarCollapsed"
      class="chat-sidebar__resize-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize chat sidebar"
      :aria-valuemin="SIDEBAR_MIN_WIDTH"
      :aria-valuemax="SIDEBAR_MAX_WIDTH"
      :aria-valuenow="sidebarWidth"
      tabindex="0"
      title="Drag to resize; double-click to reset"
      @pointerdown="startResize"
      @dblclick="resetSidebarWidth"
      @keydown="resizeWithKeyboard"
    />
  </aside>
</template>

<script setup lang="ts">
import { nextTick, ref, computed, watch, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import { buildChatSidebarGroups, canDropChatInGroup, CHAT_GROUP_PREVIEW_SIZE, cwdLabel } from '@/utils/chatGroups'
import type { ChatSidebarGroup } from '@/utils/chatGroups'
import { relativeTime } from '@/utils/time'
import { useTabStatus } from '@/composables/useTabStatus'
import TabActionsMenu from '@/components/TabActionsMenu.vue'
import type { TerminalTab } from '@/types'

defineEmits<{
  (e: 'open-archive'): void
}>()

const store = useTerminalStore()
const {
  chatTabs,
  pinnedChatIds,
  activeTabId,
  sidebarCollapsed,
  archivedTabs,
  agentStatuses,
} = storeToRefs(store)
const { toggleSidebar, setActiveTab } = store

// Tab-status logic is shared with the TabBar via useTabStatus.
const { getTabStatus, getTabStatusLabel } = useTabStatus(agentStatuses)

const filterText = ref('')
const collapsedGroups = ref<Set<string>>(new Set())
const expandedGroups = ref<Set<string>>(new Set())
const draggedTabId = ref<string | null>(null)
const dropTarget = ref<{ id: string; position: 'before' | 'after' } | null>(null)
const dropGroupKey = ref<string | null>(null)
const dropOnPinZone = ref(false)
const dropAtGroupStart = ref(false)
const SIDEBAR_DEFAULT_WIDTH = 240
const SIDEBAR_MIN_WIDTH = 200
const SIDEBAR_MAX_WIDTH = 480
const SIDEBAR_WIDTH_STORAGE_KEY = 'claude_hub_chat_sidebar_width'

function clampSidebarWidth(width: number): number {
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(width)))
}

function loadSidebarWidth(): number {
  try {
    const saved = Number(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY))
    return Number.isFinite(saved) && saved > 0
      ? clampSidebarWidth(saved)
      : SIDEBAR_DEFAULT_WIDTH
  } catch {
    return SIDEBAR_DEFAULT_WIDTH
  }
}

const sidebarWidth = ref(loadSidebarWidth())
const isResizing = ref(false)
let resizeStartX = 0
let resizeStartWidth = SIDEBAR_DEFAULT_WIDTH
const sidebarStyle = computed(() => sidebarCollapsed.value
  ? undefined
  : { width: `${sidebarWidth.value}px`, minWidth: `${sidebarWidth.value}px` })

function persistSidebarWidth() {
  try {
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(sidebarWidth.value))
  } catch {
    // Width persistence is optional in privacy-restricted browsers.
  }
}

function resizeSidebar(event: PointerEvent) {
  sidebarWidth.value = clampSidebarWidth(
    resizeStartWidth + event.clientX - resizeStartX,
  )
}

function stopResize() {
  if (!isResizing.value) return
  isResizing.value = false
  document.body.classList.remove('chat-sidebar-resizing')
  window.removeEventListener('pointermove', resizeSidebar)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
  persistSidebarWidth()
}

function startResize(event: PointerEvent) {
  if (event.button !== 0) return
  event.preventDefault()
  resizeStartX = event.clientX
  resizeStartWidth = sidebarWidth.value
  isResizing.value = true
  document.body.classList.add('chat-sidebar-resizing')
  window.addEventListener('pointermove', resizeSidebar)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
}

function resetSidebarWidth() {
  sidebarWidth.value = SIDEBAR_DEFAULT_WIDTH
  persistSidebarWidth()
}

function resizeWithKeyboard(event: KeyboardEvent) {
  let width = sidebarWidth.value
  if (event.key === 'ArrowLeft') width -= 16
  else if (event.key === 'ArrowRight') width += 16
  else if (event.key === 'Home') width = SIDEBAR_MIN_WIDTH
  else if (event.key === 'End') width = SIDEBAR_MAX_WIDTH
  else return
  event.preventDefault()
  sidebarWidth.value = clampSidebarWidth(width)
  persistSidebarWidth()
}

onUnmounted(() => {
  stopResize()
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('pointercancel', onPointerCancel)
  clearDrag()
})

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
    await store.updateTab(tabId, { name })
  } finally {
    if (renamingTabId.value === tabId) {
      renamingTabId.value = null
      renamingTabName.value = ''
    }
    savingRenameId.value = null
  }
}

function toggleGroup(cwd: string) {
  const next = new Set(collapsedGroups.value)
  if (next.has(cwd)) {
    next.delete(cwd)
  } else {
    next.add(cwd)
  }
  collapsedGroups.value = next
}

const filteredGroups = computed(() => buildChatSidebarGroups(chatTabs.value, pinnedChatIds.value, {
  query: filterText.value, activeTabId: activeTabId.value,
  collapsed: collapsedGroups.value, expanded: expandedGroups.value,
}))

function toggleExpanded(cwd: string) {
  const next = new Set(expandedGroups.value)
  if (next.has(cwd)) next.delete(cwd)
  else next.add(cwd)
  expandedGroups.value = next
}

// Navigation from another surface should never land on an invisible row.
watch(activeTabId, () => {
  filterText.value = ''
  nextTick(() => document.querySelector('.chat-sidebar__item.active')?.scrollIntoView({ block: 'nearest' }))
})

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
  window.setTimeout(() => { suppressNextRowClick = false }, 0)
  if (!cancelled) {
    // Use the last pointermove candidate that the user actually saw. Some
    // browsers retarget pointerup to the pressed button, and re-running hit
    // testing here can silently flip an `after` indicator back to `before`.
    const source = chatTabs.value.find(tab => tab.id === drag.sourceId)
    if (source && dropOnPinZone.value) {
      store.setChatPinned(source.id, true)
    } else {
      const group = filteredGroups.value.find(item => item.key === dropGroupKey.value)
      const targetId = dropTarget.value?.id
      if (source && group && canDropChatInGroup(source, group)) {
        store.setChatPinned(source.id, group.pinned)
        if (targetId && targetId !== source.id) {
          store.reorderTabById(source.id, targetId, dropTarget.value!.position)
        } else if (dropAtGroupStart.value) {
          const first = group.tabs.find(tab => tab.id !== source.id)
          if (first) store.reorderTabById(source.id, first.id, 'before')
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
  setActiveTab(tab.id)
}

function moveInGroup(tab: TerminalTab, group: ChatSidebarGroup, direction: -1 | 1) {
  const index = group.tabs.findIndex(item => item.id === tab.id)
  const target = group.tabs[index + direction]
  if (index < 0 || !target) return
  store.reorderTabById(tab.id, target.id, direction < 0 ? 'before' : 'after')
  // Keep the moved row reachable when it crosses the preview boundary.
  if (!group.pinned) expandedGroups.value = new Set([...expandedGroups.value, group.cwd])
  nextTick(() => {
    const rows = document.querySelectorAll<HTMLElement>('.chat-sidebar__item')
    const row = [...rows].find(item => item.dataset.tabId === tab.id)
    row?.querySelector<HTMLButtonElement>('.chat-sidebar__item-main')?.focus()
    row?.scrollIntoView({ block: 'nearest' })
  })
}
</script>

<style scoped>
.chat-sidebar {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 240px;
  min-width: 240px;
  height: 100%;
  background: var(--ch-color-surface);
  border-right: 1px solid var(--ch-color-border-muted);
  transition:
    width var(--ch-motion-standard),
    min-width var(--ch-motion-standard);
  overflow: hidden;
}

.chat-sidebar.resizing {
  transition: none;
}

.chat-sidebar.collapsed {
  width: 48px;
  min-width: 48px;
}

.chat-sidebar__resize-handle {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 5;
  width: 7px;
  cursor: col-resize;
  touch-action: none;
  outline: none;
}

.chat-sidebar__resize-handle::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 1px;
  background: transparent;
  transition: background var(--ch-motion-fast);
}

.chat-sidebar__resize-handle:hover::after,
.chat-sidebar__resize-handle:focus-visible::after,
.chat-sidebar.resizing .chat-sidebar__resize-handle::after {
  background: var(--ch-color-accent);
}

:global(body.chat-sidebar-resizing) {
  cursor: col-resize;
  user-select: none;
}

/* Header */
.chat-sidebar__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  padding: 10px 8px 8px;
}

.chat-sidebar__title {
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  color: var(--ch-color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding-left: 6px;
}

.chat-sidebar__icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  transition:
    background var(--ch-motion-fast),
    color var(--ch-motion-fast);
}

.chat-sidebar__icon-btn:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.chat-sidebar__icon-btn--big {
  width: 36px;
  height: 36px;
}

.chat-sidebar__chevron {
  transition: transform var(--ch-motion-fast);
}

.chat-sidebar__chevron.flipped {
  transform: rotate(180deg);
}

/* Search */
.chat-sidebar__search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 10px 8px;
  padding: 0 8px;
  height: 30px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__search:focus-within {
  border-color: var(--ch-color-accent);
}

.chat-sidebar__search-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  outline: none;
}

.chat-sidebar__search-input::placeholder {
  color: var(--ch-color-text-subtle);
}

/* Body */
.chat-sidebar__body {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 8px 8px;
}

.chat-sidebar__body--icons {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 8px;
}

/* Groups */
.chat-sidebar__group {
  margin-bottom: 2px;
}

.chat-sidebar__group--pinned {
  position: sticky;
  top: 0;
  z-index: 2;
  max-height: 40vh;
  overflow-y: auto;
  background: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
  margin-bottom: 6px;
}

.chat-sidebar__pinned-header {
  cursor: default;
}

.chat-sidebar__show-more {
  padding: 6px 12px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  text-align: left;
  font-size: var(--ch-font-size-xs);
  cursor: pointer;
}

.chat-sidebar__show-more:hover {
  background: var(--ch-color-row-hover);
  color: var(--ch-color-text);
}

.chat-sidebar__pin-drop {
  position: absolute;
  top: 0;
  left: 8px;
  right: 8px;
  z-index: 4;
  padding: 5px 8px;
  border: 1px dashed var(--ch-color-accent);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
}

.chat-sidebar__group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.chat-sidebar__group-header:hover {
  background: var(--ch-color-surface-control-hover);
}

.chat-sidebar__group-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.chat-sidebar__group-count {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__group-chevron {
  color: var(--ch-color-text-subtle);
  transition: transform var(--ch-motion-fast);
}

.chat-sidebar__group-chevron.collapsed {
  transform: rotate(-90deg);
}

.chat-sidebar__group-items {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 2px 0 4px;
}

/* Chat rows: a plain container; the clickable surface is the inner main
   button so the ⋯ menu can sit beside it as a valid sibling control. */
.chat-sidebar__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  transition: background var(--ch-motion-fast);
  cursor: grab;
  touch-action: pan-y;
}

.chat-sidebar__item:hover {
  background: var(--ch-color-row-hover);
}

.chat-sidebar__item.active {
  background: var(--ch-color-surface-selected);
  color: var(--ch-color-text-strong);
}

.chat-sidebar__item.dragging {
  opacity: 0.45;
  cursor: grabbing;
}

:global(body.chat-sidebar-dragging) {
  cursor: grabbing !important;
  user-select: none;
}

.chat-sidebar__item[data-drop-position]::before {
  content: '';
  position: absolute;
  left: 6px;
  right: 6px;
  height: 2px;
  background: var(--ch-color-accent);
  pointer-events: none;
}

.chat-sidebar__item[data-drop-position='before']::before {
  top: -1px;
}

.chat-sidebar__item[data-drop-position='after']::before {
  bottom: -1px;
}

.chat-sidebar__item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  border: none;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.chat-sidebar__item-main:focus-visible {
  outline: none;
  box-shadow: inset 0 0 0 2px var(--ch-color-accent-ring);
}

/* While the absolute ⋯ menu is shown, reserve its column inside the main
   button so the longest names ellipsize instead of running under it. */
.chat-sidebar__item:hover .chat-sidebar__item-main,
.chat-sidebar__item:focus-within .chat-sidebar__item-main {
  padding-right: 26px;
}

.chat-sidebar__item-status {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
  align-self: center;
  background: var(--ch-color-text-subtle);
}

/* Colors mirror the TabBar's .tab-indicator so the two surfaces agree. */
.chat-sidebar__item-status[data-status='working'] {
  background: var(--ch-color-warning);
  box-shadow: 0 0 6px var(--ch-color-warning-bg);
}

.chat-sidebar__item-status[data-status='idle'] {
  background: var(--ch-color-success);
}

.chat-sidebar__item-status[data-status='attention'] {
  background: var(--ch-color-attention);
  box-shadow: 0 0 6px var(--ch-color-attention-bg);
}

.chat-sidebar__item-status[data-status='offline'] {
  background: var(--ch-color-text-subtle);
}

/* Unread ping: a soft accent halo plus an accent ring that expands outward
   from the dot and fades, repeating. Shown only for tabs with an unread
   completed turn. The halo keeps the state legible between ripple frames.
   For working/attention the halo is stacked with the status glow so neither
   signal is suppressed. */
.chat-sidebar__item-status[data-unread='true'] {
  box-shadow: 0 0 0 2px var(--ch-color-accent-ring);
}

.chat-sidebar__item-status[data-status='working'][data-unread='true'] {
  box-shadow: 0 0 6px var(--ch-color-warning-bg), 0 0 0 2px var(--ch-color-accent-ring);
}

.chat-sidebar__item-status[data-status='attention'][data-unread='true'] {
  box-shadow: 0 0 6px var(--ch-color-attention-bg), 0 0 0 2px var(--ch-color-accent-ring);
}

.chat-sidebar__item-status[data-unread='true']::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  /* The global `* { box-sizing: border-box }` reset does NOT reach
     pseudo-elements; without this the 1.5px border adds onto width:100%
     (content-box), making the 11px ring sit down-right of the 8px dot. */
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  border-radius: 50%;
  border: 1.5px solid var(--ch-color-accent);
  animation: sidebar-unread-ping 1.6s cubic-bezier(0, 0, 0.2, 1) infinite;
}

@keyframes sidebar-unread-ping {
  0% { transform: scale(1); opacity: 0.9; }
  75%, 100% { transform: scale(2.4); opacity: 0; }
}

.chat-sidebar__item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.chat-sidebar__item-time {
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-subtle);
}

.chat-sidebar__item.active .chat-sidebar__item-time {
  color: var(--ch-color-text-muted);
}

/* Hover-revealed ⋯ actions menu (same actions as the TabBar). Absolute,
   over the timestamp; focus-within keeps it reachable via keyboard. */
.chat-sidebar__item-menu {
  position: absolute;
  right: 4px;
  display: none;
}

.chat-sidebar__item:hover .chat-sidebar__item-menu,
.chat-sidebar__item:focus-within .chat-sidebar__item-menu {
  display: inline-flex;
}

/* Hide the timestamp while the actions menu is shown to avoid overlap */
.chat-sidebar__item:hover .chat-sidebar__item-time,
.chat-sidebar__item:focus-within .chat-sidebar__item-time {
  visibility: hidden;
}

.chat-sidebar__rename-input {
  flex: 1;
  min-width: 0;
  /* Align with the name text inside the main button (8px dot + 6px gap). */
  margin-left: 14px;
  height: 22px;
  padding: 1px 6px;
  border: 1px solid var(--ch-color-accent);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  outline: none;
}

.chat-sidebar__empty {
  padding: 16px 8px;
  text-align: center;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-subtle);
}

/* Footer */
.chat-sidebar__footer {
  border-top: 1px solid var(--ch-color-border-muted);
  padding: 8px;
}

.chat-sidebar__archived-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 8px;
  border: none;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.chat-sidebar__archived-btn:hover {
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.chat-sidebar__archived-label {
  flex: 1;
  text-align: left;
}

/* Mobile: sidebar is hidden to save the limited width */
@media (max-width: 768px) {
  .chat-sidebar {
    display: none;
  }
}
</style>
