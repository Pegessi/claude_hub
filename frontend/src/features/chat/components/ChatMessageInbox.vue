<template>
  <aside
    v-if="receipts.length || error"
    class="message-inbox"
    :class="{ 'message-inbox--error': error }"
    aria-label="Hub message inbox"
  >
    <div v-if="error">
      <p role="status">
        Could not refresh Hub message status. Retrying automatically.
      </p>
      <p>
        Check status before resending. If this continues, save unsent text and
        attachments, then refresh the page.
      </p>
      <button
        type="button"
        @click="retry"
      >
        Retry status
      </button>
      <details>
        <summary>Error details</summary>
        <p>{{ error }}</p>
      </details>
      <p v-if="receipts.length">
        Last known status (may be out of date):
      </p>
    </div>
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
import { messageInboxReadError, pendingMessageReceipts, type PendingMessageReceipt } from '../presentation/chatMessageInbox'

const props = withDefaults(defineProps<{ tabId: string; active?: boolean }>(), { active: true })
const emit = defineEmits<{ retry: []; recovered: [] }>()
const receipts = ref<PendingMessageReceipt[]>([])
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
let request: AbortController | undefined
let polling = false
let generation = 0
let consecutiveFailures = 0

function stop() {
  polling = false
  generation += 1
  clearTimeout(timer)
  request?.abort()
}
async function refresh() {
  const identity = generation
  const controller = new AbortController()
  request = controller
  const timeout = setTimeout(() => controller.abort(), 10000)
  let responseStatus: number | undefined
  let stopPolling = false
  try {
    const response = await fetch(`/api/workspaces/tabs/${encodeURIComponent(props.tabId)}/messages`, {
      credentials: 'same-origin', signal: controller.signal,
    })
    responseStatus = response.status
    if (!response.ok) throw new Error('Inbox unavailable')
    const result: unknown = await response.json()
    if (!polling || identity !== generation) return
    const hadFailure = consecutiveFailures > 0 || Boolean(error.value)
    receipts.value = pendingMessageReceipts(result)
    consecutiveFailures = 0
    error.value = ''
    if (hadFailure) emit('recovered')
  } catch {
    if (!polling || identity !== generation) return
    consecutiveFailures += 1
    const authDenied = responseStatus === 401 || responseStatus === 403
    if (authDenied) {
      stopPolling = true
      polling = false
    }
    if (receipts.value.length > 0 || authDenied || consecutiveFailures >= 2) {
      error.value = messageInboxReadError(responseStatus, controller.signal.aborted)
    }
  } finally {
    clearTimeout(timeout)
    if (!stopPolling && polling && identity === generation) {
      timer = setTimeout(() => { void refresh() }, 3000)
    }
  }
}
function start() {
  if (polling || !props.active) return
  polling = true
  void refresh()
}
function retry() {
  // Retire the pending poll so its late result cannot replace this retry.
  stop()
  emit('retry')
  if (!props.active) {
    polling = true
    void refresh()
    return
  }
  start()
}
watch(() => props.tabId, () => {
  stop()
  consecutiveFailures = 0
  receipts.value = []
  error.value = ''
  start()
})
watch(() => props.active, isActive => {
  if (isActive) start()
  else stop()
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
.message-inbox--error { max-height: min(18rem, 40dvh); }
.message-inbox p, .message-inbox ul { margin: 0; }
.message-inbox p { margin-bottom: 4px; }
.message-inbox button, .message-inbox summary {
  min-height: 44px;
  padding: 8px;
  cursor: pointer;
}
.message-inbox button {
  color: var(--ch-color-text);
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
}
.message-inbox summary { align-content: center; }
.message-inbox ul { padding-left: 1rem; }
</style>
