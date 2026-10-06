<template>
  <dialog
    ref="dialog"
    class="feishu-config-dialog"
    aria-labelledby="feishu-config-title"
    data-testid="feishu-bot-settings-dialog"
    @cancel.prevent="close"
    @click="onBackdrop"
  >
    <header class="feishu-config-header">
      <div>
        <h2 id="feishu-config-title">
          Instance Feishu Bot
        </h2>
        <p>Applies to this Claude Hub instance and does not connect the current Chat.</p>
      </div>
      <button
        type="button"
        aria-label="Close Feishu Bot settings"
        @click="close"
      >
        ×
      </button>
    </header>

    <p
      v-if="loading"
      role="status"
    >
      Loading Bot configuration…
    </p>
    <p
      v-if="error"
      class="feishu-config-error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="success"
      class="feishu-config-success"
      role="status"
    >
      {{ success }}
    </p>

    <p
      v-if="clipboardError"
      class="feishu-config-error"
      role="alert"
    >
      {{ clipboardError }}
    </p>

    <template v-if="status && !loading">
      <section
        class="feishu-config-status"
        aria-label="Feishu Bot status"
      >
        <div class="feishu-config-status__heading">
          <strong>{{ status.configured ? 'Configured' : 'Not configured' }}</strong>
          <span v-if="!status.editable">Read-only</span>
        </div>
        <span>Source: {{ sourceLabel }}</span>
        <div class="feishu-config-webhook">
          <span>Webhook:</span>
          <code v-if="status.event_url">{{ status.event_url }}</code>
          <span v-else>Requires an instance public URL.</span>
          <button
            v-if="status.event_url"
            type="button"
            class="compact"
            @click="copyEventUrl"
          >
            {{ copiedEventUrl ? 'Copied' : 'Copy' }}
          </button>
        </div>
        <small>Configured means credentials exist or the latest save was validated. It does not verify webhook delivery, Feishu event permissions, or a real Agent round trip.</small>
      </section>

      <section
        v-if="!status.can_manage"
        class="feishu-config-notice"
      >
        <p v-if="status.configured">
          The instance Bot is configured. Connect an individual Chat from its Feishu panel.
        </p>
        <p v-else>
          Feishu Bot needs administrator configuration. Contact an administrator.
        </p>
      </section>

      <template v-else>
        <section
          v-if="adminConfig"
          class="feishu-config-details"
        >
          <h3>Safe configuration details</h3>
          <dl>
            <div><dt>App ID</dt><dd>{{ adminConfig.app_id || 'Not configured' }}</dd></div>
            <div><dt>App Secret</dt><dd>{{ configuredLabel(adminConfig.app_secret_configured) }}</dd></div>
            <div><dt>Verification Token</dt><dd>{{ configuredLabel(adminConfig.verification_token_configured) }}</dd></div>
            <div><dt>Encrypt Key</dt><dd>{{ configuredLabel(adminConfig.encrypt_key_configured) }}</dd></div>
          </dl>
        </section>

        <p
          v-if="status.source === 'environment'"
          class="feishu-config-notice"
        >
          Managed by environment variables (read-only). Change the deployment environment to update or disable this Bot.
        </p>
        <p
          v-else-if="status.source === 'invalid_environment'"
          class="feishu-config-error"
          role="alert"
        >
          The environment configuration is invalid and read-only. Fix the deployment environment before using the Bot.
        </p>

        <form
          v-if="status.editable"
          class="feishu-config-form"
          @submit.prevent="submitConfig(false)"
        >
          <p>Enter all four values for every save. Existing secrets cannot be read or reused from this form.</p>
          <label>
            <span>App ID</span>
            <input
              v-model="appId"
              required
              autocomplete="off"
              spellcheck="false"
            >
          </label>
          <label>
            <span>App Secret</span>
            <input
              v-model="appSecret"
              required
              type="password"
              autocomplete="new-password"
              spellcheck="false"
            >
            <small>{{ configuredHint(adminConfig?.app_secret_configured) }}</small>
          </label>
          <label>
            <span>Verification Token</span>
            <input
              v-model="verificationToken"
              required
              type="password"
              autocomplete="new-password"
              spellcheck="false"
            >
            <small>{{ configuredHint(adminConfig?.verification_token_configured) }}</small>
          </label>
          <label>
            <span>Encrypt Key</span>
            <input
              v-model="encryptKey"
              required
              type="password"
              autocomplete="new-password"
              spellcheck="false"
            >
            <small>{{ configuredHint(adminConfig?.encrypt_key_configured) }}</small>
          </label>
          <p class="feishu-config-note">
            Rotating credentials for the same App ID keeps existing Chat bindings. Callbacks signed with old credentials stop validating after the change.
          </p>
          <div class="feishu-config-actions">
            <button
              type="submit"
              class="primary"
              :disabled="busy || !formComplete"
            >
              {{ busy ? 'Validating…' : 'Validate and save' }}
            </button>
          </div>
        </form>

        <section
          v-if="confirmAppIdChange"
          class="feishu-config-confirm"
          role="alertdialog"
          aria-label="Confirm App ID change"
        >
          <p>Changing the App ID revokes existing bindings and binding codes. Chat history and message source labels remain. Already-sent network requests cannot be recalled.</p>
          <div class="feishu-config-actions">
            <button
              type="button"
              class="danger"
              :disabled="busy"
              @click="submitConfig(true)"
            >
              Confirm App ID change
            </button>
            <button
              type="button"
              :disabled="busy"
              @click="confirmAppIdChange = false"
            >
              Cancel
            </button>
          </div>
        </section>

        <section
          v-if="status.configured && status.editable"
          class="feishu-config-danger-zone"
        >
          <button
            v-if="!confirmDeactivate"
            type="button"
            class="danger"
            :disabled="busy"
            @click="confirmDeactivate = true"
          >
            Deactivate Bot
          </button>
          <div
            v-else
            class="feishu-config-confirm"
            role="alertdialog"
            aria-label="Confirm Bot deactivation"
          >
            <p>Deactivating stops new Bot deliveries and revokes bindings and binding codes. Chat history and message source labels remain. Already-sent network requests cannot be recalled.</p>
            <div class="feishu-config-actions">
              <button
                type="button"
                class="danger"
                :disabled="busy"
                @click="deactivate"
              >
                Confirm deactivation
              </button>
              <button
                type="button"
                :disabled="busy"
                @click="confirmDeactivate = false"
              >
                Cancel
              </button>
            </div>
          </div>
        </section>
      </template>
    </template>

    <p
      v-if="busy"
      class="feishu-config-notice"
      role="status"
    >
      Closing this dialog does not cancel an operation already submitted to the server. Reopen Bot settings to check the latest result.
    </p>

    <footer class="feishu-config-footer">
      <button
        v-if="error && !busy"
        type="button"
        @click="refresh"
      >
        Reload configuration
      </button>
      <button
        type="button"
        @click="close"
      >
        Close
      </button>
    </footer>
  </dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  FeishuBotConfigRequestError,
  deactivateFeishuBotConfiguration,
  loadFeishuBotConfiguration,
  saveFeishuBotConfiguration,
  type FeishuBotAdminConfig,
  type FeishuBotConfigStatus,
} from '@/utils/feishuBotConfig'
import { writeClipboard } from '@/utils/clipboard'

const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const status = ref<FeishuBotConfigStatus | null>(null)
const adminConfig = ref<FeishuBotAdminConfig | null>(null)
const loading = ref(true)
const busy = ref(false)
const error = ref<string | null>(null)
const success = ref<string | null>(null)
const clipboardError = ref<string | null>(null)
const appId = ref('')
const appSecret = ref('')
const verificationToken = ref('')
const encryptKey = ref('')
const copiedEventUrl = ref(false)
const confirmAppIdChange = ref(false)
const confirmDeactivate = ref(false)
let controller: AbortController | null = null
let disposed = false

const sourceLabel = computed(() => ({
  environment: 'Environment variables',
  stored: 'Saved in Claude Hub',
  none: 'None',
  invalid_environment: 'Invalid environment variables',
}[status.value?.source ?? 'none']))
const formComplete = computed(() => Boolean(
  appId.value.trim() && appSecret.value.trim() &&
  verificationToken.value.trim() && encryptKey.value.trim(),
))

function configuredLabel(value: boolean): string {
  return value ? 'Configured' : 'Not configured'
}
function configuredHint(value: boolean | undefined): string {
  return value ? 'Configured; enter the full value to replace it.' : 'Not configured.'
}
function clearSecrets() {
  appSecret.value = ''
  verificationToken.value = ''
  encryptKey.value = ''
}
function beginRequest(): AbortController {
  controller?.abort()
  controller = new AbortController()
  return controller
}
function applyAdminConfig(value: FeishuBotAdminConfig) {
  adminConfig.value = value
  status.value = value
  appId.value = value.app_id ?? ''
}
function showRequestError(cause: unknown) {
  if (cause instanceof Error && cause.name === 'AbortError') return
  error.value = cause instanceof FeishuBotConfigRequestError
    ? cause.message
    : 'The Bot configuration request failed. Try again.'
}

function shouldReloadAfterMutation(cause: unknown): cause is FeishuBotConfigRequestError {
  return cause instanceof FeishuBotConfigRequestError && (
    cause.code === 'config_revision_conflict' ||
    cause.code === 'config_read_only' ||
    cause.status === 500 ||
    (cause.status === 503 && cause.code === 'public_url_invalid')
  )
}

async function refresh() {
  loading.value = true
  error.value = null
  success.value = null
  clipboardError.value = null
  copiedEventUrl.value = false
  confirmAppIdChange.value = false
  confirmDeactivate.value = false
  clearSecrets()
  const request = beginRequest()
  try {
    const result = await loadFeishuBotConfiguration(request.signal)
    if (disposed || request.signal.aborted) return
    status.value = result.status
    adminConfig.value = result.adminConfig
    appId.value = result.adminConfig?.app_id ?? ''
  } catch (cause) {
    if (disposed || request.signal.aborted) return
    status.value = null
    adminConfig.value = null
    appId.value = ''
    showRequestError(cause)
  } finally {
    if (!disposed && controller === request) loading.value = false
  }
}

async function reloadAfterConflict(message: string) {
  clearSecrets()
  await refresh()
  if (!disposed && status.value && !error.value) error.value = message
}

async function submitConfig(allowAppIdChange: boolean) {
  if (!status.value?.editable || status.value.revision === null || !formComplete.value || busy.value) return
  busy.value = true
  error.value = null
  success.value = null
  const request = beginRequest()
  try {
    const next = await saveFeishuBotConfiguration({
      app_id: appId.value.trim(),
      app_secret: appSecret.value,
      verification_token: verificationToken.value,
      encrypt_key: encryptKey.value,
      expected_revision: status.value.revision,
    }, allowAppIdChange, request.signal)
    if (disposed || request.signal.aborted) return
    applyAdminConfig(next)
    clearSecrets()
    confirmAppIdChange.value = false
    success.value = 'Configuration validated and saved. Configure the event callback in Feishu before testing real messages.'
  } catch (cause) {
    if (disposed || request.signal.aborted) return
    if (cause instanceof FeishuBotConfigRequestError && cause.code === 'app_id_change_confirmation_required') {
      confirmAppIdChange.value = true
    } else if (
      shouldReloadAfterMutation(cause)
    ) {
      await reloadAfterConflict(cause.message)
    } else {
      showRequestError(cause)
    }
  } finally {
    if (!disposed) busy.value = false
  }
}

async function deactivate() {
  if (!status.value?.editable || status.value.revision === null || busy.value) return
  busy.value = true
  error.value = null
  success.value = null
  const request = beginRequest()
  try {
    const next = await deactivateFeishuBotConfiguration(status.value.revision, request.signal)
    if (disposed || request.signal.aborted) return
    applyAdminConfig(next)
    clearSecrets()
    confirmDeactivate.value = false
    confirmAppIdChange.value = false
    success.value = 'Feishu Bot deactivated. Existing Chat history was preserved.'
  } catch (cause) {
    if (disposed || request.signal.aborted) return
    if (
      shouldReloadAfterMutation(cause)
    ) {
      await reloadAfterConflict(cause.message)
    } else {
      showRequestError(cause)
    }
  } finally {
    if (!disposed) busy.value = false
  }
}

async function copyEventUrl() {
  if (!status.value?.event_url) return
  clipboardError.value = null
  try {
    await writeClipboard(status.value.event_url)
    copiedEventUrl.value = true
  } catch {
    clipboardError.value = 'Could not copy the webhook URL. Select and copy it manually.'
  }
}
function close() {
  clearSecrets()
  controller?.abort()
  emit('close')
}
function onBackdrop(event: MouseEvent) {
  if (!dialog.value || event.target !== dialog.value) return
  const bounds = dialog.value.getBoundingClientRect()
  if (
    event.clientX < bounds.left || event.clientX > bounds.right ||
    event.clientY < bounds.top || event.clientY > bounds.bottom
  ) close()
}

onMounted(() => {
  dialog.value?.showModal()
  void refresh()
})
onUnmounted(() => {
  disposed = true
  clearSecrets()
  controller?.abort()
  dialog.value?.close()
})
</script>

<style scoped>
.feishu-config-dialog {
  width: min(620px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  box-sizing: border-box;
  margin: auto;
  padding: 22px;
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-popover);
}
.feishu-config-dialog::backdrop { background: rgb(0 0 0 / 55%); }
.feishu-config-header, .feishu-config-status__heading, .feishu-config-webhook {
  display: flex;
  align-items: center;
  gap: 8px;
}
.feishu-config-header { align-items: flex-start; justify-content: space-between; gap: 16px; }
h2, h3, p { margin-top: 0; }
h2 { margin-bottom: 6px; font-size: 19px; }
h3 { font-size: 14px; }
p { font-size: 13px; line-height: 1.5; }
.feishu-config-header p, .feishu-config-note, small { color: var(--ch-color-text-muted); }
.feishu-config-status, .feishu-config-notice, .feishu-config-details,
.feishu-config-form, .feishu-config-confirm, .feishu-config-danger-zone {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
}
.feishu-config-status { display: grid; gap: 6px; }
.feishu-config-webhook { flex-wrap: wrap; }
.feishu-config-webhook code { overflow-wrap: anywhere; }
dl { display: grid; gap: 6px; margin: 0; }
dl div { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 8px; }
dt { color: var(--ch-color-text-muted); }
dd { margin: 0; overflow-wrap: anywhere; }
.feishu-config-form { display: grid; gap: 12px; }
label { display: grid; gap: 5px; font-size: 13px; }
input {
  min-width: 0;
  min-height: 38px;
  padding: 7px 9px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font: inherit;
}
.feishu-config-error { color: var(--ch-color-danger); }
.feishu-config-success { color: var(--ch-color-success); }
.feishu-config-actions, .feishu-config-footer {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.feishu-config-footer { margin-top: 18px; }
button {
  min-height: 38px;
  padding: 7px 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font: inherit;
  cursor: pointer;
}
button.compact { min-height: 28px; padding: 3px 8px; font-size: 12px; }
button:disabled { cursor: not-allowed; opacity: 0.55; }
button:focus-visible, input:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: 2px; }
button.primary { border-color: var(--ch-color-accent); background: var(--ch-color-accent); color: #fff; }
button.danger { color: var(--ch-color-danger); }
@media (max-width: 640px) {
  .feishu-config-dialog { width: calc(100vw - 16px); max-height: calc(100dvh - 16px); padding: 16px; }
  dl div { grid-template-columns: 1fr; gap: 2px; }
  button { min-height: 44px; }
}
</style>
