<template>
  <div
    v-if="chip"
    class="hub-subagent-link"
    data-testid="hub-subagent-link"
    :data-state="chip.state"
  >
    <span
      class="hub-subagent-dot"
      :class="`is-${chip.state}`"
      aria-hidden="true"
    />
    <span class="hub-subagent-name">{{ chip.name }}</span>
    <span class="hub-subagent-state">{{ chip.stateLabel }}</span>
    <button
      v-if="chip.canJump"
      type="button"
      class="hub-subagent-open"
      @click="dock?.jump(chip)"
    >
      {{ chip.archived ? 'Restore & open' : 'Open' }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import { findDockChip, useSubagentDock } from '../controllers/subagentDock'
import { hubSubagentIdFromTool } from '../presentation/subagentJump'

const props = defineProps<{
  argsText: string
  resultText: string
}>()

const dock = useSubagentDock()
// Only a `claude-hub agent run/send` call whose output names a sub-agent id
// that this Chat's own list also contains is linked; anything else stays a
// plain command block.
const chip = computed(() =>
  findDockChip(dock, hubSubagentIdFromTool(props.argsText, props.resultText)),
)
</script>

<style scoped>
.hub-subagent-link {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 2px 8px 4px 30px;
  font-size: 11px;
  color: var(--ch-color-text-muted);
}

.hub-subagent-dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}

.hub-subagent-dot.is-running {
  background: var(--ch-color-accent);
  animation: hub-subagent-pulse 1.6s ease-in-out infinite;
}

.hub-subagent-dot.is-completed {
  background: var(--ch-color-success-strong);
}

.hub-subagent-dot.is-failed,
.hub-subagent-dot.is-timeout {
  background: var(--ch-color-danger-strong);
}

@keyframes hub-subagent-pulse {
  50% {
    opacity: 0.35;
  }
}

.hub-subagent-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ch-color-text);
}

.hub-subagent-open {
  padding: 0 4px;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: none;
  color: var(--ch-color-accent);
  font: inherit;
  cursor: pointer;
}

.hub-subagent-open:focus-visible {
  outline: 2px solid var(--ch-color-accent);
}

@media (prefers-reduced-motion: reduce) {
  .hub-subagent-dot.is-running {
    animation: none;
  }
}
</style>
