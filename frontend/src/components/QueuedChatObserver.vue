<template>
  <span hidden />
</template>

<script setup lang="ts">
import { computed, onUnmounted, watch } from 'vue'
import { useAgentStream } from '@/composables/useAgentStream'
import { useTerminalStore } from '@/stores/terminalStore'
import { IncrementalTimelineReducer } from '@/utils/agentStreamTimeline'
import { isChatModeLocked } from '@/utils/chatTurnLifecycle'

const props = defineProps<{
  paneId: string
  tabId: string
  managedChat?: boolean
  managedSessionId?: string
}>()

const store = useTerminalStore()
const queue = store.getQueuedChatState(props.paneId, props.tabId)
const { events, connectionState, start, stop } = useAgentStream()
const reducer = new IncrementalTimelineReducer()
const turnInFlight = computed(() => isChatModeLocked(false, reducer.reduce(events.value)))

function reconcileQueue() {
  if (queue.drafts.length === 0 || queue.paused || queue.editing || queue.sendInProgress) {
    store.releaseQueuedChatActivation(props.paneId, props.tabId)
    stop()
    return
  }
  if (queue.autoActivationBlock === 'stream-failed' && connectionState.value === 'live') {
    queue.autoActivationBlock = null
  }
  if (queue.autoActivationBlock !== null) return
  if (
    connectionState.value === 'failed'
    || (connectionState.value === 'live' && !turnInFlight.value)
  ) {
    store.requestQueuedChatActivation(props.paneId, props.tabId)
  }
}

if (!props.managedChat || props.managedSessionId) {
  void start(
    props.managedSessionId ?? props.tabId,
    props.managedChat ? 'managed-session' : 'terminal-tab',
  )
}
watch(
  [
    () => queue.drafts.length,
    () => queue.paused,
    () => queue.editing,
    () => queue.sendInProgress,
    () => queue.autoActivationBlock,
    turnInFlight,
    connectionState,
  ],
  reconcileQueue,
  { immediate: true },
)

onUnmounted(() => {
  stop()
})
</script>
