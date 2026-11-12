<template>
  <Teleport to="body">
    <div
      class="subagent-sheet-backdrop"
      data-testid="subagent-sheet-backdrop"
      @click.self="emit('close')"
      @keydown.esc.stop="emit('close')"
    >
      <div
        :id="props.id"
        ref="sheetRef"
        class="subagent-sheet"
        data-testid="subagent-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="Sub-agents"
        @keydown.tab="onTab"
      >
        <span
          class="subagent-sheet-grip"
          aria-hidden="true"
        />
        <header class="subagent-sheet-head">
          <strong>Sub-agents</strong>
          <span class="subagent-sheet-count">{{ props.chips.length }}</span>
          <button
            ref="closeRef"
            type="button"
            class="subagent-sheet-close"
            aria-label="Close sub-agents"
            @click="emit('close')"
          >
            Done
          </button>
        </header>
        <div class="subagent-sheet-body">
          <ChatSubagentList
            :chips="props.chips"
            :now-ms="props.nowMs"
            @jump="onJump"
            @open="onOpen"
          />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import type { SubagentDockRow, SubagentJumpChip } from '../presentation/subagentJump'
import ChatSubagentList from './ChatSubagentList.vue'

/** The mobile reveal for the Agents pill: a bottom sheet, the same native
 *  pattern as the other phone-sized overlays. The pane chrome owns the pill;
 *  this component owns only the sheet so the pane never grows a second bar. */
const props = defineProps<{
  /** Dialog id, so the owning pill can point `aria-controls` at it. */
  id: string
  chips: SubagentDockRow[]
  nowMs: number
}>()

const emit = defineEmits<{
  (event: 'close'): void
  (event: 'jump', chip: SubagentJumpChip): void
  (event: 'open', chip: SubagentDockRow, source: HTMLElement | null): void
}>()

const closeRef = ref<HTMLButtonElement | null>(null)
const sheetRef = ref<HTMLElement | null>(null)
let previousBodyOverflow = ''

function onJump(chip: SubagentJumpChip) {
  emit('jump', chip)
}

function onOpen(chip: SubagentDockRow, source: HTMLElement | null) {
  emit('open', chip, source)
}

// Keep Tab inside the modal sheet: without this, focus walks into the pane
// behind the backdrop and only the mouse can bring it back. Only rendered
// buttons count — the "Earlier" rows are v-show hidden while something runs,
// and focusing a display:none button silently does nothing.
function onTab(event: KeyboardEvent) {
  const buttons = sheetRef.value?.querySelectorAll<HTMLElement>('button:not([disabled])')
  const focusables = buttons ? [...buttons].filter(button => button.getClientRects().length > 0) : []
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

// Move focus into the sheet the moment it opens (Esc, Done and the backdrop are
// the only ways out, so all of them must be reachable), and stop the transcript
// behind the fixed backdrop from scrolling under the finger.
onMounted(() => {
  closeRef.value?.focus()
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
})

onBeforeUnmount(() => {
  document.body.style.overflow = previousBodyOverflow
})
</script>

<style scoped>
.subagent-sheet-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: flex-end;
  background: var(--ch-color-backdrop, var(--ch-color-overlay));
  backdrop-filter: blur(var(--ch-backdrop-blur, 0));
}

.subagent-sheet {
  width: 100%;
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ch-color-border-muted);
  border-bottom: 0;
  border-radius: var(--ch-radius-xl) var(--ch-radius-xl) 0 0;
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-dialog);
  padding-bottom: max(10px, env(safe-area-inset-bottom, 0px));
  animation: subagent-sheet-in var(--ch-motion-standard) ease-out;
}

@keyframes subagent-sheet-in {
  from {
    transform: translateY(24px);
    opacity: 0;
  }
}

/* Bottom-sheet affordance, matching native iOS/Android sheets. */
.subagent-sheet-grip {
  align-self: center;
  width: 36px;
  height: 4px;
  margin-top: 8px;
  border-radius: 999px;
  background: var(--ch-color-border-strong, var(--ch-color-border));
}

.subagent-sheet-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px 8px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 13px;
  color: var(--ch-color-text-strong);
}

.subagent-sheet-count {
  margin-right: auto;
  font-size: 11px;
  color: var(--ch-color-text-subtle);
  font-variant-numeric: tabular-nums;
}

.subagent-sheet-close {
  min-height: 32px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--ch-radius-md);
  background: none;
  color: var(--ch-color-accent);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.subagent-sheet-close:focus-visible {
  outline: 2px solid var(--ch-color-accent);
}

.subagent-sheet-body {
  overflow-y: auto;
  padding: 8px;
}

@media (prefers-reduced-motion: reduce) {
  .subagent-sheet {
    animation: none;
  }
}
</style>
