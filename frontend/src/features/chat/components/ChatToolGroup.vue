<template>
  <div
    class="conversation-row conversation-row--assistant"
  >
    <span
      class="conversation-avatar conversation-avatar--tool"
      aria-hidden="true"
    >⌘</span>
    <details
      class="tool-card tool-card--group"
      :open="expandedToolGroups.get(turn.key)?.has(part.key)"
      @toggle="onToolGroupToggle(turn, part.key, $event)"
    >
      <summary
        class="tool-header"
        :class="{ 'tool-header--single': part.tools.length === 1 }"
      >
        <span class="tool-name">
          {{ part.tools.length === 1 ? part.tools[0].name : `${part.tools.length} tools` }}
        </span>
        <template v-if="part.tools.length > 1">
          <span class="tool-group-names">
            {{ toolGroupNames(part.tools) }}
          </span>
        </template>
        <span
          class="tool-status"
          :class="toolGroupStatus(part.tools)"
        >{{ toolStatusLabel(toolGroupStatus(part.tools)) }}</span>
        <span
          class="tool-chevron"
          aria-hidden="true"
        >›</span>
      </summary>
      <template v-if="expandedToolGroups.get(turn.key)?.has(part.key)">
        <div
          v-for="tool in part.tools"
          :key="tool.key"
          class="tool-group-item"
        >
          <div class="tool-group-item-header">
            <span class="tool-name">{{ tool.name }}</span>
            <span
              class="tool-status"
              :class="tool.status"
            >{{ toolStatusLabel(tool.status) }}</span>
          </div>
          <div
            v-if="tool.argsText"
            class="tool-block"
          >
            <span>Input</span>
            <pre
              tabindex="0"
              aria-label="Tool input"
            >{{ tool.argsText }}</pre>
          </div>
          <div
            v-if="tool.resultText"
            class="tool-block"
          >
            <span>Result</span>
            <pre
              tabindex="0"
              aria-label="Tool result"
            >{{ tool.resultText }}</pre>
          </div>
        </div>
        <button
          type="button"
          class="details-collapse"
          @click="collapseDetails"
        >
          Collapse
        </button>
      </template>
    </details>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatTimelineKey } from '../owners'
import { toolGroupNames, toolStatusLabel } from '@/utils/toolPresentation'
import { toolGroupStatus } from '@/features/chat/presentation/chatTurnPresentation'
import type { TimelineTurn } from '../timeline/types'
import type { TimelinePart } from '../timeline/types'
const { expandedToolGroups, onToolGroupToggle, collapseDetails } = requireChatOwner(chatTimelineKey)
defineProps<{
  turn: TimelineTurn
  part: Extract<TimelinePart, { kind: 'tool_group' }>
}>()
</script>

<style scoped>
.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tool-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}

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

.conversation-avatar {
  width: 23px;
  height: 23px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border-radius: var(--ch-radius-sm);
  border: 1px solid var(--ch-color-border-selected);
  background: var(--ch-color-surface-selected);
  color: var(--ch-color-text-selected);
  font-size: 11px;
  font-weight: 600;
}

.conversation-avatar--tool {
  border-color: var(--ch-color-border-muted);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  font-size: 10px;
}


.tool-card summary {
  cursor: pointer;
  list-style: none;
}


.tool-card summary::-webkit-details-marker {
  display: none;
}

.tool-card summary {
  position: sticky;
  top: 0;
  z-index: 1;
}

.tool-card summary {
  background: var(--ch-color-surface);
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


.tool-card summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.tool-card {
  width: min(85%, 680px);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  overflow: clip;
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 9px 12px;
  transition: background-color 120ms ease;
}

.tool-card > .tool-header:hover,

.tool-card[open] > .tool-header {
  background: var(--ch-color-surface-soft);
}

.tool-chevron {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
  font-size: 16px;
  line-height: 1;
  transition: transform 140ms ease;
}

.tool-card[open] > .tool-header .tool-chevron {
  transform: rotate(90deg);
}
.tool-card .tool-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
  text-transform: none;
  font-weight: 500;
}

.tool-card .tool-status::before {
  content: '·';
  width: 10px;
  text-align: center;
}

.tool-card .tool-status.completed::before { content: '✓'; }
.tool-card .tool-status.failed::before { content: '!'; }
.tool-card .tool-status.cancelled::before { content: '–'; }
.tool-card .tool-status.running::before { content: '◷'; }

.tool-card .tool-block pre {
  border: 1px solid var(--ch-color-border-muted);
  white-space: pre;
  word-break: normal;
  overscroll-behavior: contain;
  tab-size: 2;
}

.tool-card .tool-block pre:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.tool-name {
  flex-shrink: 0;
  min-width: 0;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}
.tool-header--single .tool-name {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
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

.tool-group-names {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-group-item {
  padding: 8px 10px;
  border-top: 1px solid var(--ch-color-border-muted);
}

.tool-group-item:first-child {
  border-top: 0;
}

.tool-group-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}

.tool-group-item-header .tool-name {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
}

.tool-group-item .tool-block {
  padding: 0;
}

.tool-status.unknown {
  color: var(--ch-color-text-subtle);
}
@media (max-width: 640px) {


  .tool-card {
    width: min(92%, 680px);
    max-width: 92%;
  }
}

@media (prefers-reduced-motion: reduce) {


  .tool-chevron,

  .tool-header {
    transition: none;
  }
}
</style>
