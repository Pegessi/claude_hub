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

    <!-- 终端视图 -->
    <TerminalView
      v-if="pane.tabId"
      :tab-id="pane.tabId"
      :agent-type="getAgentType()"
      class="pane-terminal"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
import TerminalView from '@/components/TerminalView.vue'
import type { Pane, TerminalTab } from '@/types'

const props = defineProps<{
  pane: Pane
}>()

const emit = defineEmits<{
  (e: 'click'): void
}>()

const store = useTerminalStore()
const { tabs } = storeToRefs(store)

// Resolve the current tab once via computed so we don't do a tabs.find() on
// every parent re-render (e.g. each agent-status poll tick).
const paneTab = computed<TerminalTab | undefined>(() =>
  props.pane.tabId ? tabs.value.find((t: TerminalTab) => t.id === props.pane.tabId) : undefined
)
const tabName = computed(() => paneTab.value?.name || '')
const agentType = computed(() => paneTab.value?.agent_type)

const isDragOver = ref(false)
const isRefreshingHistory = ref(false)
let refreshFeedbackTimer: number | null = null

// (F8) WindowWithTerminalHistory no longer needed — globals are typed via Window.__claudeHub in types/index.ts

function getTabName(): string {
  return tabName.value
}

function getAgentType() {
  return agentType.value
}

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
}
</style>
