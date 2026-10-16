<template>
  <dialog
    ref="dialog"
    class="restart-dialog"
    aria-labelledby="restart-title"
    aria-describedby="restart-description"
    @close="emit('close')"
    @cancel.prevent="emit('close')"
    @click="onBackdrop"
  >
    <h2 id="restart-title">
      {{ phase === 'succeeded' ? 'Service is back online' : 'Build and restart service' }}
    </h2>
    <p id="restart-description">
      {{ phase === 'confirm' ? 'This rebuilds the frontend, synchronizes backend dependencies, then restarts the service. Running chats and agent tasks are interrupted for everyone only after preparation succeeds. The page will briefly disconnect.' : message }}
    </p>
    <label
      v-if="phase === 'confirm'"
      class="restart-continuation"
    >
      <input
        v-model="continueChats"
        type="checkbox"
      >
      <span>Continue interrupted Chats automatically
        <small>Send “Please continue.” to eligible working Chats after restart. Chats waiting for your input and managed automations keep their own recovery controls.</small>
      </span>
    </label>
    <p
      v-if="phase === 'confirm' && !status?.available"
      class="restart-status"
    >
      {{ status?.reason }}
    </p>
    <p
      v-if="phase === 'waiting'"
      role="status"
      class="restart-status"
    >
      Reconnecting and restoring work… {{ elapsed }}s
    </p>
    <div class="restart-actions">
      <button
        type="button"
        autofocus
        @click="emit('close')"
      >
        {{ phase === 'confirm' ? 'Cancel' : 'Close' }}
      </button>
      <button
        v-if="phase === 'confirm'"
        type="button"
        class="restart-confirm"
        :disabled="!status?.available"
        @click="confirmRestart"
      >
        Build and restart
      </button>
      <button
        v-if="phase === 'error'"
        type="button"
        @click="initialize"
      >
        Check again
      </button>
      <button
        v-if="phase === 'succeeded'"
        type="button"
        @click="reloadPage"
      >
        Reload page
      </button>
    </div>
  </dialog>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import {
  createRestartId, fetchRestartStatus, RESTART_STORAGE_KEY, restartOutcome, restartCompletionMessage, RestartRequestError,
  type PendingRestart, type RestartStatus,
} from '@/utils/serviceRestart'

const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const phase = ref<'loading' | 'confirm' | 'waiting' | 'succeeded' | 'error'>('loading')
const message = ref('Checking service…')
const status = ref<RestartStatus | null>(null)
const elapsed = ref(0)
const continueChats = ref(true)
let pending: PendingRestart | null = null
let disposed = false
const recoveryTimeoutMs = 300_000

function savePending(value: PendingRestart | null) {
  pending = value
  if (value) sessionStorage.setItem(RESTART_STORAGE_KEY, JSON.stringify(value))
  else sessionStorage.removeItem(RESTART_STORAGE_KEY)
}

async function waitForRecovery() {
  if (!pending) return
  phase.value = 'waiting'
  const started = Date.now()
  let serviceReturned = false
  while (!disposed && Date.now() - started < recoveryTimeoutMs) {
    elapsed.value = Math.floor((Date.now() - started) / 1000)
    try {
      const current = await fetchRestartStatus()
      if (disposed) return
      const outcome = restartOutcome(current, pending)
      if (current.operation?.id === pending.id) message.value = current.operation.message
      if (outcome === 'succeeded') {
        serviceReturned = true
        if (current.operation?.continuation?.pending) {
          message.value = 'The service is back online. Continuing interrupted Chats…'
          await new Promise(resolve => window.setTimeout(resolve, 1000))
          continue
        }
      }
      if (outcome !== 'waiting') {
        phase.value = outcome === 'succeeded' ? 'succeeded' : 'error'
        if (outcome === 'succeeded') message.value = restartCompletionMessage(current.operation)
        savePending(null)
        return
      }
    } catch {
      message.value = 'The connection is temporarily unavailable. Waiting for the service to return…'
    }
    await new Promise(resolve => window.setTimeout(resolve, 1000))
  }
  if (!disposed) {
    if (serviceReturned) {
      phase.value = 'succeeded'
      message.value = 'The service restarted. Some continuation attempts still need attention; check those Chats before continuing them manually.'
      savePending(null)
      return
    }
    phase.value = 'error'
    message.value = 'Restart could not be confirmed within 5 minutes. Check again, or open a terminal on the host and inspect the service logs. Another restart has not been sent.'
  }
}

async function initialize() {
  phase.value = 'loading'
  message.value = 'Checking service…'
  try {
    const saved = JSON.parse(sessionStorage.getItem(RESTART_STORAGE_KEY) || 'null')
    if (saved?.id && saved?.instanceId) pending = saved
    if (pending) return await waitForRecovery()
    status.value = await fetchRestartStatus()
    if (disposed) return
    const operation = status.value.operation
    if (status.value.available && operation && ['preparing', 'restarting'].includes(operation.status)) {
      savePending({ id: operation.id, instanceId: operation.instance_id })
      return await waitForRecovery()
    }
    phase.value = 'confirm'
  } catch (error) {
    phase.value = 'error'
    message.value = error instanceof Error ? error.message : 'Unable to check the service.'
  }
}

async function confirmRestart() {
  if (phase.value !== 'confirm' || !status.value?.available) return
  // Persist before sending: a dropped POST response must not trigger a second POST.
  savePending({ id: createRestartId(), instanceId: status.value.instance_id })
  phase.value = 'waiting'
  message.value = 'Requesting restart…'
  try {
    const accepted = await fetchRestartStatus({ instance_id: pending!.instanceId, request_id: pending!.id, resume_running_chats: continueChats.value })
    if (accepted.operation) savePending({ id: accepted.operation.id, instanceId: accepted.operation.instance_id })
  } catch (error) {
    if (error instanceof RestartRequestError) {
      savePending(null)
      phase.value = 'error'
      message.value = error.message
      return
    }
    // The backend may already have gone away after accepting the request.
  }
  await waitForRecovery()
}

function onBackdrop(event: MouseEvent) {
  if (!dialog.value || event.target !== dialog.value) return
  const bounds = dialog.value.getBoundingClientRect()
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) emit('close')
}
function reloadPage() { window.location.reload() }
onMounted(() => { dialog.value?.showModal(); void initialize() })
onUnmounted(() => { disposed = true; dialog.value?.close() })
</script>

<style scoped>
.restart-dialog {
  width: min(460px, calc(100vw - 32px));
  box-sizing: border-box;
  margin: auto;
  padding: 24px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-popover);
}
.restart-dialog::backdrop { background: rgb(0 0 0 / 55%); }
h2 { margin: 0 0 14px; font-size: 19px; }
p { margin: 0 0 18px; font-size: 14px; line-height: 1.6; }
.restart-status { color: var(--ch-color-text-muted); }
.restart-continuation { display: flex; gap: 10px; align-items: flex-start; margin: 0 0 18px; font-size: 14px; cursor: pointer; }
.restart-continuation input { margin-top: 3px; min-width: 18px; min-height: 18px; }
.restart-continuation small { display: block; margin-top: 6px; color: var(--ch-color-text-muted); line-height: 1.5; }
.restart-actions { display: flex; justify-content: flex-end; gap: 10px; }
button {
  min-height: 40px;
  font: inherit;
  font-size: 14px;
  padding: 8px 14px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
}
button:hover { background: var(--ch-color-surface-control-hover); }
button:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: 2px; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
.restart-confirm { color: var(--ch-color-danger); }
@media (max-width: 640px) { button { min-height: 44px; } }
</style>
