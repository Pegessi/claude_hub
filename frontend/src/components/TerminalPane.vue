<template>
  <div
    class="terminal-pane"
    :class="{ active: pane.isActive, empty: !pane.tabId, 'drag-over': isDragOver }"
    @click="handleClick"
    @dragover.prevent="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
  >
    <!-- Pane 头部：显示当前 tab 名称 -->
    <div
      v-if="pane.tabId"
      class="pane-header"
    >
      <span class="pane-tab-name">{{ getTabName() }}</span>

      <!-- Structured / Raw view toggle.
           Only shown for tabs backed by a managed agent session.
           Plain terminal tabs have no structured plane. -->
      <button
        v-if="managedSession"
        type="button"
        class="pane-action-button pane-view-toggle"
        :title="viewMode === 'raw' ? 'Switch to structured view' : 'Switch to raw terminal'"
        :aria-label="viewMode === 'raw' ? 'Switch to structured view' : 'Switch to raw terminal'"
        :aria-pressed="viewMode === 'structured'"
        @click.stop="toggleView"
      >
        <span
          class="pane-action-icon"
          aria-hidden="true"
        >{{ viewMode === 'raw' ? '≡' : '⌨' }}</span>
      </button>

      <button
        type="button"
        class="pane-action-button"
        :class="{ refreshing: isRefreshingHistory }"
        :disabled="isRefreshingHistory"
        title="Refresh terminal history"
        aria-label="Refresh terminal history"
        @click.stop="refreshHistory"
      >
        <span
          class="pane-action-icon"
          aria-hidden="true"
        >&#x21bb;</span>
      </button>
    </div>

    <!-- 空状态 -->
    <div
      v-else
      class="pane-empty"
    >
      <div class="empty-icon">
        📋
      </div>
      <p>Click a tab to assign to this pane</p>
      <p class="empty-hint">
        Or drag a tab here
      </p>
    </div>

    <!-- 终端视图 (Raw).
         Always mounted when a tab is assigned so the ttyd session + scrollback
         survive switching to the structured view. Hidden via CSS when the
         structured view is active. -->
    <TerminalView
      v-if="pane.tabId"
      :tab-id="pane.tabId"
      :agent-type="getAgentType()"
      class="pane-terminal"
      :class="{ 'is-hidden': viewMode === 'structured' }"
    />

    <!-- Structured view.
         Only rendered when the user has opted in AND the tab has a managed
         session. Fail-closed: if the stream reports structured=false or fails,
         StructuredPane emits fallback-to-raw and we switch back to raw. -->
    <StructuredPane
      v-if="pane.tabId && viewMode === 'structured' && managedSession"
      :session-id="managedSession.id"
      class="pane-structured"
      @fallback-to-raw="switchToRaw"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import TerminalView from '@/components/TerminalView.vue'
import StructuredPane from '@/components/StructuredPane.vue'
import type { Pane, TerminalTab } from '@/types'

const props = defineProps<{
  pane: Pane
}>()

const emit = defineEmits<{
  (e: 'click'): void
}>()

const store = useTerminalStore()
const workspaceStore = useWorkspaceStore()
const { tabs } = storeToRefs(store)

// Resolve the current tab once via computed so we don't do a tabs.find() on
// every parent re-render (e.g. each agent-status poll tick).
const paneTab = computed<TerminalTab | undefined>(() =>
  props.pane.tabId ? tabs.value.find((t: TerminalTab) => t.id === props.pane.tabId) : undefined
)
const tabName = computed(() => paneTab.value?.name || '')
const agentType = computed(() => paneTab.value?.agent_type)

// Map the tab to its managed agent session. The structured observation plane
// is keyed by managed-session id; plain terminal tabs (no session) have no
// structured view and the toggle is hidden.
const managedSession = computed(() =>
  props.pane.tabId ? workspaceStore.sessionForTab(props.pane.tabId) : null
)

type ViewMode = 'raw' | 'structured'
const viewMode = ref<ViewMode>('raw')

const isDragOver = ref(false)
const isRefreshingHistory = ref(false)
let refreshFeedbackTimer: number | null = null

function getTabName(): string {
  return tabName.value
}

function getAgentType() {
  return agentType.value
}

function toggleView() {
  if (viewMode.value === 'raw') {
    // Fail-closed guard: only enter structured view if the tab has a managed
    // session. If not, stay on raw.
    if (!managedSession.value) return
    viewMode.value = 'structured'
  } else {
    viewMode.value = 'raw'
  }
}

function switchToRaw() {
  viewMode.value = 'raw'
}

// If the tab changes (or its session disappears) while in structured mode,
// fall back to raw so we never show a stale structured pane.
watch(
  () => props.pane.tabId,
  () => {
    viewMode.value = 'raw'
  },
)

watch(managedSession, (session) => {
  if (!session && viewMode.value === 'structured') {
    viewMode.value = 'raw'
  }
})

function handleClick() {
  emit('click')
}

function clearRefreshFeedbackTimer() {
  if (refreshFeedbackTimer !== null) {
    window.clearTimeout(refreshFeedbackTimer)
    refreshFeedbackTimer = null
  }
}

function stopRefreshFeedbackAfter(delayMs: number) {
  clearRefreshFeedbackTimer()
  refreshFeedbackTimer = window.setTimeout(() => {
    isRefreshingHistory.value = false
    refreshFeedbackTimer = null
  }, delayMs)
}

function refreshHistory() {
  if (!props.pane.tabId) return
  isRefreshingHistory.value = true
  const refreshTerminalHistory = window.__claudeHub.refreshTerminalHistory
  refreshTerminalHistory?.(props.pane.tabId)
  stopRefreshFeedbackAfter(3000)
}

function handleHistoryRefreshDone(event: Event) {
  const detail = (event as CustomEvent<{ tabId?: string }>).detail
  if (!detail || detail.tabId !== props.pane.tabId) return
  stopRefreshFeedbackAfter(250)
}

function handleMessage(event: MessageEvent) {
  if (event.data && event.data.type === 'terminal-click' && event.data.tabId === props.pane.tabId) {
    emit('click')
  }
}

function handleDragOver(event: DragEvent) {
  event.preventDefault()
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = 'move'
  }
  isDragOver.value = true
}

function handleDragLeave() {
  isDragOver.value = false
}

function handleDrop(event: DragEvent) {
  event.preventDefault()
  isDragOver.value = false

  if (event.dataTransfer) {
    const tabId = event.dataTransfer.getData('text/plain')
    if (tabId) {
      store.assignTabToPane(tabId, props.pane.id)
    }
  }
}

onMounted(() => {
  window.addEventListener('message', handleMessage)
  window.addEventListener('terminal-history-refresh-done', handleHistoryRefreshDone)
})

onUnmounted(() => {
  window.removeEventListener('message', handleMessage)
  window.removeEventListener('terminal-history-refresh-done', handleHistoryRefreshDone)
  clearRefreshFeedbackTimer()
})
</script>

<style scoped>
.terminal-pane {
  position: relative;
  display: flex;
  flex-direction: column;
  background-color: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  overflow: hidden;
  transition: border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast), border-radius 180ms cubic-bezier(0.2, 0, 0, 1);
}

.terminal-pane.active {
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 1px var(--ch-color-accent-ring), 0 10px 28px var(--ch-shadow-color-soft);
}

.terminal-pane.empty {
  border-style: dashed;
}

.terminal-pane.drag-over {
  border-color: var(--ch-color-success-strong);
  background-color: var(--ch-color-success-bg);
}

.pane-header {
  display: flex;
  align-items: center;
  gap: 8px;
  max-height: 28px;
  padding: 5px 9px;
  background-color: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
  flex-shrink: 0;
  overflow: hidden;
  transition: max-height 180ms cubic-bezier(0.2, 0, 0, 1), padding 180ms cubic-bezier(0.2, 0, 0, 1), border-color 180ms cubic-bezier(0.2, 0, 0, 1), opacity 140ms ease, transform 180ms cubic-bezier(0.2, 0, 0, 1);
}

.pane-tab-name {
  flex: 1;
  min-width: 0;
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pane-action-button {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  color: var(--ch-color-text-muted);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
  transition: color var(--ch-motion-fast), background-color var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.pane-action-button:hover:not(:disabled) {
  color: var(--ch-color-text);
  background-color: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border-muted);
}

.pane-action-button:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.pane-action-button:disabled {
  cursor: default;
  opacity: 0.8;
}

.pane-action-icon {
  display: inline-block;
  font-size: 14px;
  line-height: 1;
}

.pane-action-button.refreshing .pane-action-icon {
  animation: pane-history-spin 700ms linear infinite;
}

@keyframes pane-history-spin {
  from {
    transform: rotate(0deg);
  }

  to {
    transform: rotate(360deg);
  }
}

.pane-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--ch-color-text-subtle);
  text-align: center;
  padding: 16px;
}

.empty-icon {
  font-size: 32px;
  margin-bottom: 12px;
  opacity: 0.5;
}

.pane-empty p {
  margin: 4px 0;
  font-size: 13px;
}

.empty-hint {
  font-size: 11px;
  opacity: 0.7;
}

.pane-terminal {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

/* Hide the raw terminal when structured view is active.
   IMPORTANT: we use visibility:hidden + position:absolute (NOT display:none)
   so the ttyd iframe stays loaded and its scrollback is preserved.
   pointer-events:none prevents the hidden terminal from intercepting clicks. */
.pane-terminal.is-hidden {
  position: absolute;
  inset: 0;
  visibility: hidden;
  pointer-events: none;
}

.pane-structured {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}
</style>
