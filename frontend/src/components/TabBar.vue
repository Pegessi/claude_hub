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
      <div ref="tabsContainerRef" class="tabs" @scroll="handleTabsScroll">
        <div
          v-for="(tab, index) in tabs"
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
            v-model="editingTabName"
            type="text"
            class="tab-name-input"
            @blur="handleRenameTab"
            @keyup.enter="handleRenameTab"
            @keyup.escape="cancelRename"
            ref="renameInputRef"
          />
          <span v-else class="tab-name" @dblclick.stop="startRename(tab)">{{ tab.name }}</span>
          <span v-if="tab.is_active" class="tab-indicator"></span>
          <span v-if="getPaneCountForTab(tab.id) > 0" class="pane-indicator">
            {{ getPaneCountForTab(tab.id) }}
          </span>
          <button class="tab-duplicate" @click.stop="handleTabDuplicate(tab.id)" title="Duplicate tab">📋</button>
          <button class="tab-close" @click.stop="handleTabClose(tab.id)">×</button>
        </div>
      </div>
    </div>
    <button
      class="add-tab"
      @click="openCreateModal"
      :disabled="isLoading"
    >
      {{ isLoading ? '...' : '+' }}
    </button>

    <!-- Create Tab Modal -->
    <div v-if="showModal" class="modal-overlay" @click.self="closeCreateModal">
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
            />
          </div>
          <div class="form-group">
            <label for="tabCwd">Working Directory (optional)</label>
            <div class="cwd-input-wrapper">
              <input
                id="tabCwd"
                v-model="form.cwd"
                type="text"
                placeholder="e.g., ~/Project/my-app"
              />
              <button type="button" class="cwd-dropdown-btn" @click="toggleFileBrowser">
                📁
              </button>
            </div>
          </div>
          <div class="form-group">
            <label for="agentType">Agent Type</label>
            <select
              id="agentType"
              v-model="form.agent_type"
              class="select-input"
            >
              <option value="claude">Claude</option>
              <option value="codex">Codex</option>
              <option value="cursor">Terminal</option>
            </select>
          </div>
          <div v-if="supportsSoloMode" class="form-group">
            <label class="checkbox-label">
              <div class="checkbox-row">
                <input
                  type="checkbox"
                  v-model="form.solo_mode"
                  class="checkbox-input"
                />
                <span class="checkbox-text">Solo Mode</span>
              </div>
              <span class="checkbox-desc">{{ soloModeDescription }}</span>
            </label>
          </div>
          <div class="modal-actions">
            <button type="button" class="btn btn-secondary" @click="closeCreateModal">Cancel</button>
            <button type="submit" class="btn btn-primary" :disabled="isLoading">
              {{ isLoading ? 'Creating...' : 'Create' }}
            </button>
          </div>
        </form>
      </div>
    </div>

    <!-- File Browser Modal -->
    <div v-if="showFileBrowser" class="modal-overlay file-browser-overlay" @click.self="showFileBrowser = false">
      <div class="modal file-browser-modal">
        <div class="file-browser-header">
          <h3>Select Working Directory</h3>
          <button type="button" class="btn btn-secondary btn-small" @click="showFileBrowser = false">Close</button>
        </div>
        <div class="file-browser-path">
          <button type="button" class="path-nav-btn" @click="navigateToHome" title="Home">🏠</button>
          <span class="current-path">{{ browserCurrentPath }}</span>
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
          <div v-if="browserLoading" class="file-loading">
            Loading...
          </div>
          <div v-if="browserError" class="file-error">
            {{ browserError }}
          </div>
        </div>
        <div class="file-browser-footer">
          <button type="button" class="btn btn-secondary" @click="showFileBrowser = false">Cancel</button>
          <button type="button" class="btn btn-primary" @click="selectCurrentDirectory">Select This Directory</button>
        </div>
      </div>
    </div>

    <!-- Close Tab Confirmation Modal -->
    <div v-if="showCloseConfirm" class="modal-overlay" @click.self="showCloseConfirm = false">
      <div class="modal">
        <h3>Close Terminal</h3>
        <p class="confirm-message">Are you sure you want to close "{{ tabToClose?.name }}"?</p>
        <div class="modal-actions">
          <button type="button" class="btn btn-secondary" @click="showCloseConfirm = false">Cancel</button>
          <button type="button" class="btn btn-danger" :disabled="isLoading" @click="confirmCloseTab">
            {{ isLoading ? 'Closing...' : 'Close' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import type { TerminalTab } from '@/types'

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
const { tabs, activeTabId, isLoading } = storeToRefs(store)

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
const browserParentPath = ref<string | null>(null)
const browserItems = ref<FileInfo[]>([])
const browserLoading = ref(false)
const browserError = ref<string | null>(null)

async function listDirectory(path?: string): Promise<DirectoryListing> {
  const params = new URLSearchParams()
  if (path) {
    params.append('path', path)
  }
  const queryString = params.toString()
  const url = `/api/filesystem/list${queryString ? '?' + queryString : ''}`
  const response = await fetch(url)
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to list directory')
  }
  return await response.json()
}

async function loadDirectory(path?: string) {
  browserLoading.value = true
  browserError.value = null
  try {
    const listing = await listDirectory(path)
    browserCurrentPath.value = listing.current_path
    browserParentPath.value = listing.parent_path
    browserItems.value = listing.items
  } catch (e) {
    browserError.value = e instanceof Error ? e.message : 'Failed to load directory'
  } finally {
    browserLoading.value = false
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

function navigateToPath(path: string) {
  loadDirectory(path)
}

function navigateToHome() {
  loadDirectory('~')
}

function selectCurrentDirectory() {
  form.cwd = browserCurrentPath.value
  showFileBrowser.value = false
}

function toggleFileBrowser() {
  if (showFileBrowser.value) {
    showFileBrowser.value = false
  } else {
    showFileBrowser.value = true
    if (form.cwd) {
      loadDirectory(form.cwd)
    } else {
      loadDirectory('~')
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
    await store.updateTab(editingTabId.value, { name: editingTabName.value.trim() })
  }
  editingTabId.value = null
  editingTabName.value = ''
}

async function handleTabDuplicate(tabId: string) {
  const tab = tabs.value.find(t => t.id === tabId)
  if (!tab) return

  const data = {
    name: `${tab.name} (copy)`,
    cwd: tab.cwd,
    solo_mode: false,
    agent_type: tab.agent_type || 'claude',
  }
  await store.createTab(data)
}

async function confirmCloseTab() {
  if (tabToClose.value) {
    await store.deleteTab(tabToClose.value.id)
    tabToClose.value = null
    showCloseConfirm.value = false
  }
}

function openCreateModal() {
  showModal.value = true
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
  const defaultName = `Tab ${tabs.value.length + 1}`
  const name = form.name.trim() || defaultName
  const cwd = form.cwd.trim() || undefined
  const solo_mode = supportsSoloMode.value ? form.solo_mode : false
  const agent_type = form.agent_type

  await store.createTab({ name, cwd, solo_mode, agent_type })

  form.name = ''
  form.cwd = ''
  form.solo_mode = false
  form.agent_type = 'claude'
  showFileBrowser.value = false
  showModal.value = false
}
</script>

<style scoped>
.tab-bar {
  display: flex;
  align-items: center;
  background-color: #1e1e1e;
  border-bottom: 1px solid #333;
  padding: 4px 8px;
  gap: 4px;
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
  background: linear-gradient(to right, #1e1e1e, rgba(30, 30, 30, 0));
}

.tabs-shell::after {
  right: 0;
  background: linear-gradient(to left, #1e1e1e, rgba(30, 30, 30, 0));
}

.tabs-shell.show-left-fade::before {
  opacity: 1;
}

.tabs-shell.show-right-fade::after {
  opacity: 1;
}

.tabs {
  display: flex;
  gap: 4px;
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
  background-color: #2d2d2d;
  padding: 6px 12px;
  border-radius: 4px 4px 0 0;
  cursor: pointer;
  user-select: none;
  flex: 0 0 auto;
  white-space: nowrap;
}

.tab.active {
  background-color: #3c3c3c;
}

.tab.dragging {
  opacity: 0.4;
  background-color: #1a1a1a !important;
}

.tab.drag-over-left {
  border-left: 2px solid #60a5fa;
  margin-left: -2px;
}

.tab.drag-over-right {
  border-right: 2px solid #60a5fa;
  margin-right: -2px;
}

.tab[draggable="true"] {
  cursor: grab;
}

.tab[draggable="true"]:active {
  cursor: grabbing;
}

.tab-name {
  color: #ccc;
  font-size: 14px;
}

.tab-name-input {
  background: transparent;
  border: none;
  color: #fff;
  font-size: 14px;
  outline: 1px solid #60a5fa;
  padding: 2px 4px;
  border-radius: 2px;
  width: 120px;
}

.tab-indicator {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: #22c55e;
}

.pane-indicator {
  background-color: #60a5fa;
  color: white;
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
  color: #888;
  font-size: 18px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}

.tab-close:hover {
  color: #fff;
}

.tab-duplicate {
  background: none;
  border: none;
  color: #888;
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
  color: #fff;
}

.add-tab {
  background-color: #2d2d2d;
  border: none;
  color: #ccc;
  font-size: 20px;
  width: 28px;
  height: 28px;
  border-radius: 4px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
}

.add-tab:hover:not(:disabled) {
  background-color: #3c3c3c;
  color: #fff;
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
  background-color: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.file-browser-overlay {
  z-index: 1100;
}

.modal {
  background-color: #1e1e1e;
  border: 1px solid #333;
  border-radius: 8px;
  padding: 24px;
  min-width: 400px;
  max-width: 90%;
  max-height: 90vh;
  overflow: hidden;
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
  background-color: #2d2d2d;
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
  background-color: #3c3c3c;
}

.current-path {
  color: #ccc;
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
  border: 1px solid #333;
  border-radius: 4px;
  background-color: #1a1a1a;
  margin-bottom: 16px;
  min-height: 200px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  color: #ccc;
}

.file-item:hover {
  background-color: #2d2d2d;
}

.file-item.is-dir {
  color: #60a5fa;
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
  color: #888;
}

.file-error {
  color: #dc2626;
}

.file-browser-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  flex-shrink: 0;
}

.modal h3 {
  margin: 0 0 20px 0;
  color: #fff;
  font-size: 18px;
}

.confirm-message {
  color: #ccc;
  font-size: 14px;
  margin: 0 0 24px 0;
}

.form-group {
  margin-bottom: 16px;
  position: relative;
}

.form-group label {
  display: block;
  color: #ccc;
  margin-bottom: 6px;
  font-size: 14px;
}

.form-group input {
  width: 100%;
  padding: 10px 12px;
  background-color: #2d2d2d;
  border: 1px solid #444;
  border-radius: 4px;
  color: #fff;
  font-size: 14px;
  box-sizing: border-box;
}

.form-group input:focus,
.form-group select:focus {
  outline: none;
  border-color: #60a5fa;
}

.select-input {
  width: 100%;
  padding: 10px 12px;
  background-color: #2d2d2d;
  border: 1px solid #444;
  border-radius: 4px;
  color: #fff;
  font-size: 14px;
  box-sizing: border-box;
  cursor: pointer;
}

.select-input:hover {
  border-color: #555;
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
  background-color: #3c3c3c;
  border: 1px solid #444;
  border-left: none;
  border-top-right-radius: 4px;
  border-bottom-right-radius: 4px;
  color: #ccc;
  padding: 0 12px;
  cursor: pointer;
  font-size: 14px;
}

.cwd-dropdown-btn:hover {
  background-color: #4a4a4a;
  color: #fff;
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
  color: #fff;
  font-size: 14px;
  font-weight: 500;
}

.checkbox-desc {
  color: #888;
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
  background-color: #3c3c3c;
  color: #ccc;
}

.btn-secondary:hover:not(:disabled) {
  background-color: #4a4a4a;
  color: #fff;
}

.btn-primary {
  background-color: #60a5fa;
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  background-color: #2563eb;
}

.btn-danger {
  background-color: #dc2626;
  color: #fff;
}

.btn-danger:hover:not(:disabled) {
  background-color: #b91c1c;
}
</style>
