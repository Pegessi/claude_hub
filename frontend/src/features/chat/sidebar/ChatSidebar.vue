<template>
  <aside
    ref="panelRef"
    class="chat-sidebar"
    :class="{
      collapsed: sidebarCollapsed,
      resizing: isResizing,
      'mobile-drawer': isMobile,
      'mobile-drawer--open': isMobile && mobileDrawerOpen,
      'mobile-drawer--dragging': isMobile && edgeDragging,
    }"
    :style="isMobile ? mobilePanelStyle : sidebarStyle"
    aria-label="Chat sessions"
  >
    <!-- Header: title + collapse toggle (desktop) / close (mobile drawer) -->
    <div
      ref="headerRef"
      class="chat-sidebar__header"
    >
      <span
        v-if="!sidebarCollapsed || isMobile"
        class="chat-sidebar__title"
      >Chats</span>
      <button
        v-if="isMobile"
        type="button"
        class="chat-sidebar__icon-btn chat-sidebar__drawer-close"
        aria-label="Close sessions"
        @click="closeMobileDrawer"
      >
        <svg
          viewBox="0 0 16 16"
          width="16"
          height="16"
          aria-hidden="true"
        >
          <path
            fill="currentColor"
            d="M4.5 4.5l7 7M11.5 4.5l-7 7"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
          />
        </svg>
      </button>
      <button
        v-else
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

    <!-- Expanded body (always rendered inside the mobile drawer) -->
    <template v-if="!sidebarCollapsed || isMobile">
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
        <ChatSidebarGroup
          v-for="group in filteredGroups"
          :key="group.key"
          :group="group"
          :dragging="draggedGroupCwd === group.cwd"
          :drop-position="groupDropTarget?.cwd === group.cwd ? groupDropTarget.position : undefined"
          :filter-text="filterText"
          @toggle-pinned="pinnedCollapsed = !pinnedCollapsed"
          @group-pointerdown="onGroupPointerDown"
          @group-click="onGroupHeaderClick"
          @toggle-expanded="toggleExpanded"
        >
          <ChatSidebarRow
            v-for="tab in group.visibleTabs"
            :key="tab.id"
            v-model:rename-name="renamingTabName"
            :tab="tab"
            :active="tab.id === activeTabId"
            :dragging="tab.id === draggedTabId"
            :drop-position="dropTarget?.id === tab.id ? dropTarget.position : undefined"
            :status="getTabStatus(tab)"
            :status-label="getTabStatusAriaLabel(tab)"
            :renaming="renamingTabId === tab.id"
            :set-rename-input-ref="setRenameInputRef"
            @row-pointerdown="onRowPointerDown"
            @row-click="onRowClick"
            @rename="startRename"
            @save-rename="handleRenameTab"
            @cancel-rename="cancelRename"
          />
        </ChatSidebarGroup>

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
        @click="openArchive"
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
          v-if="!sidebarCollapsed || isMobile"
          class="chat-sidebar__archived-label"
        >Archived ({{ archivedTabs.length }})</span>
      </button>
    </div>

    <div
      v-if="!sidebarCollapsed && !isMobile"
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

    <!-- Mobile-only: invisible left-edge gesture zone (above the ttyd iframe,
         whose touches never reach this document) and the dimmed backdrop.
         Teleported so the fixed positioning escapes any pane transform. -->
    <Teleport to="body">
      <div
        v-if="isMobile && isTerminalMode"
        ref="bandRef"
        class="chat-drawer-edge-band"
        aria-hidden="true"
      />
      <Transition name="chat-drawer-fade">
        <div
          v-if="isMobile && isTerminalMode && (mobileDrawerOpen || edgeDragging)"
          ref="backdropRef"
          class="chat-drawer-backdrop"
          :class="{ 'chat-drawer-backdrop--dragging': edgeDragging }"
          :style="edgeDragging ? { opacity: edgeBackdropOpacity } : undefined"
          @click.self="closeMobileDrawer"
        />
      </Transition>
    </Teleport>
  </aside>
</template>

<script setup lang="ts">
import { nextTick, ref, computed, watch, onUnmounted, toRef } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAppStore } from '@/stores/appStore'
import { buildChatSidebarGroups } from '@/utils/chatGroups'
import { useTabStatus } from '@/composables/useTabStatus'
import { useViewport } from '@/composables/useViewport'
import { useEdgeSwipeDrawer } from '@/composables/useEdgeSwipeDrawer'
import ChatSidebarGroup from './ChatSidebarGroup.vue'
import ChatSidebarRow from './ChatSidebarRow.vue'
import { useSidebarPointerDrag } from './useSidebarPointerDrag'
import { useSidebarRename } from './useSidebarRename'
import { useSidebarResize, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } from './useSidebarResize'
import type { TerminalTab } from '@/types'

const props = defineProps<{
  /** Main content element; a rightward swipe on it opens the mobile drawer. */
  swipeSurface?: HTMLElement | null
}>()

const emit = defineEmits<{
  (e: 'open-archive'): void
}>()

const store = useTerminalStore()
const appStore = useAppStore()
const { isMobile } = useViewport()
const { mode } = storeToRefs(appStore)
const isTerminalMode = computed(() => mode.value === 'terminal')
const {
  chatTabs,
  pinnedChatIds,
  activeTabId,
  sidebarCollapsed,
  archivedTabs,
  agentStatuses,
  mobileDrawerOpen,
} = storeToRefs(store)
const {
  toggleSidebar,
  setActiveTab,
  openMobileDrawer,
  closeMobileDrawer,
  selectMobileTab,
} = store

// Mobile drawer: the same sidebar becomes a fixed overlay panel. These refs
// are the gesture zones consumed by useEdgeSwipeDrawer.
const panelRef = ref<HTMLElement | null>(null)
const bandRef = ref<HTMLElement | null>(null)
const backdropRef = ref<HTMLElement | null>(null)
const headerRef = ref<HTMLElement | null>(null)
const {
  dragging: edgeDragging,
  translatePx: edgeTranslatePx,
  backdropOpacity: edgeBackdropOpacity,
} = useEdgeSwipeDrawer({
  bandRef,
  panelRef,
  backdropRef,
  closeDragRef: headerRef,
  surfaceRef: toRef(props, 'swipeSurface'),
  enabled: computed(() => isMobile.value && isTerminalMode.value),
  open: openMobileDrawer,
  close: closeMobileDrawer,
  isOpen: () => mobileDrawerOpen.value,
})

// While dragging, the panel follows the finger 1:1 (CSS transition disabled);
// at rest the open/closed classes own the transform.
const mobilePanelStyle = computed(() => {
  if (!isMobile.value) return undefined
  return edgeDragging.value
    ? { transform: `translateX(${edgeTranslatePx.value}px)` }
    : undefined
})

// Tab-status logic is shared with the TabBar via useTabStatus.
const { getTabStatus, getTabStatusLabel } = useTabStatus(agentStatuses)

function getTabStatusAriaLabel(tab: TerminalTab): string {
  if (tab.is_unread && getTabStatus(tab) === 'idle') {
    return 'Completed, unread'
  }
  return `Session status: ${getTabStatusLabel(tab)}`
}

const filterText = ref('')
const collapsedGroups = ref<Set<string>>(new Set())
const pinnedCollapsed = ref(false)
const expandedGroups = ref<Set<string>>(new Set())
const { sidebarWidth, sidebarStyle, isResizing, startResize, resetSidebarWidth, resizeWithKeyboard } =
  useSidebarResize(sidebarCollapsed)
const { renamingTabId, renamingTabName, setRenameInputRef, startRename, cancelRename, handleRenameTab } =
  useSidebarRename(store.updateTab)

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
  pinnedCollapsed: pinnedCollapsed.value,
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
  // Off-screen drawer rows must not yank page scroll while the drawer is shut.
  if (isMobile.value && !mobileDrawerOpen.value) return
  nextTick(() => document.querySelector('.chat-sidebar__item.active')?.scrollIntoView({ block: 'nearest' }))
})

const { draggedTabId, draggedGroupCwd, dropTarget, groupDropTarget,
  onRowPointerDown, onRowClick, onGroupPointerDown, onGroupHeaderClick } = useSidebarPointerDrag({
  chatTabs, filteredGroups, filterText, renamingTabId, isMobile, selectMobileTab, setActiveTab, toggleGroup,
  setChatPinned: store.setChatPinned, reorderTabById: store.reorderTabById, reorderChatWorkspace: store.reorderChatWorkspace,
})

onUnmounted(() => window.removeEventListener('keydown', handleDrawerKeydown))

// The archived-sessions browser opens above the drawer; dismiss the drawer.
function openArchive() {
  if (isMobile.value) closeMobileDrawer()
  emit('open-archive')
}

// Escape closes the mobile drawer (mirrors the previous MobileSessionDrawer).
function handleDrawerKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeMobileDrawer()
}

watch(mobileDrawerOpen, isOpen => {
  if (!isMobile.value) return
  if (isOpen) {
    filterText.value = ''
    window.addEventListener('keydown', handleDrawerKeydown)
  } else {
    window.removeEventListener('keydown', handleDrawerKeydown)
  }
})

</script>

<style scoped>
.chat-sidebar {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 240px;
  min-width: 240px;
  height: 100%;
  background: var(--ch-color-sidebar-bg);
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

.chat-sidebar__pin-drop {
  position: absolute;
  top: 0;
  left: 8px;
  right: 8px;
  z-index: 4;
  padding: 5px 8px;
  border: 1px dashed var(--ch-color-accent);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-sidebar-bg);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
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

/* Mobile: the sidebar becomes a left-edge overlay drawer instead of being
   removed from the layout. The same groups/pinned/search/archive markup is
   reused — only its positioning changes. */
@media (max-width: 768px) {
  .chat-sidebar.mobile-drawer {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: min(82vw, 320px);
    min-width: 0;
    height: 100%;
    height: 100dvh;
    z-index: 850;
    border-right: none;
    box-shadow: var(--ch-shadow-dialog);
    transform: translateX(-100%);
    /* Delay `hidden` until the slide-out finishes; show instantly on open. */
    transition:
      transform 160ms cubic-bezier(0.2, 0, 0, 1),
      visibility 0s linear 160ms;
    /* iPhone notch / home indicator */
    padding-top: env(safe-area-inset-top, 0px);
    padding-bottom: env(safe-area-inset-bottom, 0px);
    will-change: transform;
    visibility: hidden;
  }

  .chat-sidebar.mobile-drawer.mobile-drawer--open {
    transform: translateX(0);
    visibility: visible;
    transition:
      transform 160ms cubic-bezier(0.2, 0, 0, 1),
      visibility 0s;
  }

  /* Follow the finger 1:1 while an edge swipe is in flight. */
  .chat-sidebar.mobile-drawer.mobile-drawer--dragging {
    transition: none;
    visibility: visible;
  }

  /* The collapsed (icon-only) desktop state never applies on mobile. */
  .chat-sidebar.mobile-drawer.collapsed {
    width: min(82vw, 320px);
    min-width: 0;
  }

  /* 44px touch rows: the existing desktop paddings are slightly tighter. */
  .chat-sidebar.mobile-drawer :deep(.chat-sidebar__item) {
    min-height: 44px;
  }

  /* Working-row beams animate on the main thread every frame. Hold them while
     the drawer is hidden or following the finger, so they neither tax the page
     behind a closed drawer nor drop frames from the drag. */
  .chat-sidebar.mobile-drawer:not(.mobile-drawer--open) :deep(.working-effect *),
  .chat-sidebar.mobile-drawer.mobile-drawer--dragging :deep(.working-effect *) {
    animation-play-state: paused;
  }

  /* A phone-height drawer cannot spare 40vh for a sticky block that rows
     scroll underneath, so pins scroll with the list here. */
  .chat-sidebar.mobile-drawer :deep(.chat-sidebar__group--pinned) {
    position: relative;
    z-index: auto;
    max-height: none;
    overflow: visible;
  }

  /* Horizontal drags on the header must reach JS (close gesture); vertical
     movement stays native. */
  .chat-sidebar.mobile-drawer .chat-sidebar__header {
    touch-action: pan-y;
  }

  .chat-drawer-edge-band {
    position: fixed;
    left: 0;
    /* Stay below the mobile TabBar so tab-strip taps keep working. */
    top: 48px;
    bottom: 0;
    width: 20px;
    z-index: 840;
    /* pan-y: vertical scrolls/selection stay native, horizontal edge drags
       are delivered to JS. Visually invisible; it only captures edge
       touch-start. A non-captured tap is re-dispatched to the surface beneath
       (see useEdgeSwipeDrawer). */
    background: transparent;
    touch-action: pan-y;
  }

  .chat-drawer-backdrop {
    position: fixed;
    inset: 0;
    z-index: 849;
    background: var(--ch-color-overlay);
    opacity: 1;
    transition: opacity var(--ch-motion-standard);
    touch-action: pan-y;
  }

  .chat-drawer-backdrop.chat-drawer-backdrop--dragging {
    transition: none;
  }

  .chat-drawer-fade-enter-from,
  .chat-drawer-fade-leave-to {
    opacity: 0;
  }
}
</style>
