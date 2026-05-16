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
import { ref, onMounted, onUnmounted } from 'vue'
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

const isDragOver = ref(false)

function getTabName(): string {
  if (!props.pane.tabId) return ''
  const tab = tabs.value.find((t: TerminalTab) => t.id === props.pane.tabId)
  return tab?.name || ''
}

function getAgentType() {
  if (!props.pane.tabId) return undefined
  const tab = tabs.value.find((t: TerminalTab) => t.id === props.pane.tabId)
  return tab?.agent_type
}

function handleClick() {
  emit('click')
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
})

onUnmounted(() => {
  window.removeEventListener('message', handleMessage)
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
  max-height: 28px;
  padding: 5px 9px;
  background-color: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
  flex-shrink: 0;
  overflow: hidden;
  transition: max-height 180ms cubic-bezier(0.2, 0, 0, 1), padding 180ms cubic-bezier(0.2, 0, 0, 1), border-color 180ms cubic-bezier(0.2, 0, 0, 1), opacity 140ms ease, transform 180ms cubic-bezier(0.2, 0, 0, 1);
}

.pane-tab-name {
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
