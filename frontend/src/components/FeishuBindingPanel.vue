<template>
  <div
    ref="root"
    class="feishu-binding"
  >
    <button
      type="button"
      class="trigger"
      :class="`is-${viewState}`"
      :aria-label="`Feishu connection settings · ${statusText}`"
      :title="`Feishu · ${statusText}`"
      data-testid="feishu-binding-trigger"
      aria-haspopup="dialog"
      :aria-controls="`feishu-binding-${props.tabId}`"
      :aria-expanded="open"
      @click="toggle"
    >
      <FeishuBrandMark
        class="trigger-icon"
      />
      <span
        class="status-dot"
        aria-hidden="true"
      />
    </button>

    <div
      v-if="open"
      :id="`feishu-binding-${props.tabId}`"
      class="panel"
      role="dialog"
      aria-label="Feishu connection"
      data-testid="feishu-binding-panel"
      @keydown.esc="close"
    >
      <header class="panel-header">
        <div class="panel-heading">
          <FeishuBrandMark
            class="panel-mark"
          />
          <div>
            <strong>Feishu connection</strong>
            <p>Connect this Chat to a shared Bot.</p>
          </div>
        </div>
        <button
          type="button"
          class="ch-btn ch-btn--icon ch-btn--sm ch-btn--ghost close-button"
          aria-label="Close"
          @click="close"
        >
          <svg
            viewBox="0 0 16 16"
            aria-hidden="true"
          ><path d="m4 4 8 8m0-8-8 8" /></svg>
        </button>
      </header>

      <div
        class="connection-status"
        role="status"
        :data-state="viewState"
        data-testid="feishu-binding-status"
      >
        <span
          class="connection-status-dot"
          aria-hidden="true"
        />
        <span>{{ statusText }}</span>
      </div>

      <p
        v-if="error"
        class="feedback feedback--error"
        role="alert"
      >
        {{ error }}
      </p>
      <p
        v-if="copyError"
        class="feedback feedback--error"
        role="alert"
      >
        {{ copyError }}
      </p>
      <div
        v-if="isLoading && !isHydrated"
        class="panel-loading"
        role="status"
      >
        <span
          class="panel-spinner"
          aria-hidden="true"
        />
        Loading Bots…
      </div>

      <template v-else-if="viewState === 'bound-current' && currentBot && binding">
        <section class="connected-card">
          <FeishuIdentityMark
            class="connected-card__icon"
            :avatar-url="activeAvatarUrl"
            :identity-key="activeAvatarKey || `feishu-brand:${props.tabId}`"
            data-testid="feishu-binding-identity"
          />
          <div>
            <strong>{{ currentBot.feishu_name || currentBot.name }}</strong>
            <p>{{ boundDescription }}</p>
          </div>
        </section>
        <template v-if="isPerChannelMode">
          <div
            class="channel-section"
            data-testid="feishu-binding-channels"
          >
            <div class="channel-section__header">
              <span class="field-label">Current channels</span>
              <span class="channel-section__count">{{ channelBindings.length }} / 8</span>
            </div>
            <ul
              v-if="channelBindings.length"
              class="channel-list"
              aria-label="Current channels"
              data-testid="feishu-binding-channel-list"
            >
              <li
                v-for="item in channelBindings"
                :key="item.pairing_id"
                class="channel-list__item"
                data-testid="feishu-binding-channel-item"
              >
                <span class="channel-list__source">{{ channelSourceLabel(item) }}</span>
                <span class="channel-list__target">Chat <code>{{ item.tab_id }}</code></span>
              </li>
            </ul>
          </div>
          <div class="per-channel-group-bind">
            <p class="selection-hint">
              {{ groupPathHint }}
            </p>
            <p
              v-if="groupsError"
              class="feedback feedback--error"
              role="alert"
              data-testid="feishu-binding-groups-error"
            >
              {{ groupsError }}
            </p>
            <div
              v-if="groupsLoading"
              class="panel-loading"
              role="status"
            >
              <span
                class="panel-spinner"
                aria-hidden="true"
              />
              Loading groups…
            </div>
            <template v-else-if="groups.length">
              <label class="field">
                <span class="field-label">Group</span>
                <select
                  class="ch-select"
                  aria-label="Group"
                  data-testid="feishu-binding-group-select"
                  :value="selectedChatId || ''"
                  :disabled="isMutating"
                  @change="chooseGroup"
                >
                  <option
                    value=""
                    disabled
                  >
                    Select a group
                  </option>
                  <option
                    v-for="chat in groups"
                    :key="chat.chat_id"
                    :value="chat.chat_id"
                  >
                    {{ groupOptionLabel(chat) }}
                  </option>
                </select>
              </label>
              <p
                class="group-warning"
                data-testid="feishu-binding-group-warning"
              >
                {{ groupWarningCopy }}
              </p>
              <button
                type="button"
                class="ch-btn ch-btn--primary primary-action"
                data-testid="feishu-binding-bind-group"
                :disabled="isMutating || !selectedChatId || !selectedBotReady"
                @click="submitGroupBind"
              >
                {{ isMutating ? 'Binding…' : 'Bind this group' }}
              </button>
              <button
                type="button"
                class="ch-btn ch-btn--sm ch-btn--ghost reload-action"
                data-testid="feishu-binding-reload-groups"
                :disabled="isMutating || !selectedBotReady || groupsLoading"
                @click="loadGroups"
              >
                Reload groups
              </button>
            </template>
            <template v-else>
              <button
                type="button"
                class="ch-btn ch-btn--primary primary-action"
                data-testid="feishu-binding-load-groups"
                :disabled="isMutating || !selectedBotReady || groupsLoading"
                @click="loadGroups"
              >
                {{ groupsLoaded || groupsError ? 'Reload groups' : 'Load groups' }}
              </button>
            </template>
          </div>
        </template>
        <template v-else>
          <dl class="connection-details">
            <div>
              <dt>Target</dt>
              <dd data-testid="feishu-binding-target">
                {{ boundTargetLabel }}
              </dd>
            </div>
            <div>
              <dt>Chat</dt>
              <dd>{{ binding.tab_id }}</dd>
            </div>
            <div>
              <dt>Connected</dt>
              <dd>{{ formatTime(binding.created_at) }}</dd>
            </div>
          </dl>
          <p
            v-if="boundIsTopic"
            class="selection-hint"
            data-testid="feishu-binding-topic-hint"
          >
            Replies stay inside the group topic.
          </p>
        </template>
        <template v-if="confirmDisconnect">
          <div class="confirm-card">
            <strong>Disconnect this Chat?</strong>
            <p>Future Bot messages stop here. Existing source labels remain.</p>
          </div>
          <div class="panel-actions">
            <button
              type="button"
              class="ch-btn ch-btn--danger"
              :disabled="isMutating"
              @click="doDisconnect"
            >
              Confirm disconnect
            </button>
            <button
              type="button"
              class="ch-btn"
              :disabled="isMutating"
              @click="confirmDisconnect = false"
            >
              Cancel
            </button>
          </div>
        </template>
        <button
          v-else
          type="button"
          class="ch-btn ch-btn--danger disconnect-button"
          @click="confirmDisconnect = true"
        >
          Disconnect Feishu
        </button>
      </template>

      <template v-else>
        <label class="field">
          <span class="field-label">Bot</span>
          <select
            class="ch-select"
            aria-label="Bot"
            :value="selectedBotId || ''"
            :disabled="isMutating"
            @change="choose"
          >
            <option
              value=""
              disabled
            >
              Select a Bot
            </option>
            <option
              v-for="item in bots"
              :key="item.bot.bot_id"
              :value="item.bot.bot_id"
              :disabled="item.disabled || !item.bot.enabled || !item.bot.configured"
            >
              {{ item.bot.name }} — {{ item.disabled ? `In use by Chat ${item.bot.binding?.tab_id}` : item.bot.enabled && item.bot.configured ? 'Available' : 'Unavailable' }}
            </option>
          </select>
        </label>

        <template v-if="viewState === 'pending' && pendingCode">
          <div class="step-copy">
            <span class="step-number">1</span>
            <p>Send this one-time code to <strong>{{ selectedBot?.name }}</strong> in a Feishu direct chat.</p>
          </div>
          <div class="code-box">
            <output
              aria-label="Feishu binding code"
              data-testid="feishu-binding-code"
            >{{ pendingCode.code }}</output>
            <button
              type="button"
              class="ch-btn ch-btn--sm"
              @click="copy(pendingCode.code, 'Code copied.')"
            >
              Copy code
            </button>
          </div>
          <small class="expiry">Expires {{ formatTime(pendingCode.expires_at) }} · Waiting over the Bot's WebSocket connection</small>
        </template>

        <template v-else-if="viewState === 'claimed' && claim">
          <div class="step-copy">
            <span class="step-number">2</span>
            <p>Enter the six-character confirmation word returned in Feishu. Hub never displays or prefills it.</p>
          </div>
          <label class="field">
            <span class="field-label">Confirmation word</span>
            <input
              v-model="confirmWord"
              class="ch-input confirmation-input"
              :disabled="isMutating"
              maxlength="6"
              autocomplete="off"
              spellcheck="false"
            >
          </label>
          <button
            type="button"
            class="ch-btn ch-btn--primary primary-action"
            :disabled="isMutating || confirmWord.trim().length !== 6"
            @click="activate"
          >
            {{ isMutating ? 'Activating…' : 'Activate pairing' }}
          </button>
        </template>

        <template v-else-if="viewState === 'error'">
          <a
            v-if="needsLogin"
            class="ch-btn ch-btn--primary primary-action"
            href="/api/auth/login"
          >Sign in</a>
          <button
            v-else
            type="button"
            class="ch-btn primary-action"
            :disabled="isLoading"
            @click="refresh"
          >
            Reload Bots
          </button>
        </template>

        <template v-else>
          <template v-if="isPerChannelMode">
            <div
              class="channel-section"
              data-testid="feishu-binding-channels"
            >
              <div class="channel-section__header">
                <span class="field-label">Current channels</span>
                <span class="channel-section__count">{{ channelBindings.length }} / 8</span>
              </div>
              <ul
                v-if="channelBindings.length"
                class="channel-list"
                aria-label="Current channels"
                data-testid="feishu-binding-channel-list"
              >
                <li
                  v-for="item in channelBindings"
                  :key="item.pairing_id"
                  class="channel-list__item"
                  data-testid="feishu-binding-channel-item"
                >
                  <span class="channel-list__source">{{ channelSourceLabel(item) }}</span>
                  <span class="channel-list__target">Chat <code>{{ item.tab_id }}</code></span>
                </li>
              </ul>
              <p
                v-else
                class="selection-hint"
                data-testid="feishu-binding-channel-empty"
              >
                No active channels yet. Direct messages open their own Chat automatically.
              </p>
            </div>
          </template>
          <div
            v-else
            class="path-switch"
            role="group"
            aria-label="Connection type"
          >
            <button
              type="button"
              class="ch-btn path-switch__option"
              :class="{ 'is-selected': connectMode === 'private' }"
              :aria-pressed="connectMode === 'private'"
              data-testid="feishu-binding-path-private"
              @click="connectMode = 'private'"
            >
              Private chat
            </button>
            <button
              type="button"
              class="ch-btn path-switch__option"
              :class="{ 'is-selected': connectMode === 'group' }"
              :aria-pressed="connectMode === 'group'"
              data-testid="feishu-binding-path-group"
              @click="connectMode = 'group'"
            >
              Group chat
            </button>
          </div>

          <template v-if="!isPerChannelMode && connectMode === 'private'">
            <p class="selection-hint">
              Choose an available Bot to route its private conversation into this Chat.
            </p>
            <button
              type="button"
              class="ch-btn ch-btn--primary primary-action"
              :disabled="isMutating || !selectedBot || !!selectedBot.binding || !selectedBot.enabled || !selectedBot.configured"
              @click="generateCode"
            >
              {{ isMutating ? 'Generating…' : 'Generate pairing code' }}
            </button>
          </template>

          <template v-else>
            <p class="selection-hint">
              {{ groupPathHint }}
            </p>
            <p
              v-if="groupsError"
              class="feedback feedback--error"
              role="alert"
              data-testid="feishu-binding-groups-error"
            >
              {{ groupsError }}
            </p>
            <div
              v-if="groupsLoading"
              class="panel-loading"
              role="status"
            >
              <span
                class="panel-spinner"
                aria-hidden="true"
              />
              Loading groups…
            </div>
            <template v-else-if="groups.length">
              <label class="field">
                <span class="field-label">Group</span>
                <select
                  class="ch-select"
                  aria-label="Group"
                  data-testid="feishu-binding-group-select"
                  :value="selectedChatId || ''"
                  :disabled="isMutating"
                  @change="chooseGroup"
                >
                  <option
                    value=""
                    disabled
                  >
                    Select a group
                  </option>
                  <option
                    v-for="chat in groups"
                    :key="chat.chat_id"
                    :value="chat.chat_id"
                  >
                    {{ groupOptionLabel(chat) }}
                  </option>
                </select>
              </label>
              <p
                class="group-warning"
                data-testid="feishu-binding-group-warning"
              >
                {{ groupWarningCopy }}
              </p>
              <button
                type="button"
                class="ch-btn ch-btn--primary primary-action"
                data-testid="feishu-binding-bind-group"
                :disabled="isMutating || !selectedChatId || !selectedBotReady"
                @click="submitGroupBind"
              >
                {{ isMutating ? 'Binding…' : 'Bind this group' }}
              </button>
              <button
                type="button"
                class="ch-btn ch-btn--sm ch-btn--ghost reload-action"
                data-testid="feishu-binding-reload-groups"
                :disabled="isMutating || !selectedBotReady || groupsLoading"
                @click="loadGroups"
              >
                Reload groups
              </button>
            </template>
            <template v-else>
              <button
                type="button"
                class="ch-btn ch-btn--primary primary-action"
                data-testid="feishu-binding-load-groups"
                :disabled="isMutating || !selectedBotReady || groupsLoading"
                @click="loadGroups"
              >
                {{ groupsLoaded || groupsError ? 'Reload groups' : 'Load groups' }}
              </button>
            </template>
          </template>
        </template>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, toRef, watch } from 'vue'
import FeishuBrandMark from '@/components/FeishuBrandMark.vue'
import FeishuIdentityMark from '@/components/FeishuIdentityMark.vue'
import { useFeishuBinding } from '@/composables/useFeishuBinding'
import { trustedFeishuAvatarUrl } from '@/utils/feishuIdentity'
import { writeClipboard } from '@/utils/clipboard'
import { botBindings, botMode, type FeishuActiveBinding, type FeishuBotChat } from '@/utils/feishuBotConfig'

const props = defineProps<{ tabId: string }>()
const root = ref<HTMLElement | null>(null)
const open = ref(false)
const copyError = ref<string | null>(null)
const confirmDisconnect = ref(false)
const connectMode = ref<'private' | 'group'>('private')
const selectedChatId = ref<string | null>(null)
const {
  bots, binding, bindings, mode, currentBot, selectedBotId, selectedBot, pendingCode, claim,
  confirmWord, error, needsLogin, isHydrated, isLoading, isMutating, viewState, statusText, groups,
  groupsLoaded, groupsLoading, groupsError, refresh, selectBot, generateCode, activate, bindGroup,
  loadGroups, ensureGroups, disconnect, resume, pause,
} = useFeishuBinding(toRef(props, 'tabId'))
const isPerChannelMode = computed(() => mode.value === 'per_channel')
const channelBindings = computed(() => bindings.value)
const activeAvatarUrl = computed(() => {
  if (viewState.value !== 'bound-current') return null
  return trustedFeishuAvatarUrl(currentBot.value?.feishu_avatar_url)
})
const activeAvatarKey = computed(() => {
  const botId = currentBot.value?.bot_id
  const avatarUrl = activeAvatarUrl.value
  if (!botId || !avatarUrl) return null
  return `${props.tabId}\n${botId}\n${avatarUrl}`
})
const isGroupBinding = computed(() => binding.value?.chat_type === 'group')
const boundGroup = computed(() => {
  const chatId = binding.value?.chat_id
  return chatId ? groups.value.find(chat => chat.chat_id === chatId) ?? null : null
})
const boundTargetLabel = computed(() => {
  if (!isGroupBinding.value) return 'Private chat'
  const chatId = binding.value?.chat_id
  // The binding records the group name; the freshly loaded list is the fallback
  // for a binding created before that name was stored.
  const name = binding.value?.chat_name || boundGroup.value?.name
    || chatId || 'unnamed group'
  return `Group · ${name}`
})
const boundDescription = computed(() => {
  if (isPerChannelMode.value) return 'Each Feishu conversation gets its own Chat.'
  return isGroupBinding.value
    ? 'Group messages from this Bot reach this Chat.'
    : 'New private messages enter this Chat.'
})
const boundIsTopic = computed(() => isGroupBinding.value && (boundGroup.value?.is_topic_mode
  ?? boundGroup.value?.chat_mode === 'topic'))
// Picking a group needs the same preconditions as generating a pairing code:
// a usable Bot that can still accept a group binding in its serving mode.
const selectedBotReady = computed(() => {
  const bot = selectedBot.value ?? currentBot.value
  if (!bot || !bot.enabled || !bot.configured) return false
  if (botMode(bot) === 'per_channel') return botBindings(bot).length < 8
  return !bot.binding && botBindings(bot).length === 0
})
const groupPathHint = computed(() => {
  if (groupsLoaded.value && groups.value.length === 0) {
    return 'This Bot is not in any group yet. Add the Bot to a group in Feishu, then reload.'
  }
  if (isPerChannelMode.value) {
    return 'Pick a group this Bot can see. Binding it creates a new Chat for that group.'
  }
  return 'Pick a group this Bot can see, then bind it to this Chat.'
})
const groupWarningCopy = computed(() => (isPerChannelMode.value
  ? 'Binding this group creates a new Chat. Anyone in the group can talk to it.'
  : 'Anyone in this group can talk to this Chat.'))
function channelSourceLabel(item: FeishuActiveBinding): string {
  if (item.chat_type === 'group') {
    const groupName = item.chat_name
      || (item.chat_id ? groups.value.find(chat => chat.chat_id === item.chat_id)?.name : null)
      || item.chat_id
      || 'unnamed group'
    return `Group · ${groupName}`
  }
  const senderTag = item.chat_name || item.chat_id || item.pairing_id
  return senderTag ? `DM · ${senderTag}` : 'Private chat'
}
function groupOptionLabel(chat: FeishuBotChat): string {
  const name = chat.name || chat.chat_id
  return (chat.is_topic_mode ?? chat.chat_mode === 'topic') ? `${name} · topic group` : name
}
// The chosen group belongs to one Bot: switching Bots or losing the list
// must drop it, or "Bind this group" would submit a group the user no longer
// sees. Watch the stable Bot id, not the Bot object: the pool snapshot is
// rebuilt on every poll, so an object watcher would clear a half-made choice.
watch(selectedBotId, () => { selectedChatId.value = null })
watch(groups, value => {
  if (selectedChatId.value && !value.some(chat => chat.chat_id === selectedChatId.value)) {
    selectedChatId.value = null
  }
})

function chooseGroup(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  selectedChatId.value = value || null
}
async function submitGroupBind() {
  if (await bindGroup(selectedChatId.value ?? '')) selectedChatId.value = null
}
function choose(event: Event) { selectBot((event.target as HTMLSelectElement).value) }
function toggle() { if (open.value) close(); else open.value = true }
function close() {
  open.value = false
  confirmDisconnect.value = false
  copyError.value = null
  confirmWord.value = ''
}
async function doDisconnect() { if (await disconnect()) confirmDisconnect.value = false }
async function copy(value: string, message: string) {
  copyError.value = null
  try {
    await writeClipboard(value)
    void message
  } catch {
    copyError.value = 'Could not copy. Select and copy the value manually.'
  }
}
function formatTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}
function outside(event: PointerEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) close()
}

onMounted(() => {
  document.addEventListener('pointerdown', outside)
  void resume()
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', outside)
  pause()
})
onActivated(() => { void resume() })
onDeactivated(() => { close(); pause() })
watch(() => props.tabId, close, { flush: 'sync' })
// Fetch the Bot's groups only when they are needed: the group path is open, or
// a group binding has to be named. ensureGroups never polls and never retries a
// failed read on its own; "Reload groups" stays the user's decision.
watch([open, viewState, connectMode, selectedBotId, groupsLoaded, isPerChannelMode], () => {
  if (!open.value) return
  if (viewState.value === 'bound-current') {
    if (isGroupBinding.value || isPerChannelMode.value) ensureGroups()
    return
  }
  if (viewState.value === 'unbound' && (connectMode.value === 'group' || isPerChannelMode.value)) ensureGroups()
}, { flush: 'post' })
</script>

<style scoped>
.feishu-binding {
  position: relative;
  display: flex;
  align-items: center;
  height: var(--pane-chrome-control-size, 24px);
  flex: 0 0 auto;
}

.trigger {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--pane-chrome-control-size, 24px);
  height: var(--pane-chrome-control-size, 24px);
  padding: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text-muted);
  box-shadow: 0 1px 4px var(--ch-shadow-color-soft);
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast), box-shadow var(--ch-motion-fast);
}

.trigger:hover,
.trigger[aria-expanded='true'] {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.trigger:focus-visible {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-ring);
}

.trigger-icon { width: 14px; height: 14px; }
.status-dot {
  position: absolute;
  top: 3px;
  right: 3px;
  width: 5px;
  height: 5px;
  border: 1px solid var(--ch-color-surface-raised);
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}
.trigger.is-bound-current .status-dot { background: var(--ch-color-success); }
.trigger.is-pending .status-dot,
.trigger.is-claimed .status-dot { background: var(--ch-color-warning); }
.trigger.is-error .status-dot { background: var(--ch-color-danger); }

.panel {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: min(380px, calc(100vw - 28px));
  max-height: calc(100dvh - 68px);
  padding: 16px;
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-glass);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-popover);
  backdrop-filter: blur(16px);
}

.panel-header,
.panel-heading,
.panel-actions,
.code-box,
.step-copy,
.connected-card { display: flex; }
.panel-header { align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.panel-heading { align-items: center; gap: 10px; min-width: 0; }
.panel-heading strong { display: block; color: var(--ch-color-text-strong); font-size: var(--ch-font-size-lg); font-weight: 600; }
.panel-heading p { margin: 2px 0 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-xs); }
.panel-mark {
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  color: var(--ch-color-text-muted);
}
.close-button svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }

.connection-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 26px;
  margin-bottom: 14px;
  padding: 0 9px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
}
.connection-status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ch-color-text-subtle); }
.connection-status[data-state='bound-current'] .connection-status-dot { background: var(--ch-color-success); }
.connection-status[data-state='pending'] .connection-status-dot,
.connection-status[data-state='claimed'] .connection-status-dot { background: var(--ch-color-warning); }
.connection-status[data-state='error'] .connection-status-dot { background: var(--ch-color-danger); }

.feedback,
.confirm-card { margin: 0 0 12px; padding: 10px 12px; border-radius: var(--ch-radius-md); font-size: var(--ch-font-size-sm); line-height: 1.45; }
.feedback--error { border: 1px solid var(--ch-color-danger-border); background: var(--ch-color-danger-bg); color: var(--ch-color-danger-text); }
.panel-loading { display: flex; align-items: center; gap: 8px; padding: 18px 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-sm); }
.panel-spinner { width: 14px; height: 14px; border: 2px solid var(--ch-color-border); border-top-color: var(--ch-color-accent); border-radius: 50%; animation: feishu-spin .8s linear infinite; }
@keyframes feishu-spin { to { transform: rotate(360deg); } }

.field { display: grid; gap: 6px; margin: 0 0 14px; }
.path-switch {
  display: flex;
  gap: 4px;
  margin: 0 0 12px;
  padding: 3px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
}
.path-switch .path-switch__option {
  height: 28px;
  flex: 1 1 0;
  padding: 0 10px;
  border-color: transparent;
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
}
.path-switch .path-switch__option:hover:not(:disabled) {
  border-color: transparent;
  background: transparent;
  color: var(--ch-color-text);
}
.path-switch .path-switch__option.is-selected {
  border-color: var(--ch-color-border-strong);
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text-strong);
  font-weight: 600;
  box-shadow: 0 1px 2px var(--ch-shadow-color-soft);
}
.group-warning {
  margin: 0 0 14px;
  padding: 9px 12px;
  border: 1px solid color-mix(in srgb, var(--ch-color-warning-strong) 45%, var(--ch-color-border));
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-warning-bg);
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
  line-height: 1.45;
}
.field-label { color: var(--ch-color-text-muted); font-size: var(--ch-font-size-sm); font-weight: 500; }
.selection-hint,
.step-copy p,
.connected-card p,
.confirm-card p { margin: 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-sm); line-height: 1.5; }
.selection-hint { margin-bottom: 14px; }
.primary-action,
.disconnect-button { width: 100%; }
.reload-action { width: 100%; margin-top: 8px; }

.step-copy { align-items: flex-start; gap: 9px; margin-bottom: 10px; }
.step-number {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex: 0 0 20px;
  border-radius: 50%;
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 700;
}
.code-box { align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; padding: 12px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-md); background: var(--ch-color-surface-sunken); }
.code-box output { color: var(--ch-color-text-strong); font: 700 19px/1 var(--ch-font-mono); letter-spacing: .08em; }
.expiry { display: block; margin-bottom: 14px; color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.confirmation-input { text-transform: uppercase; font-family: var(--ch-font-mono); font-weight: 700; letter-spacing: .14em; }

.connected-card { align-items: center; gap: 10px; margin-bottom: 14px; padding: 12px; border: 1px solid color-mix(in srgb, var(--ch-color-success) 35%, var(--ch-color-border)); border-radius: var(--ch-radius-md); background: var(--ch-color-success-bg); }
.connected-card__icon { width: 28px; height: 28px; flex: 0 0 28px; border-radius: var(--ch-radius-md); background: var(--ch-color-surface-raised); color: var(--ch-color-text-muted); }
.connected-card strong { display: block; margin-bottom: 2px; color: var(--ch-color-text-strong); font-size: var(--ch-font-size-sm); }
.connection-details { display: grid; gap: 8px; margin: 0 0 14px; padding: 0 2px; }
.connection-details div { display: grid; grid-template-columns: 82px minmax(0, 1fr); gap: 8px; }
.connection-details dt { color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.connection-details dd { min-width: 0; margin: 0; overflow-wrap: anywhere; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-xs); }
.channel-section { display: grid; gap: 6px; margin: 0 0 14px; }
.channel-section__header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.channel-section__count { color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.channel-list {
  display: grid;
  gap: 5px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.channel-list__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 10px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  font-size: var(--ch-font-size-xs);
}
.channel-list__source { min-width: 0; overflow: hidden; color: var(--ch-color-text); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.channel-list__target { flex: 0 0 auto; color: var(--ch-color-text-muted); }
.channel-list__target code { color: var(--ch-color-text-code); font-family: var(--ch-font-mono); }
.per-channel-group-bind { margin-bottom: 14px; }
.confirm-card { border: 1px solid var(--ch-color-danger-border); background: var(--ch-color-danger-bg); }
.confirm-card strong { display: block; margin-bottom: 3px; color: var(--ch-color-danger-text); font-size: var(--ch-font-size-sm); }
.panel-actions { justify-content: flex-end; gap: 8px; }

@media (max-width: 640px) {
  .panel { width: calc(100vw - 16px); max-height: calc(100dvh - 60px); padding: 14px; }
  .panel .ch-btn,
  .panel .ch-input,
  .panel .ch-select { min-height: 44px; }
  .panel .ch-btn--icon { width: 44px; padding: 0; }
  .code-box { align-items: stretch; flex-direction: column; }
  .code-box .ch-btn { width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .trigger { transition: none; }
  .panel-spinner { animation: none; }
}
</style>
