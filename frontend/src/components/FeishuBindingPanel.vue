<template>
  <div
    ref="rootEl"
    class="feishu-binding"
  >
    <button
      type="button"
      class="feishu-binding__trigger"
      :class="`is-${viewState}`"
      aria-label="Feishu connection settings"
      data-testid="feishu-binding-trigger"
      :aria-expanded="open"
      aria-haspopup="dialog"
      :aria-controls="panelId"
      @click="togglePanel"
    >
      <span
        class="feishu-binding__mark"
        aria-hidden="true"
      >飞</span>
      <span>Feishu</span>
      <span
        class="feishu-binding__dot"
        aria-hidden="true"
      />
    </button>

    <div
      v-if="open"
      :id="panelId"
      class="feishu-binding__panel"
      role="dialog"
      aria-label="Feishu connection"
      data-testid="feishu-binding-panel"
      @keydown.esc="closePanel"
    >
      <div class="feishu-binding__header">
        <div>
          <strong>Feishu connection</strong>
          <p>Route this Chat through your private Bot conversation.</p>
        </div>
        <button
          ref="closeButtonEl"
          type="button"
          class="feishu-binding__icon-button"
          aria-label="Close Feishu connection"
          @click="closePanel"
        >
          ×
        </button>
      </div>

      <div
        class="feishu-binding__status"
        role="status"
        aria-live="polite"
        data-testid="feishu-binding-status"
        :data-state="viewState"
      >
        <span
          class="feishu-binding__status-dot"
          aria-hidden="true"
        />
        <span>{{ statusText }}</span>
      </div>

      <p
        v-if="error && viewState !== 'error'"
        class="feishu-binding__error"
        role="alert"
      >
        {{ error }}
      </p>

      <div
        v-if="isLoading && !isHydrated"
        class="feishu-binding__loading"
      >
        Checking connection…
      </div>

      <template v-else-if="viewState === 'pending' && pendingCode">
        <p class="feishu-binding__guidance">
          Send this one-time code to the configured Bot in your Feishu direct chat.
        </p>
        <div class="feishu-binding__code-row">
          <output
            class="feishu-binding__code"
            aria-label="Feishu binding code"
            data-testid="feishu-binding-code"
          >{{ pendingCode.code }}</output>
          <button
            type="button"
            class="feishu-binding__secondary"
            aria-label="Copy binding code"
            @click="copyCode"
          >
            {{ copied ? 'Copied' : 'Copy' }}
          </button>
        </div>
        <p class="feishu-binding__meta">
          Expires {{ formatExpiry(pendingCode.expires_at) }}. This panel updates when the Bot accepts it.
        </p>
        <p
          v-if="copyError"
          class="feishu-binding__error"
          role="alert"
        >
          {{ copyError }}
        </p>
        <div
          v-if="confirmDisconnect"
          class="feishu-binding__confirm"
          role="group"
          aria-label="Confirm Feishu disconnect"
        >
          <span>
            {{ binding
              ? 'This revokes the new code and disconnects the existing Chat.'
              : 'This revokes the pending binding code.' }}
          </span>
          <div class="feishu-binding__actions">
            <button
              type="button"
              class="feishu-binding__danger"
              :disabled="isMutating"
              @click="confirmDisconnectBinding"
            >
              {{ isMutating ? 'Disconnecting…' : 'Confirm disconnect' }}
            </button>
            <button
              type="button"
              class="feishu-binding__secondary"
              :disabled="isMutating"
              @click="confirmDisconnect = false"
            >
              Cancel disconnect
            </button>
          </div>
        </div>
        <button
          v-else
          type="button"
          class="feishu-binding__danger-link"
          :disabled="isMutating"
          @click="confirmDisconnect = true"
        >
          Disconnect Feishu
        </button>
      </template>

      <template v-else-if="viewState === 'bound-current' && binding">
        <p class="feishu-binding__guidance">
          New Bot messages enter this Chat and its replies return to the same Feishu conversation.
        </p>
        <dl class="feishu-binding__details">
          <div>
            <dt>Chat target</dt>
            <dd>{{ binding.tab_id }}</dd>
          </div>
          <div>
            <dt>Connected</dt>
            <dd>{{ formatConnectedAt(binding.created_at) }}</dd>
          </div>
        </dl>
        <div
          v-if="confirmDisconnect"
          class="feishu-binding__confirm"
          role="group"
          aria-label="Confirm Feishu disconnect"
        >
          <span>This stops future Bot messages from entering this Chat.</span>
          <div class="feishu-binding__actions">
            <button
              type="button"
              class="feishu-binding__danger"
              :disabled="isMutating"
              @click="confirmDisconnectBinding"
            >
              {{ isMutating ? 'Disconnecting…' : 'Confirm disconnect' }}
            </button>
            <button
              type="button"
              class="feishu-binding__secondary"
              :disabled="isMutating"
              @click="confirmDisconnect = false"
            >
              Cancel disconnect
            </button>
          </div>
        </div>
        <button
          v-else
          type="button"
          class="feishu-binding__danger-link"
          :disabled="isMutating"
          @click="confirmDisconnect = true"
        >
          Disconnect Feishu
        </button>
      </template>

      <template v-else-if="viewState === 'bound-other' && binding">
        <p class="feishu-binding__guidance">
          Your Feishu conversation currently routes to Chat <code>{{ binding.tab_id }}</code>.
        </p>
        <button
          type="button"
          class="feishu-binding__primary"
          :disabled="isMutating"
          @click="generate"
        >
          {{ isMutating ? 'Generating…' : 'Connect this Chat' }}
        </button>
      </template>

      <template v-else-if="viewState === 'error'">
        <p class="feishu-binding__error">
          {{ error }}
        </p>
        <a
          v-if="needsLogin"
          class="feishu-binding__primary feishu-binding__login"
          href="/api/auth/login"
        >Sign in with Feishu</a>
        <button
          v-else
          type="button"
          class="feishu-binding__secondary"
          :disabled="isLoading"
          @click="refreshBinding"
        >
          {{ isLoading ? 'Retrying…' : 'Retry' }}
        </button>
      </template>

      <template v-else>
        <p class="feishu-binding__guidance">
          Generate a one-time code, then send it to the configured Bot from your own Feishu account.
        </p>
        <button
          type="button"
          class="feishu-binding__primary"
          :disabled="isMutating || isLoading"
          @click="generate"
        >
          {{ isMutating ? 'Generating…' : 'Generate binding code' }}
        </button>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, toRef } from 'vue'
import { useFeishuBinding } from '@/composables/useFeishuBinding'
import { writeClipboard } from '@/utils/clipboard'

const props = defineProps<{
  tabId: string
}>()

const rootEl = ref<HTMLElement | null>(null)
const closeButtonEl = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const copied = ref(false)
const copyError = ref<string | null>(null)
const confirmDisconnect = ref(false)
const panelId = `feishu-binding-${props.tabId}`

const {
  binding,
  pendingCode,
  error,
  needsLogin,
  isHydrated,
  isLoading,
  isMutating,
  viewState,
  refresh,
  generateCode,
  disconnect,
  resume,
  pause,
} = useFeishuBinding(toRef(props, 'tabId'))

const statusText = computed(() => {
  if (isLoading.value && !isHydrated.value) return 'Checking connection…'
  switch (viewState.value) {
    case 'pending':
      return 'Waiting for Feishu confirmation'
    case 'bound-current':
      return 'Connected to this Chat'
    case 'bound-other':
      return 'Connected to another Chat'
    case 'error':
      return 'Connection unavailable'
    default:
      return 'Not connected'
  }
})

function formatExpiry(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'soon'
  return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatConnectedAt(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'Unknown'
  return parsed.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

async function togglePanel() {
  open.value = !open.value
  confirmDisconnect.value = false
  if (!open.value) return
  await nextTick()
  closeButtonEl.value?.focus()
  if (!isHydrated.value || error.value) await refreshBinding()
}

function closePanel() {
  open.value = false
  confirmDisconnect.value = false
}

async function refreshBinding() {
  await refresh()
}

async function generate() {
  copied.value = false
  copyError.value = null
  await generateCode()
}

async function copyCode() {
  if (!pendingCode.value) return
  copyError.value = null
  try {
    await writeClipboard(pendingCode.value.code)
    copied.value = true
  } catch {
    copyError.value = 'Could not copy the binding code. Select and copy it manually.'
  }
}

async function confirmDisconnectBinding() {
  if (await disconnect()) confirmDisconnect.value = false
}

function handleOutsidePointer(event: PointerEvent) {
  if (open.value && !rootEl.value?.contains(event.target as Node)) closePanel()
}

onMounted(() => {
  document.addEventListener('pointerdown', handleOutsidePointer)
})

onActivated(() => {
  resume()
  void refresh()
})

onDeactivated(pause)

onUnmounted(() => {
  pause()
  document.removeEventListener('pointerdown', handleOutsidePointer)
})
</script>

<style scoped>
.feishu-binding {
  position: relative;
  z-index: 12;
  display: flex;
  min-height: 34px;
  padding: 6px 12px;
  align-items: center;
  flex: none;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-app-bg);
}

.feishu-binding__trigger {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 26px;
  padding: 3px 9px 3px 5px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  color: var(--ch-color-text-muted);
  background: var(--ch-color-surface);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.feishu-binding__trigger:hover,
.feishu-binding__trigger:focus-visible {
  color: var(--ch-color-text);
  border-color: var(--ch-color-accent);
  outline: none;
}

.feishu-binding__mark {
  display: inline-grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  color: #fff;
  background: #3370ff;
  font-size: 10px;
  font-weight: 700;
}

.feishu-binding__dot,
.feishu-binding__status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}

.feishu-binding__trigger.is-bound-current .feishu-binding__dot,
.feishu-binding__status[data-state='bound-current'] .feishu-binding__status-dot {
  background: var(--ch-color-success, #2eaa60);
}

.feishu-binding__trigger.is-pending .feishu-binding__dot,
.feishu-binding__status[data-state='pending'] .feishu-binding__status-dot {
  background: var(--ch-color-warning, #d99a00);
}

.feishu-binding__trigger.is-error .feishu-binding__dot,
.feishu-binding__status[data-state='error'] .feishu-binding__status-dot {
  background: var(--ch-color-danger, #d94848);
}

.feishu-binding__panel {
  position: absolute;
  top: calc(100% + 6px);
  left: 12px;
  z-index: 20;
  width: min(380px, calc(100vw - 40px));
  padding: 16px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  color: var(--ch-color-text);
  background: var(--ch-color-surface-elevated, var(--ch-color-surface));
  box-shadow: var(--ch-shadow-dialog, 0 16px 42px rgb(0 0 0 / 28%));
}

.feishu-binding__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.feishu-binding__header strong {
  font-size: 14px;
}

.feishu-binding__header p,
.feishu-binding__guidance,
.feishu-binding__meta,
.feishu-binding__error {
  margin: 4px 0 0;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.feishu-binding__icon-button {
  display: inline-grid;
  width: 26px;
  height: 26px;
  padding: 0;
  place-items: center;
  border: 0;
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-muted);
  background: transparent;
  font-size: 19px;
  cursor: pointer;
}

.feishu-binding__icon-button:hover,
.feishu-binding__icon-button:focus-visible {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-hover);
  outline: none;
}

.feishu-binding__status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0;
  padding: 9px 10px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 600;
}

.feishu-binding__loading {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.feishu-binding__code-row {
  display: flex;
  align-items: stretch;
  gap: 8px;
  margin-top: 12px;
}

.feishu-binding__code {
  flex: 1;
  padding: 9px 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text-strong);
  background: var(--ch-color-app-bg);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-align: center;
  user-select: all;
}

.feishu-binding__details {
  display: grid;
  gap: 8px;
  margin: 12px 0;
}

.feishu-binding__details div {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 8px;
}

.feishu-binding__details dt {
  color: var(--ch-color-text-subtle);
  font-size: 11px;
}

.feishu-binding__details dd {
  margin: 0;
  overflow: hidden;
  color: var(--ch-color-text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.feishu-binding__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.feishu-binding__primary,
.feishu-binding__secondary,
.feishu-binding__danger,
.feishu-binding__danger-link {
  min-height: 30px;
  padding: 5px 11px;
  border-radius: var(--ch-radius-sm);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.feishu-binding__primary {
  margin-top: 12px;
  border: 1px solid var(--ch-color-accent);
  color: var(--ch-color-on-accent, #fff);
  background: var(--ch-color-accent);
}

.feishu-binding__secondary {
  border: 1px solid var(--ch-color-border);
  color: var(--ch-color-text);
  background: var(--ch-color-surface);
}

.feishu-binding__danger {
  border: 1px solid var(--ch-color-danger, #d94848);
  color: #fff;
  background: var(--ch-color-danger, #d94848);
}

.feishu-binding__danger-link {
  margin-top: 8px;
  padding-left: 0;
  border: 0;
  color: var(--ch-color-danger, #d94848);
  background: transparent;
}

.feishu-binding__primary:disabled,
.feishu-binding__secondary:disabled,
.feishu-binding__danger:disabled,
.feishu-binding__danger-link:disabled {
  cursor: wait;
  opacity: 0.65;
}

.feishu-binding__confirm {
  margin-top: 12px;
  padding: 10px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text-muted);
  background: var(--ch-color-surface);
  font-size: 12px;
}

.feishu-binding__error {
  color: var(--ch-color-danger, #d94848);
}

.feishu-binding__login {
  display: inline-flex;
  align-items: center;
  text-decoration: none;
}

@media (max-width: 640px) {
  .feishu-binding__panel {
    left: 8px;
    width: calc(100vw - 24px);
  }
}
</style>
