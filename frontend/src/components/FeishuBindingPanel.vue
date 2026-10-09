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
      <svg
        class="trigger-icon"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden="true"
      >
        <path
          d="m9.25 14.75-1.4 1.4a3.2 3.2 0 0 1-4.5-4.5l2.3-2.3a3.2 3.2 0 0 1 4.5 0m4.6-.1 1.4-1.4a3.2 3.2 0 0 1 4.5 4.5l-2.3 2.3a3.2 3.2 0 0 1-4.5 0m-3.7-3.7 3.7-3.7"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
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
          <span
            class="panel-mark"
            aria-hidden="true"
          >飞</span>
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
          <div
            class="connected-card__icon"
            aria-hidden="true"
          >
            ✓
          </div>
          <div>
            <strong>{{ currentBot.name }}</strong>
            <p>New private messages enter this Chat.</p>
          </div>
        </section>
        <dl class="connection-details">
          <div>
            <dt>Chat</dt>
            <dd>{{ binding.tab_id }}</dd>
          </div>
          <div>
            <dt>Connected</dt>
            <dd>{{ formatTime(binding.created_at) }}</dd>
          </div>
        </dl>
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
          <div class="callback-box">
            <div>
              <span>Callback URL</span>
              <code>{{ pendingCode.event_url }}</code>
            </div>
            <button
              type="button"
              class="ch-btn ch-btn--icon ch-btn--sm ch-btn--ghost"
              aria-label="Copy callback URL"
              @click="copy(pendingCode.event_url, 'Callback URL copied.')"
            >
              <svg
                viewBox="0 0 16 16"
                aria-hidden="true"
              ><rect
                x="5.25"
                y="5.25"
                width="7.5"
                height="7.5"
                rx="1.5"
              /><path d="M10.75 5.25v-2h-7.5v7.5h2" /></svg>
            </button>
          </div>
          <small class="expiry">Expires {{ formatTime(pendingCode.expires_at) }} · Waiting for the Bot conversation</small>
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
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onActivated, onDeactivated, onMounted, onUnmounted, ref, toRef, watch } from 'vue'
import { useFeishuBinding } from '@/composables/useFeishuBinding'
import { writeClipboard } from '@/utils/clipboard'

const props = defineProps<{ tabId: string }>()
const root = ref<HTMLElement | null>(null)
const open = ref(false)
const copyError = ref<string | null>(null)
const confirmDisconnect = ref(false)
const {
  bots, binding, currentBot, selectedBotId, selectedBot, pendingCode, claim, confirmWord, error,
  needsLogin, isHydrated, isLoading, isMutating, viewState, statusText, refresh, selectBot,
  generateCode, activate, disconnect, resume, pause,
} = useFeishuBinding(toRef(props, 'tabId'))

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
.callback-box,
.step-copy,
.connected-card { display: flex; }
.panel-header { align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.panel-heading { align-items: center; gap: 10px; min-width: 0; }
.panel-heading strong { display: block; color: var(--ch-color-text-strong); font-size: var(--ch-font-size-lg); font-weight: 600; }
.panel-heading p { margin: 2px 0 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-xs); }
.panel-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 14px;
  font-weight: 700;
}
.close-button svg,
.callback-box button svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }

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
.field-label { color: var(--ch-color-text-muted); font-size: var(--ch-font-size-sm); font-weight: 500; }
.selection-hint,
.step-copy p,
.connected-card p,
.confirm-card p { margin: 0; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-sm); line-height: 1.5; }
.selection-hint { margin-bottom: 14px; }
.primary-action,
.disconnect-button { width: 100%; }

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
.callback-box { align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; padding: 8px 9px 8px 11px; border-radius: var(--ch-radius-md); background: var(--ch-color-chip-bg-muted); }
.callback-box > div { min-width: 0; }
.callback-box span { display: block; margin-bottom: 2px; color: var(--ch-color-text-subtle); font-size: 10px; text-transform: uppercase; letter-spacing: .06em; }
.callback-box code { display: block; overflow: hidden; color: var(--ch-color-text-muted); font: 10px/1.35 var(--ch-font-mono); text-overflow: ellipsis; white-space: nowrap; }
.expiry { display: block; margin-bottom: 14px; color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.confirmation-input { text-transform: uppercase; font-family: var(--ch-font-mono); font-weight: 700; letter-spacing: .14em; }

.connected-card { align-items: center; gap: 10px; margin-bottom: 14px; padding: 12px; border: 1px solid color-mix(in srgb, var(--ch-color-success) 35%, var(--ch-color-border)); border-radius: var(--ch-radius-md); background: var(--ch-color-success-bg); }
.connected-card__icon { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; flex: 0 0 24px; border-radius: 50%; background: var(--ch-color-success); color: var(--ch-color-text-inverse); font-size: 12px; font-weight: 700; }
.connected-card strong { display: block; margin-bottom: 2px; color: var(--ch-color-text-strong); font-size: var(--ch-font-size-sm); }
.connection-details { display: grid; gap: 8px; margin: 0 0 14px; padding: 0 2px; }
.connection-details div { display: grid; grid-template-columns: 82px minmax(0, 1fr); gap: 8px; }
.connection-details dt { color: var(--ch-color-text-subtle); font-size: var(--ch-font-size-xs); }
.connection-details dd { min-width: 0; margin: 0; overflow-wrap: anywhere; color: var(--ch-color-text-muted); font-size: var(--ch-font-size-xs); }
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
