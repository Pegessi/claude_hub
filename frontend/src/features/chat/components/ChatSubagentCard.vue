<template>
  <div
    class="conversation-row conversation-row--assistant conversation-row--subagent"
  >
    <details class="subagent-card subagent-card--standalone">
      <summary
        class="subagent-header"
        :class="{ 'subagent-header--running': part.tool.status === 'running' }"
      >
        <span class="subagent-idline">
          <span class="subagent-badge">{{ subagentChip(part.tool) }}</span>
          <span class="subagent-name">{{ subagentName(part.tool) }}</span>
          <span
            v-if="part.tool.subagent?.background"
            class="subagent-bg-tag"
          >Background</span>
        </span>
        <span class="subagent-headline">{{ subagentHeadline(part.tool) }}</span>
        <span
          class="tool-status"
          :class="part.tool.status"
        >{{ subagentCallStatusLabel(part.tool.status) }}</span>
      </summary>
      <div class="subagent-body">
        <div
          v-if="subagentPrompt(part.tool)"
          class="tool-block"
        >
          <span>Prompt</span>
          <pre>{{ subagentPrompt(part.tool) }}</pre>
        </div>
        <div
          v-if="part.tool.resultText"
          class="tool-block"
        >
          <span>Result</span>
          <pre>{{ part.tool.resultText }}</pre>
        </div>
        <div
          v-else-if="part.tool.status === 'running'"
          class="subagent-running-note"
        >
          Sub-agent call in progress…
        </div>
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
import { subagentChip, subagentName, subagentHeadline, subagentCallStatusLabel, subagentPrompt } from '@/features/chat/presentation/chatSubagentPresentation'
import type { TimelinePart } from '../timeline/types'
const { collapseDetails } = requireChatOwner(chatTimelineKey)
// Reducer parts mutate in place; revision invalidates this leaf without replacing its DOM.
defineProps<{
  renderRevision: number
  part: Extract<TimelinePart, { kind: 'subagent' }>
}>()
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

.tool-block {
  padding: 0 10px 10px;
}

.tool-block > span {
  display: block;
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.tool-block pre {
  max-height: 220px;
  margin: 5px 0 0;
  padding: 8px;
  overflow: auto;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
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

.subagent-card summary {
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

.subagent-bg-tag {
  flex: 0 0 auto;
  padding: 1px 6px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-muted, rgba(139, 148, 158, 0.14));
  color: var(--ch-color-text-subtle);
  font-size: 10px;
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

.subagent-header--running .subagent-headline {
  color: var(--ch-color-warning, #e0a800);
}

.subagent-body {
  padding: 2px 10px 8px 16px;
  border-top: 1px solid var(--ch-color-border-muted);
}

.subagent-body .tool-block {
  padding: 8px 0 0;
}

.subagent-running-note {
  padding: 8px 0 2px;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  font-style: italic;
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
</style>
