<template>
  <section
    class="goal-status"
    :data-status="goal.status"
    aria-label="Chat Goal status"
    :aria-busy="busy || undefined"
    @keydown.esc="expanded = false"
  >
    <span
      class="sr-only"
      aria-live="polite"
      aria-atomic="true"
    >{{ liveStatus }}</span>
    <button
      type="button"
      class="goal-status-summary"
      :aria-expanded="expanded"
      :aria-controls="detailsId"
      @click="expanded = !expanded"
    >
      <span
        class="goal-status-dot"
        aria-hidden="true"
      />
      <strong>{{ goalStatusLabel(goal.status) }}</strong>
      <span class="goal-objective">{{ goal.objective }}</span>
      <span class="goal-usage">{{ goalUsageLabel(goal.token_usage, goal.token_budget, goal.usage_quality) }}</span>
      <span aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
    </button>
    <div
      v-if="expanded"
      :id="detailsId"
      class="goal-status-details"
      role="region"
      aria-label="Chat Goal details"
    >
      <p>{{ goal.objective }}</p>
      <div class="goal-status-meta">
        <span>{{ goal.turns_completed }} / {{ goal.max_turns }} turns</span>
        <span>Started {{ formatTime(goal.created_at) }}</span>
        <span>Updated {{ formatTime(goal.updated_at) }}</span>
      </div>
      <p
        v-if="goal.status_message"
        class="goal-status-message"
      >
        {{ goal.status_message }}
      </p>
      <div
        v-if="goal.checkpoint"
        class="goal-checkpoint"
      >
        <strong>Latest checkpoint</strong>
        <p v-if="goal.checkpoint.next_step">
          <b>Next:</b> {{ goal.checkpoint.next_step }}
        </p>
        <p v-if="goal.checkpoint.blocker">
          <b>Blocked:</b> {{ goal.checkpoint.blocker }}
        </p>
        <p v-if="goal.checkpoint.remaining.length">
          <b>Remaining:</b> {{ goal.checkpoint.remaining.join(' · ') }}
        </p>
        <p v-if="goal.checkpoint.verified_progress.length">
          <b>Verified:</b> {{ goal.checkpoint.verified_progress.map(item => item.item).join(' · ') }}
        </p>
      </div>
      <p
        v-if="goal.checkpoint_warning"
        class="goal-checkpoint-warning"
      >
        Checkpoint was not updated: {{ goal.checkpoint_warning }}
      </p>
      <p
        v-if="error"
        class="goal-status-error"
        role="alert"
      >
        {{ error }}
      </p>
    </div>
    <p
      v-if="error && !expanded"
      class="goal-status-error goal-status-error--summary"
      role="alert"
    >
      {{ error }}
    </p>
    <div class="goal-status-actions">
      <button
        v-if="goal.status === 'active'"
        type="button"
        :disabled="busy"
        @click="emit('pause')"
      >
        Pause
      </button>
      <button
        v-if="goal.status === 'paused' || goal.status === 'blocked'"
        type="button"
        :disabled="busy"
        @click="emit('resume')"
      >
        Resume
      </button>
      <button
        v-if="!isGoalTerminal(goal.status)"
        type="button"
        :disabled="busy"
        @click="emit('complete')"
      >
        Complete
      </button>
      <button
        type="button"
        :disabled="busy"
        @click="emit('clear')"
      >
        Clear
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ChatGoal } from '@/types'
import { goalStatusLabel, goalUsageLabel, isGoalTerminal } from '@/utils/chatGoalPolicy'

const props = defineProps<{ goal: ChatGoal; busy?: boolean; error?: string | null }>()
const emit = defineEmits<{ pause: []; resume: []; complete: []; clear: [] }>()
const expanded = ref(false)
const detailsId = computed(() => `goal-status-details-${props.goal.id}`)
const liveStatus = computed(() => [
  `Goal ${goalStatusLabel(props.goal.status)}`,
  `${props.goal.turns_completed} of ${props.goal.max_turns} turns`,
  goalUsageLabel(props.goal.token_usage, props.goal.token_budget, props.goal.usage_quality),
].join(', '))
function formatTime(value: string): string { return new Date(value).toLocaleString() }
</script>

<style scoped>
.goal-status { display: flex; align-items: center; gap: 8px; padding: 7px 28px; border-top: 1px solid var(--ch-color-border-muted); border-bottom: 1px solid var(--ch-color-border-muted); background: var(--ch-color-surface); font-size: 12px; }
.goal-status-summary { display: flex; min-width: 0; flex: 1; align-items: center; gap: 8px; padding: 2px 0; color: inherit; text-align: left; background: transparent; border: 0; cursor: pointer; }
.goal-status-dot { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: var(--ch-color-accent); }
[data-status='paused'] .goal-status-dot, [data-status='blocked'] .goal-status-dot, [data-status='budget_limited'] .goal-status-dot { background: var(--ch-color-warning, #e0a800); }
[data-status='complete'] .goal-status-dot { background: var(--ch-color-success, #30a46c); }
[data-status='cancelled'] .goal-status-dot, [data-status='failed'] .goal-status-dot { background: var(--ch-color-danger, #e5484d); }
.goal-objective { min-width: 0; overflow: hidden; color: var(--ch-color-text-muted); text-overflow: ellipsis; white-space: nowrap; }
.goal-usage { margin-left: auto; color: var(--ch-color-text-subtle); white-space: nowrap; }
.goal-status-actions { display: flex; gap: 5px; }
.goal-status-actions button { padding: 3px 8px; color: var(--ch-color-text); background: transparent; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); cursor: pointer; }
.goal-status-actions button:disabled { cursor: default; opacity: .55; }
.goal-status-details { position: absolute; right: 28px; bottom: 72px; left: 28px; z-index: 4; max-height: min(60dvh, 480px); padding: 12px; overflow-y: auto; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-md); background: var(--ch-color-surface-elevated, var(--ch-color-surface)); box-shadow: var(--ch-shadow-md); }
.goal-status-details p { margin: 0 0 8px; white-space: pre-wrap; }
.goal-status-meta { display: flex; flex-wrap: wrap; gap: 12px; color: var(--ch-color-text-subtle); }
.goal-status-message, .goal-status-error { color: var(--ch-color-warning, #e0a800); }
.goal-status-error { color: var(--ch-color-danger, #e5484d); }
.goal-status-error--summary { max-width: 260px; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.goal-checkpoint { padding-top: 8px; border-top: 1px solid var(--ch-color-border-muted); }
.goal-checkpoint p { margin: 5px 0 0; color: var(--ch-color-text-muted); }
.goal-checkpoint-warning { color: var(--ch-color-warning, #e0a800); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (pointer: coarse) { .goal-status-summary, .goal-status-actions button { min-height: 44px; } .goal-status-actions button { min-width: 44px; } }
@media (max-width: 640px) { .goal-status { flex-wrap: wrap; padding: 7px 12px; } .goal-usage { display: none; } .goal-status-actions { width: 100%; justify-content: flex-end; } .goal-status-details { right: 12px; left: 12px; } }
</style>
