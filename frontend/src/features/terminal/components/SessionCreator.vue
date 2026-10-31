<template>
  <!-- Create Tab Modal -->
  <div
    v-if="showModal"
    class="modal-overlay"
    data-drawer-swipe-ignore
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
              :class="['segment-button', { active: form.session_kind === 'chat' }]"
              @click="setSessionKind('chat')"
            >
              Chat
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
            {{ form.session_kind === 'chat'
              ? 'Structured conversation with messages, images, and parsed model output. Chat always runs on this Hub host.'
              : 'Native terminal UI with full keyboard and shell interaction. Remote SSH is available here.' }}
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
              :disabled="form.session_kind === 'chat'"
              @click="form.target = 'remote'"
            >
              Remote
            </button>
          </div>
          <p
            v-if="form.session_kind === 'chat'"
            class="form-hint"
          >
            Remote SSH is Terminal-only. Chat uses the local native provider.
          </p>
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
              {{ profile.name }}{{ remoteProfileBadge(profile) }}
            </option>
          </select>
          <p
            v-if="remoteProfilesError"
            class="form-error"
          >
            {{ remoteProfilesError }}
          </p>
          <p
            v-else-if="!remoteInteractive(selectedRemoteProfile)"
            class="form-hint"
          >
            This alias has no drivable remote PTY. Browse directories here; use an interactive host (normal or PTY gateway such as merlin_dev) for Terminal.
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
          <div
            ref="cwdInputWrapperRef"
            class="cwd-input-wrapper"
            @focusout="handleCwdFocusOut"
          >
            <input
              id="tabCwd"
              v-model="form.cwd"
              type="text"
              :placeholder="form.target === 'remote' ? '~/workspace/project' : 'e.g., ~/Project/my-app'"
              autocomplete="off"
              role="combobox"
              aria-autocomplete="list"
              :aria-controls="visibleCwdChoices.length ? 'tab-cwd-choices' : undefined"
              :aria-expanded="cwdDropdownOpen && visibleCwdChoices.length > 0"
              :aria-activedescendant="activeCwdChoice >= 0 ? `tab-cwd-choice-${activeCwdChoice}` : undefined"
              @focus="openCwdDropdown"
              @input="handleCwdInput"
              @keydown="handleCwdKeydown"
            >
            <button
              type="button"
              class="cwd-choices-btn"
              aria-label="Choose a working directory"
              :aria-expanded="cwdDropdownOpen && visibleCwdChoices.length > 0"
              :aria-controls="visibleCwdChoices.length ? 'tab-cwd-choices' : undefined"
              :disabled="cwdChoices.length === 0"
              @click="openCwdDropdown"
            >
              ▾
            </button>
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
            <div
              v-if="cwdDropdownOpen && visibleCwdChoices.length"
              id="tab-cwd-choices"
              ref="cwdChoicesRef"
              class="cwd-choices"
              role="listbox"
              aria-label="Working directories"
            >
              <button
                v-for="(cwd, index) in visibleCwdChoices"
                :id="`tab-cwd-choice-${index}`"
                :key="cwd"
                type="button"
                role="option"
                class="cwd-choice"
                :class="{ 'is-active': activeCwdChoice === index }"
                :aria-selected="form.cwd === cwd"
                tabindex="-1"
                @mousedown.prevent
                @click="selectCwdChoice(cwd)"
              >
                {{ cwd }}
              </button>
            </div>
          </div>
          <p
            v-if="cwdChoices.length > 0"
            class="form-hint"
          >
            Choose a recent or open directory, or type a new one.
          </p>
        </div>
        <AgentConfigFields
          v-model:agent-type="form.agent_type"
          v-model:solo-mode="form.solo_mode"
          v-model:env-preset="form.env_preset"
          v-model:env-text="form.env_text"
          variant="form"
          :allow-terminal="form.session_kind === 'terminal'"
          :allow-maso="form.session_kind === 'chat'"
          :allow-antigravity="form.session_kind === 'chat' && form.target === 'local'"
          :type-label="form.session_kind === 'chat' ? 'Chat Provider' : 'Terminal Profile'"
          solo-label="Solo Mode"
        />
        <p
          v-if="form.agent_type === 'antigravity'"
          class="form-hint"
        >
          Uses your Google account signed in through agy on this Hub host.
          API-key billing and extra AI-credit usage are disabled.
        </p>
        <div
          v-if="form.agent_type === 'maso'"
          class="maso-preflight"
          :class="{ 'maso-preflight--unavailable': masoPreflightState === 'unavailable' }"
          :role="masoPreflightState === 'unavailable' ? 'alert' : 'status'"
          aria-live="polite"
        >
          <p>{{ masoPreflightMessage }}</p>
          <p
            v-if="masoEndpoint"
            class="form-hint"
          >
            Service: {{ masoEndpoint }}
          </p>
          <button
            v-if="masoPreflightState === 'unavailable'"
            type="button"
            class="ch-btn ch-btn--sm"
            @click="checkMasoService"
          >
            Retry check
          </button>
        </div>
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
</template>
<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import AgentConfigFields from '@/components/AgentConfigFields.vue'
import CodexSessionSelector from '@/components/CodexSessionSelector.vue'
import LoadingButton from '@/components/LoadingButton.vue'
import { defaultLaunchEnvPresetForAgent, parseLaunchEnv, useLaunchEnvPresets } from '@/composables/useLaunchEnvPresets'
import { useCwdHistory } from '@/composables/useCwdHistory'
import { useMasoPreflight } from '@/composables/useMasoPreflight'
import { usePendingActions } from '@/composables/usePendingActions'
import { useTerminalStore } from '@/stores/terminalStore'
import { remoteInteractive, remoteProfileBadge } from '@/utils/remoteProfiles'
import type { AgentType, RemoteProfile, SessionKind } from '@/types'
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
const { tabs, manualTabs, isLoading } = storeToRefs(store)
const { defaultPresetTextForAgent } = useLaunchEnvPresets()
const { isPending, runPending } = usePendingActions()
const showModal = ref(false)
const showFileBrowser = ref(false)
const cwdInputWrapperRef = ref<HTMLDivElement | null>(null)
const cwdChoicesRef = ref<HTMLDivElement | null>(null)
const cwdDropdownOpen = ref(false)
const cwdFilterActive = ref(false)
const activeCwdChoice = ref(-1)
const form = reactive({
  name: '',
  cwd: '',
  session_kind: 'chat' as SessionKind,
  solo_mode: true,
  agent_type: 'claude' as AgentType,
  target: 'local' as 'local' | 'remote',
  remote_profile_id: '',
  remote_reconnect: true,
  env_preset: defaultLaunchEnvPresetForAgent('claude'),
  env_text: defaultPresetTextForAgent('claude'),
  agent_session_id: '',
})
const {
  state: masoPreflightState, message: masoPreflightMessage,
  endpoint: masoEndpoint, check: checkMasoService,
} = useMasoPreflight(
  () => showModal.value && form.agent_type === 'maso' && form.session_kind === 'chat',
  () => parseLaunchEnv(form.env_text)?.MASO_BASE_URL,
)
const supportsSoloMode = computed(
  () => ['claude', 'codex', 'traex', 'antigravity'].includes(form.agent_type)
)
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
const cwdHistoryScope = computed(() =>
  form.target === 'remote' ? `remote:${form.remote_profile_id || 'none'}` : 'local'
)
const { recentCwds, mostRecentCwd, addCwd } = useCwdHistory(cwdHistoryScope)
const cwdChoices = computed(() => {
  const openPaths = tabs.value
    .filter(tab => !tab.archived && (form.target === 'remote'
      ? tab.target === 'remote' && tab.remote_profile_id === form.remote_profile_id
      : tab.target !== 'remote'))
    .map(tab => form.target === 'remote' ? tab.remote_cwd || tab.cwd : tab.cwd)
  return [...new Set([...recentCwds.value, ...openPaths].filter((path): path is string => Boolean(path?.trim())))]
})
const visibleCwdChoices = computed(() => {
  if (!cwdFilterActive.value) return cwdChoices.value
  const query = form.cwd.trim().toLowerCase()
  return cwdChoices.value.filter(path => path.toLowerCase().includes(query))
})
const isCreateDisabled = computed(
  () =>
    isLoading.value ||
    isPending('tab:create') ||
    (form.agent_type === 'maso' && masoPreflightState.value !== 'available') ||
    (form.target === 'remote' && !form.remote_profile_id) ||
    (form.target === 'remote' && !remoteInteractive(selectedRemoteProfile.value))
)
function openCwdDropdown() {
  cwdFilterActive.value = false
  activeCwdChoice.value = -1
  cwdDropdownOpen.value = true
}
function handleCwdInput() {
  cwdFilterActive.value = true
  activeCwdChoice.value = -1
  cwdDropdownOpen.value = true
}
function selectCwdChoice(path: string) {
  form.cwd = path
  cwdDropdownOpen.value = false
  cwdFilterActive.value = false
  activeCwdChoice.value = -1
}
function handleCwdKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    cwdDropdownOpen.value = false
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!cwdDropdownOpen.value) openCwdDropdown()
    const count = visibleCwdChoices.value.length
    if (count) {
      if (activeCwdChoice.value < 0) {
        activeCwdChoice.value = event.key === 'ArrowDown' ? 0 : count - 1
      } else {
        activeCwdChoice.value = (activeCwdChoice.value + (event.key === 'ArrowDown' ? 1 : -1) + count) % count
      }
      const index = activeCwdChoice.value
      void nextTick(() => {
        const choice = cwdChoicesRef.value?.children.item(index)
        if (choice instanceof HTMLElement) choice.scrollIntoView({ block: 'nearest' })
      })
    }
  } else if (event.key === 'Enter' && cwdDropdownOpen.value && activeCwdChoice.value >= 0) {
    event.preventDefault()
    selectCwdChoice(visibleCwdChoices.value[activeCwdChoice.value])
  }
}
function handleCwdFocusOut(event: FocusEvent) {
  if (!cwdInputWrapperRef.value?.contains(event.relatedTarget as Node | null)) {
    cwdDropdownOpen.value = false
  }
}
function resetEnvForAgentType(agentType: AgentType) {
  form.env_preset = defaultLaunchEnvPresetForAgent(agentType)
  form.env_text = defaultPresetTextForAgent(agentType)
}
function setSessionKind(kind: SessionKind) {
  form.session_kind = kind
  if (kind === 'terminal' && ['maso', 'antigravity'].includes(form.agent_type)) {
    form.agent_type = 'claude'
    resetEnvForAgentType(form.agent_type)
  }
  if (kind === 'chat') {
    form.target = 'local'
    if (form.agent_type === 'terminal') {
      form.agent_type = 'claude'
      resetEnvForAgentType(form.agent_type)
    }
  }
}
function agentTypeLabel(agentType: AgentType): string {
  switch (agentType) {
    case 'codex':
      return 'Codex'
    case 'traex':
      return 'TraeX'
    case 'maso':
      return 'MASO'
    case 'cursor':
      return 'Cursor'
    case 'antigravity':
      return 'Antigravity'
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
  cwdDropdownOpen.value = false
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
function openCreateModal() {
  if (form.target === 'local' && !form.cwd) {
    form.cwd = mostRecentCwd.value
  }
  showModal.value = true
  fetchRemoteProfiles()
}
function closeCreateModal() {
  showModal.value = false
  showFileBrowser.value = false
  cwdDropdownOpen.value = false
}
watch(showModal, (newVal) => {
  if (!newVal) {
    form.name = ''
    form.target = 'local'
    form.cwd = mostRecentCwd.value
    form.solo_mode = true
    form.agent_type = 'claude'
    form.remote_profile_id = remoteProfiles.value[0]?.id || ''
    form.remote_reconnect = true
    form.agent_session_id = ''
    resetEnvForAgentType(form.agent_type)
    showFileBrowser.value = false
    cwdDropdownOpen.value = false
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
      form.cwd = mostRecentCwd.value || selectedRemoteProfile.value?.default_cwd || '~'
    } else {
      form.cwd = mostRecentCwd.value
      form.remote_reconnect = true
    }
  }
)
watch(cwdHistoryScope, () => {
  cwdDropdownOpen.value = false
  cwdFilterActive.value = false
})
watch(
  () => form.remote_profile_id,
  () => {
    if (form.target === 'remote' && (!form.cwd || form.cwd === '~')) {
      form.cwd = mostRecentCwd.value || selectedRemoteProfile.value?.default_cwd || '~'
    }
  }
)
async function handleCreateTab() {
  if (form.agent_type === 'maso' && masoPreflightState.value !== 'available') return
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
  const agent_session_id =
    form.agent_type === 'codex' && target === 'local' ? form.agent_session_id : undefined
  if (form.session_kind === 'chat' && target === 'remote') {
    remoteProfilesError.value = 'Chat sessions run locally. Switch to Terminal for remote SSH.'
    return
  }
  if (target === 'remote' && !remoteInteractive(selectedProfile)) {
    remoteProfilesError.value =
      'This SSH alias is browse-only. Pick an interactive host (normal or PTY gateway) for Terminal.'
    return
  }
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
    const created = await store.createTab({
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
    if (!created) return
    addCwd(cwd)
    form.name = ''
    form.session_kind = 'chat'
    form.agent_type = 'claude'
    form.solo_mode = true
    form.target = 'local'
    form.cwd = mostRecentCwd.value
    form.remote_profile_id = remoteProfiles.value[0]?.id || ''
    form.remote_reconnect = true
    form.agent_session_id = ''
    resetEnvForAgentType(form.agent_type)
    showFileBrowser.value = false
    showModal.value = false
  })
}
function handleDocumentPointerDown(event: PointerEvent) {
  const target = event.target
  if (target instanceof Node && cwdInputWrapperRef.value && !cwdInputWrapperRef.value.contains(target)) {
    cwdDropdownOpen.value = false
  }
}
onMounted(() => document.addEventListener('pointerdown', handleDocumentPointerDown))
onUnmounted(() => document.removeEventListener('pointerdown', handleDocumentPointerDown))
defineExpose({ openCreateModal })
</script>
<style scoped src="@/features/terminal/styles/session-creator.css"></style>
