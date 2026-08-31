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
            class="tab-kind"
            :data-kind="tab.session_kind"
            :title="tab.session_kind === 'agent' ? 'Agent session' : 'Terminal session'"
          >{{ tab.session_kind === 'agent' ? 'A' : '>_' }}</span>
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
          <!-- Hover-revealed ⋯ dropdown for secondary actions; × close stays always visible on hover -->
          <button
            :ref="(el: unknown) => setTabMenuTriggerRef(tab.id, el)"
            type="button"
            class="tab-menu-trigger"
            :class="{ 'is-open': openTabMenuId === tab.id }"
            :aria-label="`${tab.name} actions`"
            :aria-expanded="openTabMenuId === tab.id"
            title="Tab actions"
            @click.stop="toggleTabMenu(tab.id)"
          >
            ⋯
          </button>
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
    <!-- Tab menu panel is teleported to body so it escapes the .tabs overflow-y:hidden -->
    <Teleport to="body">
      <div
        v-if="openTabMenuId"
        ref="tabMenuPanelRef"
        class="tab-menu-panel"
        role="menu"
        :style="tabMenuPanelStyle"
      >
        <button
          v-if="openTabMenuTab"
          type="button"
          class="tab-menu-item"
          role="menuitem"
          @click="startRename(openTabMenuTab); closeTabMenu(openTabMenuId)"
        >
          <span
            class="tab-menu-item-icon"
            aria-hidden="true"
          >✎</span>
          <span>Rename</span>
        </button>
        <LoadingButton
          v-if="openTabMenuTab"
          type="button"
          class="tab-menu-item"
          role="menuitem"
          :loading="isPending(tabActionKey('duplicate', openTabMenuTab.id))"
          loading-label="Duplicating…"
          @click="handleTabDuplicate(openTabMenuTab.id); closeTabMenu(openTabMenuId)"
        >
          <span
            class="tab-menu-item-icon"
            aria-hidden="true"
          >📋</span>
          <span>Duplicate</span>
        </LoadingButton>
        <LoadingButton
          v-if="openTabMenuTab && (openTabMenuTab.agent_type === 'claude' || openTabMenuTab.agent_type === 'codex')"
          type="button"
          class="tab-menu-item"
          role="menuitem"
          :loading="isPending(tabActionKey('switch-env', openTabMenuTab.id))"
          loading-label="Switching env…"
          @click="openSwitchEnvModal(openTabMenuTab); closeTabMenu(openTabMenuId)"
        >
          <span
            class="tab-menu-item-icon"
            aria-hidden="true"
          >⚙</span>
          <span>Switch Env / Model…</span>
        </LoadingButton>
      </div>
    </Teleport>
    <button
      class="add-tab"
      :disabled="isLoading"
      @click="openCreateModal"
    >
      {{ isLoading ? '...' : '+' }}
    </button>
    <LayoutSelector variant="menu" />
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
          <NetworkAccessMenu variant="menu" />
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
        <h3>Create New Session</h3>
        <form @submit.prevent="handleCreateTab">
          <div class="form-group">
            <label>Session Type</label>
            <div class="segmented-control session-kind-control">
              <button
                type="button"
                :class="['segment-button', { active: form.session_kind === 'agent' }]"
                @click="setSessionKind('agent')"
              >
                Agent
              </button>
              <button
                type="button"
                :class="['segment-button', { active: form.session_kind === 'terminal' }]"
                @click="setSessionKind('terminal')"
              >
                Terminal
              </button>
            </div>
            <p class="form-hint">
              {{ form.session_kind === 'agent'
                ? 'Structured conversation with messages, images, and parsed agent output.'
                : 'Native terminal UI with full keyboard and shell interaction.' }}
            </p>
          </div>
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
          <AgentConfigFields
            v-model:agent-type="form.agent_type"
            v-model:solo-mode="form.solo_mode"
            v-model:env-preset="form.env_preset"
            v-model:env-text="form.env_text"
            variant="form"
            :allow-terminal="form.session_kind === 'terminal'"
            :type-label="form.session_kind === 'agent' ? 'Agent Provider' : 'Terminal Profile'"
            solo-label="Solo Mode"
          />
          <CodexSessionSelector
            v-if="form.agent_type === 'codex' && form.target === 'local'"
            v-model:session-id="form.agent_session_id"
          />
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
              class="ch-btn"
              @click="closeCreateModal"
            >
              Cancel
            </button>
            <LoadingButton
              type="submit"
              class="ch-btn ch-btn--primary"
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
            class="ch-btn ch-btn--sm"
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
            class="ch-btn"
            @click="showFileBrowser = false"
          >
            Cancel
          </button>
          <button
            type="button"
            class="ch-btn ch-btn--primary"
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
            class="ch-btn"
            @click="showCloseConfirm = false"
          >
            Cancel
          </button>
          <LoadingButton
            type="button"
            class="ch-btn ch-btn--danger"
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

    <!-- Switch Env Modal -->
    <div
      v-if="showSwitchEnv"
      class="modal-overlay"
      @click.self="closeSwitchEnvModal"
    >
      <div class="modal switch-env-modal">
        <div class="switch-env-header">
          <div
            class="switch-env-icon"
            aria-hidden="true"
          >
            ⚙
          </div>
          <div class="switch-env-title-block">
            <h3>Switch Environment</h3>
            <p class="switch-env-subtitle">
              {{ switchEnvTab?.name }}
            </p>
          </div>
        </div>
        <p class="switch-env-callout">
          <span
            class="switch-env-callout-icon"
            aria-hidden="true"
          >↻</span>
          <span>
            The agent will restart and automatically resume its conversation.
            In-flight generation will be interrupted.
          </span>
        </p>
        <form @submit.prevent="handleSwitchEnv">
          <div class="form-group env-editor">
            <label>Environment Preset</label>
            <div class="env-preset-row">
              <select
                v-model="switchEnvForm.env_preset"
                class="select-input"
                @change="applySwitchEnvPreset(switchEnvForm.env_preset)"
              >
                <option
                  v-for="preset in envPresets"
                  :key="preset.id"
                  :value="preset.id"
                >
                  {{ preset.name }}
                </option>
                <option value="custom">
                  Custom (current values)
                </option>
              </select>
              <button
                type="button"
                class="ch-btn ch-btn--sm env-manage-button"
                @click="openSwitchEnvPresetManager"
              >
                Manage
              </button>
            </div>
          </div>
          <div class="form-group">
            <label for="switchEnvText">
              Environment Variables
              <span class="field-hint-inline">(KEY=VALUE, one per line)</span>
            </label>
            <textarea
              id="switchEnvText"
              v-model="switchEnvForm.env_text"
              class="select-input env-textarea"
              rows="6"
              placeholder="ANTHROPIC_MODEL=claude-sonnet-4-5&#10;ANTHROPIC_BASE_URL=https://..."
            />
            <p class="form-hint">
              These fully replace the tab's current environment. Include
              <code>ANTHROPIC_MODEL</code> to switch models.
            </p>
          </div>
          <div class="form-group">
            <label class="checkbox-label">
              <div class="checkbox-row">
                <input
                  v-model="switchEnvForm.solo_mode"
                  type="checkbox"
                  class="checkbox-input"
                >
                <span class="checkbox-text">Solo Mode</span>
              </div>
              <span
                v-if="switchEnvTab?.agent_type === 'codex'"
                class="checkbox-desc"
              >
                Relaunch with <code>--ask-for-approval never</code> and
                <code>--sandbox danger-full-access</code>.
              </span>
              <span
                v-else
                class="checkbox-desc"
              >
                Relaunch with <code>IS_SANDBOX=1</code> and
                <code>--dangerously-skip-permissions</code>.
              </span>
            </label>
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="ch-btn"
              @click="closeSwitchEnvModal"
            >
              Cancel
            </button>
            <LoadingButton
              type="submit"
              class="ch-btn ch-btn--primary switch-env-submit"
              :loading="switchEnvTab ? isPending(tabActionKey('switch-env', switchEnvTab.id)) : false"
              loading-label="Restarting…"
            >
              Restart Agent
            </LoadingButton>
          </div>
        </form>
      </div>
    </div>

    <!-- Env Preset Manager Modal -->
    <EnvPresetManager
      v-model:model-value="switchEnvForm.env_preset"
      :visible="showSwitchEnvManager"
      @close="closeSwitchEnvPresetManager"
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
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import type { CSSProperties } from 'vue'
import { storeToRefs } from 'pinia'
import AgentStatusFloatingPanel from '@/components/AgentStatusFloatingPanel.vue'
import LayoutSelector from '@/components/LayoutSelector.vue'
import LoadingButton from '@/components/LoadingButton.vue'
import NetworkAccessMenu from '@/components/NetworkAccessMenu.vue'
import {
  defaultLaunchEnvPresetForAgent,
  parseLaunchEnv,
  useLaunchEnvPresets,
} from '@/composables/useLaunchEnvPresets'
import AgentConfigFields from '@/components/AgentConfigFields.vue'
import CodexSessionSelector from '@/components/CodexSessionSelector.vue'
import EnvPresetManager from '@/components/EnvPresetManager.vue'
import { usePendingActions } from '@/composables/usePendingActions'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import type { AppMode, RemoteProfile, TerminalTab } from '@/types'
import type { AgentRuntimeStatus, AgentType, SessionKind, SwitchEnvRequest } from '@/types'

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
const appStore = useAppStore()
const { envPresets, getPresetText, defaultPresetTextForAgent } = useLaunchEnvPresets()
const { isPending, runPending } = usePendingActions()
const { tabs, manualTabs, managedTabs, activeTabId, isLoading, agentStatuses, notifications } = storeToRefs(store)
const { mode, colorScheme } = storeToRefs(appStore)
// Expose notification actions so template can call them (F5 toast stack)
const { dismissNotification } = store

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
const showSwitchEnv = ref(false)
const showSwitchEnvManager = ref(false)
const tabToClose = ref<TerminalTab | null>(null)
const switchEnvTab = ref<TerminalTab | null>(null)
const editingTabId = ref<string | null>(null)
const editingTabName = ref('')
const renameInputRef = ref<HTMLInputElement | null>(null)
const mobileAppMenuRef = ref<HTMLDetailsElement | null>(null)
const tabsContainerRef = ref<HTMLDivElement | null>(null)
// ---- Tab actions popover (⋯) ----
// A single popover is open at a time; we teleport the panel to <body> so it
// escapes the .tabs { overflow-y: hidden } clipping rectangle, and position
// it using the trigger button's bounding rect.
const openTabMenuId = ref<string | null>(null)
const tabMenuTriggerRefs = new Map<string, HTMLElement>()
const tabMenuPanelRef = ref<HTMLElement | null>(null)

const openTabMenuTab = computed<TerminalTab | null>(() => {
  const id = openTabMenuId.value
  if (!id) return null
  return manualTabs.value.find(t => t.id === id) ?? null
})

const tabMenuPanelStyle = computed<CSSProperties>(() => {
  const id = openTabMenuId.value
  if (!id) return {}
  const trigger = tabMenuTriggerRefs.get(id)
  if (!trigger) return {}
  const rect = trigger.getBoundingClientRect()
  // Align the panel's right edge with the trigger's right edge; place it
  // just below the tab bar with a small gap.
  const panelWidth = 200
  const panelHeightEst = 140
  let top = rect.bottom + 6
  let left = rect.right - panelWidth
  // Keep within viewport.
  const vw = window.innerWidth
  const vh = window.innerHeight
  if (left < 8) left = 8
  if (left + panelWidth > vw - 8) left = vw - panelWidth - 8
  if (top + panelHeightEst > vh - 8) top = Math.max(8, rect.top - panelHeightEst - 6)
  return {
    position: 'fixed',
    top: `${top}px`,
    left: `${left}px`,
    width: `${panelWidth}px`,
  }
})

function setTabMenuTriggerRef(tabId: string, el: unknown) {
  if (el instanceof HTMLElement) {
    tabMenuTriggerRefs.set(tabId, el)
  } else {
    tabMenuTriggerRefs.delete(tabId)
  }
}

function toggleTabMenu(tabId: string) {
  if (openTabMenuId.value === tabId) {
    openTabMenuId.value = null
  } else {
    openTabMenuId.value = tabId
  }
}

function closeTabMenu(tabId?: string | null) {
  if (tabId == null || openTabMenuId.value === tabId) {
    openTabMenuId.value = null
  }
}
const showLeftFade = ref(false)
const showRightFade = ref(false)
const form = reactive({
  name: '',
  cwd: '',
  session_kind: 'agent' as SessionKind,
  solo_mode: false,
  agent_type: 'claude' as AgentType,
  target: 'local' as 'local' | 'remote',
  remote_profile_id: '',
  remote_reconnect: true,
  env_preset: defaultLaunchEnvPresetForAgent('claude'),
  env_text: defaultPresetTextForAgent('claude'),
  agent_session_id: '',
})

const switchEnvForm = reactive({
  env_preset: 'custom' as string,
  env_text: '',
  solo_mode: false,
})

const supportsSoloMode = computed(() => form.agent_type === 'claude' || form.agent_type === 'codex')

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

function resetEnvForAgentType(agentType: AgentType) {
  form.env_preset = defaultLaunchEnvPresetForAgent(agentType)
  form.env_text = defaultPresetTextForAgent(agentType)
}

function setSessionKind(kind: SessionKind) {
  form.session_kind = kind
  if (kind === 'agent' && form.agent_type === 'terminal') {
    form.agent_type = 'claude'
    resetEnvForAgentType(form.agent_type)
  }
}

function agentTypeLabel(agentType: AgentType): string {
  switch (agentType) {
    case 'codex':
      return 'Codex'
    case 'cursor':
      return 'Cursor'
    case 'terminal':
      return 'Terminal'
    case 'claude':
    default:
      return 'Claude'
  }
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

function serializeEnv(env: Record<string, string> | undefined): string {
  if (!env) return ''
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join('\n')
}

function openSwitchEnvModal(tab: TerminalTab) {
  switchEnvTab.value = tab
  switchEnvForm.env_preset = 'custom'
  switchEnvForm.env_text = serializeEnv(tab.env)
  switchEnvForm.solo_mode = tab.solo_mode ?? false
  showSwitchEnv.value = true
}

function closeSwitchEnvModal() {
  showSwitchEnv.value = false
  showSwitchEnvManager.value = false
  switchEnvTab.value = null
}

function applySwitchEnvPreset(presetId: string) {
  if (presetId === 'custom') return
  const text = getPresetText(presetId)
  if (text === null) return
  switchEnvForm.env_text = text
}

function openSwitchEnvPresetManager() {
  showSwitchEnvManager.value = true
}

function closeSwitchEnvPresetManager() {
  showSwitchEnvManager.value = false
  applySwitchEnvPreset(switchEnvForm.env_preset)
}

async function handleSwitchEnv() {
  const tab = switchEnvTab.value
  if (!tab) return
  const env = parseLaunchEnv(switchEnvForm.env_text)
  if (!env) {
    store.pushNotification({
      type: 'error',
      message: 'Please provide at least one KEY=VALUE environment variable, or pick a preset.',
      autoDismissMs: 6000,
    })
    return
  }
  const payload: SwitchEnvRequest = {
    env,
    solo_mode: switchEnvForm.solo_mode,
  }
  const tabId = tab.id
  try {
    await runPending(tabActionKey('switch-env', tabId), async () => {
      await store.switchEnv(tabId, payload)
    })
    store.pushNotification({
      type: 'success',
      message: `Environment switched for "${tab.name}". Agent is resuming conversation.`,
      autoDismissMs: 4000,
    })
    closeSwitchEnvModal()
  } catch (e) {
    // switchEnv already notifies; let the error surface to console too.
    console.error('switch env failed', e)
  }
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

function closeMobileAppMenu() {
  if (mobileAppMenuRef.value) {
    mobileAppMenuRef.value.open = false
  }
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
  // Close the tab-actions popover when clicking outside both the trigger
  // button and the (teleported) panel.
  if (openTabMenuId.value) {
    const trigger = tabMenuTriggerRefs.get(openTabMenuId.value)
    const panel = tabMenuPanelRef.value
    const inTrigger = trigger && trigger.contains(target)
    const inPanel = panel && panel.contains(target)
    if (!inTrigger && !inPanel) {
      openTabMenuId.value = null
    }
  }
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
    form.agent_session_id = ''
    resetEnvForAgentType(form.agent_type)
    showFileBrowser.value = false
  }
})

watch(
  () => form.agent_type,
  (agentType) => {
    if (agentType === 'cursor' || agentType === 'terminal') {
      form.solo_mode = false
    }
    if (agentType !== 'codex') {
      form.agent_session_id = ''
    }
  }
)

watch(
  () => form.target,
  (target) => {
    if (target === 'remote') {
      fetchRemoteProfiles()
      form.cwd = selectedRemoteProfile.value?.default_cwd || '~'
    } else {
      form.cwd = ''
      form.remote_reconnect = true
    }
  }
)

watch(
  () => form.remote_profile_id,
  () => {
    if (form.target === 'remote' && (!form.cwd || form.cwd === '~')) {
      form.cwd = selectedRemoteProfile.value?.default_cwd || '~'
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
  document.addEventListener('pointerdown', handleDocumentPointerDown)
  document.addEventListener('keydown', handleDocumentKeyDown)
})

onUnmounted(() => {
  window.removeEventListener('resize', updateScrollFadeState)
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
  document.removeEventListener('keydown', handleDocumentKeyDown)
})

function handleDocumentKeyDown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    if (openTabMenuId.value) {
      openTabMenuId.value = null
    }
  }
}

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
  const env = parseLaunchEnv(form.env_text)
  // Only Codex local tabs support resuming a specific local session.
  const agent_session_id =
    form.agent_type === 'codex' && target === 'local' ? form.agent_session_id : undefined

  if (target === 'remote' && !selectedProfile) {
    remoteProfilesError.value = 'Select a remote server first'
    return
  }

  const tabName = form.name.trim()
    ? name
    : target === 'remote' && selectedProfile
      ? `${selectedProfile.name} · ${agentTypeLabel(agent_type)}`
      : name

  await runPending('tab:create', async () => {
    await store.createTab({
      name: tabName,
      session_kind: form.session_kind,
      cwd: target === 'local' ? cwd : undefined,
      solo_mode,
      agent_type,
      target,
      remote_profile_id,
      remote_cwd,
      remote_reconnect,
      env,
      agent_session_id,
    })

    form.name = ''
    form.cwd = ''
    form.solo_mode = false
    form.session_kind = 'agent'
    form.agent_type = 'claude'
    form.target = 'local'
    form.remote_profile_id = remoteProfiles.value[0]?.id || ''
    form.remote_reconnect = true
    form.agent_session_id = ''
    resetEnvForAgentType(form.agent_type)
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
  max-height: 48px;
  overflow: visible;
  transition: max-height 180ms cubic-bezier(0.2, 0, 0, 1), padding 180ms cubic-bezier(0.2, 0, 0, 1), gap 180ms cubic-bezier(0.2, 0, 0, 1), border-color 180ms cubic-bezier(0.2, 0, 0, 1);
}

.tab-bar > :not(.modal-overlay) {
  transition: opacity var(--ch-motion-fast);
}

.mobile-app-menu {
  display: none;
}

.mobile-app-menu-details {
  position: relative;
}

.mobile-app-menu-details summary {
  list-style: none;
}

.mobile-app-menu-details summary::-webkit-details-marker {
  display: none;
}

.mobile-app-menu-trigger {
  width: 30px;
  height: 30px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.mobile-app-menu-trigger:hover,
.mobile-app-menu-details[open] .mobile-app-menu-trigger {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.mobile-app-menu-trigger:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.mobile-app-menu-panel {
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  z-index: 1200;
  width: 184px;
  max-height: min(560px, calc(100dvh - 96px));
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 6px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-soft);
  scrollbar-width: thin;
  touch-action: pan-y;
  -webkit-overflow-scrolling: touch;
}

.mobile-app-menu-item {
  width: 100%;
  min-height: 34px;
  display: flex;
  align-items: center;
  padding: 5px 8px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
  text-align: left;
  cursor: pointer;
  transition: background var(--ch-motion-fast);
}

.mobile-app-menu-item:hover {
  background: var(--ch-color-surface-control-hover);
}

.mobile-app-menu-item:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.mobile-app-menu-item--mode {
  justify-content: space-between;
  border-color: var(--ch-color-border-muted);
  background: var(--ch-color-surface-soft);
}

.mobile-app-menu-item--mode + .mobile-app-menu-item:not(.mobile-app-menu-item--mode) {
  margin-top: 4px;
}

.mobile-app-menu-item--mode strong {
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: 10px;
  line-height: 1;
  padding: 4px 7px;
  text-transform: uppercase;
}

.mobile-app-menu-item.active {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
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
  transition: opacity var(--ch-motion-standard);
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

.tab:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
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
  font-size: var(--ch-font-size-base);
  font-weight: 500;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1;
  transition: font-size 180ms cubic-bezier(0.2, 0, 0, 1), max-width 180ms cubic-bezier(0.2, 0, 0, 1);
}

.tab-kind {
  min-width: 18px;
  padding: 2px 4px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  color: var(--ch-color-text-subtle);
  background: var(--ch-color-surface-sunken);
  font-family: var(--ch-font-mono);
  font-size: 9px;
  font-weight: 600;
  line-height: 1;
  text-align: center;
}

.tab-kind[data-kind='agent'] {
  color: var(--ch-color-accent);
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
}

.tab-name-input {
  background: transparent;
  border: none;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-base);
  font-weight: 500;
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
  transition: background-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast);
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
  border-radius: var(--ch-radius-md);
  line-height: 1;
  min-width: 14px;
  text-align: center;
}

.tab-menu-trigger {
  background: none;
  border: none;
  color: var(--ch-color-text-soft);
  font-size: 16px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
  opacity: 0;
  transition:
    color var(--ch-motion-fast),
    background var(--ch-motion-fast),
    opacity var(--ch-motion-standard);
  display: flex;
  align-items: center;
  justify-content: center;
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
  opacity: 0;
  transition:
    color var(--ch-motion-fast),
    background var(--ch-motion-fast),
    opacity var(--ch-motion-standard);
}

.tab:hover .tab-menu-trigger,
.tab.active .tab-menu-trigger,
.tab-menu-trigger.is-open,
.tab:hover .tab-close,
.tab.active .tab-close {
  opacity: 1;
}

.tab-menu-trigger:hover,
.tab-menu-trigger.is-open {
  color: var(--ch-color-text);
  background: var(--ch-color-chip-bg);
}

.tab-menu-trigger:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.tab-close:hover {
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
}

.tab-close:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.tab-menu-panel {
  z-index: 1200;
  padding: 6px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-raised);
  border: 1px solid var(--ch-color-border);
  box-shadow: var(--ch-shadow-popover);
  display: flex;
  flex-direction: column;
  gap: 2px;
  animation: tab-menu-in var(--ch-motion-fast);
  transform-origin: top right;
}

@keyframes tab-menu-in {
  from {
    opacity: 0;
    transform: translateY(-4px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.tab-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-weight: 500;
  line-height: 1.2;
  padding: 8px 10px;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  transition: background var(--ch-motion-fast), color var(--ch-motion-fast);
}

.tab-menu-item:hover {
  background: var(--ch-color-row-hover);
}

.tab-menu-item:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.tab-menu-item:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.tab-menu-item-icon {
  flex: 0 0 auto;
  width: 16px;
  text-align: center;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
}

.switch-env-modal {
  width: min(480px, calc(100vw - 32px));
}

.switch-env-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.switch-env-icon {
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 18px;
}

.switch-env-title-block {
  min-width: 0;
}

.switch-env-title-block h3 {
  margin: 0;
  font-size: var(--ch-font-size-lg);
}

.switch-env-subtitle {
  margin: 2px 0 0;
  font-size: var(--ch-font-size-sm);
  color: var(--ch-color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.switch-env-callout {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin: 0 0 18px;
  padding: 10px 12px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--ch-color-border-muted);
  border-left: 3px solid var(--ch-color-accent);
  font-size: var(--ch-font-size-sm);
  line-height: 1.5;
  color: var(--ch-color-text-muted);
}

.switch-env-callout-icon {
  flex: 0 0 auto;
  color: var(--ch-color-accent);
  font-weight: 600;
  line-height: 1.5;
}

.field-hint-inline {
  color: var(--ch-color-text-soft);
  font-weight: 400;
}

.switch-env-submit {
  min-width: 124px;
}

.switch-env-modal .form-group label code,
.switch-env-modal .checkbox-desc code,
.switch-env-modal .form-hint code {
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-sm);
  padding: 1px 5px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
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

.add-tab:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
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
  border-radius: var(--ch-radius-lg);
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
  border-radius: var(--ch-radius-md);
  margin-bottom: 12px;
  flex-shrink: 0;
}

.path-nav-btn {
  background: none;
  border: none;
  font-size: 18px;
  cursor: pointer;
  padding: 4px;
  border-radius: var(--ch-radius-md);
  transition: background var(--ch-motion-fast);
  color: var(--ch-color-text);
}

.path-nav-btn:hover {
  background-color: var(--ch-color-surface-control-hover);
}

.path-nav-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.current-path-input {
  min-width: 0;
  flex: 1;
  background-color: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  font-family: var(--ch-font-mono);
  padding: 6px 8px;
}

.current-path-input:focus {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.current-path {
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-base);
  font-family: var(--ch-font-mono);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-browser-list {
  flex: 1;
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
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
  font-size: var(--ch-font-size-base);
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
  font-size: var(--ch-font-size-base);
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
  font-size: var(--ch-font-size-md);
}

.form-group input {
  width: 100%;
  height: 36px;
  padding: 0 12px;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
  box-sizing: border-box;
}

.form-group input:focus,
.form-group select:focus,
.form-group textarea:focus,
.select-input:focus {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.form-error {
  color: var(--ch-color-danger);
  font-size: var(--ch-font-size-sm);
  margin: 6px 0 0 0;
}

.form-hint {
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-sm);
  margin: 6px 0 0 0;
}

.segmented-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  background-color: var(--ch-color-surface-sunken);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  padding: 3px;
}

.segment-button {
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: var(--ch-font-size-sm);
  font-weight: 600;
  padding: 8px 12px;
  transition: background var(--ch-motion-fast), color var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.segment-button.active {
  background-color: var(--ch-color-surface-control);
  border-color: var(--ch-color-border-hover);
  color: var(--ch-color-text);
}

.segment-button:hover {
  color: var(--ch-color-text);
}

.segment-button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.select-input {
  width: 100%;
  height: 36px;
  padding: 0 12px;
  background-color: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
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
  border-top-right-radius: var(--ch-radius-md);
  border-bottom-right-radius: var(--ch-radius-md);
  color: var(--ch-color-text);
  padding: 0 12px;
  cursor: pointer;
  font-size: var(--ch-font-size-md);
  height: 36px;
}

.cwd-dropdown-btn:hover {
  background-color: var(--ch-color-surface-pressed);
  color: var(--ch-color-text);
}

.cwd-dropdown-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
  z-index: 1;
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
  accent-color: var(--ch-color-accent);
}

.checkbox-text {
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-md);
  font-weight: 500;
}

.checkbox-desc {
  color: var(--ch-color-text-soft);
  font-size: var(--ch-font-size-sm);
  margin-left: 24px;
}

.env-editor {
  gap: 8px;
}

.env-preset-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.env-manage-button {
  white-space: nowrap;
}

.env-template-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-muted);
  padding: 10px;
}

.env-preset-name {
  width: 100%;
}

.env-textarea {
  width: 100%;
  min-height: 92px;
  height: auto;
  padding: 10px 12px;
  resize: vertical;
  font-family: var(--ch-font-mono) !important;
  line-height: 1.45;
}

.env-textarea-preview {
  margin-top: 8px;
  width: 100%;
  resize: none;
  min-height: 60px;
  height: auto;
  padding: 10px 12px;
  opacity: 0.75;
  cursor: default;
  white-space: pre-wrap;
  font-family: var(--ch-font-mono) !important;
}

.env-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 24px;
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
    border-radius: var(--ch-radius-md);
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

  .modal-actions .ch-btn,
  .file-browser-footer .ch-btn {
    flex: 1;
  }
}

@media (max-width: 768px) {
  .mobile-app-menu {
    display: flex;
    flex: 0 0 auto;
  }
}

/* ----------------------------------------------------------------
 * Toast / notification stack (F5: replaces single mutable `error`).
 * Top-right, fixed inside TabBar so it only shows in terminal mode.
 * ---------------------------------------------------------------- */
.toast-stack {
  position: fixed;
  top: 72px;
  right: 16px;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(420px, calc(100vw - 32px));
  pointer-events: none;
}

.toast {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px 10px 14px;
  border-radius: var(--ch-radius-md);
  border: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-popover);
  font-size: var(--ch-font-size-base);
  line-height: 1.45;
  overflow: hidden;
  pointer-events: auto;
  animation: toast-in 180ms cubic-bezier(0.2, 0, 0, 1);
}

@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateY(-6px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.toast::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--ch-color-text-muted);
}

.toast__icon {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  margin-top: 1px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--ch-font-size-base);
  font-weight: 700;
  line-height: 1;
}

.toast__message {
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-word;
  padding-top: 1px;
}

.toast__close {
  flex: 0 0 auto;
  background: transparent;
  border: none;
  color: var(--ch-color-text-subtle);
  font-size: 16px;
  line-height: 1;
  padding: 2px;
  margin-top: -1px;
  cursor: pointer;
  transition: color var(--ch-motion-fast), background var(--ch-motion-fast);
  border-radius: var(--ch-radius-sm);
}

.toast__close:hover {
  color: var(--ch-color-text);
  background: var(--ch-color-row-hover);
}

.toast__close:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.toast__timer {
  position: absolute;
  left: 0;
  bottom: 0;
  height: 2px;
  background: currentColor;
  opacity: 0.35;
  transform-origin: left center;
  animation-name: toast-timer;
  animation-timing-function: linear;
  animation-fill-mode: forwards;
}

@keyframes toast-timer {
  from { width: 100%; }
  to { width: 0%; }
}

.toast--error::before { background: var(--ch-color-danger); }
.toast--error .toast__icon { color: var(--ch-color-danger); }
.toast--error .toast__icon::after { content: '!'; }

.toast--warning::before { background: var(--ch-color-warning); }
.toast--warning .toast__icon { color: var(--ch-color-warning); }
.toast--warning .toast__icon::after { content: '△'; }

.toast--success::before { background: var(--ch-color-success); }
.toast--success .toast__icon { color: var(--ch-color-success); }
.toast--success .toast__icon::after { content: '✓'; }

.toast--info::before { background: var(--ch-color-info); }
.toast--info .toast__icon { color: var(--ch-color-info); }
.toast--info .toast__icon::after { content: 'i'; }

/* Error keeps a tinted background because it needs to command attention */
.toast--error {
  background: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

@media (max-width: 768px) {
  .toast-stack {
    top: 12px;
    right: 8px;
    left: 8px;
    max-width: none;
  }
}

</style>
