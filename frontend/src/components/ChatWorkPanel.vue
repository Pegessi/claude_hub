<template>
  <section
    v-if="work.length || error"
    class="chat-work-panel"
    aria-label="Background work"
  >
    <div class="chat-work-header">
      <button
        type="button"
        :aria-expanded="expanded"
        :aria-controls="listId"
        @click="expanded = !expanded"
      >
        <span aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
        <strong>Background work</strong>
        <span>{{ work.length }}</span>
        <span
          v-if="attentionCount"
          class="chat-work-attention"
        >{{ attentionCount }} need attention</span>
      </button>
      <button
        class="chat-work-refresh"
        type="button"
        :disabled="refreshing || Boolean(busyId)"
        aria-label="Refresh background work"
        @click="refresh"
      >
        {{ refreshing ? 'Refreshing…' : 'Refresh' }}
      </button>
    </div>
    <p
      v-if="error"
      class="chat-work-error"
      role="alert"
    >
      {{ stale ? 'Status may be out of date. ' : '' }}{{ error }}
    </p>
    <div
      v-if="expanded"
      :id="listId"
      class="chat-work-list"
    >
      <ChatWorkCard
        v-for="item in work"
        :key="item.id"
        :work="item"
        :busy="busyId === item.id"
        :disabled="Boolean(busyId) || stale"
        @update="change => update(item.id, change)"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, toRef, watch } from 'vue'
import { useChatWork } from '@/composables/useChatWork'
import ChatWorkCard from '@/components/ChatWorkCard.vue'
import { useTerminalStore } from '@/stores/terminalStore'
import { createWorkResultTracker } from '@/utils/chatWorkNotifications'

const props = defineProps<{ tabId: string; refreshKey?: number }>()
const terminalStore = useTerminalStore()
let storage: Storage | null = null
try { storage = localStorage } catch { /* Storage can be unavailable in private contexts. */ }
const newlyObservedResults = createWorkResultTracker(storage)
const { work, error, refreshing, busyId, stale, refresh, update, start, stop, dispose } = useChatWork(toRef(props, 'tabId'))
const expanded = ref(true)
const listId = computed(() => `chat-work-list-${props.tabId}`)
const attentionCount = computed(() => work.value.filter(item =>
  item.status === 'failed' || item.status === 'review' || ['anomaly', 'decision'].includes(item.latest_result?.kind ?? ''),
).length)
let paneActive = false
watch(work, items => {
  if (!paneActive || document.visibilityState === 'hidden') return
  const updates = newlyObservedResults(items)
  if (!updates.length) return
  const first = updates[0]
  terminalStore.pushNotification({
    type: updates.some(item => item.latest_result?.kind !== 'completed') ? 'warning' : 'success',
    message: updates.length === 1
      ? `${first.title}: ${first.latest_result?.summary ?? 'New result'}`.slice(0, 320)
      : `${updates.length} background work results are ready. Open Background work for details.`,
    autoDismissMs: 8_000,
  })
})
function reconcileVisibility() {
  if (paneActive && document.visibilityState !== 'hidden') void start()
  else stop()
}
function activate() { paneActive = true; reconcileVisibility() }
function deactivate() { paneActive = false; stop() }
onMounted(() => {
  document.addEventListener('visibilitychange', reconcileVisibility)
  activate()
})
onActivated(activate)
onDeactivated(deactivate)
onUnmounted(() => {
  document.removeEventListener('visibilitychange', reconcileVisibility)
  dispose()
})
watch(() => props.refreshKey, () => { if (paneActive) void refresh() })
watch(() => props.tabId, () => { expanded.value = true })
</script>

<style scoped>
.chat-work-panel { flex: 0 0 auto; min-width: 0; border-top: 1px solid var(--ch-color-border-muted); padding: 5px 24px 8px; color: var(--ch-color-text); background: var(--ch-color-surface); font-size: 12px; }
.chat-work-header { display: flex; justify-content: space-between; gap: 6px; }
.chat-work-header button { display: flex; align-items: center; gap: 7px; min-height: 30px; padding: 2px 4px; border: 0; background: transparent; color: var(--ch-color-text-muted); font: inherit; cursor: pointer; }
.chat-work-header strong { color: var(--ch-color-text); }
.chat-work-header button:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: 2px; }
.chat-work-header button:disabled { opacity: .5; cursor: default; }
.chat-work-attention { color: var(--ch-color-warning, #dca63d); }
.chat-work-list { display: grid; gap: 6px; max-height: min(36dvh, 380px); overflow-y: auto; overscroll-behavior: contain; }
.chat-work-error { margin: 4px 0 7px; overflow-wrap: anywhere; color: var(--ch-color-danger, #e5484d); }
@media (max-width: 640px) { .chat-work-panel { padding: 5px 10px 8px; } .chat-work-attention { display: none; } }
@media (pointer: coarse) { .chat-work-header button { min-height: 44px; } }
</style>
