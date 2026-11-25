<template>
  <div
    class="feishu-realtime-control"
    data-testid="feishu-realtime-control"
  >
    <div class="feishu-realtime-control__row">
      <label>
        <input
          v-if="!summary"
          type="checkbox"
          :checked="status?.effective_enabled ?? false"
          :disabled="!status || busy || !!error || status.env_override !== null"
          @change="toggle"
        >
        <span>{{ summary ? 'Feishu WebSocket' : 'Feishu realtime events (WebSocket)' }}</span>
      </label>
      <span
        class="feishu-realtime-control__chip"
        role="status"
      >{{ health }}</span>
    </div>
    <small v-if="!summary && status && status.env_override !== null">
      Controlled by the server environment.
    </small>
    <small v-else-if="!summary">
      Checks incoming messages in background sessions, independently of daily digests and personal /ai commands.
    </small>
    <FeishuRealtimeAgentSettings
      v-if="settings"
      :capabilities="capabilities ?? null"
    />
    <div
      v-if="error"
      role="alert"
    >
      {{ error }}
      <button
        type="button"
        class="ch-btn ch-btn--ghost ch-btn--sm"
        @click="refresh"
      >
        Refresh
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import FeishuRealtimeAgentSettings from './FeishuRealtimeAgentSettings.vue'
import type { FeishuAiCommandCapabilities } from '@/utils/feishuBotConfig'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { loadFeishuRealtime, updateFeishuRealtime, type FeishuRealtimeStatus } from '@/utils/dotApi'

defineProps<{ summary?: boolean; settings?: boolean; capabilities?: FeishuAiCommandCapabilities | null }>()

const status = ref<FeishuRealtimeStatus>()
const busy = ref(false)
const readError = ref('')
const uncertainWrite = ref('')
const error = computed(() => uncertainWrite.value || readError.value)
let reading = false
let revision = 0
let timer: ReturnType<typeof setInterval> | undefined

const health = computed(() => {
  const current = status.value
  if (error.value) return 'Unavailable'
  if (!current) return 'Loading…'
  if (!current.effective_enabled) return 'Off'
  if (!current.has_web_session) return 'No web session'
  if (current.realtime_stream_healthy) return 'Replay ready'
  return current.frontier_connected ? 'Connected' : 'Disconnected'
})

async function refresh() {
  if (busy.value || reading) return
  reading = true
  const observedRevision = revision
  try {
    const current = await loadFeishuRealtime()
    if (observedRevision === revision) {
      status.value = current
      readError.value = ''
      uncertainWrite.value = ''
    }
  } catch {
    if (observedRevision === revision) readError.value = 'Could not load Feishu realtime status.'
    return
  } finally {
    reading = false
  }
}

async function toggle(event: Event) {
  const input = event.currentTarget as HTMLInputElement
  const enabled = input.checked
  input.checked = status.value?.effective_enabled ?? false
  if (busy.value || !status.value) return
  revision += 1
  busy.value = true
  try {
    status.value = await updateFeishuRealtime(enabled)
  } catch {
    uncertainWrite.value = 'Could not confirm the change. Refresh status before retrying.'
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => {
    if (!uncertainWrite.value) void refresh()
  }, 5_000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.feishu-realtime-control {
  min-width: 0;
}

.feishu-realtime-control__row,
.feishu-realtime-control__row label {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
}

.feishu-realtime-control__row label {
  min-height: 2rem;
}

.feishu-realtime-control__chip {
  border: 1px solid currentColor;
  border-radius: 1rem;
  padding: 0.1rem 0.45rem;
  font-size: 0.78em;
  white-space: nowrap;
}
</style>
