<template>
  <dialog
    ref="dialog"
    class="bot-pool"
    data-testid="feishu-bot-settings-dialog"
    aria-labelledby="bot-pool-title"
    @cancel.prevent="close"
  >
    <header>
      <div>
        <h2 id="bot-pool-title">
          Feishu Bot pool
        </h2><p>Shared by this Hub instance. Pair a Bot from an individual Chat.</p>
      </div><button
        aria-label="Close"
        @click="close"
      >
        ×
      </button>
    </header>
    <p
      v-if="store.loading"
      role="status"
    >
      Loading Bot pool…
    </p>
    <p
      v-if="busy"
      role="status"
    >
      Closing this dialog does not cancel the submitted change.
    </p>
    <p
      v-if="error || store.error"
      class="error"
      role="alert"
    >
      {{ error || store.error }}
    </p>
    <p
      v-if="success"
      class="success"
      role="status"
    >
      {{ success }}
    </p>
    <p
      v-if="clipboardError"
      class="error"
      role="alert"
    >
      {{ clipboardError }}
    </p>
    <p
      v-if="clipboardStatus"
      class="success"
      role="status"
    >
      {{ clipboardStatus }}
    </p>
    <p
      v-if="store.deprecatedEnv.length"
      class="notice"
    >
      Deprecated environment entries: {{ store.deprecatedEnv.join(', ') }}. Migrate them to the Bot pool.
    </p>

    <section
      v-if="store.pool"
      class="layout"
    >
      <div
        class="list"
        aria-label="Bots"
      >
        <button
          v-for="bot in store.bots"
          :key="bot.bot_id"
          :disabled="busy"
          type="button"
          :class="{selected:bot.bot_id===selectedId}"
          @click="select(bot)"
        >
          <strong>{{ bot.name }}</strong><span>{{ occupancy(bot) }}</span>
        </button>
        <p v-if="!store.bots.length">
          No Bots configured.
        </p>
      </div>

      <section
        v-if="selected"
        class="detail"
      >
        <h3>{{ selected.name }}</h3>
        <dl>
          <div><dt>App ID</dt><dd>{{ selected.app_id }}</dd></div><div><dt>Source</dt><dd>{{ selected.source }}</dd></div>
          <div><dt>Status</dt><dd>{{ selected.enabled ? 'Enabled' : 'Disabled' }}</dd></div><div><dt>Draft revision</dt><dd>{{ selectedRevision }}</dd></div>
          <div><dt>App Secret</dt><dd>{{ configured(selected.app_secret_configured) }}</dd></div>
          <div><dt>Verification Token</dt><dd>{{ configured(selected.verification_token_configured) }}</dd></div>
          <div><dt>Encrypt Key</dt><dd>{{ configured(selected.encrypt_key_configured) }}</dd></div>
        </dl>
        <div class="callback">
          <code>{{ selected.event_url || 'Public URL required' }}</code><button
            v-if="selected.event_url"
            @click="copyUrl"
          >
            Copy callback
          </button>
        </div>
        <p v-if="selected.binding">
          In use by Chat <code>{{ selected.binding.tab_id }}</code><span v-if="selected.binding.owner_kind==='local'"> · Trusted local operator</span>
        </p>

        <form
          v-if="store.poolEditable"
          @submit.prevent="saveMetadata"
        >
          <label>Name<input
            v-model="editName"
            :disabled="busy"
            required
          ></label>
          <label class="checkbox-row"><input
            v-model="editEnabled"
            :disabled="busy"
            type="checkbox"
          > Enabled</label>
          <p
            v-if="selected.enabled && !editEnabled"
            class="notice"
          >
            Disabling releases this Bot's Chat and invalidates pending pairings. Save details to confirm.
          </p>
          <button :disabled="busy">
            {{ busy ? 'Saving…' : 'Save details' }}
          </button>
        </form>

        <form
          v-if="selected.credentials_editable"
          @submit.prevent="saveSecrets"
        >
          <h4>Replace credentials</h4><p>App ID is immutable. Existing active pairing is preserved.</p>
          <label>App Secret<input
            v-model="appSecret"
            :disabled="busy"
            type="password"
            required
            autocomplete="new-password"
          ></label>
          <label>Verification Token<input
            v-model="verificationToken"
            :disabled="busy"
            type="password"
            required
            autocomplete="new-password"
          ></label>
          <label>Encrypt Key<input
            v-model="encryptKey"
            :disabled="busy"
            type="password"
            required
            autocomplete="new-password"
          ></label>
          <button :disabled="busy || !secretComplete">
            Validate and replace
          </button>
        </form>

        <div
          v-if="selected.deletable"
          class="danger"
        >
          <button
            v-if="!confirmDelete"
            :disabled="busy"
            @click="confirmDelete=true"
          >
            Delete Bot
          </button>
          <template v-else>
            <p>Deleting stops this Bot, releases its Chat, and invalidates pending pairing attempts. Historical source labels remain.</p><button
              :disabled="busy"
              @click="removeBot"
            >
              Confirm delete
            </button><button
              :disabled="busy"
              @click="confirmDelete=false"
            >
              Cancel
            </button>
          </template>
        </div>
      </section>

      <form
        v-else-if="store.poolEditable"
        class="detail"
        @submit.prevent="createBot"
      >
        <h3>Add Bot</h3>
        <label>Name<input
          v-model="create.name"
          :disabled="busy"
          required
        ></label><label>App ID<input
          v-model="create.app_id"
          :disabled="busy"
          required
          autocomplete="off"
        ></label>
        <label>App Secret<input
          v-model="create.app_secret"
          :disabled="busy"
          type="password"
          required
          autocomplete="new-password"
        ></label>
        <label>Verification Token<input
          v-model="create.verification_token"
          :disabled="busy"
          type="password"
          required
          autocomplete="new-password"
        ></label>
        <label>Encrypt Key<input
          v-model="create.encrypt_key"
          :disabled="busy"
          type="password"
          required
          autocomplete="new-password"
        ></label>
        <button :disabled="busy || !createComplete">
          Validate and add
        </button>
      </form>
    </section>
    <footer>
      <button
        v-if="store.poolEditable"
        :disabled="busy"
        @click="beginCreate"
      >
        Add Bot
      </button><button
        :disabled="busy"
        @click="refresh"
      >
        Reload
      </button><button @click="close">
        Close
      </button>
    </footer>
  </dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useFeishuBotPoolStore } from '@/stores/feishuBotPoolStore'
import {
  createFeishuBot, deleteFeishuBot, FeishuBotRequestError,
  replaceFeishuBotSecrets, updateFeishuBot,
  type FeishuBotPoolResponse, type FeishuBotSummary,
} from '@/utils/feishuBotConfig'
import { writeClipboard } from '@/utils/clipboard'

const emit = defineEmits<{ close: [] }>()
const store = useFeishuBotPoolStore()
const dialog = ref<HTMLDialogElement | null>(null)
const selectedId = ref<string | null>(null)
const selectedRevision = ref<number | null>(null)
const busy = ref(false), error = ref<string | null>(null), success = ref<string | null>(null)
const confirmDelete = ref(false), editName = ref(''), editEnabled = ref(true)
const appSecret = ref(''), verificationToken = ref(''), encryptKey = ref('')
const create = reactive({ name: '', app_id: '', app_secret: '', verification_token: '', encrypt_key: '' })
const selected = computed(() => store.botById(selectedId.value))
const secretComplete = computed(() => !!(appSecret.value && verificationToken.value && encryptKey.value))
const createComplete = computed(() => Object.values(create).every(Boolean))
const clipboardError = ref<string | null>(null)
const clipboardStatus = ref<string | null>(null)
let clipboardEpoch = 0

function clearClipboardState() {
  clipboardEpoch += 1
  clipboardError.value = null
  clipboardStatus.value = null
}
let viewEpoch = 0
let closed = false

function configured(value: boolean) { return value ? 'Configured' : 'Not configured' }
function occupancy(bot: FeishuBotSummary) { return bot.binding ? `In use by Chat ${bot.binding.tab_id}` : 'Available' }
function clearSecrets() { appSecret.value = ''; verificationToken.value = ''; encryptKey.value = '' }
function clearCreate() { Object.assign(create, { name: '', app_id: '', app_secret: '', verification_token: '', encrypt_key: '' }) }
function fillSelection(bot: FeishuBotSummary | null, clear = true) {
  clearClipboardState()
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
  return cause.status === 500 || cause.code === 'public_url_invalid'
    || cause.code === 'bot_revision_conflict' || cause.code === 'bot_already_bound'
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
  const input = { app_secret: appSecret.value, verification_token: verificationToken.value, encrypt_key: encryptKey.value, expected_revision: revision }
  return mutate(() => replaceFeishuBotSecrets(botId, input), 'Credentials replaced; the active pairing was preserved.')
}
async function removeBot() {
  const botId = selectedId.value, revision = selectedRevision.value
  if (!botId || revision === null) return false
  return mutate(() => deleteFeishuBot(botId, revision), 'Bot deleted.')
}
async function copyUrl() {
  const url = selected.value?.event_url
  if (!url || closed) return
  const current = ++clipboardEpoch
  clipboardError.value = null
  clipboardStatus.value = null
  try {
    await writeClipboard(url)
    if (!closed && current === clipboardEpoch) {
      clipboardStatus.value = 'Callback URL copied.'
    }
  } catch {
    if (!closed && current === clipboardEpoch) {
      clipboardError.value = 'Could not copy the callback URL. Select and copy it manually.'
    }
  }
}

function clearLocalState() {
  clearClipboardState()
  closed = true; viewEpoch += 1; busy.value = false
  clearSecrets(); clearCreate()
}
function close() { clearLocalState(); emit('close') }
onMounted(async () => { dialog.value?.showModal(); await refresh() })
onUnmounted(() => { clearLocalState(); dialog.value?.close() })
</script>
<style scoped>
.bot-pool{width:min(860px,calc(100vw - 32px));max-height:calc(100dvh - 32px);padding:20px;overflow:auto;background:var(--ch-color-surface);color:var(--ch-color-text);border:1px solid var(--ch-color-border);border-radius:var(--ch-radius-lg)}header,footer,.callback{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.layout{display:grid;grid-template-columns:260px 1fr;gap:16px;align-items:start}.list,.detail,form{display:grid;gap:10px;align-content:start}.list>button{display:flex;justify-content:space-between;align-items:center;gap:8px;min-height:40px;padding:8px 10px;text-align:left}.checkbox-row{display:flex;align-items:center;gap:8px}.checkbox-row input{width:auto;margin:0}.selected{border-color:var(--ch-color-accent)}dl{display:grid;gap:6px}dl div{display:grid;grid-template-columns:150px 1fr}dd{margin:0;overflow-wrap:anywhere}label{display:grid;gap:4px}.error{color:var(--ch-color-danger)}.success{color:var(--ch-color-success)}.notice{color:var(--ch-color-text-muted)}.danger{margin-top:12px;padding-top:12px;border-top:1px solid var(--ch-color-border-muted)}footer{margin-top:16px;justify-content:flex-end}@media(max-width:640px){.layout{grid-template-columns:1fr}.bot-pool{width:calc(100vw - 16px)}}
</style>
