<template>
  <div
    ref="root"
    class="feishu-binding"
  >
    <button
      class="trigger"
      :class="`is-${viewState}`"
      aria-label="Feishu connection settings"
      data-testid="feishu-binding-trigger"
      aria-haspopup="dialog"
      :aria-controls="`feishu-binding-${props.tabId}`"
      :aria-expanded="open"
      @click="toggle"
    >
      <span>飞</span> Feishu <i />
    </button>
    <div
      v-if="open"
      :id="`feishu-binding-${props.tabId}`"
      class="panel"
      role="dialog"
      aria-label="Feishu connection"
      @keydown.esc="close"
    >
      <header>
        <div><strong>Feishu connection</strong><p>Pair this Chat with one shared Bot.</p></div><button
          aria-label="Close"
          @click="close"
        >
          ×
        </button>
      </header>
      <p
        role="status"
        :data-state="viewState"
        data-testid="feishu-binding-status"
      >
        {{ statusText }}
      </p>
      <p
        v-if="error"
        class="error"
        role="alert"
      >
        {{ error }}
      </p><p
        v-if="copyError"
        class="error"
        role="alert"
      >
        {{ copyError }}
      </p>
      <p v-if="isLoading&&!isHydrated">
        Loading Bots…
      </p>

      <template v-else-if="viewState==='bound-current'&&currentBot&&binding">
        <p>New messages from <strong>{{ currentBot.name }}</strong> enter this Chat.</p>
        <dl><div><dt>Chat</dt><dd>{{ binding.tab_id }}</dd></div><div><dt>Connected</dt><dd>{{ formatTime(binding.created_at) }}</dd></div></dl>
        <template v-if="confirmDisconnect">
          <p>Disconnecting stops future Bot messages. Historical source labels remain.</p><button
            :disabled="isMutating"
            @click="doDisconnect"
          >
            Confirm disconnect
          </button><button @click="confirmDisconnect=false">
            Cancel
          </button>
        </template>
        <button
          v-else
          class="danger"
          @click="confirmDisconnect=true"
        >
          Disconnect Feishu
        </button>
      </template>

      <template v-else>
        <label>Bot
          <select
            aria-label="Bot"
            :value="selectedBotId||''"
            :disabled="isMutating"
            @change="choose"
          >
            <option
              value=""
              disabled
            >Select a Bot</option>
            <option
              v-for="item in bots"
              :key="item.bot.bot_id"
              :value="item.bot.bot_id"
              :disabled="item.disabled||!item.bot.enabled||!item.bot.configured"
            >
              {{ item.bot.name }} — {{ item.disabled ? `In use by Chat ${item.bot.binding?.tab_id}` : item.bot.enabled&&item.bot.configured ? 'Available' : 'Unavailable' }}
            </option>
          </select>
        </label>

        <template v-if="viewState==='pending'&&pendingCode">
          <p>Send this one-time code to <strong>{{ selectedBot?.name }}</strong> in a Feishu direct chat.</p>
          <div class="code">
            <output
              aria-label="Feishu binding code"
              data-testid="feishu-binding-code"
            >{{ pendingCode.code }}</output><button @click="copy(pendingCode.code,'Code copied.')">
              Copy code
            </button>
          </div>
          <div class="callback">
            <code>{{ pendingCode.event_url }}</code><button @click="copy(pendingCode.event_url,'Callback URL copied.')">
              Copy callback
            </button>
          </div>
          <small>Expires {{ formatTime(pendingCode.expires_at) }}. Waiting for your Bot conversation…</small>
        </template>

        <template v-else-if="viewState==='claimed'&&claim">
          <p>Read the six-character confirmation word from your Feishu conversation and enter it here. Hub never displays or prefills it.</p>
          <label>Confirmation word<input
            v-model="confirmWord"
            :disabled="isMutating"
            maxlength="6"
            autocomplete="off"
            spellcheck="false"
          ></label>
          <button
            :disabled="isMutating||confirmWord.trim().length!==6"
            @click="activate"
          >
            {{ isMutating?'Activating…':'Activate pairing' }}
          </button>
        </template>

        <template v-else-if="viewState==='error'">
          <a
            v-if="needsLogin"
            href="/api/auth/login"
          >Sign in</a><button
            v-else
            :disabled="isLoading"
            @click="refresh"
          >
            Reload Bots
          </button>
        </template>

        <button
          v-else
          :disabled="isMutating||!selectedBot||!!selectedBot.binding||!selectedBot.enabled||!selectedBot.configured"
          @click="generateCode"
        >
          {{ isMutating?'Generating…':'Generate pairing code' }}
        </button>
      </template>
    </div>
  </div>
</template>
<script setup lang="ts">
import { onActivated,onDeactivated,onMounted,onUnmounted,ref,toRef } from 'vue'
import { useFeishuBinding } from '@/composables/useFeishuBinding'
import { writeClipboard } from '@/utils/clipboard'
const props=defineProps<{tabId:string}>();const root=ref<HTMLElement|null>(null),open=ref(false),copyError=ref<string|null>(null),confirmDisconnect=ref(false)
const {bots,binding,currentBot,selectedBotId,selectedBot,pendingCode,claim,confirmWord,error,needsLogin,isHydrated,isLoading,isMutating,viewState,statusText,refresh,selectBot,generateCode,activate,disconnect,resume,pause}=useFeishuBinding(toRef(props,'tabId'))
function choose(e:Event){selectBot((e.target as HTMLSelectElement).value)}
function toggle() { if (open.value) close(); else open.value = true }
function close() { open.value = false; confirmDisconnect.value = false; copyError.value = null; confirmWord.value = '' }
async function doDisconnect(){if(await disconnect())confirmDisconnect.value=false}
async function copy(value:string,message:string){copyError.value=null;try{await writeClipboard(value);void message}catch{copyError.value='Could not copy. Select and copy the value manually.'}}
function formatTime(value:string){const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString()}
function outside(e:PointerEvent){if(open.value&&root.value&&!root.value.contains(e.target as Node))close()}
onMounted(()=>{document.addEventListener('pointerdown',outside);void resume()});onUnmounted(()=>{document.removeEventListener('pointerdown',outside);pause()});onActivated(() => { void resume() });onDeactivated(()=>{close();pause()})
</script>
<style scoped>
.feishu-binding{position:relative}.trigger{display:flex;align-items:center;gap:6px}.trigger i{width:7px;height:7px;border-radius:50%;background:var(--ch-color-text-muted)}.trigger.is-bound-current i{background:var(--ch-color-success)}.trigger.is-pending i,.trigger.is-claimed i{background:var(--ch-color-warning)}.trigger.is-error i{background:var(--ch-color-danger)}.panel{position:absolute;right:0;bottom:calc(100% + 8px);z-index:20;width:min(390px,calc(100vw - 24px));padding:14px;background:var(--ch-color-surface);border:1px solid var(--ch-color-border);border-radius:var(--ch-radius-md);box-shadow:var(--ch-shadow-popover)}header,.code,.callback{display:flex;justify-content:space-between;gap:8px}header{align-items:flex-start}header>button{flex:0 0 auto;min-width:44px;min-height:44px}p{line-height:1.4}label{display:grid;gap:5px;margin:10px 0}select,input{width:100%;min-height:38px}.code output{font:700 22px monospace;letter-spacing:.12em}.callback code{overflow-wrap:anywhere}.error,.danger{color:var(--ch-color-danger)}dl div{display:grid;grid-template-columns:90px 1fr}dd{margin:0}@media(max-width:640px){button,select,input{min-height:44px}}
</style>
