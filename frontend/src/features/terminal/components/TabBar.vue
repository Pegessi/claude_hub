<template>
  <div class="tab-bar">
    <div
      :class="[
        'tabs-shell',
        {
          'show-left-fade': showLeftFade,
          'show-right-fade': showRightFade,
        },
      ]"
    >
      <div
        ref="tabsContainerRef"
        class="tabs"
        @scroll="handleTabsScroll"
      >
        <div
          v-for="(tab, index) in manualTabs"
          :key="tab.id"
          :data-tab-id="tab.id"
          :class="['tab', { active: tab.id === activeTabId, dragging: draggedTabId === tab.id, 'drag-over-left': dragOverIndex === index && draggedTabId !== tab.id && fromIndex !== null && fromIndex > index, 'drag-over-right': dragOverIndex === index && draggedTabId !== tab.id && fromIndex !== null && fromIndex < index }]"
          draggable="true"
          @dragstart="handleDragStart($event, tab.id, index)"
          @dragover="handleDragOver($event, index)"
          @dragenter="handleDragEnter($event, index)"
          @dragleave="handleDragLeave($event)"
          @drop="handleDrop($event, index)"
          @dragend="handleDragEnd"
          @click="handleTabClick(tab.id)"
        >
          <input
            v-if="editingTabId === tab.id"
            ref="renameInputRef"
            v-model="editingTabName"
            type="text"
            class="tab-name-input"
            @blur="handleRenameTab"
            @keyup.enter="handleRenameTab"
            @keyup.escape="cancelRename"
          >
          <span
            v-else
            class="tab-name"
            @dblclick.stop="startRename(tab)"
          >{{ tab.name }}</span>
          <AgentAvatar
            class="tab-kind"
            :agent-type="tab.agent_type"
            size="sm"
            :title="tab.session_kind === 'chat' ? 'Chat session' : 'Terminal session'"
          />
          <span
            v-if="tab.session_kind === 'chat' || tab.is_active"
            class="tab-indicator"
            :data-status="getTabStatus(tab)"
            role="img"
            :aria-label="getTabStatusLabel(tab)"
            :title="getTabStatusLabel(tab)"
          />
          <span
            v-if="getPaneCountForTab(tab.id) > 0"
            class="pane-indicator"
          >
            {{ getPaneCountForTab(tab.id) }}
          </span>
          <!-- Hover-revealed ⋯ dropdown for secondary actions (shared with
               the sidebar); × close stays always visible on hover -->
          <TabActionsMenu
            :tab="tab"
            variant="tabbar"
            @rename="startRename"
          />
          <button
            class="tab-close"
            title="Close tab"
            @click.stop="handleTabClose(tab.id)"
          >
            ×
          </button>
        </div>
      </div>
    </div>
    <button
      class="add-tab"
      :disabled="isLoading"
      @click="openCreateModal"
    >
      {{ isLoading ? '...' : '+' }}
    </button>
    <LayoutSelector variant="menu" />
    <DotLauncher
      v-if="isMobile"
      compact
    />
    <div class="mobile-app-menu">
      <details
        ref="mobileAppMenuRef"
        class="mobile-app-menu-details"
      >
        <summary
          class="mobile-app-menu-trigger"
          title="App menu"
          aria-label="App menu"
        >
          ⋯
        </summary>
        <div class="mobile-app-menu-panel">
          <button
            type="button"
            :class="['mobile-app-menu-item', 'mobile-app-menu-item--mode', { active: mode === 'terminal' }]"
            @click="setAppMode('terminal')"
          >
            <span>Terminal</span>
            <strong v-if="mode === 'terminal'">Current</strong>
          </button>
          <button
            type="button"
            :class="['mobile-app-menu-item', 'mobile-app-menu-item--mode', { active: mode === 'workspace' }]"
            @click="setAppMode('workspace')"
          >
            <span>Agent Workspace</span>
            <strong v-if="mode === 'workspace'">Current</strong>
          </button>
          <button
            type="button"
            class="mobile-app-menu-item"
            @click="openMobileDrawer"
          >
            Chats
          </button>
          <AppExtensionActions @select="closeMobileAppMenu" />
          <button
            type="button"
            class="mobile-app-menu-item"
            @click="toggleColorScheme"
          >
            {{ colorScheme === 'dark' ? 'Light Theme' : 'Dark Theme' }}
          </button>
        </div>
      </details>
    </div>
    <AgentStatusFloatingPanel
      v-if="manualTabs.length > 0"
      :hide-summary="isMobile"
      source="manual"
      label="Status"
      panel-title="Terminal Status"
    />
    <AgentStatusFloatingPanel
      v-if="managedTabs.length > 0"
      :hide-summary="isMobile"
      source="managed"
      label="Agents"
      panel-title="Workspace Agents"
    />

    <SessionCreator ref="sessionCreatorRef" />
    <CloseTabDialog
      :visible="showCloseConfirm"
      :tab="tabToClose"
      :loading="isLoading"
      :pending="tabToClose ? isPending(tabActionKey('close', tabToClose.id)) : false"
      @close="showCloseConfirm = false"
      @confirm="confirmCloseTab"
    />

    <!-- Notification / toast stack (F5: replaces single mutable error string) -->
    <div
      v-if="notifications.length"
      class="toast-stack"
      role="region"
      aria-label="Notifications"
    >
      <div
        v-for="n in notifications"
        :key="n.id"
        :class="['toast', `toast--${n.type}`]"
        role="status"
      >
        <span
          class="toast__icon"
          aria-hidden="true"
        />
        <span class="toast__message">{{ n.message }}</span>
        <button
          type="button"
          class="toast__close"
          :aria-label="'Dismiss ' + n.type + ' notification'"
          @click="dismissNotification(n.id)"
        >
          ×
        </button>
        <div
          v-if="n.autoDismissMs"
          class="toast__timer"
          :style="{ animationDuration: `${n.autoDismissMs}ms` }"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import AgentAvatar from '@/components/AgentAvatar.vue'
import CloseTabDialog from './CloseTabDialog.vue'
import AgentStatusFloatingPanel from '@/components/AgentStatusFloatingPanel.vue'
import AppExtensionActions from '@/components/AppExtensionActions.vue'
import DotLauncher from '@/components/DotLauncher.vue'
import LayoutSelector from '@/components/LayoutSelector.vue'
import TabActionsMenu from '@/components/TabActionsMenu.vue'
import { usePendingActions } from '@/composables/usePendingActions'
import { useTabStatus } from '@/composables/useTabStatus'
import { useViewport } from '@/composables/useViewport'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import type { AppMode, TerminalTab } from '@/types'
import SessionCreator from './SessionCreator.vue'

const store = useTerminalStore()
const appStore = useAppStore()
const { isMobile } = useViewport()
const { isPending, runPending } = usePendingActions()
const { tabs, manualTabs, managedTabs, activeTabId, isLoading, agentStatuses, notifications } = storeToRefs(store)
const { mode, colorScheme } = storeToRefs(appStore)
const { dismissNotification } = store
const { getTabStatus, getTabStatusLabel } = useTabStatus(agentStatuses)

const draggedTabId = ref<string | null>(null)
const dragOverIndex = ref<number | null>(null)
const fromIndex = ref<number | null>(null)
const showCloseConfirm = ref(false)
const tabToClose = ref<TerminalTab | null>(null)
const editingTabId = ref<string | null>(null)
const editingTabName = ref('')
const renameInputRef = ref<HTMLInputElement | null>(null)
const mobileAppMenuRef = ref<HTMLDetailsElement | null>(null)
const tabsContainerRef = ref<HTMLDivElement | null>(null)
const showLeftFade = ref(false)
const showRightFade = ref(false)
const sessionCreatorRef = ref<InstanceType<typeof SessionCreator> | null>(null)

function tabActionKey(action: string, tabId: string | null | undefined) {
  return `tab:${tabId || 'none'}:${action}`
}

function openCreateModal() {
  sessionCreatorRef.value?.openCreateModal()
}

function handleTabClick(tabId: string) {
  store.setActiveTab(tabId)
}

function scrollActiveTabIntoView(tabId: string) {
  const container = tabsContainerRef.value
  if (!container) return

  const tabElement = container.querySelector<HTMLElement>(`[data-tab-id="${tabId}"]`)
  tabElement?.scrollIntoView({
    behavior: 'smooth',
    inline: 'nearest',
    block: 'nearest',
  })
}

function updateScrollFadeState() {
  const container = tabsContainerRef.value
  if (!container) return

  const maxScrollLeft = Math.max(0, container.scrollWidth - container.clientWidth)
  const hasOverflow = maxScrollLeft > 2
  showLeftFade.value = hasOverflow && container.scrollLeft > 2
  showRightFade.value = hasOverflow && container.scrollLeft < maxScrollLeft - 2
}

function handleTabsScroll() {
  updateScrollFadeState()
}

function handleDragStart(event: DragEvent, tabId: string, index: number) {
  draggedTabId.value = tabId
  fromIndex.value = index
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', tabId)
  }
}

function handleDragEnter(event: DragEvent, index: number) {
  event.preventDefault()
  dragOverIndex.value = index
}

function handleDragOver(event: DragEvent, index: number) {
  event.preventDefault()
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = 'move'
  }
  dragOverIndex.value = index
}

function handleDragLeave(event: DragEvent) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const y = event.clientY
  if (y < rect.top || y > rect.bottom) {
    dragOverIndex.value = null
  }
}

function handleDrop(event: DragEvent, toIndex: number) {
  event.preventDefault()
  event.stopPropagation()

  const fIndex = fromIndex.value
  if (fIndex !== null && fIndex !== -1 && fIndex !== toIndex) {
    store.reorderTabs(fIndex, toIndex)
  }

  dragOverIndex.value = null
}

function handleDragEnd() {
  draggedTabId.value = null
  dragOverIndex.value = null
  fromIndex.value = null
}

function getPaneCountForTab(tabId: string): number {
  return store.getPaneCountForTab(tabId)
}

function handleTabClose(tabId: string) {
  const tab = tabs.value.find(t => t.id === tabId)
  if (tab) {
    tabToClose.value = tab
    showCloseConfirm.value = true
  }
}

function startRename(tab: TerminalTab) {
  editingTabId.value = tab.id
  editingTabName.value = tab.name
  // Focus the input after the next tick
  setTimeout(() => {
    renameInputRef.value?.focus()
    renameInputRef.value?.select()
  }, 0)
}

function cancelRename() {
  editingTabId.value = null
  editingTabName.value = ''
}

async function handleRenameTab() {
  if (editingTabId.value && editingTabName.value.trim()) {
    await runPending(tabActionKey('rename', editingTabId.value), () =>
      store.updateTab(editingTabId.value!, { name: editingTabName.value.trim() })
    )
  }
  editingTabId.value = null
  editingTabName.value = ''
}

async function confirmCloseTab() {
  if (tabToClose.value) {
    const tabId = tabToClose.value.id
    const deleted = await runPending(tabActionKey('close', tabId), () => store.deleteTab(tabId))
    if (!deleted) return
    tabToClose.value = null
    showCloseConfirm.value = false
  }
}
function closeMobileAppMenu() {
  if (mobileAppMenuRef.value) {
    mobileAppMenuRef.value.open = false
  }
}

function openMobileDrawer() {
  store.openMobileDrawer()
  closeMobileAppMenu()
}

function setAppMode(nextMode: AppMode) {
  appStore.setMode(nextMode)
  closeMobileAppMenu()
}

function toggleColorScheme() {
  appStore.toggleColorScheme()
  closeMobileAppMenu()
}

function handleDocumentPointerDown(event: PointerEvent) {
  const target = event.target
  if (!(target instanceof Node)) return
  // Close the mobile app menu when clicking outside.
  if (mobileAppMenuRef.value && !mobileAppMenuRef.value.contains(target)) {
    closeMobileAppMenu()
  }
}

watch(activeTabId, (tabId) => {
  if (!tabId) return
  nextTick(() => {
    scrollActiveTabIntoView(tabId)
    updateScrollFadeState()
  })
})

watch(
  () => tabs.value.length,
  () => nextTick(updateScrollFadeState),
)

onMounted(() => {
  nextTick(updateScrollFadeState)
  window.addEventListener('resize', updateScrollFadeState)
  document.addEventListener('pointerdown', handleDocumentPointerDown)
})

onUnmounted(() => {
  window.removeEventListener('resize', updateScrollFadeState)
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
})
</script>

<style scoped src="@/features/terminal/styles/tab-bar.css"></style>
