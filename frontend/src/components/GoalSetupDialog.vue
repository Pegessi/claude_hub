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
        aria-describedby="goal-dialog-tips"
        @submit.prevent="submit"
      >
        <div>
          <h2 id="goal-dialog-title">
            Set a Goal
          </h2>
          <p>What would you like to achieve?</p>
        </div>
        <label>
          Objective
          <textarea
            ref="objectiveEl"
            v-model="objective"
            maxlength="4000"
            rows="5"
            :readonly="busy"
            placeholder="Describe the outcome to achieve…"
            required
          />
        </label>
        <div
          id="goal-dialog-tips"
          class="goal-dialog-tips"
        >
          <p>Describe the outcome and how to verify it.</p>
          <p>The agent keeps working until it finishes or needs your input. You can pause anytime.</p>
          <p>Goal has no token or turn limit. Provider usage and charges still apply.</p>
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
  submit: [value: { objective: string }]
}>()
const objective = ref('')
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
  if (!focusable.includes(document.activeElement as HTMLElement)) {
    event.preventDefault()
    first.focus()
  } else if (event.shiftKey && document.activeElement === first) {
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
  if (props.busy) return
  if (!trimmed) { validationError.value = 'Objective is required.'; return }
  validationError.value = null
  emit('submit', { objective: trimmed })
}
</script>

<style scoped>
.goal-dialog-backdrop { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 16px; background: rgb(0 0 0 / 55%); }
.goal-dialog { display: grid; gap: 16px; width: min(560px, 100%); max-height: calc(100dvh - 32px); padding: 20px; overflow-y: auto; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-lg, 12px); background: var(--ch-color-surface); box-shadow: var(--ch-shadow-lg, 0 20px 50px rgb(0 0 0 / 35%)); }
h2, p { margin: 0; }
h2 { font-size: 18px; }
p { color: var(--ch-color-text-muted); }
label { display: grid; gap: 6px; font-weight: 600; }
textarea { box-sizing: border-box; width: 100%; padding: 9px 10px; color: var(--ch-color-text); background: var(--ch-color-app-bg); border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-sm); font: inherit; }
textarea { resize: vertical; }
.goal-dialog-tips { display: grid; gap: 6px; font-size: 12px; line-height: 1.5; }
.goal-dialog-error { color: var(--ch-color-danger, #e5484d); }
.goal-dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }
button { padding: 7px 12px; color: var(--ch-color-text); background: transparent; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); cursor: pointer; }
button.primary { color: white; background: var(--ch-color-accent); border-color: var(--ch-color-accent); }
button:disabled { cursor: default; opacity: .55; }
textarea:focus-visible, button:focus-visible { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 2px; }
@media (max-width: 640px), (pointer: coarse) { button { min-height: 44px; } }
</style>
