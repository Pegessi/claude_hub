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
          <span
            class="panel-refresh-icon"
            aria-hidden="true"
          >↻</span>
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
            <span>{{ group.title }}</span>
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
const { manualTabs, managedTabs, agentStatuses, activeTabId } = storeToRefs(store)
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
    return [{ key: 'manual', title: '', rows: rows.value }]
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

<style scoped>
/*
 * Agent status floating panel — trigger chip + popover list.
 *
 * Styling uses the global design-token scale (--ch-space-*, --ch-font-*,
 * --ch-leading-*, --ch-weight-*) defined in frontend/src/App.vue :root,
 * plus the established color/radius/shadow/motion tokens. Hardcoded px
 * values remain only for functional component dimensions (control height,
 * panel min/max sizes, pill diameters, avatar ring widths) and 1px borders —
 * those are layout constants, not visual rhythm.
 */

.agent-status {
  position: relative;
  align-self: flex-end;
  flex: 0 0 auto;
}

/* --- Trigger chip (collapsed state) ------------------------------------ */

.status-trigger {
  height: 28px;
  min-width: 82px;
  box-sizing: border-box;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--ch-space-2);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background-color: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  padding: 0 var(--ch-space-3);
  font-size: var(--ch-font-sm);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
  transition:
    background var(--ch-motion-fast),
    border-color var(--ch-motion-fast),
    color var(--ch-motion-fast),
    box-shadow var(--ch-motion-fast),
    transform var(--ch-motion-fast),
    height var(--ch-motion-standard) var(--ch-motion-ease),
    min-width var(--ch-motion-standard) var(--ch-motion-ease),
    padding var(--ch-motion-standard) var(--ch-motion-ease),
    gap var(--ch-motion-standard) var(--ch-motion-ease);
}

.status-trigger:hover {
  border-color: var(--ch-color-border-hover);
  background-color: var(--ch-color-surface-control-hover);
}

.status-trigger:active {
  transform: translateY(1px);
}

.agent-status[data-expanded='true'] .status-trigger {
  background-color: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
  box-shadow: 0 1px 3px var(--ch-shadow-color-soft);
}

/* Status dots (trigger + avatar-overlay) use a fixed 8px inline-flex box so
 * they sit on the text baseline regardless of surrounding glyph metrics. */
.trigger-dot,
.status-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  display: inline-block;
  border-radius: 50%;
  background: currentColor;
}

.agent-avatar-wrap {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
}

.agent-avatar-wrap .status-dot {
  position: absolute;
  right: -2px;
  bottom: -2px;
  width: 8px;
  height: 8px;
  box-shadow: 0 0 0 2px var(--ch-color-surface);
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
  font-size: var(--ch-font-sm);
  font-weight: var(--ch-weight-semibold);
  line-height: var(--ch-leading-tight);
}

.trigger-count {
  min-width: 18px;
  height: 18px;
  padding: 0 var(--ch-space-1);
  box-sizing: border-box;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: 18px;
  text-align: center;
  transition: background var(--ch-motion-fast);
}

.status-trigger:hover .trigger-count,
.agent-status[data-expanded='true'] .trigger-count {
  background: var(--ch-color-surface-control-hover);
}

/* --- Popover panel ----------------------------------------------------- */

.status-panel {
  position: absolute;
  top: calc(100% + var(--ch-space-2));
  right: 0;
  z-index: 1200;
  width: min(360px, calc(100vw - 16px));
  max-width: calc(100vw - 16px);
  max-height: min(60vh, 520px, calc(100vh - 72px));
  overflow: hidden;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-popover);
  display: flex;
  flex-direction: column;
  /* One-shot entrance only — no looping/pulsing/blinking. */
  animation: status-panel-in var(--ch-motion-panel) var(--ch-motion-ease);
  transform-origin: top right;
}

@keyframes status-panel-in {
  from {
    opacity: 0;
    transform: translateY(-4px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ch-space-2);
  padding: var(--ch-space-2) var(--ch-space-3);
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.panel-title-block {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--ch-space-1);
}

.panel-title {
  color: var(--ch-color-text);
  font-size: var(--ch-font-sm);
  font-weight: var(--ch-weight-semibold);
  line-height: var(--ch-leading-tight);
}

.panel-subtitle {
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

/* Refresh icon button: same 28px height as other controls for a consistent
 * hit-target, with an internal 14x14 glyph box aligned to the .btn-icon
 * convention used elsewhere in the app. */
.panel-refresh {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  transition: background var(--ch-motion-fast), color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.panel-refresh-icon {
  width: 14px;
  height: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: var(--ch-font-icon-sm);
  line-height: 1;
  flex-shrink: 0;
}

.panel-refresh:hover {
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
}

.panel-refresh:active {
  transform: rotate(25deg) scale(0.92);
}

/* --- Mode switch (Agents / Reviewers) ---------------------------------- */

.panel-mode-switch {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--ch-space-2);
  padding: var(--ch-space-2) var(--ch-space-3);
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.panel-mode-switch button {
  height: 28px;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ch-space-2);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  padding: 0 var(--ch-space-2);
  font-size: var(--ch-font-sm);
  line-height: var(--ch-leading-tight);
  text-align: left;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.panel-mode-switch button:hover {
  border-color: var(--ch-color-border-hover);
  color: var(--ch-color-text);
}

.panel-mode-switch button:active {
  transform: translateY(1px);
}

.panel-mode-switch button[data-active='true'] {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent-strong);
  font-weight: var(--ch-weight-semibold);
}

.panel-mode-switch span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
}

.panel-mode-switch strong {
  min-width: 18px;
  height: 18px;
  padding: 0 var(--ch-space-1);
  box-sizing: border-box;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: currentColor;
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: 18px;
  text-align: center;
}

.panel-mode-switch button:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
}

.status-trigger:focus-visible,
.agent-row:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
}

/* --- List body --------------------------------------------------------- */

.empty-status {
  padding: var(--ch-space-3);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-sm);
  line-height: var(--ch-leading-normal);
}

.agent-list {
  flex: 1;
  min-height: 0;
  max-height: min(420px, calc(60vh - 44px));
  overflow-y: auto;
}

.agent-group + .agent-group {
  border-top: 1px solid var(--ch-color-border-muted);
}

.agent-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ch-space-2);
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text-muted);
  padding: var(--ch-space-2) var(--ch-space-3);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-semibold);
  line-height: var(--ch-leading-tight);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.agent-group-header span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-group-header strong {
  flex: 0 0 auto;
  color: var(--ch-color-text);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
}

.agent-row {
  width: 100%;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--ch-space-3);
  border: 0;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: var(--ch-space-3);
  text-align: left;
  transition: background var(--ch-motion-fast);
}

.agent-row:last-child {
  border-bottom: 0;
}

.agent-row:hover {
  background: var(--ch-color-row-hover);
}

.agent-row.active {
  background: color-mix(in srgb, var(--ch-color-accent) 8%, var(--ch-color-row-hover));
  box-shadow: inset 3px 0 0 var(--ch-color-accent);
}

.agent-main {
  min-width: 0;
}

.agent-line {
  display: flex;
  align-items: center;
  gap: var(--ch-space-2);
  min-width: 0;
}

.agent-name {
  min-width: 0;
  color: var(--ch-color-text);
  font-size: var(--ch-font-md);
  font-weight: var(--ch-weight-semibold);
  line-height: var(--ch-leading-tight);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-type {
  flex: 0 0 auto;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-xs);
  line-height: var(--ch-leading-tight);
  text-transform: uppercase;
}

/* Kind chip (claude / codex / cursor / terminal) */
.agent-cli {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 2px var(--ch-space-2);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
}

.agent-cli[data-kind='claude'] {
  background: rgba(217, 119, 87, 0.18);
  color: var(--ch-agent-claude-fg);
}

.agent-cli[data-kind='codex'] {
  background: rgba(16, 163, 127, 0.18);
  color: var(--ch-agent-codex-fg-cli);
}

.agent-cli[data-kind='cursor'] {
  background: rgba(120, 120, 120, 0.22);
  color: var(--ch-color-text);
}

.agent-cli[data-kind='terminal'] {
  background: rgba(126, 231, 135, 0.16);
  color: var(--ch-color-success);
}

.agent-detail {
  display: block;
  margin-top: var(--ch-space-1);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-regular);
  line-height: var(--ch-leading-normal);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Status pill on the right of each row */
.status-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 76px;
  height: 22px;
  padding: 0 var(--ch-space-2);
  box-sizing: border-box;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  font-size: var(--ch-font-xs);
  font-weight: var(--ch-weight-medium);
  line-height: var(--ch-leading-tight);
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

/* --- Resize handle ----------------------------------------------------- */

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
  left: var(--ch-space-1);
  bottom: var(--ch-space-1);
  width: var(--ch-space-2);
  height: var(--ch-space-2);
  border-left: 1px solid var(--ch-color-border-hover);
  border-bottom: 1px solid var(--ch-color-border-hover);
}

/* --- Mobile / coarse-pointer fallback ---------------------------------- */

@media (max-width: 768px), (pointer: coarse) {
  .status-trigger {
    min-width: 60px;
    padding: 0 var(--ch-space-2);
  }

  .trigger-label {
    display: none;
  }

  .status-panel {
    position: fixed;
    top: 96px;
    right: var(--ch-space-2);
    left: var(--ch-space-2);
    width: auto !important;
    max-width: none;
    max-height: min(60vh, calc(100vh - 112px));
  }
}
</style>
