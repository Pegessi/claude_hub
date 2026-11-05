<template>
  <section
    v-if="chips.length > 0"
    class="subagent-jump-card"
    data-testid="subagent-jump-card"
    aria-label="Hub sub-agents started from this Chat"
  >
    <div class="subagent-jump-head">
      <span class="subagent-jump-badge">Sub-agents</span>
      <strong>{{ chips.length }}</strong>
    </div>
    <ul class="subagent-jump-list">
      <li
        v-for="chip in chips"
        :key="chip.key"
      >
        <button
          type="button"
          class="subagent-jump-row"
          :data-testid="`subagent-jump-row-${chip.key}`"
          :data-state="chip.state"
          :disabled="!chip.canJump"
          :title="chip.canJump
            ? (chip.archived ? `Restore and open ${chip.name}` : `Open ${chip.name}`)
            : 'The sub-agent Chat tab is unavailable'"
          @click="emitJump(chip)"
        >
          <span class="subagent-jump-idline">
            <span class="subagent-jump-name">{{ chip.name }}</span>
            <span class="subagent-jump-id">{{ chip.shortId }}</span>
          </span>
          <span class="subagent-jump-summary">{{ chip.summary }}</span>
          <span class="subagent-jump-meta">
            <span
              v-if="chip.archived"
              class="subagent-jump-archived"
            >archived</span>
            <span class="subagent-jump-status">{{ chip.stateLabel }}</span>
            <span
              class="subagent-jump-arrow"
              aria-hidden="true"
            >→</span>
          </span>
        </button>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import type { SubagentJumpChip } from '../presentation/subagentJump'

defineProps<{
  chips: SubagentJumpChip[]
}>()

const emit = defineEmits<{
  (event: 'jump', chip: SubagentJumpChip): void
}>()

function emitJump(chip: SubagentJumpChip) {
  if (chip.canJump) emit('jump', chip)
}
</script>

<style scoped>
.subagent-jump-card {
  margin: 6px 12px 4px;
  border: 1px solid var(--ch-color-border-muted, var(--ch-color-border));
  border-radius: var(--ch-radius-md, 8px);
  background: var(--ch-color-surface-muted, transparent);
  overflow: hidden;
}

.subagent-jump-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  border-bottom: 1px solid var(--ch-color-border-muted, var(--ch-color-border));
}

.subagent-jump-badge {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 999px;
  background-color: var(--ch-color-border);
  color: var(--ch-color-text-muted);
}

.subagent-jump-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.subagent-jump-row {
  width: 100%;
  display: grid;
  grid-template-columns: minmax(140px, max-content) minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border: 0;
  border-bottom: 1px solid var(--ch-color-border-muted, var(--ch-color-border));
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
  font: inherit;
}

.subagent-jump-list li:last-child .subagent-jump-row {
  border-bottom: 0;
}

.subagent-jump-row:hover:not(:disabled) {
  background: var(--ch-color-surface-hover, rgba(139, 148, 158, 0.08));
}

.subagent-jump-row:disabled {
  cursor: default;
  opacity: 0.7;
}

.subagent-jump-idline {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}

.subagent-jump-name {
  font-weight: 600;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 240px;
}

.subagent-jump-id {
  font-size: 10px;
  color: var(--ch-color-text-muted);
  font-variant-numeric: tabular-nums;
}

.subagent-jump-summary {
  font-size: 12px;
  color: var(--ch-color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.subagent-jump-meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
}

.subagent-jump-archived {
  text-transform: uppercase;
  color: var(--ch-color-text-muted);
}

.subagent-jump-status {
  text-transform: uppercase;
  padding: 1px 6px;
  border-radius: 999px;
  background-color: var(--ch-color-border);
  color: var(--ch-color-text-muted);
}

.subagent-jump-row[data-state='completed'] .subagent-jump-status {
  background-color: var(--ch-color-success-bg, rgba(46, 160, 67, 0.12));
  color: var(--ch-color-success-strong, #2ea043);
}

.subagent-jump-row[data-state='failed'] .subagent-jump-status,
.subagent-jump-row[data-state='timeout'] .subagent-jump-status {
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.12));
  color: var(--ch-color-danger-strong, #f85149);
}

.subagent-jump-row[data-state='running'] .subagent-jump-status {
  background-color: var(--ch-color-warning-bg, rgba(224, 168, 0, 0.12));
  color: var(--ch-color-warning, #e0a800);
}

.subagent-jump-arrow {
  color: var(--ch-color-text-muted);
}

.subagent-jump-row:hover:not(:disabled) .subagent-jump-arrow {
  color: var(--ch-color-text);
}
</style>
