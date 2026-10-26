<template>
  <section
    class="goal-status"
    :data-status="goal.status"
    :data-execution-owner="goal.execution_owner ?? 'hub_managed'"
    aria-label="Chat Goal status"
    :aria-busy="busy || undefined"
    :data-error="error ? '' : undefined"
    @keydown.esc="expanded = false"
  >
    <span
      class="sr-only"
      aria-live="polite"
      aria-atomic="true"
    >{{ liveStatus }}</span>
    <button
      ref="summaryEl"
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
      <strong>{{ statusText }}</strong>
      <span class="goal-objective">{{ goal.objective }}</span>
      <span
        class="goal-status-chevron"
        aria-hidden="true"
      >{{ expanded ? '▾' : (isPhone ? '▴' : '▸') }}</span>
    </button>
    <!-- Phones keep one quick action next to the summary; the rest are in the sheet. -->
    <button
      v-if="isPhone && quickAction && !expanded"
      type="button"
      class="goal-status-quick"
      :class="quickAction.tone && `goal-action--${quickAction.tone}`"
      :aria-describedby="error ? summaryErrorId : undefined"
      :disabled="quickAction.disabled"
      @click="quickAction.run"
    >
      {{ quickAction.label }}
    </button>
    <Teleport
      to="body"
      :disabled="!isPhone"
    >
      <div
        v-if="expanded && isPhone"
        class="goal-sheet-backdrop"
        aria-hidden="true"
        @click="expanded = false"
      />
      <div
        v-if="expanded"
        :id="detailsId"
        class="goal-status-details"
        :class="{ 'goal-status-details--sheet': isPhone }"
        :data-status="goal.status"
        :role="isPhone ? 'dialog' : 'region'"
        :aria-modal="isPhone || undefined"
        aria-label="Chat Goal details"
        @keydown.esc="expanded = false"
        @keydown.tab="trapFocus"
      >
        <div class="goal-status-details-header">
          <span
            class="goal-status-dot"
            aria-hidden="true"
          />
          <strong>{{ isPhone ? `Goal · ${statusText}` : 'Goal details' }}</strong>
          <button
            ref="closeEl"
            type="button"
            class="goal-status-details-close"
            aria-label="Collapse Goal details"
            @click="expanded = false"
          >
            ×
          </button>
        </div>
        <div class="goal-status-details-body">
          <p class="goal-status-objective">
            {{ goal.objective }}
          </p>
          <div class="goal-status-meta">
            <span>{{ goal.execution_owner === 'provider_native' ? 'Managed by provider' : 'Managed by Claude Hub' }}</span>
            <span>Started {{ formatTime(goal.created_at) }}</span>
            <span>Updated {{ formatTime(goal.updated_at) }}</span>
          </div>
          <p
            v-if="goal.status_message"
            class="goal-status-message"
          >
            {{ goal.status_message }}
          </p>
          <p
            v-if="goal.pending_image_count"
            class="goal-status-message"
          >
            Image delivery was interrupted. Review Chat, reattach images in the composer, then select Recover Goal.
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
        <!-- The sheet's actions stay pinned below the scrolling details. -->
        <div
          v-if="isPhone"
          class="goal-sheet-actions"
        >
          <button
            v-for="action in actions"
            :key="action.key"
            type="button"
            :class="action.tone && `goal-action--${action.tone}`"
            :disabled="action.disabled"
            @click="runFromSheet(action)"
          >
            {{ action.label }}
          </button>
        </div>
      </div>
    </Teleport>
    <p
      v-if="error && !expanded"
      :id="summaryErrorId"
      class="goal-status-error goal-status-error--summary"
      role="alert"
    >
      {{ error }}
    </p>
    <div
      v-if="!isPhone"
      class="goal-status-actions"
    >
      <button
        v-for="action in actions"
        :key="action.key"
        type="button"
        :disabled="action.disabled"
        @click="action.run"
      >
        {{ action.label }}
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ChatGoal } from '@/types'
import { goalStatusLabel, goalStopRetryAction, isGoalTerminal } from '@/utils/chatGoalPolicy'

const props = defineProps<{ goal: ChatGoal; busy?: boolean; error?: string | null }>()
const emit = defineEmits<{ pause: []; resume: []; complete: []; clear: []; edit: []; refresh: [] }>()
const expanded = ref(false)
const summaryEl = ref<HTMLButtonElement | null>(null)
const closeEl = ref<HTMLButtonElement | null>(null)
const needsReconciliation = computed(() => props.goal.dispatch_state === 'uncertain')
function retryStop() {
  if (goalStopRetryAction(props.goal) === 'pause') emit('pause')
  else emit('clear')
}
const detailsId = computed(() => `goal-status-details-${props.goal.id}`)
const statusText = computed(() => needsReconciliation.value ? 'Stop unconfirmed' : goalStatusLabel(props.goal.status))
const liveStatus = computed(() => `Goal ${needsReconciliation.value ? 'stop unconfirmed' : goalStatusLabel(props.goal.status)}`)
function formatTime(value: string): string { return new Date(value).toLocaleString() }

interface GoalAction { key: string; label: string; disabled: boolean; run: () => void; tone?: 'primary' | 'danger' }

// One list feeds the desktop row and the phone sheet, in the same order.
const actions = computed<GoalAction[]>(() => {
  const goal = props.goal
  const busy = Boolean(props.busy)
  const list: GoalAction[] = []
  if (props.error) list.push({ key: 'refresh', label: 'Reload Goal status', disabled: busy, run: () => emit('refresh'), tone: 'danger' })
  if (needsReconciliation.value) list.push({ key: 'retry-stop', label: 'Retry stop', disabled: busy, run: retryStop, tone: 'primary' })
  if (goal.status === 'active') list.push({ key: 'pause', label: 'Pause', disabled: busy, run: () => emit('pause'), tone: 'primary' })
  if (!needsReconciliation.value && (goal.status === 'paused' || goal.status === 'blocked')) {
    list.push({ key: 'resume', label: 'Resume', disabled: busy || Boolean(goal.pending_image_count), run: () => emit('resume'), tone: 'primary' })
  }
  if (!isGoalTerminal(goal.status)) {
    list.push({ key: 'edit', label: 'Edit Goal', disabled: busy || needsReconciliation.value, run: () => emit('edit') })
    list.push({ key: 'complete', label: 'Complete', disabled: busy, run: () => emit('complete') })
  }
  list.push({ key: 'clear', label: 'Clear', disabled: busy, run: () => emit('clear'), tone: 'danger' })
  return list
})
// A failed Goal request needs a reload first; otherwise the main next step.
const quickAction = computed(() =>
  actions.value.find(action => action.key === 'refresh')
  ?? actions.value.find(action => action.tone === 'primary'))
const summaryErrorId = computed(() => `goal-status-error-${props.goal.id}`)
// The dialog opened by an action (Edit Goal) must not sit under the sheet.
function runFromSheet(action: GoalAction) {
  expanded.value = false
  action.run()
}

// Phones get a bottom sheet with pinned actions instead of the inline row.
const PHONE_QUERY = '(max-width: 640px)'
const isPhone = ref(false)
let phoneQuery: MediaQueryList | null = null
function syncPhone() { isPhone.value = Boolean(phoneQuery?.matches) }
onMounted(() => {
  if (typeof window.matchMedia !== 'function') return
  phoneQuery = window.matchMedia(PHONE_QUERY)
  syncPhone()
  phoneQuery.addEventListener('change', syncPhone)
})
// The sheet is modal on phones: Escape works wherever focus is, Tab stays in
// it, and the page behind does not scroll.
function onDocumentKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') expanded.value = false
}
function trapFocus(event: KeyboardEvent) {
  if (!isPhone.value) return
  const sheet = closeEl.value?.closest<HTMLElement>('.goal-status-details')
  const focusable = sheet ? [...sheet.querySelectorAll<HTMLElement>('button:not(:disabled)')] : []
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
let lockedOverflow: string | null = null
function setSheetOpen(open: boolean) {
  if (open && lockedOverflow === null) {
    lockedOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.addEventListener('keydown', onDocumentKeydown)
  } else if (!open && lockedOverflow !== null) {
    document.body.style.overflow = lockedOverflow
    lockedOverflow = null
    document.removeEventListener('keydown', onDocumentKeydown)
  }
}
onBeforeUnmount(() => {
  phoneQuery?.removeEventListener('change', syncPhone)
  setSheetOpen(false)
})
watch([expanded, isPhone], ([open, phone], [wasOpen, wasPhone]) => {
  // Crossing the breakpoint moves the panel: close it rather than lose focus.
  if (open && wasOpen && phone !== wasPhone) {
    expanded.value = false
    return
  }
  setSheetOpen(open && phone)
  if (!phone) return
  void nextTick(() => (open ? closeEl.value : summaryEl.value)?.focus())
})
</script>

<style scoped>
.goal-status { position: relative; display: flex; align-items: center; gap: 8px; padding: 7px 28px; border-top: 1px solid var(--ch-color-border-muted); border-bottom: 1px solid var(--ch-color-border-muted); background: var(--ch-color-surface); font-size: 12px; }
.goal-status-summary { display: flex; min-width: 0; flex: 1; align-items: center; gap: 8px; padding: 2px 0; color: inherit; text-align: left; background: transparent; border: 0; cursor: pointer; }
.goal-status-dot { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: var(--ch-color-accent); }
[data-status='paused'] .goal-status-dot, [data-status='blocked'] .goal-status-dot, [data-status='usageLimited'] .goal-status-dot, [data-status='budgetLimited'] .goal-status-dot { background: var(--ch-color-warning, #e0a800); }
[data-status='complete'] .goal-status-dot { background: var(--ch-color-success, #30a46c); }
[data-status='cancelled'] .goal-status-dot, [data-status='failed'] .goal-status-dot { background: var(--ch-color-danger, #e5484d); }
.goal-objective { min-width: 0; overflow: hidden; color: var(--ch-color-text-muted); text-overflow: ellipsis; white-space: nowrap; }
.goal-status-chevron { flex: 0 0 auto; margin-left: auto; color: var(--ch-color-text-muted); }
.goal-status-actions { display: flex; gap: 5px; }
.goal-status-actions button, .goal-status-quick, .goal-sheet-actions button { padding: 3px 8px; color: var(--ch-color-text); background: transparent; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); cursor: pointer; }
.goal-status-actions button:disabled, .goal-status-quick:disabled, .goal-sheet-actions button:disabled { cursor: default; opacity: .55; }
.goal-status-details { position: absolute; right: 0; bottom: calc(100% + 8px); left: 0; z-index: 4; max-height: min(60dvh, 480px); padding: 12px; overflow-y: auto; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-md); background: var(--ch-color-surface-elevated, var(--ch-color-surface)); box-shadow: var(--ch-shadow-md); font-size: 12px; }
.goal-status-details-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.goal-status-details-header strong { flex: 1; min-width: 0; }
.goal-status-details-header .goal-status-dot { display: none; }
.goal-status-details-close { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; padding: 0; color: var(--ch-color-text-muted); font-size: 18px; line-height: 1; background: transparent; border: 0; border-radius: var(--ch-radius-sm); cursor: pointer; }
.goal-status-details-close:hover { color: var(--ch-color-text); background: var(--ch-color-surface-control-hover); }
.goal-status-details p { margin: 0 0 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
.goal-status-meta { display: flex; flex-wrap: wrap; gap: 12px; color: var(--ch-color-text-subtle); }
.goal-status-message, .goal-status-error { color: var(--ch-color-warning, #e0a800); }
.goal-status-error { color: var(--ch-color-danger, #e5484d); }
.goal-status-error--summary { max-width: 260px; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.goal-checkpoint { padding-top: 8px; border-top: 1px solid var(--ch-color-border-muted); }
.goal-checkpoint p { margin: 5px 0 0; color: var(--ch-color-text-muted); }
.goal-checkpoint-warning { color: var(--ch-color-warning, #e0a800); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (pointer: coarse) { .goal-status-summary, .goal-status-actions button, .goal-status-details-close { min-height: 44px; } .goal-status-actions button, .goal-status-details-close { min-width: 44px; } }

/* Phones: one tappable row (summary + one quick action); the details open as a
   bottom sheet whose actions stay pinned below the scrolling text, so a long
   Goal never pushes them out of reach. */
@media (max-width: 640px) {
  .goal-status { gap: 8px; padding: 4px 6px 4px 12px; font-size: 13px; }
  .goal-status-summary { flex: 1 1 auto; min-width: 0; min-height: 44px; }
  .goal-status-quick { flex: 0 0 auto; min-width: 72px; min-height: 40px; padding: 0 14px; font-size: 13px; font-weight: 600; }
  .goal-status-error--summary { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  .goal-status[data-error] .goal-status-summary .goal-status-dot { background: var(--ch-color-danger, #e5484d); }
  .goal-sheet-backdrop { position: fixed; inset: 0; z-index: 1000; background: rgb(0 0 0 / 45%); }
  .goal-status-details--sheet { position: fixed; top: auto; right: 0; bottom: 0; left: 0; z-index: 1001; display: flex; flex-direction: column; max-height: 85dvh; padding: 0; overflow: hidden; border-width: 1px 0 0; border-radius: 16px 16px 0 0; font-size: 14px; line-height: 1.5; box-shadow: 0 -12px 32px rgb(0 0 0 / 30%); }
  .goal-status-details--sheet .goal-status-details-header { flex: 0 0 auto; margin: 0; padding: 6px 6px 6px 16px; border-bottom: 1px solid var(--ch-color-border-muted); }
  .goal-status-details--sheet .goal-status-details-header .goal-status-dot { display: inline-block; }
  .goal-status-details--sheet .goal-status-details-close { width: 44px; height: 44px; font-size: 22px; }
  .goal-status-details-body { flex: 1 1 auto; min-height: 0; padding: 12px 16px; overflow-y: auto; overscroll-behavior: contain; -webkit-overflow-scrolling: touch; }
  .goal-sheet-actions { flex: 0 0 auto; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 10px 16px calc(10px + env(safe-area-inset-bottom)); border-top: 1px solid var(--ch-color-border-muted); background: inherit; }
  .goal-sheet-actions button { min-height: 44px; padding: 0 12px; font-size: 14px; }
  .goal-sheet-actions button:only-child, .goal-sheet-actions button:last-child:nth-child(odd) { grid-column: 1 / -1; }
}
.goal-action--primary { color: white !important; background: var(--ch-color-accent-strong) !important; border-color: var(--ch-color-accent-strong) !important; }
.goal-action--danger { color: var(--ch-color-danger, #e5484d) !important; border-color: color-mix(in srgb, var(--ch-color-danger, #e5484d) 45%, transparent) !important; }
</style>
