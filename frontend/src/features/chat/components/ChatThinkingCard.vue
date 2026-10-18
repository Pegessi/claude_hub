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
.thinking-body {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
}

.thinking-card {
  align-self: flex-start;
  width: min(85%, 680px);
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  padding: 7px 10px;
}

.thinking-card summary {
  cursor: pointer;
  list-style: none;
}

.thinking-card summary::-webkit-details-marker {
  display: none;
}
.thinking-card summary {
  position: sticky;
  top: 0;
  z-index: 1;
}

.thinking-card summary {
  margin: -7px -10px 0;
  padding: 7px 10px;
  background: var(--ch-color-surface-soft);
}
.details-collapse {
  display: block;
  width: 100%;
  margin-top: 8px;
  padding: 5px 0;
  border: none;
  border-top: 1px solid var(--ch-color-border-muted);
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

.thinking-card summary {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  font-weight: 600;
}

.thinking-card summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.thinking-card--active {
  border-style: solid;
}

.thinking-chevron {
  margin-left: auto;
  font-size: 16px;
  line-height: 1;
  transition: transform 160ms ease;
}

.thinking-card[open] > summary .thinking-chevron {
  transform: rotate(90deg);
}

.thinking-body {
  margin: 8px 0 0;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.5;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}
@media (max-width: 640px) {


  .thinking-card {
    width: min(92%, 680px);
    max-width: 92%;
  }
}

@media (prefers-reduced-motion: reduce) {


  .thinking-chevron {
    transition: none;
  }
}
</style>
