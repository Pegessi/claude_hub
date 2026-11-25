<template>
  <button
    type="button"
    class="subagent-row"
    :class="{ 'has-aside': hasAside }"
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
    <span class="subagent-line">
      <span class="subagent-name">{{ displayName }}</span>
      <span class="subagent-type">{{ chip.agentType }}</span>
    </span>
    <span
      v-if="time"
      class="subagent-time"
    >{{ time }}</span>
    <span class="subagent-summary">{{ chip.summary }}</span>
    <span
      v-if="showAction"
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
const showAction = computed(() => props.chip.canJump || props.chip.canOpen)
const hasAside = computed(() => Boolean(time.value || showAction.value))
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
  display: grid;
  grid-template-columns: 7px minmax(0, 1fr);
  grid-template-rows: auto auto;
  column-gap: 8px;
  row-gap: 2px;
  align-items: center;
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

.subagent-row.has-aside {
  grid-template-columns: 7px minmax(0, 1fr) auto;
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
  grid-column: 1;
  grid-row: 1;
  align-self: center;
  width: 7px;
  height: 7px;
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

.subagent-line {
  grid-column: 2;
  grid-row: 1;
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
  line-height: 1.35;
  font-weight: 600;
  color: var(--ch-color-text-strong);
}

.subagent-type,
.subagent-time {
  font-size: 10px;
  line-height: 1.35;
  color: var(--ch-color-text-subtle);
  white-space: nowrap;
}

.subagent-type {
  flex: none;
}

.subagent-time {
  grid-column: 3;
  grid-row: 1;
  justify-self: end;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.subagent-summary {
  grid-column: 2;
  grid-row: 2;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  line-height: 1.35;
  color: var(--ch-color-text-muted);
}

.subagent-action {
  grid-column: 3;
  grid-row: 2;
  justify-self: end;
  text-align: right;
  font-size: 11px;
  line-height: 1.35;
  color: var(--ch-color-text-subtle);
  white-space: nowrap;
  transition: color var(--ch-motion-fast);
}

.subagent-row:hover:not(:disabled) .subagent-action,
.subagent-row:focus-visible .subagent-action {
  color: var(--ch-color-accent);
}

@media (max-width: 768px), (pointer: coarse) {
  .subagent-row {
    min-height: 44px;
    padding: 8px 10px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .subagent-row,
  .subagent-action {
    transition: none;
  }

  .subagent-dot.is-running {
    animation: none;
  }
}
</style>
