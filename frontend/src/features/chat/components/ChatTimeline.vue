<template>
  <div
    ref="timelineEl"
    class="structured-timeline"
    :class="{ 'is-timeline-hidden': timelinePhase !== 'revealed' }"
    role="log"
    aria-live="polite"
    aria-label="Chat conversation"
    :aria-busy="Boolean(timelineLoadingMessage)"
    @scroll.passive="handleTimelineScroll"
  >
    <div
      ref="timelineContentEl"
      class="structured-timeline-content"
    >
      <button
        v-if="historyWindowStart > 0"
        type="button"
        class="structured-load-earlier"
        :disabled="isLoadingEarlier"
        @click="loadEarlierTurns"
      >
        {{ isLoadingEarlier ? 'Loading earlier messages…' : 'Load earlier messages' }}
      </button>
      <div
        v-if="turns.length === 0 && pendingDirectTurns.length === 0 && isHistoryVisible"
        class="structured-empty"
      >
        <span
          class="empty-orbit"
          aria-hidden="true"
        >✦</span>
        <strong>Ready when you are</strong>
        <p>Send a message below to start this chat.</p>
      </div>

      <ChatTurn
        v-for="{ turn, ordinal } in visibleTurns"
        :key="turn.key"
        v-memo="[turn.renderRevision, ordinal, feishuBotPoolStore.pool?.pool_revision, erroredAttachments.size, erroredAgentImages.size, turnApprovalSignature(turn), turnFoldSignature(turn), expandedToolGroups.get(turn.key), forkingOrdinal === ordinal, implementingPlanKey === turn.key, isEditingTurn(turn), isEditingTurn(turn) ? editError : null, isEditingTurn(turn) ? isEditSending : false, copyFeedback?.turnKey === turn.key ? copyFeedback.state : null]"
        :turn="turn"
        :ordinal="ordinal"
      />

      <!-- Optimistic turns are reconciled by client_turn_id, never by text. -->
      <ChatPendingTurn
        v-for="turn in pendingTurns"
        :key="turn.key"
        :turn="turn"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatTimelineKey, chatDeliveryKey, chatImagesKey, chatComposerKey, chatActionsKey, chatIdentityKey } from '../owners'
import ChatTurn from './ChatTurn.vue'
import ChatPendingTurn from './ChatPendingTurn.vue'
const { timelineEl, timelinePhase, timelineLoadingMessage, handleTimelineScroll, timelineContentEl, historyWindowStart, isLoadingEarlier, loadEarlierTurns, turns, visibleTurns, turnFoldSignature, expandedToolGroups } = requireChatOwner(chatTimelineKey)
const { pendingDirectTurns, pendingTurns } = requireChatOwner(chatDeliveryKey)
const { erroredAttachments, erroredAgentImages } = requireChatOwner(chatImagesKey)
const { turnApprovalSignature } = requireChatOwner(chatComposerKey)
const { forkingOrdinal, implementingPlanKey, isEditingTurn, editError, isEditSending, copyFeedback } = requireChatOwner(chatActionsKey)
const { isHistoryVisible, feishuBotPoolStore } = requireChatOwner(chatIdentityKey)
</script>

<style scoped>
.structured-timeline {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  min-height: 0;
}
.structured-timeline.is-timeline-hidden {
  visibility: hidden;
  pointer-events: none;
}

.structured-load-earlier {
  align-self: center;
  padding: 7px 14px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-muted);
  background: var(--ch-color-surface);
  cursor: pointer;
}

.structured-load-earlier:disabled {
  cursor: wait;
  opacity: 0.7;
}

.structured-empty {
  text-align: center;
  color: var(--ch-color-text-subtle);
  padding: 24px 12px;
}

.structured-timeline {
  padding: 34px 28px 26px;
}

.structured-timeline-content {
  width: 100%;
  max-width: 860px;
  min-height: 100%;
  margin: 0 auto;
}

.structured-empty {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 24px;
}

.structured-empty strong {
  color: var(--ch-color-text-muted);
  font-weight: 500;
}

.structured-empty p {
  max-width: 340px;
  margin: 0;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  line-height: 1.5;
}

.empty-orbit {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 4px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  color: var(--ch-color-accent);
  box-shadow: 0 8px 20px var(--ch-shadow-color-soft);
}
@media (max-width: 640px) {
  .structured-timeline {
    padding: 40px 12px 18px;
  }
}
</style>
