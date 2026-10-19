<template>
  <div
    class="conversation-row conversation-row--assistant"
  >
    <div
      class="approval-card"
      :class="{ 'approval-card--resolved': isApprovalResolved(part.approval) }"
    >
      <div class="approval-card-header">
        <span class="approval-card-title">
          {{ part.approval.title || '需要你的选择' }}
        </span>
        <span
          v-if="isApprovalResolved(part.approval)"
          class="approval-card-badge"
        >已提交</span>
      </div>
      <div
        v-for="question in part.approval.questions"
        :key="question.id"
        class="approval-question"
      >
        <div class="approval-question-prompt">
          {{ question.prompt }}
        </div>
        <div
          class="approval-options"
          role="group"
          :aria-label="question.prompt"
        >
          <button
            v-for="option in question.options"
            :key="option.id"
            type="button"
            class="approval-option"
            :class="{
              'approval-option--selected': isQuestionOptionSelected(
                part.approval.key,
                question.id,
                option.id,
              ),
            }"
            :disabled="isApprovalResolved(part.approval) || isSending"
            @click="toggleQuestionOption(
              part.approval.key,
              question.id,
              option.id,
              question.allowMultiple,
            )"
          >
            <span>{{ option.label }}</span>
            <small v-if="option.description">{{ option.description }}</small>
          </button>
        </div>
        <!-- The listed options are the agent's guess at the answer,
           not the whole space of them. What the user types here is
           merged with the ticked options on submit, so it satisfies
           the completion check, travels in the same payload, and is
           what the agent reads. -->
        <input
          :type="question.isSecret ? 'password' : 'text'"
          class="approval-custom-answer"
          :value="customAnswer(part.approval.key, question)"
          :disabled="isApprovalResolved(part.approval) || isSending"
          :placeholder="question.options.length === 0 ? '输入你的回答' : question.allowMultiple ? '其他（可多选，自己输入）' : '其他（自己输入）'"
          :aria-label="`${question.prompt} — 其他答案`"
          @input="setCustomAnswer(
            part.approval.key,
            question,
            ($event.target as HTMLInputElement).value,
          )"
        >
      </div>
      <button
        v-if="!isApprovalResolved(part.approval)"
        type="button"
        class="approval-submit-btn"
        :disabled="!canSubmitQuestion(part.approval) || isSending"
        @click="submitQuestionResponse(turn, part.approval)"
      >
        提交选择
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatComposerKey, chatDeliveryKey } from '../owners'
import type { TimelineTurn } from '../timeline/types'
import type { TimelinePart } from '../timeline/types'
const { isApprovalResolved, isQuestionOptionSelected, toggleQuestionOption, customAnswer, setCustomAnswer, canSubmitQuestion, submitQuestionResponse } = requireChatOwner(chatComposerKey)
const { isSending } = requireChatOwner(chatDeliveryKey)
defineProps<{
  turn: TimelineTurn
  part: Extract<TimelinePart, { kind: 'approval' }>
}>()
</script>

<style scoped>
.approval-card {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
}

.approval-card--resolved {
  opacity: 0.75;
}

.approval-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.approval-card-title {
  font-weight: 600;
}

.approval-card-badge {
  font-size: 11px;
  color: var(--ch-color-text-muted);
}

.approval-question + .approval-question {
  margin-top: 10px;
}

.approval-question-prompt {
  margin-bottom: 6px;
  font-weight: 500;
}

.approval-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.approval-custom-answer {
  width: 100%;
  margin-top: 6px;
  padding: 6px 10px;
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 13px;
}

.approval-custom-answer:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-style: solid;
}

.approval-custom-answer:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.approval-option {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--ch-color-text);
  background: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  cursor: pointer;
}

.approval-option small {
  color: var(--ch-color-text-muted);
  text-align: left;
}

.approval-option--selected {
  border-color: var(--ch-color-accent);
  background: color-mix(in srgb, var(--ch-color-accent) 12%, transparent);
}

.approval-option:disabled {
  cursor: default;
}

.approval-submit-btn {
  margin-top: 10px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 500;
  color: #fff;
  background: var(--ch-color-accent-strong);
  border: none;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
}

.approval-submit-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
}


</style>
