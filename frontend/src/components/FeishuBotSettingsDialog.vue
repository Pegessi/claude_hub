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
<style scoped>
.bot-pool {
  width: min(940px, calc(100vw - 32px));
  height: min(720px, calc(100dvh - 32px));
  max-width: none;
  max-height: none;
  box-sizing: border-box;
  margin: auto;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-xl);
  background: var(--ch-color-surface);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-dialog);
}

.bot-pool::backdrop { background: var(--ch-color-overlay); }

.bot-pool__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 72px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-raised);
}

.bot-pool__heading { display: flex; align-items: center; gap: 12px; min-width: 0; }
.bot-pool__heading h2 { margin: 0; color: var(--ch-color-text-strong); font-size: 17px; font-weight: 600; letter-spacing: -.015em; }
.bot-pool__heading p { margin: 3px 0 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-sm); }
.bot-pool__mark,
.empty-state__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  border: 1px solid color-mix(in srgb, var(--ch-color-accent) 25%, var(--ch-color-border));
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 16px;
  font-weight: 700;
}
.bot-pool__close svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; }

.bot-pool__notifications {
  position: absolute;
  top: 78px;
  right: 18px;
  z-index: 3;
  display: grid;
  width: min(440px, calc(100% - 36px));
  gap: 6px;
  pointer-events: none;
}
.notification {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  padding: 9px 11px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text-muted);
  box-shadow: var(--ch-shadow-soft);
  font-size: var(--ch-font-size-sm);
  line-height: 1.4;
}
.notification--error { border-color: var(--ch-color-danger-border); background: var(--ch-color-danger-bg); color: var(--ch-color-danger-text); }
.notification--success { border-color: color-mix(in srgb, var(--ch-color-success) 35%, var(--ch-color-border)); background: var(--ch-color-success-bg); color: var(--ch-color-success); }
.notification--warning { border-color: color-mix(in srgb, var(--ch-color-warning) 35%, var(--ch-color-border)); background: var(--ch-color-warning-bg); color: var(--ch-color-warning); }
.notification__spinner { width: 13px; height: 13px; border: 2px solid var(--ch-color-border); border-top-color: var(--ch-color-accent); border-radius: 50%; animation: settings-spin .8s linear infinite; }
@keyframes settings-spin { to { transform: rotate(360deg); } }

.layout {
  display: grid;
  grid-template-columns: 250px minmax(0, 1fr);
  height: calc(100% - 129px);
  min-height: 0;
}

.bot-sidebar {
  display: flex;
  width: 100%;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  border-right: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-sunken);
}
.bot-sidebar__title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 14px 9px;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.bot-sidebar__count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text-subtle);
  font-size: 10px;
}
.list { display: grid; width: 100%; min-width: 0; min-height: 0; gap: 3px; padding: 0 8px 10px; overflow-y: auto; }
.bot-row {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 16px;
  align-items: center;
  gap: 9px;
  width: 100%;
  min-height: 54px;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  text-align: left;
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast);
}
.bot-row:hover:not(:disabled) { background: var(--ch-color-row-hover); }
.bot-row.selected { border-color: var(--ch-color-accent-ring-strong); background: var(--ch-color-surface-selected); }
.bot-row:focus-visible { outline: none; border-color: var(--ch-color-accent); box-shadow: 0 0 0 2px var(--ch-color-accent-ring); }
.bot-row:disabled { opacity: .6; cursor: not-allowed; }
.bot-row__mark { display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; border-radius: var(--ch-radius-md); background: var(--ch-color-chip-bg); color: var(--ch-color-text-muted); font-size: 12px; font-weight: 700; }
.bot-row.selected .bot-row__mark { background: var(--ch-color-accent-soft); color: var(--ch-color-accent); }
.bot-row__content,
.bot-row__name { min-width: 0; }
.bot-row__name { display: flex; align-items: center; gap: 7px; }
.bot-row__name strong { overflow: hidden; color: var(--ch-color-text); font-size: var(--ch-font-size-sm); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.bot-row__meta { display: block; margin-top: 2px; overflow: hidden; color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); text-overflow: ellipsis; white-space: nowrap; }
.bot-row__status { width: 6px; height: 6px; flex: 0 0 6px; border-radius: 50%; background: var(--ch-color-success); }
.bot-row__status.is-disabled { background: var(--ch-color-text-subtle); }
.bot-row__status.is-connecting { background: var(--ch-color-warning); }
.bot-row__status.is-failed { background: var(--ch-color-danger); }
.bot-row__chevron { width: 14px; height: 14px; fill: none; stroke: var(--ch-color-text-subtle); stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }
.bot-sidebar__empty { padding: 28px 8px; text-align: center; color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-sm); }
.bot-sidebar__hint { margin-top: auto; padding: 12px 14px 14px; border-top: 1px solid var(--ch-color-border-muted); color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); line-height: 1.45; }

.detail { min-width: 0; min-height: 0; padding: 22px 24px 28px; overflow-y: auto; }
.detail-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.detail-title-row { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }
.detail-title-row h3,
.create-heading h3 { margin: 0; color: var(--ch-color-text-strong); font-size: 18px; font-weight: 600; letter-spacing: -.015em; }
.app-id { display: block; margin-top: 5px; color: var(--ch-color-text-subtle); font: 11px/1.4 var(--ch-font-mono); }
.status-chip,
.source-chip,
.occupancy-chip { display: inline-flex; align-items: center; min-height: 21px; padding: 0 7px; border-radius: 999px; font-size: 10px; font-weight: 600; }
.status-chip.is-enabled { background: var(--ch-color-success-bg); color: var(--ch-color-success); }
.status-chip.is-disabled { background: var(--ch-color-chip-bg); color: var(--ch-color-text-muted); }
.source-chip { background: var(--ch-color-chip-bg-muted); color: var(--ch-color-text-subtle); text-transform: capitalize; }
.occupancy-chip { background: var(--ch-color-accent-soft); color: var(--ch-color-accent); }

.connection-card,
.binding-card { border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-md); background: var(--ch-color-surface-sunken); }
.connection-card { display: flex; align-items: center; gap: 10px; padding: 11px 12px; }
.connection-card > div { display: grid; min-width: 0; gap: 2px; }
.connection-card strong { color: var(--ch-color-text); font-size: var(--ch-font-size-sm); font-weight: 600; }
.connection-card small { color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); line-height: 1.4; }
.connection-card__dot { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: var(--ch-color-text-subtle); }
.connection-card[data-state='connected'] .connection-card__dot { background: var(--ch-color-success); }
.connection-card[data-state='connecting'] .connection-card__dot { background: var(--ch-color-warning); }
.connection-card[data-state='failed'] .connection-card__dot { background: var(--ch-color-danger); }
.section-eyebrow { display: block; margin-bottom: 4px; color: var(--ch-color-text-subtle); font-size: 10px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; }
.binding-card { display: flex; align-items: center; gap: 8px; margin: 8px 0 0; padding: 9px 11px; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-xs); }
.binding-card__dot { width: 7px; height: 7px; flex: 0 0 7px; border-radius: 50%; background: var(--ch-color-success); }
.binding-card code { color: var(--ch-color-text-code); font-family: var(--ch-font-mono); }

.settings-section { display: grid; gap: 13px; margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--ch-color-border-muted); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.section-heading h4,
.danger-zone h4 { margin: 0; color: var(--ch-color-text-strong); font-size: var(--ch-font-size-base); font-weight: 600; }
.section-heading p,
.danger-zone p { margin: 3px 0 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-xs); line-height: 1.45; }
.field { display: grid; min-width: 0; gap: 6px; }
.field-label { color: var(--ch-color-text-muted); font-size: var(--ch-font-size-sm); font-weight: 500; }
.field small { color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.toggle-row { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 10px 12px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-md); background: var(--ch-color-surface-sunken); }
.toggle-row > span { display: grid; gap: 2px; }
.toggle-row strong { color: var(--ch-color-text); font-size: var(--ch-font-size-sm); font-weight: 500; }
.toggle-row small { color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.toggle-row input { width: 16px; height: 16px; accent-color: var(--ch-color-accent-strong); }
.inline-notice { margin: 0; padding: 9px 11px; border-radius: var(--ch-radius-md); font-size: var(--ch-font-size-xs); line-height: 1.45; }
.inline-notice--warning { border: 1px solid color-mix(in srgb, var(--ch-color-warning) 35%, var(--ch-color-border)); background: var(--ch-color-warning-bg); color: var(--ch-color-warning); }
.section-actions,
.create-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.section-actions > span,
.create-actions > span { color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.section-actions--end { justify-content: flex-end; }
.secret-grid,
.create-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px; }
.secret-grid--single { grid-template-columns: minmax(0, 1fr); }
.field--wide { grid-column: 1 / -1; }
.credential-state { display: flex; align-items: center; gap: 5px; }
.credential-state span { padding: 3px 6px; border-radius: 999px; background: var(--ch-color-chip-bg-muted); color: var(--ch-color-text-subtle); font-size: 9px; font-weight: 600; text-transform: uppercase; }
.credential-state span.configured { background: var(--ch-color-success-bg); color: var(--ch-color-success); }

.danger-zone { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--ch-color-border-muted); }
.danger-zone__actions { display: flex; gap: 8px; flex: 0 0 auto; }

.create-view { max-width: 650px; margin: 0 auto; }
.empty-state { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; padding: 13px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-lg); background: var(--ch-color-surface-sunken); }
.empty-state__icon { width: 32px; height: 32px; flex-basis: 32px; border-radius: var(--ch-radius-md); font-size: 13px; }
.empty-state strong { display: block; margin-bottom: 2px; color: var(--ch-color-text); font-size: var(--ch-font-size-sm); }
.empty-state p,
.create-heading p { margin: 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-xs); line-height: 1.5; }
.create-heading { margin-bottom: 20px; }
.create-heading p { margin-top: 5px; }
.create-grid { gap: 15px; }
.create-actions { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--ch-color-border-muted); }

.bot-pool__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 57px;
  padding: 10px 14px;
  border-top: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-raised);
}
.bot-pool__footer-actions { display: flex; align-items: center; gap: 6px; }
.button-icon { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; }

@media (max-width: 700px) {
  .bot-pool { width: calc(100vw - 16px); height: calc(100dvh - 16px); }
  .bot-pool__header { min-height: 64px; padding: 10px 12px; }
  .bot-pool__heading p { display: none; }
  .bot-pool__mark { width: 32px; height: 32px; flex-basis: 32px; }
  .layout { grid-template-columns: 1fr; height: calc(100% - 121px); overflow-y: auto; }
  .bot-sidebar { min-height: auto; border-right: 0; border-bottom: 1px solid var(--ch-color-border); }
  .list { grid-auto-flow: column; grid-auto-columns: minmax(180px, 72vw); overflow-x: auto; overflow-y: hidden; }
  .bot-sidebar__hint { display: none; }
  .detail { min-height: auto; padding: 18px 14px 24px; overflow: visible; }
  .secret-grid,
  .create-grid { grid-template-columns: 1fr; }
  .field--wide { grid-column: auto; }
  .section-heading,
  .danger-zone,
  .create-actions { align-items: stretch; flex-direction: column; }
  .credential-state { align-self: flex-start; }
  .danger-zone .ch-btn,
  .danger-zone__actions,
  .danger-zone__actions .ch-btn,
  .create-actions .ch-btn { width: 100%; }
  .bot-pool .ch-btn,
  .bot-pool .ch-input { min-height: 44px; }
  .bot-pool .ch-btn--icon { width: 44px; padding: 0; }
  .bot-pool__notifications { top: 70px; right: 10px; width: calc(100% - 20px); }
}

@media (prefers-reduced-motion: reduce) {
  .bot-row { transition: none; }
  .notification__spinner { animation: none; }
}
</style>
