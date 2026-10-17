<template>
  <div
    v-if="availableRows.length > 0"
    class="agent-status"
    :data-expanded="expanded"
  >
    <button
      type="button"
      class="status-trigger"
      :data-status="summaryStatus"
      :aria-expanded="expanded"
      :title="triggerTitle"
      @click="toggleExpanded"
    >
      <span class="trigger-dot" />
      <span class="trigger-label">{{ label }}</span>
      <span class="trigger-count">{{ rows.length }}</span>
    </button>

    <aside
      v-if="expanded"
      class="status-panel"
      :style="panelStyle"
      :aria-label="panelTitle"
    >
      <div class="panel-header">
        <div class="panel-title-block">
          <span class="panel-title">{{ panelTitle }}</span>
          <span
            v-if="isManagedSource"
            class="panel-subtitle"
          >
            {{ managedViewLabel }} {{ rows.length }}
          </span>
        </div>
        <LoadingButton
          type="button"
          class="panel-refresh"
          title="Refresh statuses"
          :loading="isPanelRefreshLoading"
          hide-content-while-loading
          loading-label="Refreshing statuses"
          @click="refreshPanelData()"
        >
          ↻
        </LoadingButton>
      </div>

      <div
        v-if="isManagedSource"
        class="panel-mode-switch"
        aria-label="Workspace terminal role view"
      >
        <button
          type="button"
          :data-active="managedView === 'agents'"
          @click="managedView = 'agents'"
        >
          <span>Agents</span>
          <strong>{{ managedAgentRows.length }}</strong>
        </button>
        <button
          type="button"
          :data-active="managedView === 'reviewers'"
          @click="managedView = 'reviewers'"
        >
          <span>Reviewers</span>
          <strong>{{ managedReviewerRows.length }}</strong>
        </button>
      </div>

      <div
        v-if="rows.length === 0"
        class="empty-status"
      >
        {{ emptyStatusText }}
      </div>

      <div
        v-else
        class="agent-list"
      >
        <section
          v-for="group in rowGroups"
          :key="group.key"
          class="agent-group"
        >
          <div
            v-if="group.title"
            class="agent-group-header"
          >
            <span :title="group.tooltip">{{ group.title }}</span>
            <strong>{{ group.rows.length }}</strong>
          </div>
          <button
            v-for="row in group.rows"
            :key="row.tab.id"
            type="button"
            class="agent-row"
            :class="{ active: row.tab.id === activeTabId }"
            @click="selectTab(row.tab.id)"
          >
            <span class="agent-avatar-wrap">
              <AgentAvatar
                :agent-type="row.tab.agent_type"
                size="sm"
              />
              <span
                class="status-dot"
                :data-status="getRowStatus(row)"
              />
            </span>
            <span class="agent-main">
              <span class="agent-line">
                <span class="agent-name">{{ row.tab.name }}</span>
                <span
                  class="agent-cli"
                  :data-kind="row.tab.agent_type || 'terminal'"
                >{{ row.tab.agent_type || 'terminal' }}</span>
              </span>
              <span class="agent-detail">
                {{ getRowDetail(row) }}
              </span>
            </span>
            <span
              class="status-pill"
              :data-status="getRowStatus(row)"
            >
              {{ getRowStatusText(row) }}
            </span>
          </button>
        </section>
      </div>

      <button
        type="button"
        class="resize-handle"
        title="Resize status panel"
        @pointerdown="startResize"
      />
    </aside>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import AgentAvatar from '@/components/AgentAvatar.vue'
import LoadingButton from '@/components/LoadingButton.vue'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { cwdLabel, groupChatsByCwd } from '@/utils/chatGroups'
import type { AgentRuntimeStatus, TerminalAgentStatus, TerminalTab } from '@/types'

const STATUS_PRIORITY: AgentRuntimeStatus[] = ['attention', 'working', 'idle', 'offline']
const MIN_PANEL_WIDTH = 280
const MAX_PANEL_WIDTH = 640
const MIN_PANEL_HEIGHT = 160
const MAX_PANEL_HEIGHT_RATIO = 0.72
const PANEL_OPEN_EVENT = 'claude-hub-status-panel-open'

interface PanelSize {
  width: number
  height: number
}

type ManagedView = 'agents' | 'reviewers'

const props = withDefaults(defineProps<{
  source?: 'manual' | 'managed'
  label?: string
  panelTitle?: string
}>(), {
  source: 'managed',
  label: 'Agents',
  panelTitle: 'Workspace Agents',
})

const store = useTerminalStore()
const appStore = useAppStore()
const {
  chatTabs, pinnedChatIds, manualTabs, managedTabs, agentStatuses, activeTabId,
} = storeToRefs(store)
const { mode } = storeToRefs(appStore)
const storageKeyExpanded = `claude_hub_${props.source}_status_expanded`
const storageKeySize = `claude_hub_${props.source}_status_size`
const panelInstanceKey = props.source
const expanded = ref(localStorage.getItem(storageKeyExpanded) === 'true')
const panelSize = ref<PanelSize | null>(loadPanelSize())
const isPanelRefreshLoading = ref(false)
let statusPollingActive = false
let resizeState: {
  startX: number
  startY: number
  startWidth: number
  startHeight: number
  pointerId: number
} | null = null

interface AgentRow {
  tab: TerminalTab
  status?: TerminalAgentStatus
}

interface AgentRowGroup {
  key: string
  title: string
  /** Optional native tooltip — holds the full cwd path for manual directory groups. */
  tooltip?: string
  rows: AgentRow[]
}

const statusByTabId = computed<Record<string, TerminalAgentStatus>>(() => {
  const map: Record<string, TerminalAgentStatus> = {}
  for (const status of agentStatuses.value) {
    map[status.tab_id] = status
  }
  return map
})

const targetTabs = computed<TerminalTab[]>(() =>
  props.source === 'manual' ? manualTabs.value : managedTabs.value
)

const allRows = computed<AgentRow[]>(() =>
  targetTabs.value.map(tab => ({
    tab,
    status: statusByTabId.value[tab.id],
  }))
)

const isManagedSource = computed(() => props.source === 'managed')
const managedView = ref<ManagedView>('agents')

const managedAgentRows = computed<AgentRow[]>(() =>
  allRows.value.filter(row => isManagedAgentTab(row.tab))
)

const managedReviewerRows = computed<AgentRow[]>(() =>
  allRows.value.filter(row => row.tab.workspace_role === 'reviewer')
)

const rows = computed<AgentRow[]>(() => {
  if (!isManagedSource.value) return allRows.value
  return managedView.value === 'reviewers' ? managedReviewerRows.value : managedAgentRows.value
})

const availableRows = computed<AgentRow[]>(() =>
  isManagedSource.value ? [...managedAgentRows.value, ...managedReviewerRows.value] : allRows.value
)

const rowGroups = computed<AgentRowGroup[]>(() => {
  if (!isManagedSource.value) {
    // Same display order as the Chat sidebar: group manual sessions by working
    // directory, preserving the server's persisted order both within a
    // directory and for the first appearance of each directory. We reuse the
    // Chat list's grouping primitive (utils/chatGroups) so the two surfaces
    // share one ordering scheme. The status panel keeps every manual session
    // (including raw terminal tabs); it only adds directory headers.
    const rowByTabId = new Map(rows.value.map(row => [row.tab.id, row]))
    const chatCwdOrder = groupChatsByCwd(
      chatTabs.value.filter(tab => !pinnedChatIds.value.has(tab.id)),
    ).map(group => group.cwd)
    return groupChatsByCwd(rows.value.map(row => row.tab), chatCwdOrder).map(group => ({
      key: `cwd:${group.cwd}`,
      title: cwdLabel(group.cwd),
      tooltip: group.cwd === 'No directory' ? undefined : group.cwd,
      rows: group.tabs
        .map(tab => rowByTabId.get(tab.id))
        .filter((row): row is AgentRow => Boolean(row)),
    }))
  }

  const groups = new Map<string, AgentRowGroup>()
  for (const row of rows.value) {
    const key = row.tab.workspace_id || 'unknown'
    const title = row.tab.workspace_name || 'Workspace'
    const existing = groups.get(key)
    if (existing) {
      existing.rows.push(row)
    } else {
      groups.set(key, { key, title, rows: [row] })
    }
  }
  return Array.from(groups.values())
})

const label = computed(() =>
  isManagedSource.value && managedView.value === 'reviewers' ? 'Reviewers' : props.label
)
const panelTitle = computed(() => props.panelTitle)
const managedViewLabel = computed(() => (managedView.value === 'reviewers' ? 'Reviewers' : 'Agents'))
const emptyStatusText = computed(() =>
  isManagedSource.value && managedView.value === 'reviewers'
    ? 'No reviewer terminals.'
    : 'No agent terminals.'
)
const triggerTitle = computed(() =>
  props.source === 'manual' ? 'Manual terminal statuses' : 'Workspace agent terminals'
)

const summaryStatus = computed<AgentRuntimeStatus>(() => {
  for (const status of STATUS_PRIORITY) {
    if (rows.value.some(row => getRowStatus(row) === status)) {
      return status
    }
  }
  return 'offline'
})

const panelStyle = computed(() => {
  if (!panelSize.value) return {}
  return {
    width: `${panelSize.value.width}px`,
    height: `${panelSize.value.height}px`,
  }
})

function loadPanelSize(): PanelSize | null {
  try {
    const raw = localStorage.getItem(storageKeySize)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<PanelSize>
    if (typeof parsed.width !== 'number' || typeof parsed.height !== 'number') {
      return null
    }
    return clampPanelSize(parsed.width, parsed.height)
  } catch {
    return null
  }
}

function clampPanelSize(width: number, height: number): PanelSize {
  const maxWidth = Math.min(MAX_PANEL_WIDTH, Math.max(MIN_PANEL_WIDTH, window.innerWidth - 16))
  const maxHeight = Math.min(520, Math.max(MIN_PANEL_HEIGHT, window.innerHeight * MAX_PANEL_HEIGHT_RATIO))
  return {
    width: Math.round(Math.min(Math.max(width, MIN_PANEL_WIDTH), maxWidth)),
    height: Math.round(Math.min(Math.max(height, MIN_PANEL_HEIGHT), maxHeight)),
  }
}

function toggleExpanded() {
  expanded.value = !expanded.value
  if (expanded.value) {
    window.dispatchEvent(new CustomEvent(PANEL_OPEN_EVENT, { detail: panelInstanceKey }))
    void refreshPanelData({ showIndicator: false })
  }
}

async function refreshPanelData(options: { showIndicator?: boolean } = {}) {
  const showIndicator = options.showIndicator !== false
  if (showIndicator) {
    if (isPanelRefreshLoading.value) return
    isPanelRefreshLoading.value = true
  }

  try {
    await Promise.all([
      store.fetchTabs(),
      store.fetchAgentStatuses(),
    ])
  } finally {
    if (showIndicator) {
      isPanelRefreshLoading.value = false
    }
  }
}

function handlePeerPanelOpen(event: Event) {
  const openedPanel = event instanceof CustomEvent ? event.detail : null
  if (openedPanel !== panelInstanceKey) {
    expanded.value = false
  }
}

function selectTab(tabId: string) {
  store.setActiveTab(tabId)
}

function getRowStatus(row: AgentRow): AgentRuntimeStatus {
  return row.status?.status ?? (row.tab.is_active ? 'idle' : 'offline')
}

function getRowStatusText(row: AgentRow): string {
  return row.status?.status_text ?? (row.tab.is_active ? 'Idle' : 'Offline')
}

function isManagedAgentTab(tab: TerminalTab): boolean {
  return tab.workspace_role !== 'reviewer' && tab.workspace_role !== 'dispatcher'
}

function getRowDetail(row: AgentRow): string {
  if (row.status?.detail) return row.status.detail
  if (row.tab.workspace_name) return row.tab.workspace_name
  if (row.tab.cwd) return row.tab.cwd
  return row.status?.status_text ?? getRowStatusText(row)
}

function startResize(event: PointerEvent) {
  const panel = (event.currentTarget as HTMLElement).closest('.status-panel')
  if (!(panel instanceof HTMLElement)) return

  event.preventDefault()
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  resizeState = {
    startX: event.clientX,
    startY: event.clientY,
    startWidth: panel.offsetWidth,
    startHeight: panel.offsetHeight,
    pointerId: event.pointerId,
  }
  window.addEventListener('pointermove', handleResize)
  window.addEventListener('pointerup', stopResize)
  window.addEventListener('pointercancel', stopResize)
}

function handleResize(event: PointerEvent) {
  if (!resizeState) return
  const nextWidth = resizeState.startWidth + resizeState.startX - event.clientX
  const nextHeight = resizeState.startHeight + event.clientY - resizeState.startY
  panelSize.value = clampPanelSize(nextWidth, nextHeight)
}

function stopResize() {
  resizeState = null
  window.removeEventListener('pointermove', handleResize)
  window.removeEventListener('pointerup', stopResize)
  window.removeEventListener('pointercancel', stopResize)
}

watch(expanded, value => {
  localStorage.setItem(storageKeyExpanded, String(value))
})

watch(panelSize, value => {
  if (value) {
    localStorage.setItem(storageKeySize, JSON.stringify(value))
  }
})

function setStatusPolling(active: boolean) {
  if (active === statusPollingActive) return
  statusPollingActive = active
  if (active) {
    void store.fetchTabs()
    store.startAgentStatusPolling()
  } else {
    store.stopAgentStatusPolling()
  }
}

watch(() => mode.value === 'terminal', setStatusPolling, { immediate: true })

onMounted(() => {
  window.addEventListener(PANEL_OPEN_EVENT, handlePeerPanelOpen)
  if (expanded.value) {
    window.dispatchEvent(new CustomEvent(PANEL_OPEN_EVENT, { detail: panelInstanceKey }))
    void refreshPanelData({ showIndicator: false })
  }
})

onUnmounted(() => {
  setStatusPolling(false)
  stopResize()
  window.removeEventListener(PANEL_OPEN_EVENT, handlePeerPanelOpen)
})
</script>

<style scoped src="./agent-status-floating-panel.css"></style>
