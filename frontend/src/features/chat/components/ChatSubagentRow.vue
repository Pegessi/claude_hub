<template>
  <button
    type="button"
    class="subagent-row"
    :data-testid="`subagent-row-${chip.key}`"
    :data-kind="chip.kind"
    :data-state="chip.state"
    :disabled="!chip.canJump && !chip.canOpen"
    :aria-label="`${actionLabel} ${chip.name}, ${chip.stateLabel}`"
    :title="unavailableTitle"
    @click="activate"
  >
    <span
      class="subagent-dot"
      :class="`is-${chip.state}`"
      aria-hidden="true"
    />
    <span class="subagent-main">
      <span class="subagent-line">
        <span class="subagent-name">{{ displayName }}</span>
        <span class="subagent-type">{{ chip.agentType }}</span>
        <span
          v-if="time"
          class="subagent-time"
        >{{ time }}</span>
      </span>
      <span class="subagent-summary">{{ chip.summary }}</span>
    </span>
    <span
      v-if="chip.canJump || chip.canOpen"
      class="subagent-action"
    >{{ actionLabel }}<span aria-hidden="true"> ›</span></span>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { relativeSubagentTime, subagentDisplayName, type SubagentDockRow, type SubagentJumpChip } from '../presentation/subagentJump'

/** One dock row. Hub children jump to their own Chat tab; native children
 *  have no tab, so the row opens the pane's read-only detail window instead. */
const props = defineProps<{
  chip: SubagentDockRow
  nowMs: number
}>()

const emit = defineEmits<{
  (event: 'jump', chip: SubagentJumpChip): void
  (event: 'open', chip: SubagentDockRow, source: HTMLElement | null): void
}>()

const time = computed(() => relativeSubagentTime(props.chip.createdAt, props.nowMs))
const displayName = computed(() => subagentDisplayName(props.chip.name))
const actionLabel = computed(() => {
  if (props.chip.kind === 'native') return 'View'
  if (props.chip.canOpen) return 'View'
  return props.chip.archived ? 'Restore & open' : 'Open'
})
const unavailableTitle = computed(() => {
  if (props.chip.kind === 'native' || props.chip.canOpen) return `View ${props.chip.name}`
  return props.chip.canJump ? `Open ${props.chip.name}` : 'The sub-agent Chat tab is unavailable'
})

function activate(event: MouseEvent) {
  if (props.chip.canOpen) {
    emit('open', props.chip, event.currentTarget as HTMLElement | null)
    return
  }
  if (props.chip.kind === 'hub') {
    emit('jump', props.chip)
  }
}
</script>

<style scoped>
.subagent-row {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 7px 8px;
  border: 0;
  border-radius: var(--ch-radius-md);
  background: transparent;
  color: var(--ch-color-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background-color var(--ch-motion-fast);
}

.subagent-row:hover:not(:disabled) {
  background: var(--ch-color-surface-control-hover);
}

.subagent-row:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: -2px;
}

.subagent-row:disabled {
  cursor: default;
  opacity: 0.7;
}

.subagent-dot {
  flex: none;
  width: 7px;
  height: 7px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}

.subagent-dot.is-running {
  background: var(--ch-color-accent);
  animation: subagent-dot-pulse 1.6s ease-in-out infinite;
}

.subagent-dot.is-completed {
  background: var(--ch-color-success-strong);
}

.subagent-dot.is-failed,
.subagent-dot.is-timeout {
  background: var(--ch-color-danger-strong);
}

@keyframes subagent-dot-pulse {
  50% {
    opacity: 0.35;
  }
}

.subagent-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.subagent-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}

.subagent-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
  color: var(--ch-color-text-strong);
}

.subagent-type,
.subagent-time {
  flex: none;
  font-size: 10px;
  color: var(--ch-color-text-subtle);
}

.subagent-time {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}

.subagent-summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--ch-color-text-muted);
}

.subagent-action {
  flex: none;
  align-self: center;
  font-size: 11px;
  color: var(--ch-color-text-subtle);
  white-space: nowrap;
}

.subagent-row:hover:not(:disabled) .subagent-action,
.subagent-row:focus-visible .subagent-action {
  color: var(--ch-color-accent);
}

@media (prefers-reduced-motion: reduce) {
  .subagent-row {
    transition: none;
  }

  .subagent-dot.is-running {
    animation: none;
  }
}
</style>
