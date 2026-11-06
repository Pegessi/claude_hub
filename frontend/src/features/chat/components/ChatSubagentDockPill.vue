<template>
  <div
    v-if="counts.total > 0"
    ref="rootRef"
    class="subagent-dock"
    data-testid="subagent-dock"
    @keydown.esc.stop="close(true)"
  >
    <button
      ref="triggerRef"
      type="button"
      class="subagent-dock-pill"
      :class="{ 'is-running': counts.running > 0, 'is-open': open }"
      data-testid="subagent-dock-pill"
      aria-haspopup="dialog"
      :aria-expanded="open"
      :aria-controls="popoverId"
      :aria-label="`Sub-agents: ${label}`"
      @click.stop="toggle"
    >
      <span
        class="subagent-dock-dot"
        aria-hidden="true"
      />
      <span class="subagent-dock-label">{{ label }}</span>
    </button>
    <div
      v-if="open"
      :id="popoverId"
      class="subagent-popover"
      data-testid="subagent-popover"
      role="dialog"
      aria-label="Sub-agents"
      @click.stop
    >
      <header class="subagent-popover-head">
        <strong>Sub-agents</strong>
        <span class="subagent-popover-count">{{ counts.total }}</span>
      </header>
      <div class="subagent-popover-body">
        <ChatSubagentList
          :chips="chips"
          :now-ms="nowMs"
          @jump="onJump"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { subagentDockCounts, type SubagentJumpChip } from '../presentation/subagentJump'
import ChatSubagentList from './ChatSubagentList.vue'

const props = defineProps<{
  chips: SubagentJumpChip[]
}>()

const emit = defineEmits<{
  (event: 'jump', chip: SubagentJumpChip): void
}>()

const popoverId = `subagent-popover-${Math.random().toString(36).slice(2, 8)}`
const open = ref(false)
const nowMs = ref(Date.now())
const rootRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)

const counts = computed(() => subagentDockCounts(props.chips))
const label = computed(() =>
  counts.value.running > 0 ? `${counts.value.running} running` : `${counts.value.history} done`,
)

function toggle() {
  if (open.value) close(false)
  else {
    nowMs.value = Date.now()
    open.value = true
  }
}

function close(restoreFocus: boolean) {
  if (!open.value) return
  open.value = false
  if (restoreFocus) void nextTick(() => triggerRef.value?.focus())
}

function onJump(chip: SubagentJumpChip) {
  close(false)
  emit('jump', chip)
}

function onOutsidePointer(event: PointerEvent) {
  if (rootRef.value && !rootRef.value.contains(event.target as Node)) close(false)
}

watch(open, value => {
  if (value) document.addEventListener('pointerdown', onOutsidePointer, true)
  else document.removeEventListener('pointerdown', onOutsidePointer, true)
})

// The pill disappears with the last sub-agent; never leave the popover open.
watch(() => counts.value.total, total => {
  if (total === 0) open.value = false
})

onBeforeUnmount(() => document.removeEventListener('pointerdown', onOutsidePointer, true))
</script>

<style scoped>
.subagent-dock {
  position: relative;
  flex: none;
}

/* Matches the 24px raised pill used by the session name beside it. */
.subagent-dock-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: var(--pane-chrome-control-size, 24px);
  padding: 0 9px;
  border-radius: 999px;
  background: var(--ch-color-surface-raised);
  border: 1px solid var(--ch-color-border-muted);
  box-shadow: 0 1px 4px var(--ch-shadow-color-soft);
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.5;
  white-space: nowrap;
  cursor: pointer;
  transition:
    color var(--ch-motion-fast),
    border-color var(--ch-motion-fast);
}

.subagent-dock-pill:hover,
.subagent-dock-pill.is-open {
  color: var(--ch-color-text-strong);
  border-color: var(--ch-color-border-strong);
}

.subagent-dock-pill:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 1px;
}

.subagent-dock-pill.is-running {
  color: var(--ch-color-text);
  border-color: var(--ch-color-accent-ring);
}

.subagent-dock-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}

.is-running .subagent-dock-dot {
  background: var(--ch-color-accent);
  box-shadow: 0 0 0 0 var(--ch-color-accent-ring);
  animation: subagent-dock-pulse 1.8s ease-out infinite;
}

@keyframes subagent-dock-pulse {
  70% {
    box-shadow: 0 0 0 5px transparent;
  }

  100% {
    box-shadow: 0 0 0 0 transparent;
  }
}

.subagent-popover {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 10;
  width: min(360px, 80vw);
  max-height: min(420px, 70vh);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-popover);
  overflow: hidden;
}

.subagent-popover-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 9px 12px 7px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 12px;
  color: var(--ch-color-text-strong);
}

.subagent-popover-count {
  font-size: 11px;
  color: var(--ch-color-text-subtle);
  font-variant-numeric: tabular-nums;
}

.subagent-popover-body {
  overflow-y: auto;
  padding: 6px;
}

@media (prefers-reduced-motion: reduce) {
  .subagent-dock-pill {
    transition: none;
  }

  .is-running .subagent-dock-dot {
    animation: none;
  }
}
</style>
