<template>
  <ChatThinkingCard
    v-if="part.kind === 'thinking'"
    :part="part"
  />

  <div
    v-else-if="part.kind === 'text'"
    class="conversation-row conversation-row--assistant"
  >
    <span
      class="conversation-avatar"
      aria-hidden="true"
    >✦</span>
    <div class="conversation-bubble conversation-bubble--assistant">
      <MarkdownContent
        :text="stripTrailingGoalProtocol(part.text)"
        :tab-id="markdownImageScope"
        compact
        :complete="turn.completed"
        @image-click="openImageLightbox"
      />
    </div>
  </div>

  <ChatToolGroup
    v-else-if="part.kind === 'tool_group'"
    :turn="turn"
    :part="part"
  />

  <!-- Dedicated Codex-style card for a confirmed sub-agent spawn.
                 The timeline splits these out of ordinary tool groups; the
                 whole internal prompt/result stays behind the fold so normal
                 folding/copy never ships its JSON as a tool row. -->
  <ChatSubagentCard
    v-else-if="part.kind === 'subagent'"
    :part="part"
    :render-revision="turn.renderRevision"
  />

  <!-- One native sub-agent region per parent turn. Correlated spawn
                 calls and child streams share one row keyed by the complete provider
                 thread id; expanding a row preserves the child's full process. -->
  <ChatSubagentRegion
    v-else-if="part.kind === 'subagents'"
    :turn="turn"
    :part="part"
  />

  <!-- Agent-produced local image (Codex/TraeX view_image, Claude
                 image Read). Render the picture itself; click opens the shared
                 lightbox. The scoped endpoint serves only whitelisted images
                 inside the tab cwd. A deleted/moved/non-image file degrades to
                 a path-only placeholder via the @error handler. -->
  <ChatAgentImage
    v-else-if="part.kind === 'agent_image'"
    :part="part"
    :render-revision="turn.renderRevision"
  />

  <ChatApprovalCard
    v-else-if="part.kind === 'approval'"
    :turn="turn"
    :part="part"
  />

  <div
    v-else-if="part.kind === 'error'"
    class="event-error"
    role="alert"
  >
    <span
      class="error-icon"
      aria-hidden="true"
    >⚠</span>
    <span class="error-body">
      <span>{{ part.message }}</span>
      <!-- The provider's verbatim failure payload. De-emphasized:
                     it is evidence for whoever debugs the run, not the
                     statement the reader has to parse. -->
      <code
        v-if="part.detail"
        class="error-detail"
      >{{ part.detail }}</code>
    </span>
  </div>

  <div
    v-else-if="part.kind === 'reasoning_status'"
    class="reasoning-status"
  >
    <ThinkingIndicator :state="part.state" />
  </div>

  <div
    v-else-if="part.kind === 'status'"
    class="event-status"
  >
    <span>{{ part.text }}</span>
  </div>

  <template v-else-if="part.kind === 'process'">
    <button
      type="button"
      class="process-fold"
      data-testid="history-details-toggle"
      :class="{ 'process-fold--open': part.expanded, 'process-fold--error': part.detailStatus === 'error' }"
      :aria-expanded="part.expanded"
      aria-label="Execution details"
      @click="toggleTurnProcess(turn)"
    >
      <span
        class="process-fold-chevron"
        aria-hidden="true"
      >{{ part.expanded ? '▾' : '▸' }}</span>
      <span class="process-fold-meta">{{ part.meta }}</span>
      <span
        v-if="part.detailStatus === 'loading'"
        class="process-fold-status"
      >Loading…</span>
      <span
        v-else-if="part.detailStatus === 'error'"
        class="process-fold-status process-fold-status--error"
      >Failed</span>
    </button>
    <div
      v-if="part.detailStatus === 'error'"
      class="history-details-error"
      role="alert"
    >
      <span>{{ part.detailError || 'Failed to load execution details.' }}</span>
      <button
        type="button"
        class="history-details-retry"
        data-testid="history-details-retry"
        @click="retryHistoryDetail(turn)"
      >
        Retry
      </button>
    </div>
  </template>
</template>

<script setup lang="ts">
import { requireChatOwner, chatImagesKey, chatTimelineKey, chatIdentityKey } from '../owners'
import MarkdownContent from '@/components/MarkdownContent.vue'
import ThinkingIndicator from '@/components/ThinkingIndicator.vue'
import { stripTrailingGoalProtocol } from '@/utils/chatGoalPolicy'
import ChatThinkingCard from './ChatThinkingCard.vue'
import ChatToolGroup from './ChatToolGroup.vue'
import ChatSubagentCard from './ChatSubagentCard.vue'
import ChatSubagentRegion from './ChatSubagentRegion.vue'
import ChatAgentImage from './ChatAgentImage.vue'
import ChatApprovalCard from './ChatApprovalCard.vue'
import type { TimelineTurn } from '../timeline/types'
import type { TimelinePart } from '../timeline/types'
import type { ThinkingDisplayPart } from '@/utils/thinkingPresentation'
const { openImageLightbox } = requireChatOwner(chatImagesKey)
const { toggleTurnProcess, retryHistoryDetail } = requireChatOwner(chatTimelineKey)
const { markdownImageScope } = requireChatOwner(chatIdentityKey)
defineProps<{
  turn: TimelineTurn
  part: ThinkingDisplayPart<TimelinePart>
}>()
</script>

<style scoped>
.event-error {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.1));
  border: 1px solid var(--ch-color-danger-border, rgba(248, 81, 73, 0.3));
  border-radius: var(--ch-radius-md);
  padding: 6px 10px;
  color: var(--ch-color-danger-strong, #f85149);
  font-size: 12px;
}

.error-icon {
  flex-shrink: 0;
}

.error-body {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}

.error-detail {
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  color: var(--ch-color-text-subtle);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.event-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ch-color-text-subtle);
}

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
}

.conversation-bubble {
  max-width: min(85%, 680px);
  min-width: 0;
  padding: 9px 12px;
  border-radius: var(--ch-radius-md);
  overflow-wrap: anywhere;
}

.conversation-bubble--assistant {
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border-muted);
  border-bottom-left-radius: var(--ch-radius-sm);
  box-shadow: 0 6px 18px var(--ch-shadow-color-soft);
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
.process-fold {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  max-width: 100%;
  margin: 2px 0;
  padding: 5px 10px;
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}

.process-fold:hover {
  color: var(--ch-color-text-muted);
}

.process-fold:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
}
.process-fold--open {
  position: sticky;
  top: 0;
  z-index: 1;
  border-style: solid;
}

.process-fold-chevron {
  flex-shrink: 0;
  font-size: 10px;
}

.process-fold-meta {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.process-fold-status {
  flex-shrink: 0;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
}

.process-fold-status--error,
.process-fold--error {
  color: var(--ch-color-danger-strong, #f85149);
}

.history-details-error {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  max-width: 100%;
  padding: 5px 10px;
  border: 1px solid var(--ch-color-danger-border, rgba(248, 81, 73, 0.3));
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.1));
  color: var(--ch-color-danger-strong, #f85149);
  font-size: 12px;
}

.history-details-retry {
  padding: 2px 8px;
  border: 1px solid currentColor;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: inherit;
  cursor: pointer;
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

.event-error {
  align-self: flex-start;
  width: min(85%, 680px);
  margin: 0;
}

.event-status {
  justify-content: center;
  font-style: italic;
  text-align: center;
}
@media (max-width: 640px) {

  .conversation-bubble,

  .event-error {
    width: min(92%, 680px);
    max-width: 92%;
  }
}

/* Compact Chat prose stays larger than dense Workspace Markdown. */
.conversation-bubble :deep(.markdown-content.compact) {
  font-size: 14px;
  line-height: 1.65;
}
</style>
