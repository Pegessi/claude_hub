<template>
  <article
    class="chat-work-card"
    :data-work-id="work.id"
    :data-status="work.status"
  >
    <button
      class="chat-work-summary"
      type="button"
      :aria-expanded="expanded"
      :aria-controls="detailsId"
      @click="expanded = !expanded"
    >
      <span
        class="chat-work-dot"
        aria-hidden="true"
      />
      <span class="chat-work-title">{{ work.title }}</span>
      <span class="chat-work-status">{{ statusLabel }}</span>
      <span aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
    </button>
    <p
      v-if="importantResult"
      class="chat-work-notice"
    >
      {{ importantResult }}
    </p>
    <div
      v-if="expanded"
      :id="detailsId"
      class="chat-work-details"
    >
      <div class="chat-work-meta">
        <span>{{ work.kind === 'monitor' ? 'Monitor' : 'Task' }} · {{ work.agent_type }}{{ work.model ? ` / ${work.model}` : '' }}</span>
        <span v-if="work.kind === 'monitor'">{{ work.run_count }} {{ work.run_count === 1 ? 'check' : 'checks' }} · Every {{ intervalLabel }}</span>
        <span v-if="work.next_run_at && work.status !== 'paused' && work.status !== 'stopped'">Next: {{ formatTime(work.next_run_at) }}</span>
      </div>
      <p
        v-if="work.status === 'review'"
        class="chat-work-note"
      >
        Waiting for review or acceptance. Ask in this chat to inspect or continue this task.
      </p>
      <p
        v-if="work.status === 'paused'"
        class="chat-work-note"
      >
        Future checks are paused. A check already running may still finish.
      </p>
      <p
        v-if="work.status === 'stopped'"
        class="chat-work-note"
      >
        Future work is stopped. Interruption of a running worker is best effort; external operations may still finish.
      </p>
      <section
        v-if="work.latest_result"
        class="chat-work-result"
        aria-label="Latest work result"
      >
        <strong>{{ resultLabel }}</strong>
        <p>{{ work.latest_result.summary }}</p>
        <details v-if="work.latest_result.validation || work.latest_result.report_id">
          <summary>Reported validation and source</summary>
          <p v-if="work.latest_result.validation">
            {{ work.latest_result.validation }}
          </p>
          <p
            v-if="work.latest_result.report_id"
            class="chat-work-source"
          >
            Report: {{ work.latest_result.report_id }}
          </p>
          <p
            v-if="work.latest_result.task_id"
            class="chat-work-source"
          >
            Task: {{ work.latest_result.task_id }}
          </p>
        </details>
        <time :datetime="work.latest_result.created_at">{{ formatTime(work.latest_result.created_at) }}</time>
      </section>
      <p
        v-else
        class="chat-work-note"
      >
        No result yet. You can keep chatting while this runs.
      </p>
      <details
        v-if="work.executions.length"
        class="chat-work-runs"
      >
        <summary>Recent {{ work.kind === 'monitor' ? 'checks' : 'executions' }} ({{ work.executions.length }})</summary>
        <ol>
          <li
            v-for="run in work.executions"
            :key="run.task_id"
          >
            <span>{{ run.status }}{{ run.outcome ? ` · ${run.outcome}` : '' }} · {{ formatTime(run.updated_at) }}</span>
            <p v-if="run.summary">
              {{ run.summary }}
            </p>
            <span class="chat-work-source">Task: {{ run.task_id }}</span>
          </li>
        </ol>
      </details>
      <div
        v-if="!terminal"
        class="chat-work-controls"
      >
        <button
          v-if="work.status === 'paused' || work.status === 'failed'"
          type="button"
          :disabled="disabled"
          @click="emit('update', { action: 'resume' })"
        >
          {{ work.status === 'failed' ? 'Retry' : 'Resume' }}
        </button>
        <button
          v-else-if="work.kind === 'monitor'"
          type="button"
          :disabled="disabled"
          title="Pause future checks; the current check may finish"
          @click="emit('update', { action: 'pause' })"
        >
          Pause checks
        </button>
        <button
          type="button"
          :disabled="disabled"
          title="Stop future work and request interruption of the current worker"
          @click="emit('update', { action: 'stop' })"
        >
          Stop work
        </button>
        <span
          v-if="busy"
          role="status"
        >Updating…</span>
      </div>
      <form
        v-if="work.kind === 'monitor' && !terminal"
        class="chat-work-interval"
        @submit.prevent="saveInterval"
      >
        <label :for="intervalId">Check every</label>
        <input
          :id="intervalId"
          v-model="intervalDraft"
          type="number"
          min="1"
          step="any"
          required
          :disabled="disabled"
          aria-label="Check interval in minutes"
        >
        <span>minutes</span>
        <button
          type="submit"
          :disabled="disabled || !validInterval || !intervalChanged"
        >
          Save interval
        </button>
      </form>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ChatWork, ChatWorkUpdate } from '@/types/chatWork'

const props = defineProps<{ work: ChatWork; busy: boolean; disabled: boolean }>()
const emit = defineEmits<{ update: [change: ChatWorkUpdate] }>()
const expanded = ref(false)
const intervalDraft = ref('')
const detailsId = computed(() => `chat-work-details-${props.work.id}`)
const intervalId = computed(() => `chat-work-interval-${props.work.id}`)
const terminal = computed(() => ['stopped', 'completed'].includes(props.work.status))
const labels: Record<ChatWork['status'], string> = {
  running: 'Running', waiting: 'Waiting', paused: 'Paused', stopped: 'Stop requested',
  completed: 'Completed', failed: 'Failed', review: 'Needs review',
}
const statusLabel = computed(() => labels[props.work.status])
const resultLabel = computed(() => ({
  progress: 'Latest update', anomaly: 'Attention needed', completed: 'Result', decision: 'Decision needed', failed: 'Execution failed',
})[props.work.latest_result?.kind ?? 'progress'])
const importantResult = computed(() => {
  const result = props.work.latest_result
  return !expanded.value && result && result.kind !== 'progress' ? result.summary : null
})
const intervalLabel = computed(() => {
  const seconds = props.work.interval_seconds ?? 0
  if (seconds % 3600 === 0 && seconds >= 3600) return `${seconds / 3600}h`
  return `${Number((seconds / 60).toFixed(2))}m`
})
const intervalSeconds = computed(() => Math.round(Number(intervalDraft.value) * 60))
const validInterval = computed(() => Number.isFinite(intervalSeconds.value) && intervalSeconds.value >= 60)
const intervalChanged = computed(() => intervalSeconds.value !== props.work.interval_seconds)
watch(() => props.work.interval_seconds, value => { intervalDraft.value = String((value ?? 60) / 60) }, { immediate: true })
function saveInterval() {
  if (props.disabled || !validInterval.value || !intervalChanged.value) return
  emit('update', { interval_seconds: intervalSeconds.value })
}
function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}
</script>

<style scoped>
.chat-work-card { border: 1px solid var(--ch-color-border-muted); border-radius: 8px; background: var(--ch-color-surface); }
.chat-work-summary { display: flex; width: 100%; min-height: 36px; align-items: center; gap: 8px; padding: 7px 10px; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; }
.chat-work-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ch-color-accent); flex-shrink: 0; }
[data-status='paused'] .chat-work-dot, [data-status='stopped'] .chat-work-dot { background: var(--ch-color-text-muted); }
[data-status='failed'] .chat-work-dot, [data-status='review'] .chat-work-dot { background: var(--ch-color-warning, #dca63d); }
[data-status='completed'] .chat-work-dot { background: var(--ch-color-success, #30a46c); }
.chat-work-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.chat-work-status { color: var(--ch-color-text-muted); font-size: 11px; }
.chat-work-details { padding: 2px 10px 10px; overflow-wrap: anywhere; }
.chat-work-notice { margin: 0 10px 8px 25px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; color: var(--ch-color-text-muted); }
.chat-work-meta { display: flex; flex-wrap: wrap; gap: 5px 12px; color: var(--ch-color-text-muted); font-size: 11px; }
.chat-work-note { color: var(--ch-color-text-muted); }
.chat-work-result { margin: 10px 0; padding: 8px 10px; background: var(--ch-color-surface-elevated, var(--ch-color-surface)); border-left: 2px solid var(--ch-color-border); }
.chat-work-result p, .chat-work-runs p { margin: 5px 0; white-space: pre-wrap; }
.chat-work-result time, .chat-work-source { font-size: 11px; color: var(--ch-color-text-subtle, var(--ch-color-text-muted)); }
.chat-work-source { display: block; }
.chat-work-result time { display: block; margin-top: 6px; }
.chat-work-runs { margin: 10px 0; }
.chat-work-runs ol { padding-left: 18px; }
.chat-work-runs li { margin: 8px 0; }
summary { cursor: pointer; color: var(--ch-color-text-muted); }
.chat-work-controls, .chat-work-interval { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 10px; }
.chat-work-controls button, .chat-work-interval button, .chat-work-interval input { border: 1px solid var(--ch-color-border); border-radius: 5px; padding: 4px 8px; background: transparent; color: inherit; }
.chat-work-controls button, .chat-work-interval button { cursor: pointer; }
.chat-work-interval input { width: 80px; background: var(--ch-color-surface-control, transparent); }
button:disabled, input:disabled { opacity: .5; cursor: default; }
button:focus-visible, summary:focus-visible, input:focus-visible { outline: 2px solid var(--ch-color-accent); outline-offset: 2px; }
@media (pointer: coarse) { .chat-work-summary, .chat-work-controls button, .chat-work-interval button, .chat-work-interval input { min-height: 44px; } }
</style>
