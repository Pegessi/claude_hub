<template>
  <div
    v-if="counts.running > 0"
    class="subagent-mobile-bar"
  >
    <button
      ref="triggerRef"
      type="button"
      class="subagent-mobile-pill"
      data-testid="subagent-mobile-pill"
      aria-haspopup="dialog"
      :aria-expanded="open"
      @click="openSheet"
    >
      <span
        class="subagent-mobile-dot"
        aria-hidden="true"
      />
      <span>{{ counts.running }} sub-agent{{ counts.running === 1 ? '' : 's' }} running</span>
      <span aria-hidden="true">›</span>
    </button>
  </div>
  <Teleport to="body">
    <div
      v-if="open"
      class="subagent-sheet-backdrop"
      data-testid="subagent-sheet-backdrop"
      @click.self="closeSheet"
      @keydown.esc.stop="closeSheet"
    >
      <div
        class="subagent-sheet"
        data-testid="subagent-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="Sub-agents"
      >
        <span
          class="subagent-sheet-grip"
          aria-hidden="true"
        />
        <header class="subagent-sheet-head">
          <strong>Sub-agents</strong>
          <button
            ref="closeRef"
            type="button"
            class="subagent-sheet-close"
            aria-label="Close sub-agents"
            @click="closeSheet"
          >
            Done
          </button>
        </header>
        <div class="subagent-sheet-body">
          <ChatSubagentList
            :chips="chips"
            :now-ms="nowMs"
            @jump="onJump"
          />
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import { subagentDockCounts, type SubagentJumpChip } from '../presentation/subagentJump'
import ChatSubagentList from './ChatSubagentList.vue'

const props = defineProps<{
  chips: SubagentJumpChip[]
}>()

const emit = defineEmits<{
  (event: 'jump', chip: SubagentJumpChip): void
}>()

const open = ref(false)
const nowMs = ref(Date.now())
const triggerRef = ref<HTMLButtonElement | null>(null)
const closeRef = ref<HTMLButtonElement | null>(null)
const counts = computed(() => subagentDockCounts(props.chips))

function openSheet() {
  nowMs.value = Date.now()
  open.value = true
  void nextTick(() => closeRef.value?.focus())
}

function closeSheet() {
  open.value = false
  void nextTick(() => triggerRef.value?.focus())
}

function onJump(chip: SubagentJumpChip) {
  open.value = false
  emit('jump', chip)
}

// Nothing running means nothing to show: the bar and its sheet go away.
watch(() => counts.value.running, running => {
  if (running === 0) open.value = false
})
</script>

<style scoped>
.subagent-mobile-bar {
  flex: none;
  display: flex;
  padding: 4px 12px 0;
}

.subagent-mobile-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 28px;
  padding: 0 11px;
  border: 1px solid var(--ch-color-accent-ring);
  border-radius: 999px;
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.subagent-mobile-pill:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
}

.subagent-mobile-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ch-color-accent);
  animation: subagent-mobile-pulse 1.6s ease-in-out infinite;
}

@keyframes subagent-mobile-pulse {
  50% {
    opacity: 0.35;
  }
}

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
  justify-content: space-between;
  padding: 6px 14px 8px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 13px;
  color: var(--ch-color-text-strong);
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
  .subagent-mobile-dot {
    animation: none;
  }

  .subagent-sheet {
    animation: none;
  }
}
</style>
