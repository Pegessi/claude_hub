<template>
  <div
    v-if="rows.length > 0"
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
        <span class="panel-title">{{ panelTitle }}</span>
        <LoadingButton
          type="button"
          class="panel-refresh"
          title="Refresh statuses"
          :loading="isStatusLoading"
          hide-content-while-loading
          loading-label="Refreshing statuses"
          @click="store.fetchAgentStatuses"
        >
          ↻
        </LoadingButton>
      </div>

      <div
        v-if="rows.length === 0"
        class="empty-status"
      >
        Loading statuses...
      </div>

      <div
        v-else
        class="agent-list"
      >
        <button
          v-for="row in rows"
          :key="row.tab.id"
          type="button"
          class="agent-row"
          :class="{ active: row.tab.id === activeTabId }"
          @click="selectTab(row.tab.id)"
        >
          <span
            class="status-dot"
            :data-status="getRowStatus(row)"
          />
          <span class="agent-main">
            <span class="agent-line">
              <span class="agent-name">{{ row.tab.name }}</span>
              <span class="agent-type">{{ getTabKindLabel(row.tab) }}</span>
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
import LoadingButton from '@/components/LoadingButton.vue'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
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
const { manualTabs, managedTabs, agentStatuses, activeTabId, isStatusLoading } = storeToRefs(store)
const { mode } = storeToRefs(appStore)
const storageKeyExpanded = `claude_hub_${props.source}_status_expanded`
const storageKeySize = `claude_hub_${props.source}_status_size`
const panelInstanceKey = props.source
const expanded = ref(localStorage.getItem(storageKeyExpanded) === 'true')
const panelSize = ref<PanelSize | null>(loadPanelSize())
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

const rows = computed<AgentRow[]>(() =>
  targetTabs.value.map(tab => ({
    tab,
    status: statusByTabId.value[tab.id],
  }))
)

const label = computed(() => props.label)
const panelTitle = computed(() => props.panelTitle)
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

function getTabKindLabel(tab: TerminalTab): string {
  if (props.source === 'managed') {
    return tab.workspace_role === 'worker' ? 'Worker' : 'Agent'
  }
  return tab.agent_type || 'terminal'
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
  }
})

onUnmounted(() => {
  setStatusPolling(false)
  stopResize()
  window.removeEventListener(PANEL_OPEN_EVENT, handlePeerPanelOpen)
})
</script>

<style scoped>
.agent-status {
  position: relative;
  align-self: flex-end;
  flex: 0 0 auto;
}

.status-trigger {
  height: 28px;
  min-width: 82px;
  box-sizing: border-box;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 1px solid var(--ch-color-border);
  border-radius: 4px;
  background-color: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  padding: 0 9px;
}

.status-trigger:hover,
.agent-status[data-expanded='true'] .status-trigger {
  background-color: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.trigger-dot,
.status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: currentColor;
}

.status-trigger[data-status='idle'],
.status-dot[data-status='idle'],
.status-pill[data-status='idle'] {
  color: var(--ch-color-success);
}

.status-trigger[data-status='working'],
.status-dot[data-status='working'],
.status-pill[data-status='working'] {
  color: var(--ch-color-warning);
}

.status-trigger[data-status='attention'],
.status-dot[data-status='attention'],
.status-pill[data-status='attention'] {
  color: var(--ch-color-attention);
}

.status-trigger[data-status='offline'],
.status-dot[data-status='offline'],
.status-pill[data-status='offline'] {
  color: var(--ch-color-text-muted);
}

.trigger-label {
  color: inherit;
  font-size: 12px;
  font-weight: 700;
}

.trigger-count {
  min-width: 18px;
  height: 18px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
  font-size: 11px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
}

.status-panel {
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  z-index: 120;
  width: min(360px, calc(100vw - 16px));
  max-width: calc(100vw - 16px);
  max-height: min(60vh, 520px, calc(100vh - 72px));
  overflow: hidden;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 8px;
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-popover);
  backdrop-filter: blur(14px);
  display: flex;
  flex-direction: column;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 10px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.panel-title {
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 700;
}

.panel-refresh {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: 14px;
}

.panel-refresh:hover {
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
}

.empty-status {
  padding: 14px 12px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.agent-list {
  flex: 1;
  min-height: 0;
  max-height: min(420px, calc(60vh - 44px));
  overflow-y: auto;
}

.agent-row {
  width: 100%;
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 0;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: 10px 11px;
  text-align: left;
}

.agent-row:last-child {
  border-bottom: 0;
}

.agent-row:hover,
.agent-row.active {
  background: var(--ch-color-row-hover);
}

.agent-main {
  min-width: 0;
}

.agent-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.agent-name {
  min-width: 0;
  color: var(--ch-color-text);
  font-size: 13px;
  font-weight: 650;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-type {
  flex: 0 0 auto;
  color: var(--ch-color-text-muted);
  font-size: 10px;
  text-transform: uppercase;
}

.agent-detail {
  display: block;
  margin-top: 3px;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 76px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  padding: 5px 8px;
  font-size: 11px;
  font-weight: 700;
}

.status-pill[data-status='idle'] {
  background: var(--ch-color-success-bg);
}

.status-pill[data-status='working'] {
  background: var(--ch-color-warning-bg);
}

.status-pill[data-status='attention'] {
  background: var(--ch-color-attention-bg);
}

.status-pill[data-status='offline'] {
  background: var(--ch-color-chip-bg);
}

.resize-handle {
  position: absolute;
  left: 0;
  bottom: 0;
  width: 18px;
  height: 18px;
  border: 0;
  background: transparent;
  cursor: nesw-resize;
}

.resize-handle::before {
  content: '';
  position: absolute;
  left: 5px;
  bottom: 5px;
  width: 8px;
  height: 8px;
  border-left: 1px solid var(--ch-color-border-hover);
  border-bottom: 1px solid var(--ch-color-border-hover);
}

@media (max-width: 768px), (pointer: coarse) {
  .status-trigger {
    min-width: 60px;
    padding: 0 7px;
  }

  .trigger-label {
    display: none;
  }

  .status-panel {
    position: fixed;
    top: 96px;
    right: 8px;
    left: 8px;
    width: auto !important;
    max-width: none;
    max-height: min(60vh, calc(100vh - 112px));
  }
}
</style>
