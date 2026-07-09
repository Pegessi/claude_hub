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
/*
 * Terminal pane — chrome (header bar, refresh button, empty-state) surrounding
 * the ttyd/xterm canvas. The canvas itself (.pane-terminal and everything it
 * hosts) is out of scope for this pass.
 *
 * Styling consumes the global design-token scale (--ch-space-*, --ch-font-*)
 * where values match exactly. This pass applies exactly 7 enumerated
 * substitutions and nothing speculative:
 *   • .pane-header         gap:8px              → --ch-space-2
 *   • .pane-empty          padding:16px         → --ch-space-4
 *   • .empty-icon          margin-bottom:12px   → --ch-space-3
 *   • .pane-empty p        margin:4px 0         → var(--ch-space-1) 0
 *   • .pane-tab-name       font-size:12px       → --ch-font-sm
 *   • .pane-empty p        font-size:13px       → --ch-font-md
 *   • .empty-hint          font-size:11px       → --ch-font-xs
 *
 * The following hardcoded px are LEFT LITERAL with a brief inline comment,
 * per the task's explicit preserve list (off-scale / functional geometry
 * constants, not spacing/type on the token scale):
 *   • .pane-header max-height:28px (header strip height; nearest token 24/32
 *     would visibly change chrome density)
 *   • .pane-header padding:5px 9px (tight chrome padding; asymmetric, no
 *     matching token pair)
 *   • .pane-action-button width/height:22px (icon button geometry; nearest
 *     token 24 would shift the button grid)
 *   • .pane-action-icon font-size:14px (glyph size inside the 22px button;
 *     lg=15px would visibly enlarge the ↻ glyph)
 *   • .empty-icon font-size:32px (large emoji glyph; no 32px font token,
 *     xl=18px is UI body scale)
 *   • .pane-tab-name font-weight now uses var(--ch-weight-medium) (=500);
 *     tokenized per minimalist-audit Finding #18 (zero visual change).
 *
 * Functional constants left without inline comment (stroke/affordance/motion,
 * not spacing or type): 1px/2px borders & outlines, outline-offset:1px,
 * box-shadow offsets, transition durations and cubic-bezier curves (some
 * already on --ch-motion-fast; the 180ms/140ms/700ms/360deg values are
 * bespoke motion curves). Colors, radii, and shadows already on
 * the --ch-color, --ch-radius, --ch-shadow, and --ch-motion-fast tokens
 * are unchanged.
 */

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
  gap: var(--ch-space-2);
  max-height: 28px; /* off-scale chrome height; nearest 24/32 shifts density */
  padding: 5px 9px; /* tight asymmetric chrome padding; no matching token pair */
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
  font-size: var(--ch-font-sm);
  font-weight: var(--ch-weight-medium);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pane-action-button {
  width: 22px; /* off-scale icon-button geometry; nearest 24 shifts grid */
  height: 22px; /* off-scale icon-button geometry; nearest 24 shifts grid */
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
  font-size: 14px; /* ↻ glyph size inside 22px button; lg=15px would enlarge glyph */
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
  padding: var(--ch-space-4);
}

.empty-icon {
  font-size: 32px; /* large empty-state emoji; xl=18px is UI body scale */
  margin-bottom: var(--ch-space-3);
  opacity: 0.5;
}

.pane-empty p {
  margin: var(--ch-space-1) 0;
  font-size: var(--ch-font-md);
}

.empty-hint {
  font-size: var(--ch-font-xs);
  opacity: 0.7;
}

.pane-terminal {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
</style>
