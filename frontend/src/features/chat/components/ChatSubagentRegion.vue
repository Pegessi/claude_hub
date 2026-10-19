<template>
  <div
    class="conversation-row conversation-row--assistant conversation-row--subagent"
  >
    <div
      v-if="part.compact"
      class="subagent-card subagent-region subagent-region--compact"
      data-testid="subagent-region"
    >
      <div class="subagent-header">
        <span class="subagent-idline">
          <span class="subagent-badge">Native sub-agents</span>
        </span>
        <span class="subagent-headline">{{ subagentRegionHeadline(part) }}</span>
        <span
          class="tool-status subagent-region-status"
          :class="subagentRegionStatusClass(part)"
        >{{ subagentRegionStatusLabel(part) }}</span>
      </div>
    </div>
    <details
      v-else
      class="subagent-card subagent-region"
      data-testid="subagent-region"
    >
      <summary class="subagent-header">
        <span class="subagent-idline">
          <span class="subagent-badge">Native sub-agents</span>
        </span>
        <span class="subagent-headline">{{ subagentRegionHeadline(part) }}</span>
        <span
          class="tool-status subagent-region-status"
          :class="subagentRegionStatusClass(part)"
        >{{ subagentRegionStatusLabel(part) }}</span>
      </summary>
      <div class="subagent-body subagent-region-body">
        <ChatSubthread
          v-for="thread in part.threads"
          :key="thread.key"
          :thread="thread"
          :turn="turn"
        />
      </div>
      <button
        type="button"
        class="details-collapse"
        @click="collapseDetails"
      >
        Collapse
      </button>
    </details>
  </div>
</template>
<script setup lang="ts">
import { requireChatOwner, chatTimelineKey } from '../owners'
import { subagentRegionHeadline, subagentRegionStatusClass, subagentRegionStatusLabel } from '../presentation/chatSubagentPresentation'
import type { TimelineTurn, TimelinePart } from '../timeline/types'
import ChatSubthread from './ChatSubthread.vue'
const { collapseDetails } = requireChatOwner(chatTimelineKey)
defineProps<{ turn: TimelineTurn; part: Extract<TimelinePart, { kind: 'subagents' }> }>()
</script>
<style scoped>
.tool-status {
  font-size: 10px;
  text-transform: uppercase;
  padding: 1px 6px;
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

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
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

.tool-status {
  flex: 0 0 auto;
  padding: 2px 8px;
}
.conversation-row--subagent {
  padding-left: 14px;
}


.subagent-card {
  width: 100%;
  border: 1px solid var(--ch-color-border-muted);
  border-left: 2px solid var(--ch-color-accent);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  overflow: clip;
}

.subagent-card summary {
  cursor: pointer;
  list-style: none;
}

.subagent-card summary::-webkit-details-marker {
  display: none;
}

.subagent-card summary,
.subagent-card .subagent-header {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--ch-color-surface);
}

.subagent-card summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: -2px;
}

.subagent-idline {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 0 0 auto;
  min-width: 0;
}

.subagent-badge {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.02em;
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

.subagent-body {
  padding: 2px 10px 8px 16px;
  border-top: 1px solid var(--ch-color-border-muted);
}
.subagent-region-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.subagent-region-status {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  white-space: nowrap;
}

.tool-status.unknown {
  color: var(--ch-color-text-subtle);
}
@media (max-width: 640px) {


  .subagent-card {
    width: 100%;
    max-width: 100%;
  }
}

.subagent-region:not([open]) :deep(.thinking-indicator--active) {
  animation: none;
}
</style>
