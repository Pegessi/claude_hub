<template>
  <span
    class="thinking-label"
    :class="`thinking-label--${state}`"
    role="status"
    aria-live="polite"
    aria-atomic="true"
  >
    <span
      class="thinking-indicator"
      :class="`thinking-indicator--${state}`"
      aria-hidden="true"
    >{{ state === 'done' ? '✓' : state === 'interrupted' ? '–' : '' }}</span>
    <span>{{ state === 'active' ? 'Thinking…' : state === 'interrupted' ? 'Thinking interrupted' : 'Thought' }}</span>
  </span>
</template>

<script setup lang="ts">
import type { ThinkingState } from '@/utils/thinkingPresentation'

defineProps<{ state: ThinkingState }>()
</script>

<style scoped>
.thinking-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  line-height: 1.5;
  font-weight: 500;
}

.thinking-label--active {
  color: var(--ch-color-text-muted);
}

.thinking-indicator {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  flex: 0 0 12px;
  width: 12px;
  height: 12px;
  font-size: 12px;
  line-height: 1;
}

.thinking-indicator--active {
  border: 2px solid var(--ch-color-border-muted);
  border-top-color: var(--ch-color-accent-ring);
  border-right-color: var(--ch-color-accent-ring);
  border-radius: 50%;
  animation: thinking-turn 1.1s linear infinite;
}

@keyframes thinking-turn {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .thinking-indicator--active {
    animation: none;
  }
}
</style>
