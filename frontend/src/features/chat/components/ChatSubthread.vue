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
    <div class="subthread-body">
      <div
        v-if="thread.launchTool"
        class="subthread-instruction subthread-launch"
      >
        <div class="subthread-instruction-head">
          <span class="subthread-instruction-arrow">→ Original task</span>
          <span
            class="tool-status"
            :class="thread.launchTool.status"
          >{{ subagentDispatchLabel(thread.launchTool.status) }}</span>
        </div>
        <pre class="subthread-instruction-prompt">{{ subthreadInstructionText(thread.launchTool) }}</pre>
        <details
          v-if="thread.launchTool.resultText"
          class="subthread-launch-result"
        >
          <summary>Dispatch result</summary>
          <pre>{{ thread.launchTool.resultText }}</pre>
        </details>
      </div>

      <template
        v-for="sub in subthreadPartsFor(thread)"
        :key="sub.key"
      >
        <div
          v-if="sub.kind === 'instruction'"
          class="subthread-instruction"
        >
          <div class="subthread-instruction-head">
            <span class="subthread-instruction-arrow">→ Instruction</span>
            <span
              class="tool-status"
              :class="sub.tool.status"
            >{{ subagentDispatchLabel(sub.tool.status) }}</span>
          </div>
          <pre class="subthread-instruction-prompt">{{ subthreadInstructionText(sub.tool) }}</pre>
        </div>

        <details
          v-else-if="sub.kind === 'thinking'"
          class="thinking-card subthread-thinking"
          :class="{ 'thinking-card--active': sub.state === 'active' }"
        >
          <summary>
            <ThinkingIndicator :state="sub.state" />
            <span
              class="thinking-chevron"
              aria-hidden="true"
            >›</span>
          </summary>
          <pre class="thinking-body">{{ sub.text }}</pre>
          <button
            type="button"
            class="details-collapse"
            @click="collapseDetails"
          >
            Collapse
          </button>
        </details>

        <div
          v-else-if="sub.kind === 'text'"
          class="subthread-message"
        >
          <MarkdownContent
            :text="sub.text"
            :tab-id="markdownImageScope"
            compact
            :complete="subthreadTerminal(thread)"
            @image-click="openImageLightbox"
          />
        </div>

        <div
          v-else-if="sub.kind === 'reasoning_status'"
          class="reasoning-status"
        >
          <ThinkingIndicator :state="sub.state" />
        </div>

        <div
          v-else-if="sub.kind === 'status'"
          class="event-status subthread-status"
        >
          <span>{{ sub.text }}</span>
        </div>

        <details
          v-else-if="sub.kind === 'tool_group'"
          class="tool-card tool-card--group"
          :open="expandedToolGroups.get(turn.key)?.has(sub.key)"
          @toggle="onToolGroupToggle(turn, sub.key, $event)"
        >
          <summary
            class="tool-header"
            :class="{ 'tool-header--single': sub.tools.length === 1 }"
          >
            <span class="tool-name">
              {{ sub.tools.length === 1 ? sub.tools[0].name : `${sub.tools.length} tools` }}
            </span>
            <template v-if="sub.tools.length > 1">
              <span class="tool-group-names">
                {{ toolGroupNames(sub.tools) }}
              </span>
            </template>
            <span
              class="tool-status"
              :class="toolGroupStatus(sub.tools)"
            >{{ toolStatusLabel(toolGroupStatus(sub.tools)) }}</span>
            <span
              class="tool-chevron"
              aria-hidden="true"
            >›</span>
          </summary>
          <template v-if="expandedToolGroups.get(turn.key)?.has(sub.key)">
            <div
              v-for="tool in sub.tools"
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

        <div
          v-else-if="sub.kind === 'agent_image'"
          class="agent-image-card"
        >
          <button
            v-if="!erroredAgentImages.has(sub.key)"
            type="button"
            class="turn-attachment-button agent-image-button"
            :aria-label="`Open image ${agentImageBasename(sub.path)}`"
            @click="openImageLightbox(agentImageUrl(sub.path), agentImageBasename(sub.path), $event)"
          >
            <img
              :src="agentImageUrl(sub.path)"
              class="turn-attachment-img"
              :alt="agentImageBasename(sub.path)"
              loading="lazy"
              decoding="async"
              @error="onAgentImageError($event, sub.key)"
            >
          </button>
          <div
            v-else
            class="turn-attachment-placeholder agent-image-missing"
          >
            Image unavailable
          </div>
          <div
            class="agent-image-path"
            :title="sub.path"
          >
            {{ agentImageBasename(sub.path) }}
          </div>
        </div>
      </template>
    </div>
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

import { requireChatOwner, chatTimelineKey, chatImagesKey, chatIdentityKey } from '../owners'
import ThinkingIndicator from '@/components/ThinkingIndicator.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import { subthreadHeadline, subthreadStatusClass, subthreadStatusLabel, subagentDispatchLabel, subthreadInstructionText, subthreadTerminal } from '@/features/chat/presentation/chatSubagentPresentation'
import { toolGroupNames, toolStatusLabel } from '@/utils/toolPresentation'
import { toolGroupStatus } from '@/features/chat/presentation/chatTurnPresentation'
import type { TimelineTurn } from '../timeline/types'
import type { TimelineSubthread } from '../timeline/types'
const { subthreadPartsFor, collapseDetails, expandedToolGroups, onToolGroupToggle } = requireChatOwner(chatTimelineKey)
const { openImageLightbox, erroredAgentImages, agentImageBasename, agentImageUrl, onAgentImageError } = requireChatOwner(chatImagesKey)
const { markdownImageScope } = requireChatOwner(chatIdentityKey)
defineProps<{
  turn: TimelineTurn
  thread: TimelineSubthread
}>()

</script>
<style scoped>
.thinking-body {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
}

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

.event-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ch-color-text-subtle);
}

.turn-attachment-button {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: block;
  padding: 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, #fff 34%, transparent);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  box-shadow: 0 2px 8px rgb(0 0 0 / 18%);
  cursor: zoom-in;
}

.turn-attachment-button:hover {
  border-color: color-mix(in srgb, #fff 70%, transparent);
}

.turn-attachment-button:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 2px;
}

.turn-attachment-img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  transition: transform 140ms ease;
}

.turn-attachment-button:hover .turn-attachment-img {
  transform: scale(1.025);
}
.agent-image-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.agent-image-path {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-xs);
  color: var(--ch-color-text-muted);
}

.agent-image-missing {
  width: clamp(72px, 8vw, 88px);
  cursor: default;
}

.turn-attachment-placeholder {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: grid;
  place-items: center;
  padding: 10px;
  border: 1px dashed var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  text-align: center;
}

.thinking-card {
  align-self: flex-start;
  width: 100%;
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  padding: 7px 10px;
}

.thinking-card summary,

.tool-card summary {
  cursor: pointer;
  list-style: none;
}

.thinking-card summary::-webkit-details-marker,

.tool-card summary::-webkit-details-marker {
  display: none;
}
.thinking-card summary,

.tool-card summary {
  position: sticky;
  top: 0;
  z-index: 1;
}

.thinking-card summary {
  margin: -7px -10px 0;
  padding: 7px 10px;
  background: var(--ch-color-surface-soft);
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

.thinking-card summary {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  font-weight: 600;
}

.thinking-card summary:focus-visible,

.tool-card summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.reasoning-status {
  display: inline-flex;
  align-self: flex-start;
  max-width: 100%;
  padding: 5px 10px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
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

.tool-card {
  width: 100%;
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

.subthread-launch {
  background: var(--ch-color-surface-muted, rgba(139, 148, 158, 0.08));
}

.tool-status.unknown {
  color: var(--ch-color-text-subtle);
}

.subthread-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.subthread-instruction {
  border-left: 2px solid var(--ch-color-accent);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-accent-soft);
  padding: 6px 8px;
}

.subthread-instruction-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}

.subthread-instruction-arrow {
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 600;
}

.subthread-instruction-prompt {
  margin: 0;
  color: var(--ch-color-text-code);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.subthread-message {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.subthread-body > .tool-card {
  width: 100%;
  min-width: 0;
}

.subthread-thinking {
  margin: 0;
}

.event-status {
  justify-content: center;
  font-style: italic;
  text-align: center;
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


  .thinking-card,

  .tool-card {
    width: 100%;
    max-width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {

  .turn-attachment-img,

  .thinking-chevron,

  .tool-chevron,

  .tool-header {
    transition: none;
  }
}

.subagent-thread-row:not([open]) :deep(.thinking-indicator--active) {
  animation: none;
}
</style>
