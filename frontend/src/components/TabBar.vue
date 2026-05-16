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
          <span
            v-if="tab.is_active"
            class="tab-indicator"
            :data-status="getTabStatus(tab)"
          />
          <span
            v-if="getPaneCountForTab(tab.id) > 0"
            class="pane-indicator"
          >
            {{ getPaneCountForTab(tab.id) }}
          </span>
          <LoadingButton
            type="button"
            class="tab-duplicate"
            title="Duplicate tab"
            :loading="isPending(tabActionKey('duplicate', tab.id))"
            hide-content-while-loading
            loading-label="Duplicating tab"
            @click.stop="handleTabDuplicate(tab.id)"
          >
            📋
          </LoadingButton>
          <button
            class="tab-close"
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
    <AgentStatusFloatingPanel
      v-if="manualTabs.length > 0"
      source="manual"
      label="Status"
      panel-title="Terminal Status"
    />
    <AgentStatusFloatingPanel
      v-if="managedTabs.length > 0"
      source="managed"
      label="Agents"
      panel-title="Workspace Agents"
    />

    <!-- Create Tab Modal -->
    <div
      v-if="showModal"
      class="modal-overlay"
      @click.self="closeCreateModal"
    >
      <div class="modal">
        <h3>Create New Terminal</h3>
        <form @submit.prevent="handleCreateTab">
          <div class="form-group">
            <label for="tabName">Tab Name</label>
            <input
              id="tabName"
              v-model="form.name"
              type="text"
              placeholder="Enter tab name"
              autofocus
            >
          </div>
          <div class="form-group">
            <label>Run On</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button', { active: form.target === 'local' }]"
                @click="form.target = 'local'"
              >
                Local
              </button>
              <button
                type="button"
                :class="['segment-button', { active: form.target === 'remote' }]"
                @click="form.target = 'remote'"
              >
                Remote
              </button>
            </div>
          </div>
          <div
            v-if="form.target === 'remote'"
            class="form-group"
          >
            <label for="remoteProfile">Remote Server</label>
            <select
              id="remoteProfile"
              v-model="form.remote_profile_id"
              class="select-input"
              :disabled="remoteProfilesLoading"
            >
              <option
                v-if="remoteProfiles.length === 0"
                value=""
              >
                No remote servers configured
              </option>
              <option
                v-for="profile in remoteProfiles"
                :key="profile.id"
                :value="profile.id"
              >
                {{ profile.name }}
              </option>
            </select>
            <p
              v-if="remoteProfilesError"
              class="form-error"
            >
              {{ remoteProfilesError }}
            </p>
            <p
              v-else-if="remoteProfiles.length === 0"
              class="form-hint"
            >
              Add profiles in ~/.claude_hub/remote_profiles.json or ~/.ssh/config
            </p>
          </div>
          <div class="form-group">
            <label for="tabCwd">Working Directory (optional)</label>
            <div class="cwd-input-wrapper">
              <input
                id="tabCwd"
                v-model="form.cwd"
                type="text"
                :placeholder="form.target === 'remote' ? '~/workspace/project' : 'e.g., ~/Project/my-app'"
              >
              <LoadingButton
                type="button"
                class="cwd-dropdown-btn"
                :disabled="form.target === 'remote' && !form.remote_profile_id"
                :loading="isPending('tab-browser:open')"
                loading-label="Opening browser"
                @click="toggleFileBrowser"
              >
                Browse
              </LoadingButton>
            </div>
          </div>
          <div class="form-group">
            <label for="agentType">Agent Type</label>
            <select
              id="agentType"
              v-model="form.agent_type"
              class="select-input"
            >
              <option value="claude">
                Claude
              </option>
              <option value="codex">
                Codex
              </option>
              <option value="cursor">
                Terminal
              </option>
            </select>
          </div>
          <div
            v-if="supportsSoloMode"
            class="form-group"
          >
            <label class="checkbox-label">
              <div class="checkbox-row">
                <input
                  v-model="form.solo_mode"
                  type="checkbox"
                  class="checkbox-input"
                >
                <span class="checkbox-text">Solo Mode</span>
              </div>
              <span class="checkbox-desc">{{ soloModeDescription }}</span>
            </label>
          </div>
          <div
            v-if="form.target === 'remote'"
            class="form-group"
          >
            <label class="checkbox-label">
              <div class="checkbox-row">
                <input
                  v-model="form.remote_reconnect"
                  type="checkbox"
                  class="checkbox-input"
                >
                <span class="checkbox-text">Auto Reconnect</span>
              </div>
              <span class="checkbox-desc">Reconnect SSH automatically if the network drops</span>
            </label>
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="btn btn-secondary"
              @click="closeCreateModal"
            >
              Cancel
            </button>
            <LoadingButton
              type="submit"
              class="btn btn-primary"
              :disabled="isCreateDisabled"
              :loading="isPending('tab:create')"
              loading-label="Creating tab"
            >
              {{ isLoading ? 'Creating...' : 'Create' }}
            </LoadingButton>
          </div>
        </form>
      </div>
    </div>

    <!-- File Browser Modal -->
    <div
      v-if="showFileBrowser"
      class="modal-overlay file-browser-overlay"
      @click.self="showFileBrowser = false"
    >
      <div class="modal file-browser-modal">
        <div class="file-browser-header">
          <h3>{{ form.target === 'remote' ? 'Select Remote Directory' : 'Select Working Directory' }}</h3>
          <button
            type="button"
            class="btn btn-secondary btn-small"
            @click="showFileBrowser = false"
          >
            Close
          </button>
        </div>
        <div class="file-browser-path">
          <LoadingButton
            type="button"
            class="path-nav-btn"
            title="Home"
            :loading="isPending('tab-browser:home')"
            hide-content-while-loading
            loading-label="Loading home"
            @click="navigateToHome"
          >
            🏠
          </LoadingButton>
          <LoadingButton
            v-if="browserParentPath"
            type="button"
            class="path-nav-btn"
            title="Parent"
            :loading="isPending('tab-browser:up')"
            hide-content-while-loading
            loading-label="Loading parent"
            @click="navigateToParent"
          >
            ↑
          </LoadingButton>
          <input
            v-model="browserPathInput"
            type="text"
            class="current-path-input"
            @keyup.enter="navigateToPath(browserPathInput)"
          >
          <LoadingButton
            type="button"
            class="path-nav-btn"
            title="Refresh"
            :loading="isPending('tab-browser:refresh')"
            hide-content-while-loading
            loading-label="Refreshing directory"
            @click="refreshDirectory"
          >
            ↻
          </LoadingButton>
        </div>
        <div class="file-browser-list">
          <div
            v-if="browserParentPath"
            class="file-item"
            @click="navigateToPath(browserParentPath)"
          >
            <span class="file-icon">⬆️</span>
            <span class="file-name">..</span>
          </div>
          <div
            v-for="item in browserItems"
            :key="item.path"
            :class="['file-item', { 'is-dir': item.is_dir }]"
            @click="handleFileItemClick(item)"
          >
            <span class="file-icon">{{ item.is_dir ? '📁' : '📄' }}</span>
            <span class="file-name">{{ item.name }}</span>
          </div>
          <div
            v-if="browserLoading"
            class="file-loading"
          >
            Loading...
          </div>
          <div
            v-if="browserError"
            class="file-error"
          >
            {{ browserError }}
          </div>
        </div>
        <div class="file-browser-footer">
          <button
            type="button"
            class="btn btn-secondary"
            @click="showFileBrowser = false"
          >
            Cancel
          </button>
          <button
            type="button"
            class="btn btn-primary"
            @click="selectCurrentDirectory"
          >
            Select This Directory
          </button>
        </div>
      </div>
    </div>

    <!-- Close Tab Confirmation Modal -->
    <div
      v-if="showCloseConfirm"
      class="modal-overlay"
      @click.self="showCloseConfirm = false"
    >
      <div class="modal">
        <h3>Close Terminal</h3>
        <p class="confirm-message">
          Are you sure you want to close "{{ tabToClose?.name }}"?
        </p>
        <div class="modal-actions">
          <button
            type="button"
            class="btn btn-secondary"
            @click="showCloseConfirm = false"
          >
            Cancel
          </button>
          <LoadingButton
            type="button"
            class="btn btn-danger"
            :disabled="isLoading"
            :loading="tabToClose ? isPending(tabActionKey('close', tabToClose.id)) : false"
            loading-label="Closing tab"
            @click="confirmCloseTab"
          >
            {{ isLoading ? 'Closing...' : 'Close' }}
          </LoadingButton>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import AgentStatusFloatingPanel from '@/components/AgentStatusFloatingPanel.vue'
import LayoutSelector from '@/components/LayoutSelector.vue'
import LoadingButton from '@/components/LoadingButton.vue'
import { usePendingActions } from '@/composables/usePendingActions'
import { useTerminalStore } from '@/stores/terminalStore'
import type { RemoteProfile, TerminalTab } from '@/types'
import type { AgentRuntimeStatus } from '@/types'

interface FileInfo {
  name: string
  path: string
  is_dir: boolean
  is_symlink: boolean
}

interface DirectoryListing {
  current_path: string
  parent_path: string | null
  items: FileInfo[]
}

const store = useTerminalStore()
const { isPending, runPending } = usePendingActions()
const { tabs, manualTabs, managedTabs, activeTabId, isLoading, agentStatuses } = storeToRefs(store)

const tabStatusById = computed<Record<string, AgentRuntimeStatus>>(() => {
  const map: Record<string, AgentRuntimeStatus> = {}
  for (const s of agentStatuses.value) {
    map[s.tab_id] = s.status
  }
  return map
})

function getTabStatus(tab: TerminalTab): AgentRuntimeStatus {
  return tabStatusById.value[tab.id] ?? (tab.is_active ? 'idle' : 'offline')
}

// Drag and drop state for tab reordering
const draggedTabId = ref<string | null>(null)
const dragOverIndex = ref<number | null>(null)
const fromIndex = ref<number | null>(null)

const showModal = ref(false)
const showCloseConfirm = ref(false)
const showFileBrowser = ref(false)
const tabToClose = ref<TerminalTab | null>(null)
const editingTabId = ref<string | null>(null)
const editingTabName = ref('')
const renameInputRef = ref<HTMLInputElement | null>(null)
const tabsContainerRef = ref<HTMLDivElement | null>(null)
const showLeftFade = ref(false)
const showRightFade = ref(false)
const form = reactive({
  name: '',
  cwd: '',
  solo_mode: false,
  agent_type: 'claude' as 'claude' | 'codex' | 'cursor',
  target: 'local' as 'local' | 'remote',
  remote_profile_id: '',
  remote_reconnect: true,
})

const supportsSoloMode = computed(() => form.agent_type === 'claude' || form.agent_type === 'codex')
const soloModeDescription = computed(() => {
  if (form.agent_type === 'codex') {
    return 'Start Codex with --ask-for-approval never and --sandbox danger-full-access'
  }
  return 'Start Claude with IS_SANDBOX=1 and --dangerously-skip-permissions'
})

// File browser state
const browserCurrentPath = ref('')
const browserPathInput = ref('')
const browserParentPath = ref<string | null>(null)
const browserItems = ref<FileInfo[]>([])
const browserLoading = ref(false)
const browserError = ref<string | null>(null)
const remoteProfiles = ref<RemoteProfile[]>([])
const remoteProfilesLoading = ref(false)
const remoteProfilesError = ref<string | null>(null)

const selectedRemoteProfile = computed(() =>
  remoteProfiles.value.find(profile => profile.id === form.remote_profile_id) || null
)
const isCreateDisabled = computed(
  () =>
    isLoading.value ||
    isPending('tab:create') ||
    (form.target === 'remote' && !form.remote_profile_id)
)

function tabActionKey(action: string, tabId: string | null | undefined) {
  return `tab:${tabId || 'none'}:${action}`
}

async function listDirectory(path?: string): Promise<DirectoryListing> {
  const params = new URLSearchParams()
  if (path) {
    params.append('path', path)
  }
  if (form.target === 'remote') {
    if (!form.remote_profile_id) {
      throw new Error('Select a remote server first')
    }
    params.append('profile_id', form.remote_profile_id)
  }
  const queryString = params.toString()
  const endpoint = form.target === 'remote' ? '/api/remote/filesystem/list' : '/api/filesystem/list'
  const url = `${endpoint}${queryString ? '?' + queryString : ''}`
  const response = await fetch(url)
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to list directory')
  }
  return await response.json()
}

async function loadDirectory(path?: string, pendingKey = 'tab-browser:load') {
  await runPending(pendingKey, async () => {
    browserLoading.value = true
    browserError.value = null
    try {
      const listing = await listDirectory(path)
      browserCurrentPath.value = listing.current_path
      browserPathInput.value = listing.current_path
      browserParentPath.value = listing.parent_path
      browserItems.value = listing.items
    } catch (e) {
      browserError.value = e instanceof Error ? e.message : 'Failed to load directory'
    } finally {
      browserLoading.value = false
    }
  })
}

async function fetchRemoteProfiles() {
  remoteProfilesLoading.value = true
  remoteProfilesError.value = null
  try {
    const response = await fetch('/api/remote/profiles')
    if (!response.ok) throw new Error('Failed to load remote servers')
    remoteProfiles.value = await response.json()
    if (!form.remote_profile_id && remoteProfiles.value.length > 0) {
      form.remote_profile_id = remoteProfiles.value[0].id
    }
  } catch (e) {
    remoteProfilesError.value = e instanceof Error ? e.message : 'Failed to load remote servers'
  } finally {
    remoteProfilesLoading.value = false
  }
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

function handleFileItemClick(item: FileInfo) {
  if (item.is_dir) {
    loadDirectory(item.path)
  }
}

function navigateToPath(path: string, pendingKey = 'tab-browser:path') {
  loadDirectory(path, pendingKey)
}

function navigateToHome() {
  if (form.target === 'remote') {
    loadDirectory(selectedRemoteProfile.value?.default_cwd || '~', 'tab-browser:home')
  } else {
    loadDirectory('~', 'tab-browser:home')
  }
}

function navigateToParent() {
  if (!browserParentPath.value) return
  loadDirectory(browserParentPath.value, 'tab-browser:up')
}

function refreshDirectory() {
  loadDirectory(browserCurrentPath.value || browserPathInput.value || '~', 'tab-browser:refresh')
}

function selectCurrentDirectory() {
  form.cwd = browserCurrentPath.value
  showFileBrowser.value = false
}

async function toggleFileBrowser() {
  if (showFileBrowser.value) {
    showFileBrowser.value = false
  } else {
    showFileBrowser.value = true
    if (form.cwd) {
      await loadDirectory(form.cwd, 'tab-browser:open')
    } else if (form.target === 'remote') {
      await loadDirectory(selectedRemoteProfile.value?.default_cwd || '~', 'tab-browser:open')
    } else {
      await loadDirectory('~', 'tab-browser:open')
    }
  }
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

async function handleTabDuplicate(tabId: string) {
  await runPending(tabActionKey('duplicate', tabId), () => store.duplicateTab(tabId))
}

async function confirmCloseTab() {
  if (tabToClose.value) {
    const tabId = tabToClose.value.id
    await runPending(tabActionKey('close', tabId), async () => {
      await store.deleteTab(tabId)
      tabToClose.value = null
      showCloseConfirm.value = false
    })
  }
}

function openCreateModal() {
  showModal.value = true
  fetchRemoteProfiles()
}

function closeCreateModal() {
  showModal.value = false
  showFileBrowser.value = false
}

watch(showModal, (newVal) => {
  if (!newVal) {
    form.name = ''
    form.cwd = ''
    form.solo_mode = false
    form.agent_type = 'claude'
    form.target = 'local'
    form.remote_profile_id = remoteProfiles.value[0]?.id || ''
    form.remote_reconnect = true
    showFileBrowser.value = false
  }
})

watch(
  () => form.agent_type,
  (agentType) => {
    if (agentType === 'cursor') {
      form.solo_mode = false
    }
  }
)

watch(
  () => form.target,
  (target) => {
    if (target === 'remote') {
      fetchRemoteProfiles()
      if (!form.cwd && selectedRemoteProfile.value?.default_cwd) {
        form.cwd = selectedRemoteProfile.value.default_cwd
      }
    } else {
      form.remote_reconnect = true
    }
  }
)

watch(
  () => form.remote_profile_id,
  () => {
    if (form.target === 'remote' && !form.cwd && selectedRemoteProfile.value?.default_cwd) {
      form.cwd = selectedRemoteProfile.value.default_cwd
    }
  }
)

watch(activeTabId, (tabId) => {
  if (!tabId) return
  nextTick(() => {
    scrollActiveTabIntoView(tabId)
    updateScrollFadeState()
  })
})

watch(
  () => tabs.value.length,
  () => {
    nextTick(() => {
      updateScrollFadeState()
    })
  }
)

onMounted(() => {
  nextTick(() => {
    updateScrollFadeState()
  })
  window.addEventListener('resize', updateScrollFadeState)
})

onUnmounted(() => {
  window.removeEventListener('resize', updateScrollFadeState)
})

async function handleCreateTab() {
  const defaultName = `Tab ${manualTabs.value.length + 1}`
  const name = form.name.trim() || defaultName
  const cwd = form.cwd.trim() || undefined
  const solo_mode = supportsSoloMode.value ? form.solo_mode : false
  const agent_type = form.agent_type
  const target = form.target
  const selectedProfile = selectedRemoteProfile.value
  const remote_profile_id = target === 'remote' ? form.remote_profile_id : undefined
  const remote_cwd = target === 'remote' ? cwd : undefined
  const remote_reconnect = target === 'remote' ? form.remote_reconnect : undefined

  if (target === 'remote' && !selectedProfile) {
    remoteProfilesError.value = 'Select a remote server first'
    return
  }

  const tabName = form.name.trim()
    ? name
    : target === 'remote' && selectedProfile
      ? `${selectedProfile.name} · ${agent_type === 'cursor' ? 'Terminal' : agent_type === 'codex' ? 'Codex' : 'Claude'}`
      : name

  await runPending('tab:create', async () => {
    await store.createTab({
      name: tabName,
      cwd: target === 'local' ? cwd : undefined,
      solo_mode,
      agent_type,
      target,
      remote_profile_id,
      remote_cwd,
      remote_reconnect,
    })

    form.name = ''
    form.cwd = ''
    form.solo_mode = false
    form.agent_type = 'claude'
    form.target = 'local'
    form.remote_profile_id = remoteProfiles.value[0]?.id || ''
    form.remote_reconnect = true
    showFileBrowser.value = false
    showModal.value = false
  })
}
</script>

<style scoped>
.tab-bar {
  display: flex;
  align-items: flex-end;
  background-color: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
  padding: 7px 10px 6px;
  gap: 6px;
  transition: padding 180ms cubic-bezier(0.2, 0, 0, 1), gap 180ms cubic-bezier(0.2, 0, 0, 1);
}

.tabs-shell {
  position: relative;
  flex: 1;
  min-width: 0;
}

.tabs-shell::before,
.tabs-shell::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  width: 14px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.2s;
  z-index: 2;
}

.tabs-shell::before {
  left: 0;
  background: linear-gradient(to right, var(--ch-tab-fade-start), var(--ch-tab-fade-end));
}

.tabs-shell::after {
  right: 0;
  background: linear-gradient(to left, var(--ch-tab-fade-start), var(--ch-tab-fade-end));
}

.tabs-shell.show-left-fade::before {
  opacity: 1;
}

.tabs-shell.show-right-fade::after {
  opacity: 1;
}

.tabs {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  overflow-x: auto;
  overflow-y: hidden;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  -ms-overflow-style: none;
  scroll-behavior: smooth;
  touch-action: pan-x;
}

.tabs::-webkit-scrollbar {
  display: none;
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  box-sizing: border-box;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 0 10px;
  cursor: pointer;
  user-select: none;
  flex: 0 0 auto;
  white-space: nowrap;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast), transform var(--ch-motion-fast), height 180ms cubic-bezier(0.2, 0, 0, 1), padding 180ms cubic-bezier(0.2, 0, 0, 1), gap 180ms cubic-bezier(0.2, 0, 0, 1), border-radius 180ms cubic-bezier(0.2, 0, 0, 1);
}

.tab:hover {
  border-color: var(--ch-color-border-hover);
  background-color: var(--ch-color-surface-control-hover);
}

.tab.active {
  background-color: var(--ch-color-surface-selected);
  border-color: var(--ch-color-accent-ring-strong);
  box-shadow: 0 1px 3px var(--ch-shadow-color-soft);
}

.tab.dragging {
  opacity: 0.4;
  background-color: var(--ch-color-app-bg) !important;
}

.tab.drag-over-left {
  border-left: 2px solid var(--ch-color-accent);
  margin-left: -2px;
}

.tab.drag-over-right {
  border-right: 2px solid var(--ch-color-accent);
  margin-right: -2px;
}

.tab[draggable="true"] {
  cursor: grab;
}

.tab[draggable="true"]:active {
  cursor: grabbing;
}

.tab-name {
  color: var(--ch-color-text);
  font-size: 14px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1;
  transition: font-size 180ms cubic-bezier(0.2, 0, 0, 1), max-width 180ms cubic-bezier(0.2, 0, 0, 1);
}

.tab-name-input {
  background: transparent;
  border: none;
  color: var(--ch-color-text);
  font-size: 14px;
  outline: 1px solid var(--ch-color-accent);
  padding: 2px 4px;
  border-radius: 2px;
  width: 120px;
}

.tab-indicator {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--ch-color-success);
  transition: background-color 120ms ease, box-shadow 120ms ease;
}

.tab-indicator[data-status='idle'] {
  background-color: var(--ch-color-success);
}

.tab-indicator[data-status='working'] {
  background-color: var(--ch-color-warning);
  box-shadow: 0 0 6px var(--ch-color-warning-bg);
}

.tab-indicator[data-status='attention'] {
  background-color: var(--ch-color-attention);
  box-shadow: 0 0 6px var(--ch-color-attention-bg);
}

.tab-indicator[data-status='offline'] {
  background-color: var(--ch-color-text-subtle);
}

.pane-indicator {
  background-color: var(--ch-color-accent);
  color: var(--ch-color-text-inverse);
  font-size: 10px;
  font-weight: bold;
  padding: 1px 5px;
  border-radius: 8px;
  line-height: 1;
  min-width: 14px;
  text-align: center;
}

.tab-close {
  background: none;
  border: none;
  color: var(--ch-color-text-soft);
  font-size: 17px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
  border-radius: var(--ch-radius-sm);
  transition: color var(--ch-motion-fast), background var(--ch-motion-fast), font-size 180ms cubic-bezier(0.2, 0, 0, 1), padding 180ms cubic-bezier(0.2, 0, 0, 1);
}

.tab-close:hover {
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
}

.tab-duplicate {
  background: none;
  border: none;
  color: var(--ch-color-text-soft);
  font-size: 12px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
  opacity: 0;
  transition: opacity 0.2s;
}

.tab:hover .tab-duplicate {
  opacity: 1;
}

.tab-duplicate:hover {
  color: var(--ch-color-text);
}

.add-tab {
  align-self: flex-end;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-muted);
  box-sizing: border-box;
  color: var(--ch-color-text);
  font-size: 18px;
  width: 30px;
  height: 30px;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast), width 180ms cubic-bezier(0.2, 0, 0, 1), height 180ms cubic-bezier(0.2, 0, 0, 1), border-radius 180ms cubic-bezier(0.2, 0, 0, 1);
}

.add-tab:hover:not(:disabled) {
  background-color: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border-hover);
  color: var(--ch-color-text);
}

.add-tab:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Modal Styles */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: var(--ch-color-overlay-soft);
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding: 16px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  z-index: 1000;
}

.file-browser-overlay {
  z-index: 1100;
}

.modal {
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: 8px;
  padding: 24px;
  min-width: 400px;
  width: min(520px, 100%);
  max-width: 100%;
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  display: flex;
  flex-direction: column;
}

.file-browser-modal {
  min-width: 500px;
  width: 80%;
  max-width: 600px;
  height: 70vh;
  max-height: 600px;
  padding: 16px;
  overflow: hidden;
}

.file-browser-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-shrink: 0;
}

.file-browser-header h3 {
  margin: 0;
}

.file-browser-path {
  display: flex;
  align-items: center;
  gap: 8px;
  background-color: var(--ch-color-surface-control);
  padding: 8px 12px;
  border-radius: 4px;
  margin-bottom: 12px;
  flex-shrink: 0;
}

.path-nav-btn {
  background: none;
  border: none;
  font-size: 18px;
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
}

.path-nav-btn:hover {
  background-color: var(--ch-color-surface-control-hover);
}

.current-path-input {
  min-width: 0;
  flex: 1;
  background-color: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  color: var(--ch-color-text);
  font-size: 13px;
  font-family: monospace;
  padding: 6px 8px;
}

.current-path-input:focus {
  outline: none;
  border-color: var(--ch-color-accent);
}

.current-path {
  color: var(--ch-color-text);
  font-size: 13px;
  font-family: monospace;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-browser-list {
  flex: 1;
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: 4px;
  background-color: var(--ch-color-app-bg);
  margin-bottom: 16px;
  min-height: 200px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  color: var(--ch-color-text);
}

.file-item:hover {
  background-color: var(--ch-color-surface-control);
}

.file-item.is-dir {
  color: var(--ch-color-accent);
}

.file-icon {
  font-size: 16px;
}

.file-name {
  font-size: 14px;
}

.file-loading,
.file-error {
  padding: 16px;
  text-align: center;
  color: var(--ch-color-text-soft);
}

.file-error {
  color: var(--ch-color-danger-strong);
}

.file-browser-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  flex-shrink: 0;
}

.modal h3 {
  margin: 0 0 20px 0;
  color: var(--ch-color-text);
  font-size: 18px;
}

.confirm-message {
  color: var(--ch-color-text);
  font-size: 14px;
  margin: 0 0 24px 0;
}

.form-group {
  margin-bottom: 16px;
  position: relative;
}

.form-group label {
  display: block;
  color: var(--ch-color-text);
  margin-bottom: 6px;
  font-size: 14px;
}

.form-group input {
  width: 100%;
  padding: 10px 12px;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  color: var(--ch-color-text);
  font-size: 14px;
  box-sizing: border-box;
}

.form-group input:focus,
.form-group select:focus {
  outline: none;
  border-color: var(--ch-color-accent);
}

.form-error {
  color: var(--ch-color-danger);
  font-size: 12px;
  margin: 6px 0 0 0;
}

.form-hint {
  color: var(--ch-color-text-soft);
  font-size: 12px;
  margin: 6px 0 0 0;
}

.segmented-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  background-color: var(--ch-color-surface-sunken);
  border: 1px solid var(--ch-color-border);
  border-radius: 4px;
  padding: 4px;
}

.segment-button {
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: 14px;
  padding: 8px 10px;
}

.segment-button.active {
  background-color: var(--ch-color-surface-control);
  border-color: var(--ch-color-border-hover);
  color: var(--ch-color-text);
}

.segment-button:hover {
  color: var(--ch-color-text);
}

.select-input {
  width: 100%;
  padding: 10px 12px;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  color: var(--ch-color-text);
  font-size: 14px;
  box-sizing: border-box;
  cursor: pointer;
}

.select-input:hover {
  border-color: var(--ch-color-border-hover);
}

.cwd-input-wrapper {
  position: relative;
  display: flex;
}

.cwd-input-wrapper input {
  flex: 1;
  border-top-right-radius: 0;
  border-bottom-right-radius: 0;
}

.cwd-dropdown-btn {
  background-color: var(--ch-color-surface-control-hover);
  border: 1px solid var(--ch-color-border-strong);
  border-left: none;
  border-top-right-radius: 4px;
  border-bottom-right-radius: 4px;
  color: var(--ch-color-text);
  padding: 0 12px;
  cursor: pointer;
  font-size: 14px;
}

.cwd-dropdown-btn:hover {
  background-color: var(--ch-color-surface-pressed);
  color: var(--ch-color-text);
}

.cwd-dropdown-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.checkbox-label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  cursor: pointer;
}

.checkbox-row {
  display: flex;
  align-items: center;
}

.checkbox-input {
  margin-right: 8px;
  width: 16px;
  height: 16px;
  cursor: pointer;
}

.checkbox-text {
  color: var(--ch-color-text);
  font-size: 14px;
  font-weight: 500;
}

.checkbox-desc {
  color: var(--ch-color-text-soft);
  font-size: 12px;
  margin-left: 24px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 24px;
}

.btn {
  padding: 10px 20px;
  border: none;
  border-radius: 4px;
  font-size: 14px;
  cursor: pointer;
  transition: background-color 0.2s;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-small {
  padding: 6px 12px;
  font-size: 12px;
}

.btn-secondary {
  background-color: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.btn-secondary:hover:not(:disabled) {
  background-color: var(--ch-color-surface-pressed);
  color: var(--ch-color-text);
}

.btn-primary {
  background-color: var(--ch-color-accent);
  color: var(--ch-color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background-color: var(--ch-color-accent-hover);
}

.btn-danger {
  background-color: var(--ch-color-danger-strong);
  color: var(--ch-color-text-inverse);
}

.btn-danger:hover:not(:disabled) {
  background-color: var(--ch-color-danger-hover);
}

@media (max-width: 640px) {
  .modal-overlay {
    align-items: flex-start;
    justify-content: flex-start;
    padding: 10px;
  }

  .modal {
    min-width: 0;
    width: 100%;
    max-height: calc(100dvh - 20px);
    padding: 16px;
    border-radius: 6px;
  }

  .file-browser-modal {
    min-width: 0;
    width: 100%;
    height: calc(100dvh - 20px);
    max-height: calc(100dvh - 20px);
  }

  .file-browser-path {
    padding: 8px;
  }

  .modal-actions,
  .file-browser-footer {
    position: sticky;
    bottom: -1px;
    background-color: var(--ch-color-surface);
    padding-top: 12px;
  }

  .modal-actions .btn,
  .file-browser-footer .btn {
    flex: 1;
  }
}
</style>
