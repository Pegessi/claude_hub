<template>
  <div
    class="structured-turn"
    :data-turn-key="turn.key"
  >
    <!-- A right-aligned user bubble and a left-aligned assistant bubble make
               this the same conversation as the terminal, not terminal text
               pasted into a second surface. -->
    <ChatUserMessage
      v-if="turn.userText || turn.attachments?.length"
      :turn="turn"
    />

    <!-- The message's own actions: a row under the bubble that the turn's
               hover reveals, with room for both the buttons and the time.
               Normal flow, not pinned over the turn — absolutely positioned it
               sat behind the bubble (which is itself positioned) and swallowed
               clicks. -->
    <div
      v-if="turn.userText && !isEditingTurn(turn)"
      class="turn-actions turn-actions--message"
    >
      <button
        v-if="turn.turnId && supportsEditResend"
        type="button"
        class="edit-resend-hover-btn"
        :disabled="turnInFlight || Boolean(goalEditReason)"
        :title="turnInFlight ? 'A turn is currently running' : (goalEditReason || 'Edit message')"
        :aria-label="turnInFlight ? 'Edit message (unavailable while a turn is running)' : (goalEditReason ? `Edit message (${goalEditReason})` : 'Edit message')"
        @click="startEdit(turn)"
      >
        ✎ 编辑
      </button>
      <time
        v-if="messageClockLabel(turn)"
        class="turn-time"
      >{{ messageClockLabel(turn) }}</time>
    </div>

    <div
      v-if="turn.awaitingAgentActivity"
      class="conversation-row conversation-row--assistant"
      role="status"
      aria-live="polite"
    >
      <span
        class="conversation-avatar conversation-avatar--waiting"
        aria-hidden="true"
      >✦</span>
      <div class="agent-waiting-card">
        <span
          class="agent-waiting-pulse"
          aria-hidden="true"
        >
          <i />
          <i />
          <i />
        </span>
        <span>Waiting for response…</span>
      </div>
    </div>

    <!-- Ordered parts: thinking, text, tool, error, and status render in
               the exact order the provider emitted them. Paseo does not defer
               protocol errors to the turn end, so neither do we. -->
    <ChatTurnParts
      v-for="part in turnPartsFor(turn)"
      :key="part.key"
      :turn="turn"
      :part="part"
    />

    <!-- A user Stop (or a watchdog reap) terminalized the turn: mark the
               cancelled round instead of leaving its thinking/working
               placeholder spinning. -->
    <div
      v-if="turn.completed && turn.completionStatus === 'cancelled'"
      class="turn-cancelled-marker"
      role="status"
    >
      <span
        class="turn-cancelled-dot"
        aria-hidden="true"
      >■</span>
      Stopped
    </div>

    <!-- Turn-level actions close the turn the way the message actions
               open it, so a long turn's controls sit with its result rather
               than floating back at the top. -->
    <div class="turn-actions turn-actions--turn">
      <button
        v-if="turnCopyText(turn)"
        type="button"
        class="turn-fork-button turn-copy-button"
        :class="{ 'turn-copy-button--feedback': copyFeedbackLabel(turn.key) }"
        :aria-label="copyTurnButtonLabel(turn.key)"
        :title="copyTurnButtonLabel(turn.key)"
        @click="copyTurn(turn)"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
        >
          <path d="M5 1.75h7.25a2 2 0 0 1 2 2V11h-1.5V3.75a.5.5 0 0 0-.5-.5H5v-1.5Z" />
          <path d="M3.75 4h6.5a2 2 0 0 1 2 2v6.25a2 2 0 0 1-2 2h-6.5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm0 1.5a.5.5 0 0 0-.5.5v6.25c0 .276.224.5.5.5h6.5a.5.5 0 0 0 .5-.5V6a.5.5 0 0 0-.5-.5h-6.5Z" />
        </svg>
        <span
          v-if="copyFeedbackLabel(turn.key)"
          class="turn-copy-result"
          :class="`turn-copy-result--${copyFeedbackState(turn.key)}`"
          aria-live="polite"
        >{{ copyFeedbackLabel(turn.key) }}</span>
      </button>
      <button
        v-if="!isManagedChat && completedPlanText(turn)"
        type="button"
        class="turn-fork-button turn-implement-button"
        :disabled="implementingPlanKey !== null || turnInFlight || goalComposerLocked"
        :title="goalComposerReason || 'Switch to Agent mode and ask the CLI to implement this plan'"
        @click="implementPlan(turn)"
      >
        {{ implementingPlanKey === turn.key ? 'Starting…' : 'Implement plan' }}
      </button>
      <button
        v-if="!isManagedChat"
        type="button"
        class="turn-fork-button"
        :disabled="forkingOrdinal !== null"
        title="Fork a new chat from this turn"
        @click="forkFromTurn(ordinal)"
      >
        {{ forkingOrdinal === ordinal ? 'Forking…' : 'Fork from here' }}
      </button>
      <time
        v-if="turnClockLabel(turn)"
        class="turn-time"
      >{{ turnClockLabel(turn) }}</time>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatActionsKey, chatDeliveryKey, chatGoalKey, chatTimelineKey, chatIdentityKey } from '../owners'
import { messageClockLabel, turnClockLabel } from '../timeline/turnProcess'
import ChatUserMessage from './ChatUserMessage.vue'
import ChatTurnParts from './ChatTurnParts.vue'
import type { TimelineTurn } from '../timeline/types'
const { isEditingTurn, startEdit, turnCopyText, copyFeedbackLabel, copyTurnButtonLabel, copyTurn, copyFeedbackState, completedPlanText, implementingPlanKey, implementPlan, forkingOrdinal, forkFromTurn } = requireChatOwner(chatActionsKey)
const { turnInFlight } = requireChatOwner(chatDeliveryKey)
const { goalEditReason, goalComposerLocked, goalComposerReason } = requireChatOwner(chatGoalKey)
const { turnPartsFor } = requireChatOwner(chatTimelineKey)
const { supportsEditResend, isManagedChat } = requireChatOwner(chatIdentityKey)
defineProps<{
  turn: TimelineTurn & { awaitingAgentActivity?: boolean }
  ordinal: number
}>()
</script>

<style scoped>
.turn-cancelled-marker {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 6px 0 2px;
  font-size: var(--ch-font-xs, 11px);
  color: var(--ch-color-text-muted);
}

.turn-cancelled-dot {
  font-size: 9px;
  color: var(--ch-color-text-subtle);
}

.structured-turn {
  margin-bottom: 16px;
  position: relative;
}
.turn-actions {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 20px;
  margin: 0 4px 6px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease;
}

.turn-actions--message,

.turn-actions--turn {
  justify-content: flex-end;
}

.structured-turn:hover .turn-actions,

.turn-actions:focus-within {
  opacity: 1;
  pointer-events: auto;
}
.turn-time {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--ch-color-text-muted, var(--ch-color-text-subtle, currentColor));
  white-space: nowrap;
}

.turn-fork-button {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
  background: var(--ch-color-bg-elevated, canvas);
  color: var(--ch-color-text-subtle, currentColor);
  cursor: pointer;
}

.turn-fork-button:hover:not(:disabled) {
  border-color: var(--ch-color-accent);
  color: var(--ch-color-accent);
}

.turn-fork-button:disabled {
  opacity: 0.6;
  cursor: default;
}

.turn-copy-button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 7px;
}

.turn-copy-button svg {
  width: 12px;
  height: 12px;
  fill: currentColor;
  flex: 0 0 auto;
}

.turn-copy-result {
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}

.turn-copy-button--feedback {
  border-color: currentColor;
}
@media (hover: none), (pointer: coarse) {
  .structured-turn .turn-actions {
    opacity: 1;
    pointer-events: auto;
  }
}

.structured-turn {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 22px;
}

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
}
.edit-resend-hover-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
  border-radius: 999px;
  background: var(--ch-color-bg-elevated, canvas);
  color: var(--ch-color-text-subtle, currentColor);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.6;
  cursor: pointer;
}

.edit-resend-hover-btn:hover:not(:disabled) {
  border-color: var(--ch-color-accent);
  color: var(--ch-color-accent);
}
.edit-resend-hover-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
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

.conversation-avatar--waiting {
  border-color: var(--ch-color-border-muted);
  color: var(--ch-color-text-subtle);
  background: var(--ch-color-surface-control);
}

.agent-waiting-card {
  min-height: 34px;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  padding: 7px 11px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  border-bottom-left-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.agent-waiting-pulse {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.agent-waiting-pulse i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentcolor;
  animation: agent-waiting-dot 1.2s ease-in-out infinite;
}

.agent-waiting-pulse i:nth-child(2) {
  animation-delay: 0.15s;
}

.agent-waiting-pulse i:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes agent-waiting-dot {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }

  30% {
    opacity: 1;
    transform: translateY(-2px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .agent-waiting-pulse i {
    animation: none;
  }
}
.turn-copy-result--copied { color: var(--ch-color-accent); }
.turn-copy-result--error { color: var(--ch-color-danger, var(--ch-color-accent)); }
</style>
