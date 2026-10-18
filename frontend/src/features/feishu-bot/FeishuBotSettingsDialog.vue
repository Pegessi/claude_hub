<template>
  <dialog
    ref="dialog"
    class="bot-pool"
    data-testid="feishu-bot-settings-dialog"
    aria-labelledby="bot-pool-title"
    @cancel.prevent="close"
  >
    <header class="bot-pool__header">
      <div class="bot-pool__heading">
        <div
          class="bot-pool__mark"
          aria-hidden="true"
        >
          飞
        </div>
        <div>
          <h2 id="bot-pool-title">
            Feishu Bots
          </h2>
          <p>Manage the shared Bot pool for this Hub instance.</p>
        </div>
      </div>
      <button
        type="button"
        class="ch-btn ch-btn--icon ch-btn--ghost bot-pool__close"
        aria-label="Close Feishu Bot settings"
        @click="close"
      >
        <svg
          viewBox="0 0 16 16"
          aria-hidden="true"
        ><path d="m4 4 8 8m0-8-8 8" /></svg>
      </button>
    </header>

    <div
      v-if="store.loading || busy || error || store.error || success || store.deprecatedEnv.length"
      class="bot-pool__notifications"
    >
      <p
        v-if="store.loading"
        class="notification"
        role="status"
      >
        <span
          class="notification__spinner"
          aria-hidden="true"
        />
        Loading Bot pool…
      </p>
      <p
        v-if="busy"
        class="notification"
        role="status"
      >
        A change is being saved. Closing this dialog will not cancel it.
      </p>
      <p
        v-if="error || store.error"
        class="notification notification--error"
        role="alert"
      >
        {{ error || store.error }}
      </p>
      <p
        v-if="success"
        class="notification notification--success"
        role="status"
      >
        {{ success }}
      </p>
      <p
        v-if="store.deprecatedEnv.length"
        class="notification notification--warning"
      >
        Deprecated environment entries: {{ store.deprecatedEnv.join(', ') }}. They are ignored in WebSocket mode and can be removed.
      </p>
    </div>

    <section
      v-if="store.pool"
      class="layout"
    >
      <aside class="bot-sidebar">
        <div class="bot-sidebar__title">
          <span>Bots</span>
          <span class="bot-sidebar__count">{{ store.bots.length }}</span>
        </div>
        <div
          class="list"
          aria-label="Bots"
        >
          <button
            v-for="bot in store.bots"
            :key="bot.bot_id"
            :disabled="busy"
            type="button"
            class="bot-row"
            :class="{ selected: bot.bot_id === selectedId }"
            :aria-pressed="bot.bot_id === selectedId"
            @click="select(bot)"
          >
            <span
              class="bot-row__mark"
              aria-hidden="true"
            >飞</span>
            <span class="bot-row__content">
              <span class="bot-row__name">
                <strong>{{ bot.name }}</strong>
                <span
                  class="bot-row__status"
                  :class="connectionClass(bot)"
                  aria-hidden="true"
                />
              </span>
              <span class="bot-row__meta">{{ occupancy(bot) }}</span>
            </span>
            <svg
              class="bot-row__chevron"
              viewBox="0 0 16 16"
              aria-hidden="true"
            ><path d="m6 3 5 5-5 5" /></svg>
          </button>
          <div
            v-if="!store.bots.length"
            class="bot-sidebar__empty"
          >
            No Bots yet
          </div>
        </div>
        <div class="bot-sidebar__hint">
          One Bot can connect to one Chat at a time.
        </div>
      </aside>

      <section
        v-if="selected"
        class="detail"
      >
        <div class="detail-header">
          <div>
            <div class="detail-title-row">
              <h3>{{ selected.name }}</h3>
              <span
                class="status-chip"
                :class="selected.enabled ? 'is-enabled' : 'is-disabled'"
              >{{ selected.enabled ? 'Enabled' : 'Disabled' }}</span>
              <span class="source-chip">{{ selected.source }}</span>
            </div>
            <code class="app-id">{{ selected.app_id }}</code>
          </div>
          <span
            v-if="selected.binding"
            class="occupancy-chip"
          >Connected</span>
        </div>

        <div
          class="connection-card"
          :data-state="selected.connection_status"
        >
          <span
            class="connection-card__dot"
            aria-hidden="true"
          />
          <div>
            <span class="section-eyebrow">WebSocket long connection</span>
            <strong>{{ connectionText(selected) }}</strong>
            <small>Events arrive directly from Feishu; no public callback URL is required.</small>
          </div>
        </div>
        <p
          v-if="selected.binding"
          class="binding-card"
        >
          <span
            class="binding-card__dot"
            aria-hidden="true"
          />
          <span>In use by Chat <code>{{ selected.binding.tab_id }}</code><span v-if="selected.binding.owner_kind === 'local'"> · Trusted local operator</span></span>
        </p>

        <form
          v-if="store.poolEditable"
          class="settings-section"
          @submit.prevent="saveMetadata"
        >
          <div class="section-heading">
            <div>
              <h4>Bot details</h4>
              <p>Update the display name and availability.</p>
            </div>
          </div>
          <label class="field">
            <span class="field-label">Name</span>
            <input
              v-model="editName"
              class="ch-input"
              :disabled="busy"
              required
            >
          </label>
          <label class="toggle-row">
            <span>
              <strong>Enabled</strong>
              <small>Keep the long connection active and allow new Chat pairings.</small>
            </span>
            <input
              v-model="editEnabled"
              :disabled="busy"
              type="checkbox"
            >
          </label>
          <p
            v-if="selected.enabled && !editEnabled"
            class="inline-notice inline-notice--warning"
          >
            Disabling releases this Bot's Chat and invalidates pending pairings. Save details to confirm.
          </p>
          <div class="section-actions">
            <span>Revision {{ selectedRevision }}</span>
            <button
              class="ch-btn ch-btn--primary"
              :disabled="busy"
            >
              {{ busy ? 'Saving…' : 'Save details' }}
            </button>
          </div>
        </form>

        <form
          v-if="selected.credentials_editable"
          class="settings-section"
          @submit.prevent="saveSecrets"
        >
          <div class="section-heading">
            <div>
              <h4>Replace credentials</h4>
              <p>App ID is immutable. Existing active pairing is preserved.</p>
            </div>
            <div
              class="credential-state"
              aria-label="Credential configuration"
            >
              <span :class="{ configured: selected.app_secret_configured }">Secret {{ selected.app_secret_configured ? 'set' : 'missing' }}</span>
            </div>
          </div>
          <div class="secret-grid secret-grid--single">
            <label class="field">
              <span class="field-label">App Secret</span>
              <input
                v-model="appSecret"
                class="ch-input"
                :disabled="busy"
                type="password"
                required
                autocomplete="new-password"
              >
            </label>
          </div>
          <div class="section-actions section-actions--end">
            <button
              class="ch-btn"
              :disabled="busy || !secretComplete"
            >
              Validate and replace
            </button>
          </div>
        </form>

        <section
          v-if="selected.deletable"
          class="danger-zone"
        >
          <div>
            <h4>Delete Bot</h4>
            <p v-if="!confirmDelete">
              Remove stored credentials and release its Chat.
            </p>
            <p v-else>
              Deleting stops this Bot, releases its Chat, and invalidates pending pairing attempts. Historical source labels remain.
            </p>
          </div>
          <button
            v-if="!confirmDelete"
            type="button"
            class="ch-btn ch-btn--danger"
            :disabled="busy"
            @click="confirmDelete = true"
          >
            Delete Bot
          </button>
          <template v-else>
            <div class="danger-zone__actions">
              <button
                type="button"
                class="ch-btn ch-btn--danger"
                :disabled="busy"
                @click="removeBot"
              >
                Confirm delete
              </button>
              <button
                type="button"
                class="ch-btn"
                :disabled="busy"
                @click="confirmDelete = false"
              >
                Cancel
              </button>
            </div>
          </template>
        </section>
      </section>

      <form
        v-else-if="store.poolEditable"
        class="detail create-view"
        @submit.prevent="createBot"
      >
        <div
          v-if="!store.bots.length"
          class="empty-state"
        >
          <div
            class="empty-state__icon"
            aria-hidden="true"
          >
            飞
          </div>
          <div>
            <strong>No Bots configured</strong>
            <p>Add a Feishu custom Bot to connect private messages with a Hub Chat.</p>
          </div>
        </div>
        <div class="create-heading">
          <span class="section-eyebrow">{{ store.bots.length ? 'New Bot' : 'Get started' }}</span>
          <h3>Add Bot</h3>
          <p>App ID and App Secret open a WebSocket long connection to Feishu. No callback token or encryption key is needed.</p>
        </div>
        <div class="create-grid">
          <label class="field field--wide">
            <span class="field-label">Name</span>
            <input
              v-model="create.name"
              class="ch-input"
              :disabled="busy"
              required
              placeholder="e.g. Team assistant"
            >
          </label>
          <label class="field">
            <span class="field-label">App ID</span>
            <input
              v-model="create.app_id"
              class="ch-input"
              :disabled="busy"
              required
              autocomplete="off"
              placeholder="cli_…"
            >
          </label>
          <label class="field">
            <span class="field-label">App Secret</span>
            <input
              v-model="create.app_secret"
              class="ch-input"
              :disabled="busy"
              type="password"
              required
              autocomplete="new-password"
            >
          </label>
        </div>
        <div class="create-actions">
          <span>App ID cannot be changed after creation.</span>
          <button
            class="ch-btn ch-btn--primary"
            :disabled="busy || !createComplete"
          >
            Validate and add
          </button>
        </div>
      </form>
    </section>

    <footer class="bot-pool__footer">
      <div>
        <button
          v-if="store.poolEditable"
          type="button"
          class="ch-btn"
          :disabled="busy"
          @click="beginCreate"
        >
          <svg
            class="button-icon"
            viewBox="0 0 16 16"
            aria-hidden="true"
          ><path d="M8 3v10M3 8h10" /></svg>
          Add Bot
        </button>
      </div>
      <div class="bot-pool__footer-actions">
        <button
          type="button"
          class="ch-btn ch-btn--ghost"
          :disabled="busy"
          @click="refresh"
        >
          Reload
        </button>
        <button
          type="button"
          class="ch-btn"
          @click="close"
        >
          Close
        </button>
      </div>
    </footer>
  </dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useFeishuBotPoolStore } from '@/stores/feishuBotPoolStore'
import {
  createFeishuBot, deleteFeishuBot, FeishuBotRequestError,
  replaceFeishuBotSecrets, updateFeishuBot,
  type FeishuBotPoolResponse, type FeishuBotSummary,
} from '@/utils/feishuBotConfig'

const emit = defineEmits<{ close: [] }>()
const store = useFeishuBotPoolStore()
const dialog = ref<HTMLDialogElement | null>(null)
const selectedId = ref<string | null>(null)
const selectedRevision = ref<number | null>(null)
const busy = ref(false), error = ref<string | null>(null), success = ref<string | null>(null)
const confirmDelete = ref(false), editName = ref(''), editEnabled = ref(true)
const appSecret = ref('')
const create = reactive({ name: '', app_id: '', app_secret: '' })
const selected = computed(() => store.botById(selectedId.value))
const secretComplete = computed(() => !!appSecret.value)
const createComplete = computed(() => Object.values(create).every(Boolean))
const CONNECTION_REFRESH_MS = 2_000
let viewEpoch = 0
let closed = false
let connectionRefreshTimer: ReturnType<typeof setTimeout> | null = null
let connectionRefreshController: AbortController | null = null

function awaitingConnectionUpdate(): boolean {
  return store.bots.some(bot => bot.enabled && bot.configured
    && (bot.connection_status === 'connecting' || bot.connection_status === 'failed'))
}
function stopConnectionRefresh() {
  if (connectionRefreshTimer !== null) {
    clearTimeout(connectionRefreshTimer)
    connectionRefreshTimer = null
  }
  connectionRefreshController?.abort()
  connectionRefreshController = null
}
function queueConnectionRefresh() {
  connectionRefreshTimer = null
  void refreshConnectionStatuses()
}
function syncConnectionRefresh() {
  if (closed || !awaitingConnectionUpdate()) {
    stopConnectionRefresh()
    return
  }
  if (connectionRefreshTimer === null && connectionRefreshController === null) {
    connectionRefreshTimer = setTimeout(queueConnectionRefresh, CONNECTION_REFRESH_MS)
  }
}
async function refreshConnectionStatuses() {
  if (closed || busy.value || store.loading || !awaitingConnectionUpdate()) {
    syncConnectionRefresh()
    return
  }
  const controller = new AbortController()
  connectionRefreshController = controller
  try {
    await store.refresh(controller.signal)
  } finally {
    if (connectionRefreshController === controller) connectionRefreshController = null
    syncConnectionRefresh()
  }
}

watch(
  () => store.bots.map(bot => [
    bot.bot_id, bot.enabled, bot.configured, bot.connection_status,
  ].join(':')).join('|'),
  syncConnectionRefresh,
  { immediate: true },
)

function occupancy(bot: FeishuBotSummary) {
  if (bot.binding) return `In use by Chat ${bot.binding.tab_id}`
  if (!bot.enabled) return 'Disabled'
  if (!bot.configured) return 'Needs configuration'
  if (bot.connection_status === 'failed') return 'Connection failed'
  if (bot.connection_status === 'connecting') return 'Connecting'
  return bot.connection_status === 'connected' ? 'Available' : 'Offline'
}
function connectionClass(bot: FeishuBotSummary) {
  return {
    'is-disabled': !bot.enabled || !bot.configured || bot.connection_status === 'stopped',
    'is-connecting': bot.connection_status === 'connecting',
    'is-failed': bot.connection_status === 'failed',
  }
}
function connectionText(bot: FeishuBotSummary) {
  if (!bot.enabled) return 'Stopped while this Bot is disabled'
  if (!bot.configured) return 'Credentials are incomplete'
  return ({ connected: 'Connected to Feishu', connecting: 'Connecting to Feishu', failed: 'Connection failed; Hub will retry', stopped: 'Connection stopped' } as const)[bot.connection_status]
}
function clearSecrets() { appSecret.value = '' }
function clearCreate() { Object.assign(create, { name: '', app_id: '', app_secret: '' }) }
function fillSelection(bot: FeishuBotSummary | null, clear = true) {
  selectedId.value = bot?.bot_id ?? null
  selectedRevision.value = bot?.revision ?? null
  editName.value = bot?.name ?? ''
  editEnabled.value = bot?.enabled ?? true
  confirmDelete.value = false
  if (clear) clearSecrets()
}
function select(bot: FeishuBotSummary) {
  if (busy.value || closed) return
  viewEpoch += 1
  fillSelection(bot)
  error.value = null; success.value = null
}
function beginCreate() {
  if (busy.value || closed) return
  viewEpoch += 1
  fillSelection(null)
  clearCreate()
  error.value = null; success.value = null
}
async function refresh() {
  if (busy.value || closed) return
  const current = ++viewEpoch
  error.value = null
  const value = await store.refresh()
  if (closed || current !== viewEpoch || !value) return
  if (selectedId.value) fillSelection(store.botById(selectedId.value))
}
function shouldReconcile(cause: unknown): boolean {
  if (!(cause instanceof FeishuBotRequestError)) return true
  if (cause.code === 'bot_operation_busy') return false
  return cause.status === 500 || cause.code === 'bot_revision_conflict' || cause.code === 'bot_already_bound'
    || cause.code === 'chat_already_bound'
}
async function mutate(
  run: () => Promise<FeishuBotPoolResponse>, message: string,
  onSuccess: () => void = () => undefined,
): Promise<boolean> {
  if (busy.value || closed) return false
  const current = ++viewEpoch
  busy.value = true; error.value = null; success.value = null
  store.invalidateRefresh()
  try {
    const next = await run()
    // Closing a dialog does not undo a successful server mutation.
    store.applyPool(next)
    if (closed || current !== viewEpoch) return false
    onSuccess()
    clearSecrets()
    fillSelection(store.botById(next.focus_bot_id ?? selectedId.value))
    success.value = message
    return true
  } catch (cause) {
    const requestError = cause instanceof FeishuBotRequestError ? cause : null
    const reconcile = shouldReconcile(cause)
    if (reconcile) await store.refresh()
    if (closed || current !== viewEpoch) return false
    if (reconcile && selectedId.value) {
      fillSelection(store.botById(selectedId.value), requestError?.code === 'bot_revision_conflict')
    }
    // Refresh must not erase the explanation for the failed mutation.
    error.value = requestError?.message ?? 'The Bot operation failed. Check the refreshed pool before trying again.'
    return false
  } finally {
    if (current === viewEpoch) busy.value = false
  }
}
async function createBot() {
  const input = { ...create }
  return mutate(() => createFeishuBot(input), 'Bot added.', clearCreate)
}
async function saveMetadata() {
  const botId = selectedId.value, revision = selectedRevision.value
  if (!botId || revision === null) return false
  const input = { name: editName.value.trim(), enabled: editEnabled.value, expected_revision: revision }
  return mutate(() => updateFeishuBot(botId, input), 'Bot details saved.')
}
async function saveSecrets() {
  const botId = selectedId.value, revision = selectedRevision.value
  if (!botId || revision === null) return false
  const input = { app_secret: appSecret.value, expected_revision: revision }
  return mutate(() => replaceFeishuBotSecrets(botId, input), 'Credentials replaced; the active pairing was preserved.')
}
async function removeBot() {
  const botId = selectedId.value, revision = selectedRevision.value
  if (!botId || revision === null) return false
  return mutate(() => deleteFeishuBot(botId, revision), 'Bot deleted.')
}
function clearLocalState() {
  closed = true; viewEpoch += 1; busy.value = false
  stopConnectionRefresh()
  clearSecrets(); clearCreate()
}
function close() { clearLocalState(); emit('close') }
onMounted(async () => { dialog.value?.showModal(); await refresh() })
onUnmounted(() => { clearLocalState(); dialog.value?.close() })
</script>

<style scoped src="./feishu-bot-settings-dialog.css"></style>
