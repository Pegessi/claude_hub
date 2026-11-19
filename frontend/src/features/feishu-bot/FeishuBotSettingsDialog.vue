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
        <div class="bot-sidebar__title">
          <span>Personal /ai</span>
        </div>
        <button
          :disabled="busy"
          type="button"
          class="bot-row"
          :class="{ selected: aiCommandActive }"
          :aria-pressed="aiCommandActive"
          data-testid="feishu-ai-command-nav"
          @click="openAiCommand"
        >
          <span
            class="bot-row__mark"
            aria-hidden="true"
          >AI</span>
          <span class="bot-row__content">
            <span class="bot-row__name">
              <strong>/ai Assistant</strong>
            </span>
            <span class="bot-row__meta">{{ aiSidebarMeta }}</span>
          </span>
          <svg
            class="bot-row__chevron"
            viewBox="0 0 16 16"
            aria-hidden="true"
          ><path d="m6 3 5 5-5 5" /></svg>
        </button>
      </aside>

      <section
        v-if="aiCommandActive"
        class="detail"
        data-testid="feishu-ai-command-panel"
      >
        <div class="detail-header">
          <div>
            <div class="detail-title-row">
              <h3>Personal /ai Assistant</h3>
              <span
                class="status-chip"
                :class="aiEdit.enabled ? 'is-enabled' : 'is-disabled'"
              >{{ aiEdit.enabled ? 'Enabled' : 'Disabled' }}</span>
              <span
                v-if="aiTokenStatus"
                class="source-chip"
              >{{ aiTokenChipLabel }}</span>
            </div>
            <p>Detect your own <code>{{ aiEdit.prefix || '/ai' }} &lt;instruction&gt;</code> messages, react <code>{{ aiEdit.ack_emoji }}</code> &rarr; <code>{{ aiEdit.done_emoji }}</code>, and reply as your personal Feishu account (or configured bot fallback when <code>im:message.send_as_user</code> is not granted).</p>
          </div>
        </div>

        <div
          class="connection-card"
          :data-state="aiTokenCanSend ? 'connected' : 'failed'"
          data-testid="feishu-ai-token-card"
        >
          <span
            class="connection-card__dot"
            aria-hidden="true"
          />
          <div>
            <span class="section-eyebrow">User access token &amp; scope check</span>
            <strong>{{ aiTokenSummary }}</strong>
            <small
              v-if="aiTokenGuidance"
              data-testid="feishu-ai-token-guidance"
            >
              {{ aiTokenGuidance }}
            </small>
            <small
              v-if="aiTokenStatus?.auth_login_command && !aiTokenCanSend"
              data-testid="feishu-ai-token-login-cmd"
            >
              Run: <code>{{ aiTokenStatus.auth_login_command }}</code>
            </small>
            <div class="section-actions">
              <button
                type="button"
                class="ch-btn"
                :disabled="busy || aiCommandLoading"
                data-testid="feishu-ai-import-token-btn"
                @click="importAiToken"
              >
                Import ~/.lark-cli Token
              </button>
            </div>
          </div>
        </div>

        <p
          v-if="aiCommandLoading && !aiCommandLoaded"
          class="empty"
          data-testid="feishu-ai-loading"
        >
          Loading /ai command settings…
        </p>

        <div
          v-else-if="aiCommandLoadError && !aiCommandLoaded"
          class="inline-notice inline-notice--error"
          data-testid="feishu-ai-load-error"
        >
          <p>{{ aiCommandLoadError }}</p>
          <div class="section-actions">
            <button
              type="button"
              class="ch-btn"
              :disabled="busy || aiCommandLoading"
              data-testid="feishu-ai-retry-btn"
              @click="retryLoadAiCommand"
            >
              Retry
            </button>
          </div>
        </div>

        <form
          v-else
          class="settings-section"
          data-testid="feishu-ai-settings-form"
          @submit.prevent="saveAiCommand"
        >
          <div class="section-heading">
            <div>
              <h4>Default execution agent</h4>
              <p>Choose the default provider, model, and thinking intensity for <code>/ai</code> commands. Override per message with <code>/ai --provider &hellip; --model &hellip; --effort &hellip;</code>.</p>
            </div>
          </div>

          <label class="toggle-row">
            <span>
              <strong>Enable /ai personal command</strong>
              <small>Poll active/watched chats and hook WebSocket events for your own <code>/ai</code> instructions.</small>
            </span>
            <input
              v-model="aiEdit.enabled"
              :disabled="busy || !aiCommandLoaded"
              type="checkbox"
              data-testid="feishu-ai-enabled-toggle"
            >
          </label>

          <div
            v-if="aiTupleConflict"
            class="inline-notice inline-notice--warning"
            data-testid="feishu-ai-tuple-conflict"
          >
            <p>
              Server execution settings changed to
              <code>{{ aiTupleConflict.provider }} · {{ aiTupleConflict.model || 'default' }} · {{ aiTupleConflict.reasoning_effort || 'default' }}</code>
              while you were editing. Your local draft was preserved as a coherent unit.
            </p>
            <div class="section-actions">
              <button
                type="button"
                class="ch-btn"
                :disabled="busy || aiCommandLoading || !aiCommandLoaded"
                data-testid="feishu-ai-accept-server-tuple-btn"
                @click="acceptServerAiTuple"
              >
                Use server values
              </button>
            </div>
          </div>

          <label class="field">
            <span class="field-label">Provider</span>
            <select
              v-model="aiEdit.provider"
              class="ch-input"
              :disabled="busy || !aiCommandLoaded"
              data-testid="feishu-ai-provider-select"
              @change="onAiProviderChange(($event.target as HTMLSelectElement).value as FeishuAiCommandProvider)"
            >
              <option
                v-for="prov in aiProviderOptions"
                :key="prov"
                :value="prov"
              >{{ prov }}</option>
            </select>
          </label>

          <label class="field">
            <span class="field-label">Model</span>
            <select
              :value="aiEdit.model"
              class="ch-input"
              :disabled="busy || !aiCommandLoaded"
              data-testid="feishu-ai-model-input"
              @change="onAiModelChange(($event.target as HTMLSelectElement).value)"
            >
              <option value="">Provider default</option>
              <option
                v-if="aiCustomModelOption"
                :value="aiCustomModelOption.value"
              >{{ aiCustomModelOption.label }}</option>
              <option
                v-for="opt in aiModelSelectOptions"
                :key="opt.key"
                :value="opt.value"
              >{{ opt.label }}</option>
            </select>
          </label>

          <label class="field">
            <span class="field-label">Thinking intensity (reasoning_effort)</span>
            <select
              :value="aiEdit.reasoning_effort"
              class="ch-input"
              :disabled="busy || !aiCommandLoaded || aiEffortOptions.length === 0"
              data-testid="feishu-ai-effort-select"
              @change="onAiEffortChange(($event.target as HTMLSelectElement).value as FeishuAiCommandReasoningEffort)"
            >
              <option value="">Provider default</option>
              <option
                v-for="eff in aiEffortOptions"
                :key="eff"
                :value="eff"
              >{{ aiEffortLabel(eff) }}</option>
            </select>
          </label>

          <div class="create-grid">
            <label class="field">
              <span class="field-label">Command prefix</span>
              <input
                v-model="aiEdit.prefix"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
                placeholder="/ai"
                data-testid="feishu-ai-prefix-input"
              >
            </label>
            <label class="field">
              <span class="field-label">Owner Open ID</span>
              <input
                v-model="aiEdit.owner_open_id"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
                placeholder="ou_…"
                data-testid="feishu-ai-owner-input"
              >
            </label>
            <label class="field">
              <span class="field-label">Feishu App ID</span>
              <input
                v-model="aiEdit.app_id"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
                placeholder="cli_…"
                data-testid="feishu-ai-app-id-input"
              >
            </label>
            <label class="field">
              <span class="field-label">Workspace directory (cwd)</span>
              <input
                v-model="aiEdit.cwd"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
                placeholder="/home/user/project"
                data-testid="feishu-ai-cwd-input"
              >
            </label>
            <label class="field">
              <span class="field-label">Ack reaction</span>
              <input
                v-model="aiEdit.ack_emoji"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
                placeholder="OnIt"
              >
            </label>
            <label class="field">
              <span class="field-label">Done reaction</span>
              <input
                v-model="aiEdit.done_emoji"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
                placeholder="DONE"
              >
            </label>
            <label class="field">
              <span class="field-label">Poll interval (seconds)</span>
              <input
                v-model.number="aiEdit.poll_interval_seconds"
                class="ch-input"
                type="number"
                min="2"
                max="300"
                :disabled="busy || !aiCommandLoaded"
              >
            </label>
            <label class="field">
              <span class="field-label">Watched chats (comma-separated oc_…)</span>
              <input
                v-model="aiWatchChatsText"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
                placeholder="oc_xxx, oc_yyy (blank = active chats)"
                data-testid="feishu-ai-watch-chats-input"
              >
            </label>
            <label class="field field--wide">
              <span class="field-label">Reply footer</span>
              <input
                v-model="aiEdit.footer"
                class="ch-input"
                :disabled="busy || !aiCommandLoaded"
              >
            </label>
          </div>

          <div class="section-actions section-actions--end">
            <button
              class="ch-btn ch-btn--primary"
              :disabled="busy || aiCommandLoading || !aiCommandLoaded"
              data-testid="feishu-ai-save-btn"
            >
              {{ busy ? 'Saving…' : 'Save /ai settings' }}
            </button>
          </div>
        </form>
      </section>

      <section
        v-else-if="selected"
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
            v-if="selectedBindings.length"
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
          v-if="selectedBindings.length === 1 && selectedMode === 'single'"
          class="binding-card"
        >
          <span
            class="binding-card__dot"
            aria-hidden="true"
          />
          <span>In use by Chat <code>{{ selectedBindings[0].tab_id }}</code><span v-if="selectedBindings[0].owner_kind === 'local'"> · Trusted local operator</span></span>
        </p>
        <p
          v-else-if="selectedBindings.length"
          class="binding-card"
        >
          <span
            class="binding-card__dot"
            aria-hidden="true"
          />
          <span>{{ selectedBindings.length === 1 ? '1 active channel' : `${selectedBindings.length} active channels` }} · <code>{{ selectedBindings.map(item => item.tab_id).join(', ') }}</code></span>
        </p>

        <form
          v-if="store.poolEditable"
          class="settings-section"
          @submit.prevent="saveMetadata"
        >
          <div class="section-heading">
            <div>
              <h4>Bot details</h4>
              <p>Update the display name, availability, and serving mode.</p>
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
          <div
            class="field"
            role="group"
            aria-label="Serving mode"
            data-testid="feishu-bot-mode-control"
          >
            <span class="field-label">Serving mode</span>
            <div class="mode-grid">
              <button
                type="button"
                class="ch-btn mode-card"
                :class="{ 'is-selected': editMode === 'single' }"
                :aria-pressed="editMode === 'single'"
                :disabled="busy || modeSwitchDisabled"
                data-testid="feishu-bot-mode-single"
                @click="selectMode('single')"
              >
                <span class="mode-card__header">
                  <strong>Single Chat</strong>
                  <code>single</code>
                </span>
                <small>This Bot serves one Chat</small>
              </button>
              <button
                type="button"
                class="ch-btn mode-card"
                :class="{ 'is-selected': editMode === 'per_channel' }"
                :aria-pressed="editMode === 'per_channel'"
                :disabled="busy || modeSwitchDisabled"
                data-testid="feishu-bot-mode-per-channel"
                @click="selectMode('per_channel')"
              >
                <span class="mode-card__header">
                  <strong>Per channel</strong>
                  <code>per_channel</code>
                </span>
                <small>Everyone who talks to this Bot gets their own Chat</small>
              </button>
            </div>
            <p
              v-if="modeSwitchDisabledReason"
              class="inline-notice inline-notice--warning"
              data-testid="feishu-bot-mode-disabled-reason"
            >
              {{ modeSwitchDisabledReason }}
            </p>
          </div>
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
          :disabled="busy || aiCommandLoading"
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
import { aiEffortLabel, matchesAiModelOption, resolveVariantModelId } from './feishuModelOptions'
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useFeishuBotPoolStore } from '@/stores/feishuBotPoolStore'
import {
  botBindings, botMode, createFeishuBot, deleteFeishuBot, FeishuBotRequestError,
  importFeishuAiCommandToken, loadFeishuAiCommand, replaceFeishuBotSecrets,
  updateFeishuAiCommand, updateFeishuBot,
  type FeishuAiCommandCapabilities, type FeishuAiCommandConfig,
  type FeishuAiCommandModelOption, type FeishuAiCommandProvider,
  type FeishuAiCommandReasoningEffort, type FeishuAiCommandStatusResponse,
  type FeishuAiCommandTokenStatus, type FeishuBotMode,
  type FeishuBotPoolResponse, type FeishuBotSummary,
} from '@/utils/feishuBotConfig'
const emit = defineEmits<{ close: [] }>()
const store = useFeishuBotPoolStore()
const dialog = ref<HTMLDialogElement | null>(null)
const selectedId = ref<string | null>(null)
const selectedRevision = ref<number | null>(null)
const busy = ref(false), error = ref<string | null>(null), success = ref<string | null>(null)
const confirmDelete = ref(false), editName = ref(''), editEnabled = ref(true)
const editMode = ref<FeishuBotMode>('single')
const appSecret = ref('')
const create = reactive({ name: '', app_id: '', app_secret: '' })
const aiCommandActive = ref(false)
const aiCommandLoading = ref(false)
const aiCommandLoaded = ref(false)
const aiCommandLoadError = ref<string | null>(null)
const aiTokenStatus = ref<FeishuAiCommandTokenStatus | null>(null)
const aiCapabilities = ref<FeishuAiCommandCapabilities | null>(null)
const aiWatchChatsText = ref('')
const aiServerSnapshot = ref<FeishuAiCommandConfig | null>(null)
const aiServerWatchChatsText = ref('')
const aiTupleConflict = ref<Pick<FeishuAiCommandConfig, 'provider' | 'model' | 'reasoning_effort'> | null>(null)
const DEFAULT_PROVIDER_EFFORTS: Record<FeishuAiCommandProvider, string[]> = {
  claude: [],
  codex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  traex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  maso: ['default', 'none', 'low', 'medium', 'high', 'xhigh', 'max'],
  cursor: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
  antigravity: [],
}
// Provider models are supplied by the authenticated capabilities response.
const DEFAULT_PROVIDER_MODEL_OPTIONS: Record<FeishuAiCommandProvider, FeishuAiCommandModelOption[]> = {
  claude: [], codex: [], traex: [], maso: [], cursor: [], antigravity: [],
}
function resolveAiModelOptionsForProvider(provider: FeishuAiCommandProvider): FeishuAiCommandModelOption[] {
  const rich = aiCapabilities.value?.provider_model_options?.[provider]
  if (Array.isArray(rich) && rich.length > 0) return rich
  const legacyIds = aiCapabilities.value?.provider_models?.[provider]
  if (Array.isArray(legacyIds) && legacyIds.length > 0) {
    return legacyIds.map(id => ({ id, label: id, provider_model_id: id }))
  }
  return DEFAULT_PROVIDER_MODEL_OPTIONS[provider] ?? []
}
const aiEdit = reactive<FeishuAiCommandConfig>({
  enabled: false,
  prefix: '/ai',
  owner_open_id: '',
  app_id: '',
  provider: 'codex',
  model: 'gpt-5.4',
  reasoning_effort: 'high',
  cwd: '',
  watch_chats: [],
  poll_interval_seconds: 15,
  max_active_chats: 10,
  max_context_messages: 20,
  ack_emoji: 'OnIt',
  done_emoji: 'DONE',
  footer: '> 🤖 AI 代答，仅供参考',
  reply_in_thread: true,
})
const aiProviderOptions = computed<FeishuAiCommandProvider[]>(() => (
  aiCapabilities.value?.providers?.length
    ? aiCapabilities.value.providers
    : ['claude', 'codex', 'traex', 'maso', 'cursor', 'antigravity']
))
const aiProviderModelCatalog = computed<FeishuAiCommandModelOption[]>(() => (
  resolveAiModelOptionsForProvider(aiEdit.provider)
))
const currentAiModelOption = computed<FeishuAiCommandModelOption | null>(() => {
  const modelKey = aiEdit.model.trim()
  if (!modelKey) return null
  return aiProviderModelCatalog.value.find(opt => matchesAiModelOption(opt, modelKey)) ?? null
})
const aiModelSelectOptions = computed<Array<{ key: string; value: string; label: string }>>(() => {
  const activeModel = aiEdit.model.trim()
  return aiProviderModelCatalog.value.map(opt => ({
    key: opt.id,
    value: matchesAiModelOption(opt, activeModel)
      ? activeModel
      : resolveVariantModelId(aiEdit.provider, opt, aiEdit.reasoning_effort),
    label: opt.label || opt.id,
  }))
})
const aiCustomModelOption = computed<{ value: string; label: string } | null>(() => {
  const activeModel = aiEdit.model.trim()
  if (!activeModel || currentAiModelOption.value) return null
  return { value: activeModel, label: `${activeModel} (custom/current)` }
})
function resolveEffortsForModel(
  provider: FeishuAiCommandProvider,
  modelId: string,
  option?: FeishuAiCommandModelOption | null,
): string[] {
  const clean = modelId.trim()
  if (!clean) {
    // Only ENV_KEY providers (codex, traex) have a separate effort channel when Provider default
    // (model === '') is chosen; model-bound effort providers (maso, cursor) and non-effort
    // providers (claude, antigravity) require a concrete model ID.
    if (provider === 'maso' || provider === 'cursor' || provider === 'claude' || provider === 'antigravity') {
      return []
    }
    const perProviderDefault = aiCapabilities.value?.provider_reasoning_efforts?.[provider]
    if (Array.isArray(perProviderDefault)) return perProviderDefault
    return DEFAULT_PROVIDER_EFFORTS[provider] ?? []
  }
  const matched = option ?? resolveAiModelOptionsForProvider(provider).find(opt => matchesAiModelOption(opt, clean)) ?? null
  if (matched && Array.isArray(matched.supported_reasoning_efforts)) {
    if (matched.supported_reasoning_efforts.length > 0) {
      return matched.supported_reasoning_efforts.map(item => item.id)
    }
    if (provider === 'maso' || provider === 'cursor' || provider === 'claude' || provider === 'antigravity') {
      return []
    }
  }
  const perModel = aiCapabilities.value?.model_reasoning_efforts?.[clean]
  if (Array.isArray(perModel)) return perModel
  const perProvider = aiCapabilities.value?.provider_reasoning_efforts?.[provider]
  if (Array.isArray(perProvider)) return perProvider
  return DEFAULT_PROVIDER_EFFORTS[provider] ?? []
}
const aiEffortOptions = computed<string[]>(() => (
  resolveEffortsForModel(aiEdit.provider, aiEdit.model, currentAiModelOption.value)
))
const aiSidebarMeta = computed(() => {
  if (aiEdit.enabled) {
    const modelLabel = currentAiModelOption.value?.label || aiEdit.model || 'default'
    return `${aiEdit.provider} · ${modelLabel}`
  }
  const tok = aiTokenStatus.value
  if (tok?.can_send_as_user) return 'Personal account reply'
  if (tok && (tok.send_mode === 'bot_fallback' || tok.can_send_via_bot)) {
    return tok.fallback_bot_app_id ? `Bot fallback (${tok.fallback_bot_app_id})` : 'Bot fallback reply'
  }
  return 'Personal account reply'
})
const aiTokenCanSend = computed(() => {
  const tok = aiTokenStatus.value
  if (!tok) return false
  return Boolean(tok.can_send ?? (tok.can_send_as_user || tok.can_send_via_bot))
})
const aiTokenChipLabel = computed(() => {
  const tok = aiTokenStatus.value
  if (!tok) return ''
  if (tok.send_mode === 'web_session' || (tok.has_web_session && tok.can_send_as_user && !tok.has_send_as_user_scope)) {
    return 'Web session ready'
  }
  if (tok.can_send_as_user) return 'Scope ready'
  if (tok.send_mode === 'bot_fallback' || tok.can_send_via_bot) {
    return tok.fallback_bot_app_id ? `Bot fallback (${tok.fallback_bot_app_id})` : 'Bot fallback'
  }
  return 'Missing send_as_user'
})
const aiTokenSummary = computed(() => {
  const tok = aiTokenStatus.value
  if (!tok || !tok.has_token) return 'No user token loaded yet (sign in via Feishu OAuth or import ~/.lark-cli token)'
  const userLabel = tok.user_name ? `${tok.user_name} (${tok.open_id})` : (tok.open_id ?? 'User')
  if (tok.send_mode === 'web_session' || (tok.has_web_session && tok.can_send_as_user && !tok.has_send_as_user_scope)) {
    return `Ready (web session): ${userLabel} · user-identity reply active`
  }
  if (tok.can_send_as_user) return `Ready: ${userLabel} · im:message.send_as_user granted`
  if (tok.send_mode === 'bot_fallback' || tok.can_send_via_bot) {
    const botLabel = tok.fallback_bot_app_id ?? tok.app_id ?? 'app bot'
    return `Ready (bot fallback: ${botLabel}): ${userLabel} · im:message.send_as_user not granted`
  }
  return `${userLabel} · Missing required scope: ${tok.missing_required_scopes.join(', ') || 'im:message.send_as_user'} (and no bot fallback configured)`
})
const aiTokenGuidance = computed(() => {
  const tok = aiTokenStatus.value
  if (!tok || aiTokenCanSend.value) return ''
  const firstStep = tok.remediation_steps?.[0]
  return typeof firstStep === 'string' ? firstStep : ''
})
const selected = computed(() => store.botById(selectedId.value))
const selectedBindings = computed(() => botBindings(selected.value))
const selectedMode = computed(() => botMode(selected.value))
const modeSwitchDisabled = computed(() => selectedBindings.value.length > 0)
const modeSwitchDisabledReason = computed(() => (modeSwitchDisabled.value
  ? 'Switching mode requires no active channels. Disconnect active Feishu channels before switching.'
  : null))
const secretComplete = computed(() => !!appSecret.value)
const createComplete = computed(() => Object.values(create).every(Boolean))
const CONNECTION_REFRESH_MS = 2_000
let viewEpoch = 0
let aiLoadGeneration = 0
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
  const active = botBindings(bot)
  if (botMode(bot) === 'per_channel' && active.length > 0) {
    return active.length === 1
      ? `1 active channel (${active[0].tab_id})`
      : `${active.length} active channels`
  }
  if (active[0]) return `In use by Chat ${active[0].tab_id}`
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
  editMode.value = botMode(bot)
  confirmDelete.value = false
  if (clear) clearSecrets()
}
function retireAiCommandLoad() {
  aiLoadGeneration += 1
  aiCommandLoading.value = false
}
function selectMode(mode: FeishuBotMode) {
  if (busy.value || closed || modeSwitchDisabled.value) return
  editMode.value = mode
}
function select(bot: FeishuBotSummary) {
  if (busy.value || closed) return
  viewEpoch += 1
  retireAiCommandLoad()
  aiCommandActive.value = false
  fillSelection(bot)
  error.value = null; success.value = null
}
function beginCreate() {
  if (busy.value || closed) return
  viewEpoch += 1
  retireAiCommandLoad()
  aiCommandActive.value = false
  fillSelection(null)
  clearCreate()
  error.value = null; success.value = null
}
function applyAiCommandStatus(
  payload: FeishuAiCommandStatusResponse,
  options: { preserveDraft?: boolean } = {},
) {
  if (payload.token_status) aiTokenStatus.value = payload.token_status
  if (payload.capabilities) aiCapabilities.value = payload.capabilities
  if (!payload.config) return

  const nextWatchText = (payload.config.watch_chats ?? []).join(', ')
  const prev = aiServerSnapshot.value

  if (!options.preserveDraft || !prev) {
    Object.assign(aiEdit, payload.config)
    aiWatchChatsText.value = nextWatchText
    aiTupleConflict.value = null
  } else {
    const tupleDirty = (
      aiEdit.provider !== prev.provider
      || aiEdit.model !== prev.model
      || aiEdit.reasoning_effort !== prev.reasoning_effort
    )
    const serverTupleChanged = (
      payload.config.provider !== prev.provider
      || payload.config.model !== prev.model
      || payload.config.reasoning_effort !== prev.reasoning_effort
    )
    if (!tupleDirty) {
      aiEdit.provider = payload.config.provider
      aiEdit.model = payload.config.model
      aiEdit.reasoning_effort = payload.config.reasoning_effort
      aiTupleConflict.value = null
    } else if (serverTupleChanged) {
      aiTupleConflict.value = {
        provider: payload.config.provider,
        model: payload.config.model,
        reasoning_effort: payload.config.reasoning_effort,
      }
    }

    const scalarKeys: Array<Exclude<keyof FeishuAiCommandConfig, 'provider' | 'model' | 'reasoning_effort' | 'watch_chats'>> = [
      'enabled',
      'prefix',
      'owner_open_id',
      'app_id',
      'cwd',
      'poll_interval_seconds',
      'max_active_chats',
      'max_context_messages',
      'ack_emoji',
      'done_emoji',
      'footer',
      'reply_in_thread',
    ]
    for (const key of scalarKeys) {
      const currentVal = aiEdit[key]
      const prevVal = prev[key]
      const isIdentityKey = key === 'owner_open_id' || key === 'app_id'
      if (currentVal === prevVal || (isIdentityKey && !String(currentVal ?? '').trim())) {
        ;(aiEdit as Record<string, unknown>)[key] = payload.config[key]
      }
    }
    if (aiWatchChatsText.value === aiServerWatchChatsText.value) {
      aiWatchChatsText.value = nextWatchText
      aiEdit.watch_chats = [...(payload.config.watch_chats ?? [])]
    }
  }

  aiServerSnapshot.value = {
    ...payload.config,
    watch_chats: [...(payload.config.watch_chats ?? [])],
  }
  aiServerWatchChatsText.value = nextWatchText
}
function acceptServerAiTuple() {
  if (busy.value || closed || aiCommandLoading.value || !aiCommandLoaded.value || !aiTupleConflict.value) return
  aiEdit.provider = aiTupleConflict.value.provider
  aiEdit.model = aiTupleConflict.value.model
  aiEdit.reasoning_effort = aiTupleConflict.value.reasoning_effort
  aiTupleConflict.value = null
}
function onAiProviderChange(nextProvider: FeishuAiCommandProvider) {
  if (busy.value || closed || aiCommandLoading.value || !aiCommandLoaded.value) return
  aiEdit.provider = nextProvider
  aiTupleConflict.value = null
  const catalog = resolveAiModelOptionsForProvider(nextProvider)
  const legacySuggestions = aiCapabilities.value?.provider_models?.[nextProvider]
  const preferredId = nextProvider === 'codex' && aiCapabilities.value?.provider_model_options?.codex?.length
    ? catalog[0]?.provider_model_id || catalog[0]?.id
    : legacySuggestions?.[0]
  const preferredOpt = (preferredId
    ? catalog.find(opt => matchesAiModelOption(opt, preferredId))
    : undefined) ?? catalog[0] ?? null
  const supportedEfforts = resolveEffortsForModel(
    nextProvider,
    preferredId ?? preferredOpt?.id ?? aiEdit.model,
    preferredOpt,
  )
  let nextEffort: FeishuAiCommandReasoningEffort = aiEdit.reasoning_effort
  if (supportedEfforts.length === 0) {
    nextEffort = ''
  } else if (!nextEffort || !supportedEfforts.includes(nextEffort)) {
    const optDefault = preferredOpt?.default_reasoning_effort
    nextEffort = (
      (optDefault && supportedEfforts.includes(optDefault) ? optDefault : undefined)
      ?? (supportedEfforts.includes('high') ? 'high' : supportedEfforts[0])
    ) as FeishuAiCommandReasoningEffort
  }
  aiEdit.reasoning_effort = nextEffort
  if (preferredOpt) {
    aiEdit.model = resolveVariantModelId(nextProvider, preferredOpt, nextEffort)
  } else if (preferredId) {
    aiEdit.model = preferredId
  }
}
function onAiModelChange(nextModelValue: string) {
  if (busy.value || closed || aiCommandLoading.value || !aiCommandLoaded.value) return
  aiTupleConflict.value = null
  const clean = nextModelValue.trim()
  if (!clean) {
    aiEdit.model = ''
    const providerEfforts = resolveEffortsForModel(aiEdit.provider, '', null)
    if (providerEfforts.length === 0) {
      aiEdit.reasoning_effort = ''
    } else if (aiEdit.reasoning_effort && !providerEfforts.includes(aiEdit.reasoning_effort)) {
      aiEdit.reasoning_effort = (
        providerEfforts.includes('high') ? 'high' : providerEfforts[0]
      ) as FeishuAiCommandReasoningEffort
    }
    return
  }
  const matched = aiProviderModelCatalog.value.find(opt => matchesAiModelOption(opt, clean)) ?? null
  const supportedEfforts = resolveEffortsForModel(aiEdit.provider, clean, matched)
  let nextEffort: FeishuAiCommandReasoningEffort = aiEdit.reasoning_effort
  if (supportedEfforts.length === 0) {
    nextEffort = ''
  } else if (!nextEffort || !supportedEfforts.includes(nextEffort)) {
    const matchedVariantEffort = matched?.supported_reasoning_efforts?.find(
      eff => eff.provider_model_id === clean,
    )?.id
    const optDefault = matched?.default_reasoning_effort
    nextEffort = (
      (matchedVariantEffort && supportedEfforts.includes(matchedVariantEffort) ? matchedVariantEffort : undefined)
      ?? (optDefault && supportedEfforts.includes(optDefault) ? optDefault : undefined)
      ?? (supportedEfforts.includes('high') ? 'high' : supportedEfforts[0])
    ) as FeishuAiCommandReasoningEffort
  }
  aiEdit.reasoning_effort = nextEffort
  aiEdit.model = matched
    ? resolveVariantModelId(aiEdit.provider, matched, nextEffort)
    : clean
}
function onAiEffortChange(nextEffort: FeishuAiCommandReasoningEffort) {
  if (busy.value || closed || aiCommandLoading.value || !aiCommandLoaded.value) return
  aiTupleConflict.value = null
  aiEdit.reasoning_effort = nextEffort
  const matched = currentAiModelOption.value
  if (matched && (aiEdit.provider === 'maso' || aiEdit.provider === 'cursor')) {
    aiEdit.model = resolveVariantModelId(aiEdit.provider, matched, nextEffort)
  }
}
async function openAiCommand() {
  if (busy.value || closed) return
  const current = ++viewEpoch
  const loadGen = ++aiLoadGeneration
  const preserveExistingDraft = aiCommandLoaded.value
  fillSelection(null)
  aiCommandActive.value = true
  aiCommandLoadError.value = null
  error.value = null; success.value = null
  if (!preserveExistingDraft) {
    aiCommandLoading.value = true
  }
  try {
    const status = await loadFeishuAiCommand()
    if (closed || current !== viewEpoch || loadGen !== aiLoadGeneration) return
    aiCommandLoaded.value = true
    aiCommandLoadError.value = null
    applyAiCommandStatus(status, { preserveDraft: preserveExistingDraft })
  } catch (cause) {
    if (closed || current !== viewEpoch || loadGen !== aiLoadGeneration) return
    const reqErr = cause instanceof FeishuBotRequestError ? cause : null
    const msg = reqErr?.message ?? 'Failed to load /ai command settings.'
    if (!aiCommandLoaded.value) {
      aiCommandLoadError.value = msg
    }
    error.value = msg
  } finally {
    if (loadGen === aiLoadGeneration) aiCommandLoading.value = false
  }
}
async function retryLoadAiCommand() {
  if (busy.value || closed || aiCommandLoading.value) return
  await openAiCommand()
}
async function saveAiCommand() {
  if (busy.value || closed || aiCommandLoading.value || !aiCommandLoaded.value) return false
  const current = ++viewEpoch
  retireAiCommandLoad()
  busy.value = true; error.value = null; success.value = null
  const watchChats = aiWatchChatsText.value
    .split(/[\s,]+/)
    .map(item => item.trim())
    .filter(Boolean)
  try {
    const next = await updateFeishuAiCommand({
      enabled: aiEdit.enabled,
      prefix: aiEdit.prefix.trim() || '/ai',
      owner_open_id: aiEdit.owner_open_id.trim(),
      app_id: aiEdit.app_id.trim(),
      provider: aiEdit.provider,
      model: aiEdit.model.trim(),
      reasoning_effort: aiEdit.reasoning_effort,
      cwd: aiEdit.cwd.trim(),
      watch_chats: watchChats,
      poll_interval_seconds: Number(aiEdit.poll_interval_seconds) || 15,
      ack_emoji: aiEdit.ack_emoji.trim() || 'OnIt',
      done_emoji: aiEdit.done_emoji.trim() || 'DONE',
      footer: aiEdit.footer.trim() || '> 🤖 AI 代答，仅供参考',
      reply_in_thread: aiEdit.reply_in_thread,
    })
    if (closed || current !== viewEpoch) return false
    applyAiCommandStatus(next, { preserveDraft: false })
    success.value = '/ai command settings saved.'
    return true
  } catch (cause) {
    if (closed || current !== viewEpoch) return false
    const reqErr = cause instanceof FeishuBotRequestError ? cause : null
    error.value = reqErr?.message ?? 'Failed to save /ai command settings.'
    return false
  } finally {
    if (current === viewEpoch) busy.value = false
  }
}
async function importAiToken() {
  if (busy.value || closed || aiCommandLoading.value) return false
  const current = ++viewEpoch
  retireAiCommandLoad()
  busy.value = true; error.value = null; success.value = null
  try {
    const next = await importFeishuAiCommandToken({
      app_id: aiEdit.app_id.trim() || undefined,
      open_id: aiEdit.owner_open_id.trim() || undefined,
    })
    if (closed || current !== viewEpoch) return false
    applyAiCommandStatus(next, { preserveDraft: aiCommandLoaded.value })
    aiCommandLoaded.value = true
    aiCommandLoadError.value = null
    success.value = next.imported
      ? 'Imported user token from ~/.lark-cli.'
      : 'No matching ~/.lark-cli user token found.'
    return true
  } catch (cause) {
    if (closed || current !== viewEpoch) return false
    const reqErr = cause instanceof FeishuBotRequestError ? cause : null
    error.value = reqErr?.message ?? 'Failed to import ~/.lark-cli token.'
    return false
  } finally {
    if (current === viewEpoch) busy.value = false
  }
}
async function refresh() {
  if (busy.value || closed) return
  const current = ++viewEpoch
  const loadGen = aiCommandActive.value ? ++aiLoadGeneration : aiLoadGeneration
  if (!aiCommandActive.value) {
    retireAiCommandLoad()
  }
  error.value = null
  try {
    const value = await store.refresh()
    if (closed || current !== viewEpoch || !value) return
    if (selectedId.value) fillSelection(store.botById(selectedId.value))
    if (aiCommandActive.value) {
      try {
        const status = await loadFeishuAiCommand()
        if (!closed && current === viewEpoch && loadGen === aiLoadGeneration) {
          const preserve = aiCommandLoaded.value
          aiCommandLoaded.value = true
          aiCommandLoadError.value = null
          applyAiCommandStatus(status, { preserveDraft: preserve })
        }
      } catch (cause) {
        if (!closed && current === viewEpoch && loadGen === aiLoadGeneration && !aiCommandLoaded.value) {
          const reqErr = cause instanceof FeishuBotRequestError ? cause : null
          const msg = reqErr?.message ?? 'Failed to load /ai command settings.'
          aiCommandLoadError.value = msg
          error.value = msg
        }
      }
    }
  } finally {
    if (aiCommandActive.value && loadGen === aiLoadGeneration) {
      aiCommandLoading.value = false
    }
  }
}
function shouldReconcile(cause: unknown): boolean {
  if (!(cause instanceof FeishuBotRequestError)) return true
  if (cause.code === 'bot_operation_busy') return false
  return cause.status === 500 || cause.status === 409
}
async function mutate(
  run: () => Promise<FeishuBotPoolResponse>, message: string,
  onSuccess: () => void = () => undefined,
  options: { switchingMode?: boolean } = {},
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
    if (
      options.switchingMode
      && requestError?.status === 409
      && requestError.code !== 'bot_revision_conflict'
      && requestError.code !== 'bot_credentials_read_only'
      && requestError.code === null
    ) {
      error.value = 'This Bot has an active Feishu channel. Disconnect it before switching serving mode.'
    } else {
      error.value = requestError?.message ?? 'The Bot operation failed. Check the refreshed pool before trying again.'
    }
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
  const switchingMode = !!selected.value && editMode.value !== botMode(selected.value)
  const input = {
    name: editName.value.trim(),
    enabled: editEnabled.value,
    ...(switchingMode ? { mode: editMode.value } : {}),
    expected_revision: revision,
  }
  return mutate(() => updateFeishuBot(botId, input), 'Bot details saved.', () => undefined, { switchingMode })
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
  retireAiCommandLoad()
  stopConnectionRefresh()
  clearSecrets(); clearCreate()
}
function close() { clearLocalState(); emit('close') }
onMounted(async () => { dialog.value?.showModal(); await refresh() })
onUnmounted(() => { clearLocalState(); dialog.value?.close() })
</script>

<style scoped src="./feishu-bot-settings-dialog.css"></style>
