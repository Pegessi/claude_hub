<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="goal-dialog-backdrop"
      @click.self="emit('close')"
    >
      <form
        class="goal-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="goal-dialog-title"
        @submit.prevent="submit"
      >
        <div>
          <h2 id="goal-dialog-title">
            Start a Goal
          </h2>
          <p>Set an explicit outcome. Goal runs independently from Agent and Plan modes.</p>
        </div>
        <label>
          Objective
          <textarea
            ref="objectiveEl"
            v-model="objective"
            maxlength="4000"
            rows="5"
            placeholder="Describe the outcome to achieve…"
            required
          />
        </label>
        <div class="goal-dialog-fields">
          <label>
            Token budget <span>(optional)</span>
            <input
              v-model="tokenBudget"
              type="number"
              min="1"
              step="1"
              placeholder="No token limit"
            >
          </label>
          <label>
            Maximum turns
            <input
              v-model="maxTurns"
              type="number"
              min="1"
              max="100"
              step="1"
            >
          </label>
        </div>
        <p
          v-if="validationError || error"
          class="goal-dialog-error"
          role="alert"
        >
          {{ validationError || error }}
        </p>
        <div class="goal-dialog-actions">
          <button
            type="button"
            :disabled="busy"
            @click="emit('close')"
          >
            Cancel
          </button>
          <button
            type="submit"
            class="primary"
            :disabled="busy"
          >
            {{ busy ? 'Starting…' : 'Start Goal' }}
          </button>
        </div>
      </form>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

const props = defineProps<{ open: boolean; busy?: boolean; error?: string | null }>()
const emit = defineEmits<{
  close: []
  submit: [value: { objective: string; token_budget?: number; max_turns?: number }]
}>()
const objective = ref('')
const tokenBudget = ref('')
const maxTurns = ref('20')
const validationError = ref<string | null>(null)
const objectiveEl = ref<HTMLTextAreaElement | null>(null)

watch(() => props.open, open => {
  if (!open) return
  validationError.value = null
  void nextTick(() => objectiveEl.value?.focus())
})

function submit() {
  const trimmed = objective.value.trim()
  const budget = tokenBudget.value === '' ? undefined : Number(tokenBudget.value)
  const turns = Number(maxTurns.value)
  if (!trimmed) { validationError.value = 'Objective is required.'; return }
  if (budget !== undefined && (!Number.isInteger(budget) || budget < 1)) { validationError.value = 'Token budget must be a positive whole number.'; return }
  if (!Number.isInteger(turns) || turns < 1 || turns > 100) { validationError.value = 'Maximum turns must be between 1 and 100.'; return }
  validationError.value = null
  emit('submit', { objective: trimmed, ...(budget === undefined ? {} : { token_budget: budget }), max_turns: turns })
}
</script>

<style scoped>
.goal-dialog-backdrop { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 16px; background: rgb(0 0 0 / 55%); }
.goal-dialog { display: grid; gap: 16px; width: min(560px, 100%); padding: 20px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-lg, 12px); background: var(--ch-color-surface); box-shadow: var(--ch-shadow-lg, 0 20px 50px rgb(0 0 0 / 35%)); }
h2, p { margin: 0; }
h2 { font-size: 18px; }
p { color: var(--ch-color-text-muted); }
label { display: grid; gap: 6px; font-weight: 600; }
label span { color: var(--ch-color-text-subtle); font-weight: 400; }
textarea, input { box-sizing: border-box; width: 100%; padding: 9px 10px; color: var(--ch-color-text); background: var(--ch-color-app-bg); border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-sm); font: inherit; }
textarea { resize: vertical; }
.goal-dialog-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.goal-dialog-error { color: var(--ch-color-danger, #e5484d); }
.goal-dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }
button { padding: 7px 12px; color: var(--ch-color-text); background: transparent; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); cursor: pointer; }
button.primary { color: white; background: var(--ch-color-accent); border-color: var(--ch-color-accent); }
button:disabled { cursor: default; opacity: .55; }
@media (max-width: 520px) { .goal-dialog-fields { grid-template-columns: 1fr; } }
</style>
