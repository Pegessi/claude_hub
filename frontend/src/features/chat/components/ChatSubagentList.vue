<template>
  <div
    class="subagent-list"
    data-testid="subagent-list"
  >
    <section
      v-if="active.length > 0"
      class="subagent-section"
      aria-label="Active sub-agents"
    >
      <h3 class="subagent-section-title">
        Active <span class="subagent-section-count">{{ active.length }}</span>
      </h3>
      <ul class="subagent-rows">
        <li
          v-for="chip in active"
          :key="chip.key"
        >
          <ChatSubagentRow
            :chip="chip"
            :now-ms="nowMs"
            @jump="emit('jump', $event)"
            @open="onOpen"
          />
        </li>
      </ul>
    </section>
    <section
      v-if="earlier.length > 0"
      class="subagent-section"
      aria-label="Earlier sub-agents"
    >
      <button
        type="button"
        class="subagent-section-toggle"
        data-testid="subagent-earlier-toggle"
        :aria-expanded="earlierOpen"
        :aria-controls="earlierId"
        @click="toggleEarlier"
      >
        <span
          class="subagent-chevron"
          :class="{ 'is-open': earlierOpen }"
          aria-hidden="true"
        >›</span>
        Earlier <span class="subagent-section-count">{{ earlier.length }}</span>
      </button>
      <ul
        v-show="earlierOpen"
        :id="earlierId"
        class="subagent-rows"
      >
        <li
          v-for="chip in earlier"
          :key="chip.key"
        >
          <ChatSubagentRow
            :chip="chip"
            :now-ms="nowMs"
            @jump="emit('jump', $event)"
            @open="onOpen"
          />
        </li>
      </ul>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import type { SubagentDockRow, SubagentJumpChip } from '../presentation/subagentJump'
import ChatSubagentRow from './ChatSubagentRow.vue'

const props = defineProps<{
  chips: SubagentDockRow[]
  nowMs: number
}>()

const emit = defineEmits<{
  (event: 'jump', chip: SubagentJumpChip): void
  (event: 'open', chip: SubagentDockRow, source: HTMLElement | null): void
}>()

function onOpen(chip: SubagentDockRow, source: HTMLElement | null) {
  emit('open', chip, source)
}

const earlierId = `subagent-earlier-${Math.random().toString(36).slice(2, 8)}`

const active = computed(() => props.chips.filter(chip => chip.group === 'active'))
const earlier = computed(() => props.chips.filter(chip => chip.group === 'earlier'))
// History stays out of the way while something is running; with nothing
// running it is the whole point of the list, so it opens by default.
const earlierOverride = ref<boolean | null>(null)
const earlierOpen = computed(() => earlierOverride.value ?? active.value.length === 0)

function toggleEarlier() {
  earlierOverride.value = !earlierOpen.value
}
</script>

<style scoped>
.subagent-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.subagent-section-title,
.subagent-section-toggle {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 0;
  padding: 4px 6px;
  border: 0;
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.subagent-section-toggle {
  width: 100%;
  cursor: pointer;
  border-radius: var(--ch-radius-sm);
  text-align: left;
}

.subagent-section-toggle:hover {
  color: var(--ch-color-text-muted);
}

.subagent-section-toggle:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: -2px;
}

.subagent-section-count {
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}

.subagent-chevron {
  display: inline-block;
  width: 10px;
  transition: transform var(--ch-motion-fast);
}

.subagent-chevron.is-open {
  transform: rotate(90deg);
}

.subagent-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

@media (prefers-reduced-motion: reduce) {
  .subagent-chevron {
    transition: none;
  }
}
</style>
