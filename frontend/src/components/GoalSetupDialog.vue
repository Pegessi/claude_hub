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
        <header class="goal-dialog-head">
          <span class="goal-dialog-eyebrow">{{ editing ? 'Goal settings' : 'New goal' }}</span>
          <h2 id="goal-dialog-title">
            {{ editing ? 'Edit Goal' : 'Set a Goal' }}
          </h2>
          <p class="goal-dialog-subtitle">
            {{ editing
              ? 'Changes apply to the next Goal turn. The current turn can finish.'
              : 'A Goal keeps the agent working on one outcome until it is done or needs you.' }}
          </p>
        </header>

        <div class="goal-dialog-body">
          <section class="goal-dialog-section">
            <div class="goal-dialog-section-head">
              <label
                class="goal-dialog-field-label"
                for="goal-dialog-objective"
              >Objective</label>
              <span
                class="goal-dialog-count"
                :data-near-limit="objective.length > MAX_OBJECTIVE * 0.9 ? 'true' : 'false'"
              >{{ objective.length }}/{{ MAX_OBJECTIVE }}</span>
            </div>
            <div class="goal-dialog-field">
              <textarea
                id="goal-dialog-objective"
                ref="objectiveEl"
                v-model="objective"
                :maxlength="MAX_OBJECTIVE"
                rows="5"
                :readonly="busy"
                placeholder="Describe the outcome to achieve…"
                aria-describedby="goal-dialog-objective-hint"
                required
                @paste="$emit('paste', $event)"
              />
              <span
                id="goal-dialog-objective-hint"
                class="goal-dialog-field-hint"
              >
                Say what “done” looks like, and how you would verify it.
              </span>
            </div>
          </section>

          <section class="goal-dialog-section goal-dialog-images">
            <slot name="images" />
          </section>

          <section
            v-if="!editing"
            class="goal-dialog-option"
            :data-on="planFirst ? 'true' : 'false'"
            :data-disabled="planFirstDisabledReason ? 'true' : 'false'"
          >
            <div class="goal-dialog-plan-row">
              <label class="goal-dialog-plan-toggle">
                <input
                  type="checkbox"
                  :checked="planFirst"
                  :disabled="busy || Boolean(planFirstDisabledReason)"
                  @change="togglePlanFirst"
                >
                <span>Plan first</span>
              </label>
              <span class="goal-dialog-option-badge">{{ planFirst ? 'Review first' : 'Off' }}</span>
            </div>
            <p class="goal-dialog-plan-note">
              <template v-if="planFirstDisabledReason">
                {{ planFirstDisabledReason }}
              </template>
              <template v-else-if="planFirst">
                The first turn proposes the scope, steps and execution budget
                (sub-agents, models, parallelism). Nothing starts until you approve.
              </template>
              <template v-else>
                The Goal starts working immediately. Tick this to review its plan
                before any work begins.
              </template>
            </p>
          </section>

          <section
            id="goal-dialog-tips"
            class="goal-dialog-tips"
          >
            <p>
              <span
                class="goal-dialog-tip-mark"
                aria-hidden="true"
              >•</span>
              The agent keeps working until it finishes or needs your input. You can pause anytime.
            </p>
            <p>
              <span
                class="goal-dialog-tip-mark"
                aria-hidden="true"
              >•</span>
              A Goal has no token or turn limit. Provider usage and charges still apply.
            </p>
          </section>

          <p
            v-if="validationError || error"
            class="goal-dialog-error"
            role="alert"
          >
            {{ validationError || error }}
          </p>
          <slot
            v-if="error && !validationError"
            name="recovery"
          />
        </div>

        <footer class="goal-dialog-actions">
          <button
            type="button"
            class="goal-dialog-button"
            :disabled="busy"
            @click="close"
          >
            Cancel
          </button>
          <button
            type="submit"
            class="goal-dialog-button goal-dialog-button--primary"
            :disabled="busy || submitDisabled"
          >
            {{ busy ? (editing ? 'Saving…' : 'Starting…') : (editing ? 'Save Goal' : 'Start Goal') }}
          </button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

/** The objective's storage bound; the counter reads it instead of a literal. */
const MAX_OBJECTIVE = 4000

const props = defineProps<{
  open: boolean
  busy?: boolean
  submitDisabled?: boolean
  error?: string | null
  editing?: boolean
  initialObjective?: string
  /** Remembered choice for a new Goal; the parent owns the preference. */
  initialPlanFirst?: boolean
  /** Why the provider cannot plan first, or null when it can. */
  planFirstDisabledReason?: string | null
}>()
const emit = defineEmits<{
  close: []
  submit: [value: { objective: string; planFirst: boolean }]
  /** A paste inside the objective field. The parent decides whether the
   *  clipboard carries images; the dialog only forwards the raw event so it
   *  stays free of attachment state. */
  paste: [event: ClipboardEvent]
}>()
const objective = ref('')
const planFirst = ref(false)
const validationError = ref<string | null>(null)
const objectiveEl = ref<HTMLTextAreaElement | null>(null)
let returnFocusEl: HTMLElement | null = null

function togglePlanFirst(event: Event) {
  planFirst.value = (event.target as HTMLInputElement).checked
}

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
    if (props.editing) objective.value = props.initialObjective ?? ''
    planFirst.value = Boolean(props.initialPlanFirst ?? false) && !props.planFirstDisabledReason
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
  if (props.busy || props.submitDisabled) return
  if (!trimmed) { validationError.value = 'Objective is required.'; return }
  validationError.value = null
  emit('submit', { objective: trimmed, planFirst: planFirst.value })
}
</script>

<style scoped>
/* The dialog is the surface where a Goal is scoped, so it reads as a document
   rather than a stack of form controls: one eyebrow/title block, labelled
   sections on the app's own surfaces, and a footer that owns the decision. */
.goal-dialog-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  padding: 16px;
  background: var(--ch-color-overlay);
}

.goal-dialog {
  position: relative;
  display: flex;
  flex-direction: column;
  width: min(580px, 100%);
  max-height: calc(var(--visual-viewport-height, 100dvh) - 32px);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-overlay);
  background: var(--ch-color-surface);
  box-shadow: var(--ch-shadow-dialog);
  overflow: hidden;
}

.goal-dialog-head {
  display: grid;
  gap: 6px;
  padding: 20px 22px 16px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: linear-gradient(180deg, var(--ch-color-surface-raised), var(--ch-color-surface) 92%);
}

.goal-dialog-eyebrow {
  color: var(--ch-color-accent);
  font-size: var(--ch-font-size-xs);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.goal-dialog-head h2 {
  margin: 0;
  color: var(--ch-color-text-strong);
  font-size: 19px;
  font-weight: 650;
  line-height: 1.25;
}

.goal-dialog-subtitle {
  margin: 0;
  max-width: 46ch;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  line-height: 1.55;
}

.goal-dialog-body {
  display: grid;
  gap: 18px;
  min-height: 0;
  padding: 18px 22px;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.goal-dialog-section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 7px;
}

.goal-dialog-field-label {
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-base);
  font-weight: 650;
}

.goal-dialog-count {
  color: var(--ch-color-text-subtle);
  font-size: var(--ch-font-size-xs);
  font-variant-numeric: tabular-nums;
}

.goal-dialog-count[data-near-limit='true'] {
  color: var(--ch-color-warning);
}

.goal-dialog-field {
  display: grid;
  gap: 0;
}

.goal-dialog-field textarea {
  box-sizing: border-box;
  width: 100%;
  min-height: 120px;
  padding: 11px 12px;
  color: var(--ch-color-text);
  background: var(--ch-color-surface-sunken);
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  font: inherit;
  font-size: var(--ch-font-size-lg);
  line-height: 1.55;
  resize: vertical;
  transition:
    border-color var(--ch-motion-fast),
    box-shadow var(--ch-motion-fast);
}

.goal-dialog-field textarea::placeholder {
  color: var(--ch-color-text-subtle);
}

.goal-dialog-field textarea:hover:not(:read-only) {
  border-color: var(--ch-color-border-hover);
}

.goal-dialog-field textarea:focus-visible {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 3px var(--ch-color-accent-soft);
}

.goal-dialog-field textarea:read-only {
  color: var(--ch-color-text-muted);
  cursor: default;
}

.goal-dialog-field-hint {
  margin-top: 6px;
  color: var(--ch-color-text-subtle);
  font-size: var(--ch-font-size-sm);
  line-height: 1.5;
}

/* Attachment row: the picker itself belongs to the parent (it owns the files).
   Only its trigger is restyled — a bare `button` selector would also hit the
   thumbnail preview and the remove badge inside the chips. */
.goal-dialog-images:empty {
  display: none;
}

.goal-dialog-images :deep(p) {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  line-height: 1.5;
}

.goal-dialog-images :deep(.goal-image-picker > button) {
  min-height: 32px;
  padding: 5px 11px;
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  font: inherit;
  font-size: var(--ch-font-size-sm);
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.goal-dialog-images :deep(.goal-image-picker > button:hover:not(:disabled)) {
  background: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border-hover);
}

.goal-dialog-images :deep(.goal-image-picker > button:disabled) {
  opacity: 0.55;
  cursor: default;
}

/* A plan review is an option, not a needed step: the card carries its own
   state so "Off" is visibly the resting state. */
.goal-dialog-option {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-soft);
  transition: border-color var(--ch-motion-standard), background var(--ch-motion-standard);
}

.goal-dialog-option[data-on='true'] {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
}

.goal-dialog-option[data-disabled='true'] {
  background: var(--ch-color-surface-muted);
}

.goal-dialog-plan-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.goal-dialog-plan-toggle {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 24px;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-base);
  font-weight: 650;
  cursor: pointer;
}

.goal-dialog-plan-toggle input {
  width: 15px;
  height: 15px;
  margin: 0;
  accent-color: var(--ch-color-accent-strong);
  cursor: pointer;
}

.goal-dialog-plan-toggle input:disabled { cursor: default; }

.goal-dialog-option-badge {
  flex: 0 0 auto;
  padding: 1px 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  color: var(--ch-color-text-subtle);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.goal-dialog-option[data-on='true'] .goal-dialog-option-badge {
  border-color: var(--ch-color-accent-ring-strong);
  color: var(--ch-color-text-selected);
}

.goal-dialog-plan-note {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  line-height: 1.55;
}

.goal-dialog-tips {
  display: grid;
  gap: 6px;
  color: var(--ch-color-text-subtle);
  font-size: var(--ch-font-size-sm);
  line-height: 1.5;
}

.goal-dialog-tips p {
  display: grid;
  grid-template-columns: 10px 1fr;
  gap: 8px;
  margin: 0;
}

.goal-dialog-tip-mark {
  color: var(--ch-color-border-hover);
  font-weight: 700;
  line-height: inherit;
}

.goal-dialog-error {
  margin: 0;
  padding: 9px 12px;
  border: 1px solid var(--ch-color-danger-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-text);
  font-size: var(--ch-font-size-sm);
  line-height: 1.5;
}

.goal-dialog :deep(.goal-status-recovery) {
  margin: 0;
}

.goal-dialog-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
  padding: 14px 22px calc(14px + env(safe-area-inset-bottom, 0px));
  border-top: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-soft);
}

.goal-dialog-button {
  min-height: 36px;
  padding: 7px 15px;
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  font: inherit;
  font-size: var(--ch-font-size-base);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.goal-dialog-button:hover:not(:disabled) {
  background: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border-hover);
}

.goal-dialog-button:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
}

.goal-dialog-button:disabled {
  opacity: 0.55;
  cursor: default;
}

.goal-dialog-button--primary {
  color: var(--ch-color-on-accent);
  background: var(--ch-color-accent-strong);
  border-color: var(--ch-color-accent-strong);
  box-shadow: var(--ch-shadow-button-primary);
}

.goal-dialog-button--primary:hover:not(:disabled) {
  background: var(--ch-color-accent-fill-hover);
  border-color: var(--ch-color-accent-fill-hover);
}

/* Phones: overlays.css already turns `.goal-dialog-backdrop > .goal-dialog`
   into a bottom sheet (width, radius, sheet shadow, safe-area height). Only
   this form's own insets and touch targets are set here, so the shared rules
   stay the single owner of sheet geometry. */
@media (max-width: 640px), (pointer: coarse) {
  .goal-dialog-head,
  .goal-dialog-body,
  .goal-dialog-actions {
    padding-left: 18px;
    padding-right: 18px;
  }

  .goal-dialog-button,
  .goal-dialog-images :deep(.goal-image-picker > button) {
    min-height: 44px;
  }

  .goal-dialog-plan-toggle {
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .goal-dialog-field textarea,
  .goal-dialog-option,
  .goal-dialog-button {
    transition: none;
  }
}
</style>
