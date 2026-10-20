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

    <ChatTurnWorking
      v-if="isLive(turn) && !turn.awaitingAgentActivity"
      :started-at="turn.startedAt"
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
    <div
      class="turn-actions turn-actions--turn"
      :class="{ 'turn-actions--live': isLive(turn) }"
    >
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
import ChatTurnWorking from './ChatTurnWorking.vue'
import type { TimelineTurn } from '../timeline/types'
const { isEditingTurn, startEdit, turnCopyText, copyFeedbackLabel, copyTurnButtonLabel, copyTurn, copyFeedbackState, completedPlanText, implementingPlanKey, implementPlan, forkingOrdinal, forkFromTurn } = requireChatOwner(chatActionsKey)
const { turnInFlight } = requireChatOwner(chatDeliveryKey)
const { goalEditReason, goalComposerLocked, goalComposerReason } = requireChatOwner(chatGoalKey)
const { turnPartsFor, latestTurnKey } = requireChatOwner(chatTimelineKey)
const { supportsEditResend, isManagedChat } = requireChatOwner(chatIdentityKey)
/** Live means still streaming now: the latest turn while one is in flight.
 *  An incomplete turn whose completion never arrived is history, not live. */
function isLive(turn: TimelineTurn): boolean {
  return !turn.completed && turn.key === latestTurnKey.value && turnInFlight.value
}

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
/* Fork and plan actions describe a finished answer; while it streams they
   are noise (touch shows action rows permanently). Copy stays, so a partial
   answer can still be taken. */
.turn-actions--live > :not(.turn-copy-button) {
  display: none;
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
  gap: 10px;
  margin-bottom: 26px;
}

/* Hover actions hug the message they belong to instead of reserving a full
   row of empty space between the question and the answer. */
.turn-actions--message {
  margin-top: -6px;
  margin-bottom: -4px;
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

.agent-waiting-card {
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 2px 2px;
  color: var(--ch-color-text-subtle);
  font-size: 13px;
}

.agent-waiting-card > span:last-child {
  background: linear-gradient(
    90deg,
    var(--ch-color-text-subtle) 0%,
    var(--ch-color-text-strong, var(--ch-color-text)) 50%,
    var(--ch-color-text-subtle) 100%
  );
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: agent-waiting-shimmer 1.8s linear infinite;
}

@keyframes agent-waiting-shimmer {
  from { background-position: 100% 0; }
  to { background-position: -100% 0; }
}

.agent-waiting-pulse {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.agent-waiting-pulse i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--ch-color-accent);
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
  .agent-waiting-pulse i,
  .agent-waiting-card > span:last-child {
    animation: none;
  }
}
.turn-copy-result--copied { color: var(--ch-color-accent); }
.turn-copy-result--error { color: var(--ch-color-danger, var(--ch-color-accent)); }
</style>
