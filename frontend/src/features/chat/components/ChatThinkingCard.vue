<template>
  <details
    class="thinking-card"
    :class="{ 'thinking-card--active': part.state === 'active' }"
  >
    <summary>
      <ThinkingIndicator :state="part.state" />
      <span
        class="thinking-chevron"
        aria-hidden="true"
      >›</span>
    </summary>
    <!-- Thinking is rendered as plain text (no marked/DOMPurify) to
       avoid re-parsing multi-kilobyte reasoning streams on every
       delta. Whitespace is preserved with pre-wrap. -->
    <pre class="thinking-body">{{ part.text }}</pre>
    <button
      type="button"
      class="details-collapse"
      @click="collapseDetails"
    >
      收起
    </button>
  </details>
</template>

<script setup lang="ts">
import { requireChatOwner, chatTimelineKey } from '../owners'
import ThinkingIndicator from '@/components/ThinkingIndicator.vue'
import type { TimelinePart } from '../timeline/types'
import type { ThinkingDisplayPart } from '@/utils/thinkingPresentation'
const { collapseDetails } = requireChatOwner(chatTimelineKey)
defineProps<{
  part: Extract<ThinkingDisplayPart<TimelinePart>, { kind: 'thinking' }>
}>()
</script>

<style scoped>
/* Reasoning is secondary to the answer: a quiet line like tool activity,
   opening into a quoted block rather than a second chat bubble. */
.thinking-card {
  align-self: flex-start;
  width: 100%;
  color: var(--ch-color-text-muted);
}

.thinking-card summary {
  position: sticky;
  top: 0;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  max-width: 100%;
  min-height: 30px;
  padding: 4px 8px;
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  list-style: none;
  transition: background-color 120ms ease;
}

.thinking-card summary::-webkit-details-marker {
  display: none;
}

.thinking-card summary:hover {
  background: var(--ch-color-row-hover, var(--ch-color-surface-soft));
}

.thinking-card[open] summary {
  background: var(--ch-color-surface-glass, var(--ch-color-main-bg));
  backdrop-filter: blur(10px);
}

.thinking-card summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.thinking-chevron {
  font-size: 15px;
  line-height: 1;
  opacity: 0;
  transition: transform 160ms ease, opacity 120ms ease;
}

.thinking-card summary:hover .thinking-chevron,
.thinking-card summary:focus-visible .thinking-chevron,
.thinking-card[open] > summary .thinking-chevron {
  opacity: 1;
}

.thinking-card[open] > summary .thinking-chevron {
  transform: rotate(90deg);
}

.thinking-body {
  margin: 4px 0 0 14px;
  padding: 2px 0 2px 14px;
  border-left: 2px solid var(--ch-color-border-muted);
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--ch-color-text-subtle);
  white-space: pre-wrap;
  word-break: break-word;
}

.details-collapse {
  display: block;
  margin: 6px 0 0 14px;
  padding: 3px 14px;
  border: none;
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.details-collapse:hover {
  color: var(--ch-color-text-muted);
}

.details-collapse:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

@media (prefers-reduced-motion: reduce) {
  .thinking-card summary,
  .thinking-chevron {
    transition: none;
  }
}
</style>
