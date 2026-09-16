<template>
  <div
    class="terminal-pane"
    :class="{ active: pane.isActive, empty: !pane.tabId, 'drag-over': isDragOver }"
    @click="handleClick"
    @dragover.prevent="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
  >
    <!-- The session kind is fixed at creation: Chat sessions own the
         structured conversation surface, while Terminal sessions own raw PTY.
         No per-pane info header: the TabBar already identifies the active tab,
         so a header would just duplicate it and cost a row of space. -->

    <!-- 空状态 -->
    <div
      v-if="!pane.tabId"
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

    <!-- Session kind is an ownership boundary: Terminal mounts raw PTY and
         Chat mounts structured UI. There is no silent cross-mode fallback. -->
    <div
      v-if="pane.tabId && !isChatSession"
      class="pane-terminal"
    >
      <TerminalView
        :tab-id="pane.tabId"
        :agent-type="getAgentType()"
      />
    </div>

    <!-- Only top-level Chat sessions use the native structured endpoint.
         Workspace-managed agents always remain on their Terminal surface.
         KeepAlive caches one StructuredPane per chat tab (keyed by tabId) so
         switching tabs preserves scroll, draft, attachments, and expand state. -->
    <div
      v-if="pane.tabId && isChatSession"
      class="pane-structured"
    >
      <KeepAlive :max="MAX_CACHED_CHAT_PANES">
        <StructuredPane
          :key="pane.tabId"
          :tab-id="pane.tabId"
        />
      </KeepAlive>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useTerminalStore } from '@/stores/terminalStore'
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
const { tabs } = storeToRefs(store)

// Resolve the current tab once via computed so we don't do a tabs.find() on
// every parent re-render (e.g. each agent-status poll tick).
const paneTab = computed<TerminalTab | undefined>(() =>
  props.pane.tabId ? tabs.value.find((t: TerminalTab) => t.id === props.pane.tabId) : undefined
)
const agentType = computed(() => paneTab.value?.agent_type)
// workspace_id is optional display metadata on direct top-level sessions.
// Only a workspace role marks an internal Agent Workspace runner.
const isManagedTab = computed(() => Boolean(paneTab.value?.workspace_role))
const isChatSession = computed(() =>
  paneTab.value?.session_kind === 'chat' && !isManagedTab.value
)

// Upper bound on cached StructuredPane instances per pane. Each entry holds a
// full conversation DOM + composer state, so this stays modest; the global
// history LRU (agentStreamHistoryCache) is the larger safety net for panes
// evicted from this cache.
const MAX_CACHED_CHAT_PANES = 8

const isDragOver = ref(false)

function getAgentType() {
  return agentType.value
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
  position: relative;
  z-index: 1;
}

.pane-structured {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
  position: relative;
  z-index: 2;
  background-color: var(--ch-color-app-bg);
}
</style>
