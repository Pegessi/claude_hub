<template>
  <aside
    v-if="receipts.length || error"
    class="message-inbox"
    aria-label="Hub message inbox"
  >
    <p
      v-if="error"
      role="status"
    >
      {{ error }}
    </p>
    <ul v-if="receipts.length">
      <li
        v-for="receipt in receipts"
        :key="receipt.message_id"
      >
        Hub message {{ receipt.message_id.slice(0, 8) }} · {{ receipt.state }}
        <span v-if="receipt.waiting_reason"> · {{ receipt.waiting_reason }}</span>
        <span v-if="receipt.state === 'uncertain'"> · Delivery uncertain; check status before retrying.</span>
      </li>
    </ul>
  </aside>
</template>

<script setup lang="ts">
import { onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { pendingMessageReceipts, type PendingMessageReceipt } from '../presentation/chatMessageInbox'

const props = defineProps<{ tabId: string }>()
const receipts = ref<PendingMessageReceipt[]>([])
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
let request: AbortController | undefined
let active = false
let generation = 0

function stop() {
  active = false
  generation += 1
  clearTimeout(timer)
  request?.abort()
}
async function refresh() {
  const identity = generation
  const controller = new AbortController()
  request = controller
  const timeout = setTimeout(() => controller.abort(), 10000)
  try {
    const response = await fetch(`/api/workspaces/tabs/${encodeURIComponent(props.tabId)}/messages`, {
      credentials: 'same-origin', signal: controller.signal,
    })
    if (!response.ok) throw new Error('Inbox unavailable')
    const result: unknown = await response.json()
    if (!active || identity !== generation) return
    receipts.value = pendingMessageReceipts(result)
    error.value = ''
  } catch {
    if (!active || identity !== generation) return
    error.value = 'Hub message inbox unavailable; retrying…'
  } finally {
    clearTimeout(timeout)
    if (active && identity === generation) timer = setTimeout(() => { void refresh() }, 3000)
  }
}
function start() {
  if (active) return
  active = true
  void refresh()
}
watch(() => props.tabId, () => {
  stop()
  receipts.value = []
  error.value = ''
  start()
})
onMounted(start)
onActivated(start)
onDeactivated(stop)
onUnmounted(stop)
</script>

<style scoped>
.message-inbox {
  flex-shrink: 0;
  max-height: 8rem;
  overflow: auto;
  overflow-wrap: anywhere;
  padding: 0.5rem 1rem;
  font-size: 0.75rem;
  color: var(--text-secondary);
  border-top: 1px solid var(--border-color);
}
.message-inbox p, .message-inbox ul { margin: 0; }
.message-inbox ul { padding-left: 1rem; }
</style>
