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
         No full info header: the TabBar already identifies the active tab, so
         a header would just duplicate it and cost a row of space. -->
    <!-- Minimal session name in the top-right corner so a pane can still be
         identified at a glance (multi-pane layouts, or when reviewing history)
         without the cost of a full header row. Terminal panes get a tiny
         manual reconnect button beside it: re-attaches the same ttyd/tmux
         stream (Chat panes have their own surface and never show it). -->
    <div
      v-if="pane.tabId"
      class="pane-chrome"
    >
      <button
        v-if="!isChatSession"
        type="button"
        class="pane-reconnect-button"
        :class="reconnectStateClass"
        :title="reconnectTitle"
        :aria-label="reconnectTitle"
        :disabled="isReconnecting"
        @click.stop="handleReconnect"
      >
        <span
          class="pane-reconnect-icon"
          aria-hidden="true"
        >
          <!-- connecting: spinner; success: check; error: alert; idle: ↻ -->
          <svg
            v-if="reconnectStatus?.state === 'connecting'"
            class="pane-reconnect-spinner"
            viewBox="0 0 16 16"
            fill="none"
          >
            <circle
              cx="8"
              cy="8"
              r="6"
              stroke="currentColor"
              stroke-opacity="0.25"
              stroke-width="2"
            />
            <path
              d="M14 8a6 6 0 0 0-6-6"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
            />
          </svg>
          <svg
            v-else-if="reconnectStatus?.state === 'success'"
            viewBox="0 0 16 16"
            fill="none"
          >
            <path
              d="M3.5 8.5l3 3 6-6.5"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <svg
            v-else-if="reconnectStatus?.state === 'error'"
            viewBox="0 0 16 16"
            fill="none"
          >
            <path
              d="M8 4.5v4"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
            <circle
              cx="8"
              cy="11.4"
              r="0.9"
              fill="currentColor"
            />
          </svg>
          <svg
            v-else
            viewBox="0 0 16 16"
            fill="none"
          >
            <path
              d="M13.2 8A5.2 5.2 0 1 1 8 2.8c1.7 0 3.2.8 4.1 2.1M12.6 2.2v3h-3"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </span>
      </button>
      <span
        class="pane-session-name"
        :title="tabName"
      >{{ tabName }}</span>
    </div>

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
const tabName = computed(() => paneTab.value?.name || '')
// workspace_id is optional display metadata on direct top-level sessions.
// Only a workspace role marks an internal Agent Workspace runner.
const isManagedTab = computed(() => Boolean(paneTab.value?.workspace_role))
const isChatSession = computed(() =>
  paneTab.value?.session_kind === 'chat' && !isManagedTab.value
)

// ── Manual reconnect button (Terminal panes only) ──────────────────────────
// Status is per-tab in the store; TerminalView performs the actual re-attach
// when the request nonce bumps. While connecting the button shows a spinner
// and is disabled (the store itself also coalesces in-flight requests).
const reconnectStatus = computed(() =>
  props.pane.tabId ? store.paneReconnectStatus(props.pane.tabId) : null
)
const isReconnecting = computed(() => reconnectStatus.value?.state === 'connecting')
const reconnectStateClass = computed(() => {
  const state = reconnectStatus.value?.state
  return state && state !== 'idle' ? `is-${state}` : ''
})
const reconnectTitle = computed(() => {
  switch (reconnectStatus.value?.state) {
    case 'connecting':
      return 'Reconnecting…'
    case 'success':
      return 'Reconnected'
    case 'error':
      return 'Reconnect failed — click to retry'
    default:
      return 'Reconnect terminal'
  }
})

function handleReconnect() {
  const tabId = props.pane.tabId
  if (!tabId || isReconnecting.value) return
  // Re-dispatch after a previous failure/clear is allowed; an in-flight
  // reconnect is deduped in the store and disabled in the UI.
  store.requestPaneReconnect(tabId)
}

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

/* Floating top-right chrome cluster: manual reconnect icon + session name.
   Sits in the same spot the session-name pill used to occupy; the cluster
   never takes a full row and never blocks the terminal surface below. */
.pane-chrome {
  position: absolute;
  top: 6px;
  right: 8px;
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 5px;
  max-width: 55%;
}

/* Tiny icon-only reconnect control, styled to match the floating name pill:
   same raised surface, hairline border, and muted color. It stays quiet until
   hover/focus, and only the transient reconnect states (spinner/success/
   error) bring in color. */
.pane-reconnect-button {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 21px;
  height: 21px;
  padding: 0;
  border-radius: 999px;
  background: var(--ch-color-surface-raised);
  border: 1px solid var(--ch-color-border-muted);
  box-shadow: 0 1px 4px var(--ch-shadow-color-soft);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  transition:
    color var(--ch-motion-fast),
    border-color var(--ch-motion-fast),
    background-color var(--ch-motion-fast);
}

.pane-reconnect-button:hover:not(:disabled) {
  color: var(--ch-color-text-strong);
  border-color: var(--ch-color-border-strong);
}

.pane-reconnect-button:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 1px;
}

.pane-reconnect-button:disabled {
  cursor: default;
}

.pane-reconnect-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 13px;
  height: 13px;
}

.pane-reconnect-icon svg {
  width: 13px;
  height: 13px;
}

.pane-reconnect-button.is-connecting {
  color: var(--ch-color-accent);
}

.pane-reconnect-spinner {
  animation: pane-reconnect-spin 700ms linear infinite;
}

@keyframes pane-reconnect-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .pane-reconnect-spinner {
    animation-duration: 1400ms;
  }
}

.pane-reconnect-button.is-success {
  color: var(--ch-color-success-strong);
  border-color: var(--ch-color-success-strong);
}

.pane-reconnect-button.is-error {
  color: var(--ch-color-danger-strong);
  border-color: var(--ch-color-danger-border);
}

@media (prefers-reduced-motion: reduce) {
  .pane-reconnect-button,
  .pane-reconnect-button:hover:not(:disabled) {
    transition: none;
  }
}

.pane-session-name {
  min-width: 0;
  padding: 2px 9px;
  border-radius: 999px;
  background: var(--ch-color-surface-raised);
  border: 1px solid var(--ch-color-border-muted);
  box-shadow: 0 1px 4px var(--ch-shadow-color-soft);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 500;
  line-height: 1.5;
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
