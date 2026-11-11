<template>
  <details
    class="subagent-thread-row"
    :data-subagent-thread-id="thread.threadId"
  >
    <summary class="subagent-thread-header">
      <span
        class="subagent-name"
        :title="thread.threadId"
      >{{ thread.threadId }}</span>
      <span class="subagent-headline">{{ subthreadHeadline(thread) }}</span>
      <span
        class="tool-status"
        :class="subthreadStatusClass(thread)"
      >{{ subthreadStatusLabel(thread) }}</span>
    </summary>
    <ChatSubthreadBody
      :turn="turn"
      :thread="thread"
    />
    <button
      type="button"
      class="details-collapse"
      @click="collapseDetails"
    >
      Collapse
    </button>
  </details>
</template>
<script setup lang="ts">
import { requireChatOwner, chatTimelineKey } from '../owners'
import {
  subthreadHeadline,
  subthreadStatusClass,
  subthreadStatusLabel,
} from '@/features/chat/presentation/chatSubagentPresentation'
import ChatSubthreadBody from './ChatSubthreadBody.vue'
import type { TimelineTurn, TimelineSubthread } from '../timeline/types'

/** One child thread inside its parent turn's inline region. Rows live in
 *  `ChatSubthreadBody` so the dock's detail window shows exactly the same
 *  content as the transcript. */
const { collapseDetails } = requireChatOwner(chatTimelineKey)
defineProps<{
  turn: TimelineTurn
  thread: TimelineSubthread
}>()
</script>
<style scoped>
.tool-status {
  flex: 0 0 auto;
  font-size: 10px;
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: 999px;
  background-color: var(--ch-color-border);
  color: var(--ch-color-text-muted);
}

.tool-status.completed {
  background-color: var(--ch-color-success-bg, rgba(46, 160, 67, 0.12));
  color: var(--ch-color-success-strong, #2ea043);
}

.tool-status.failed {
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.12));
  color: var(--ch-color-danger-strong, #f85149);
}

.tool-status.running {
  background-color: var(--ch-color-warning-bg, rgba(224, 168, 0, 0.12));
  color: var(--ch-color-warning, #e0a800);
}

.tool-status.cancelled {
  background-color: var(--ch-color-surface-muted, rgba(139, 148, 158, 0.12));
  color: var(--ch-color-text-muted, #8b949e);
}

.tool-status.unknown {
  color: var(--ch-color-text-subtle);
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

.subagent-name {
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.subagent-headline {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.subagent-thread-row {
  overflow: clip;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-app-bg);
}

.subagent-thread-row > summary {
  position: static;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
  background: transparent;
}

.subagent-thread-row > .subthread-body {
  padding: 8px;
  border-top: 1px solid var(--ch-color-border-muted);
}

.subagent-thread-row .subagent-name {
  flex: 0 1 260px;
}

@media (max-width: 640px) {

  .subagent-thread-row > summary {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .subagent-thread-row .subagent-name {
    flex-basis: 100%;
    max-width: 100%;
  }

  .subagent-thread-row .subagent-headline {
    flex: 1 1 180px;
  }

}

.subagent-thread-row:not([open]) :deep(.thinking-indicator--active) {
  animation: none;
}
</style>
