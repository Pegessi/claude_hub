<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="goal-dialog-backdrop"
      @click.self="close"
      @keydown.esc="close"
      @keydown.tab="trapFocus"
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
            @click="close"
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
let returnFocusEl: HTMLElement | null = null

function close() {
  if (!props.busy) emit('close')
}

function trapFocus(event: KeyboardEvent) {
  const dialog = objectiveEl.value?.closest<HTMLElement>('.goal-dialog')
  if (!dialog) return
  const focusable = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled)')]
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (!first || !last) return
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(() => props.open, open => {
  if (open) {
    returnFocusEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
    validationError.value = null
    void nextTick(() => objectiveEl.value?.focus())
  } else {
    returnFocusEl?.focus()
    returnFocusEl = null
  }
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
.goal-dialog { display: grid; gap: 16px; width: min(560px, 100%); max-height: calc(100dvh - 32px); padding: 20px; overflow-y: auto; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-lg, 12px); background: var(--ch-color-surface); box-shadow: var(--ch-shadow-lg, 0 20px 50px rgb(0 0 0 / 35%)); }
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
