<template>
  <div
    class="conversation-row conversation-row--assistant"
  >
    <!-- Answered: one quiet line. The decision is settled, so the form
         (options, tradeoffs, submit) would only ask the reader to re-read a
         question they can no longer act on. -->
    <div
      v-if="answered"
      class="approval-card approval-card--resolved"
    >
      <span
        class="approval-resolved-mark"
        aria-hidden="true"
      >✓</span>
      <span class="approval-resolved-text">{{ resolvedText }}</span>
      <span
        v-if="resolvedNote"
        class="approval-resolved-note"
      >· {{ resolvedNote }}</span>
    </div>

    <div
      v-else
      ref="cardEl"
      class="approval-card"
      :class="{ 'approval-card--active': isLatestPending }"
    >
      <div class="approval-card-header">
        <div class="approval-card-heading">
          <span
            v-if="eyebrow"
            class="approval-card-eyebrow"
          >{{ eyebrow }}</span>
          <span class="approval-card-title">{{ heading }}</span>
        </div>
        <span
          v-if="progressLabel"
          class="approval-card-badge"
        >{{ progressLabel }}</span>
        <span
          v-else-if="receipt.label.value"
          class="approval-card-badge"
        >{{ receipt.label.value }}</span>
      </div>

      <!-- Multi-question cards walk one question at a time; the bars say how
           far along the card is without repeating the counter in words. -->
      <div
        v-if="isMultiQuestion"
        class="approval-card-steps"
        aria-hidden="true"
      >
        <span
          v-for="(question, index) in questions"
          :key="`step-${question.id}`"
          class="approval-step"
          :class="{
            'approval-step--done': stepAnswered(index),
            'approval-step--current': index === stepIndex,
          }"
        />
      </div>

      <div
        v-for="question in visibleQuestions"
        :key="question.id"
        class="approval-question"
      >
        <div
          v-if="isMultiQuestion"
          class="approval-question-prompt"
        >
          {{ question.prompt }}
        </div>
        <p
          v-if="isMultiQuestion && question.allowMultiple"
          class="approval-question-note"
        >
          Select all that apply.
        </p>
        <div
          class="approval-options"
          role="group"
          :aria-label="question.prompt"
        >
          <button
            v-for="(option, index) in question.options"
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
            :disabled="controlDisabled"
            :aria-pressed="isQuestionOptionSelected(
              part.approval.key,
              question.id,
              option.id,
            )"
            :aria-label="optionAccessibleName(option)"
            :aria-describedby="optionNoteId(option, index)"
            @click="chooseOption(question, option)"
          >
            <span
              v-if="showNumberKeys"
              class="approval-option-index"
              aria-hidden="true"
            >{{ index + 1 }}</span>
            <span class="approval-option-body">
              <span class="approval-option-label">
                <span class="approval-option-text">{{ option.label }}</span>
                <span
                  v-if="option.recommended"
                  class="approval-option-recommended"
                >Recommended</span>
              </span>
              <span
                v-if="option.description"
                :id="optionNoteId(option, index)"
                class="approval-option-note"
              >{{ option.description }}</span>
            </span>
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
          :disabled="controlDisabled"
          :placeholder="customAnswerPlaceholder(question)"
          :aria-label="`${question.prompt} — other answer`"
          @input="setCustomAnswer(
            part.approval.key,
            question,
            ($event.target as HTMLInputElement).value,
          )"
        >
      </div>

      <div class="approval-actions">
        <button
          v-if="isMultiQuestion && stepIndex > 0"
          type="button"
          class="approval-secondary"
          @click="goToStep(stepIndex - 1)"
        >
          Back
        </button>
        <button
          v-if="isMultiQuestion && !isLastStep"
          type="button"
          class="approval-secondary"
          :disabled="!currentAnswered"
          @click="goToStep(stepIndex + 1)"
        >
          Next
        </button>
        <button
          v-if="isLastStep && !receipt.blocked.value"
          type="button"
          class="approval-submit-btn"
          :disabled="!canSubmitQuestion(part.approval) || isSending"
          @click="submitQuestionResponse(turn, part.approval)"
        >
          Submit answer
        </button>
        <span
          v-if="showNumberKeys"
          class="approval-key-hint"
        >Press 1–9 to choose</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatComposerKey, chatDeliveryKey, chatIdentityKey, chatTimelineKey } from '../owners'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useQuestionReceipt } from './useQuestionReceipt'
import { describeQuestionAnswers, isQuestionAnswered, latestPendingApprovalKey, questionOptionNoteId } from '@/utils/chatQuestionResponse'
import type { TimelinePart, TimelineQuestion, TimelineQuestionOption, TimelineTurn } from '../timeline/types'
const { isApprovalResolved, isQuestionOptionSelected, toggleQuestionOption, customAnswer, setCustomAnswer, canSubmitQuestion, submitQuestionResponse, answersFor } = requireChatOwner(chatComposerKey)
const { isSending } = requireChatOwner(chatDeliveryKey)
const timeline = requireChatOwner(chatTimelineKey)
const props = defineProps<{
  turn: TimelineTurn
  part: Extract<TimelinePart, { kind: 'approval' }>
}>()
const identity = requireChatOwner(chatIdentityKey)
const receipt = useQuestionReceipt(
  computed(() => identity.isManagedChat.value
    ? `/api/workspaces/sessions/${encodeURIComponent(identity.props.managedSessionId ?? '')}`
    : `/api/workspaces/tabs/${encodeURIComponent(identity.props.tabId)}`),
  computed(() => props.turn.turnId ?? null),
  computed(() => props.part.approval.callId ?? null),
)

/** Position in a multi-question card. Component-local: the timeline turn is
 *  rebuilt on every stream batch, and which question is on screen is the
 *  reader's place in the card, not part of the record. */
const cardEl = ref<HTMLElement | null>(null)
const stepIndex = ref(0)
const questions = computed(() => props.part.approval.questions)
const isMultiQuestion = computed(() => questions.value.length > 1)
const currentQuestion = computed<TimelineQuestion | undefined>(() => (
  questions.value[Math.min(stepIndex.value, questions.value.length - 1)]
))
const visibleQuestions = computed(() => (currentQuestion.value ? [currentQuestion.value] : []))
const isLastStep = computed(() => stepIndex.value >= questions.value.length - 1)

/** Settled either by the provider (``approval_resolved``) or by the answer
 *  inbox (the card was answered through Feishu, or here and then reloaded). */
const answered = computed(() => (
  isApprovalResolved(props.part.approval) || receipt.state.value === 'resolved'
))
const chosenText = computed(() => describeQuestionAnswers(
  questions.value,
  answersFor(props.part.approval.key),
))
const resolvedText = computed(() => (
  chosenText.value
    ? `You chose: ${chosenText.value}`
    : receipt.state.value === 'resolved' ? 'Answered' : 'Question answered'
))
const resolvedNote = computed(() => {
  const note = receipt.label.value
  // "You chose: Fast · Answered" says the same thing twice.
  if (!note || receipt.state.value === 'resolved') return ''
  return note
})

const heading = computed(() => {
  const title = (props.part.approval.title ?? '').trim()
  const firstPrompt = questions.value[0]?.prompt ?? ''
  if (!isMultiQuestion.value) return firstPrompt || title || 'Decision needed'
  return title || 'A few decisions'
})
/** The provider's short header, kept as context only when it is not already
 *  the question on screen. */
const eyebrow = computed(() => {
  if (isMultiQuestion.value) return ''
  const title = (props.part.approval.title ?? '').trim()
  return title && title !== heading.value ? title : ''
})
const progressLabel = computed(() => (
  isMultiQuestion.value
    ? `Question ${Math.min(stepIndex.value, questions.value.length - 1) + 1} of ${questions.value.length}`
    : ''
))

function stepAnswered(index: number): boolean {
  const question = questions.value[index]
  return !!question && isQuestionAnswered(question, answersFor(props.part.approval.key))
}
const currentAnswered = computed(() => {
  const question = currentQuestion.value
  return !!question && isQuestionAnswered(question, answersFor(props.part.approval.key))
})

/** An answer already claimed elsewhere (another channel, another browser)
 *  makes every control inert; the card keeps its state visible instead. */
const controlDisabled = computed(() => (
  answered.value || receipt.blocked.value || isSending.value
))

/** The decision the conversation is waiting on. Only this card advertises
 *  the number keys and listens for them. */
const isLatestPending = computed(() => {
  if (isApprovalResolved(props.part.approval)) return false
  const approvals: { key: string; resolved: boolean }[] = []
  for (const item of timeline.turns.value) {
    for (const approval of item.approvals) {
      approvals.push({ key: approval.key, resolved: isApprovalResolved(approval) })
    }
  }
  return latestPendingApprovalKey(approvals) === props.part.approval.key
})
const showNumberKeys = computed(() => (
  isLatestPending.value
  && !receipt.blocked.value
  && (currentQuestion.value?.options.length ?? 0) > 1
))

function customAnswerPlaceholder(question: TimelineQuestion): string {
  if (question.options.length === 0) return 'Type your answer'
  return question.allowMultiple ? 'Other (type your own, adds to the selection)' : 'Other (type your own)'
}

/** The button's own text contains the shortcut number and the tradeoff line,
 *  so the accessible name is set explicitly: the choice, plus the
 *  recommendation when the provider marked one. The tradeoff travels as
 *  ``aria-describedby`` so screen readers announce it as detail rather than
 *  part of the name. */
function optionAccessibleName(option: TimelineQuestionOption): string {
  return option.recommended ? `${option.label} (recommended)` : option.label
}

/** The tradeoff line's DOM id, or none when the option has no tradeoff.
 *
 *  A provider option id is often the label itself ("Close and archive the
 *  report"), which cannot be an ``id``/``aria-describedby`` token, so the id
 *  is built from the positions plus a sanitized card key. */
function optionNoteId(option: TimelineQuestionOption, optionIndex: number): string | undefined {
  if (!option.description) return undefined
  return questionOptionNoteId(
    props.part.approval.key,
    Math.min(stepIndex.value, Math.max(questions.value.length - 1, 0)),
    optionIndex,
  )
}

function chooseOption(question: TimelineQuestion, option: TimelineQuestionOption): void {
  toggleQuestionOption(props.part.approval.key, question.id, option.id, question.allowMultiple)
}

function goToStep(index: number): void {
  stepIndex.value = Math.max(0, Math.min(index, questions.value.length - 1))
}

/** Typing in the composer, the custom-answer box or any other field must
 *  never be read as a shortcut. */
function isEditableTarget(target: EventTarget | null): boolean {
  const element = target as (HTMLElement & { closest?: (selector: string) => unknown }) | null
  if (!element || typeof element.closest !== 'function') return false
  return element.closest('input, textarea, select, [contenteditable], [role="textbox"]') !== null
}

/** A dialog or sheet owns the keyboard while it is open; a digit pressed there
 *  is meant for it, not for the card behind it. */
function isBlockingOverlayOpen(): boolean {
  if (typeof document === 'undefined') return false
  return document.querySelector('[role="dialog"][aria-modal="true"]') !== null
}

/** Whether the reader is already inside this card (tabbed to an option). */
function hasFocusInside(): boolean {
  const root = cardEl.value
  const active = document.activeElement
  return !!root && !!active && root.contains(active)
}

function handleDocumentKeydown(event: KeyboardEvent): void {
  if (!/^[1-9]$/.test(event.key)) return
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return
  if (controlDisabled.value) return
  // The open decision of the conversation, or a card the user is already on.
  if (!isLatestPending.value && !hasFocusInside()) return
  if (isEditableTarget(event.target) || isBlockingOverlayOpen()) return
  if (!identity.isPaneActivated.value) return
  const question = currentQuestion.value
  const option = question?.options[Number(event.key) - 1]
  if (!question || !option) return
  event.preventDefault()
  chooseOption(question, option)
}

onMounted(() => document.addEventListener('keydown', handleDocumentKeydown))
onBeforeUnmount(() => document.removeEventListener('keydown', handleDocumentKeydown))
</script>

<style scoped>
.approval-card {
  flex: 1;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--ch-color-border);
  border-left-width: 2px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
}

/* The open decision the turn is waiting on. A single accent edge marks it
   without decorating the card: nothing here animates. */
.approval-card--active {
  border-left-color: var(--ch-color-accent-ring-strong);
}

.approval-card--resolved {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 2px 2px 2px 0;
  border: none;
  background: transparent;
  font-size: 12px;
  color: var(--ch-color-text-muted);
}

.approval-resolved-mark {
  color: var(--ch-color-text-subtle);
}

.approval-resolved-text {
  overflow-wrap: anywhere;
}

.approval-resolved-note {
  color: var(--ch-color-text-subtle);
}

.approval-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.approval-card-heading {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.approval-card-eyebrow {
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ch-color-text-subtle);
}

/* The question itself is the title: the provider's short header alone made the
   card read like a form field instead of a question. */
.approval-card-title {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--ch-color-text-strong);
  overflow-wrap: anywhere;
}

.approval-card-badge {
  flex: none;
  padding-top: 2px;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  font-variant-numeric: tabular-nums;
}

.approval-card-steps {
  display: flex;
  gap: 4px;
  margin-bottom: 10px;
}

.approval-step {
  height: 2px;
  flex: 1;
  border-radius: 999px;
  background: var(--ch-color-border-muted);
}

.approval-step--done {
  background: var(--ch-color-text-subtle);
}

.approval-step--current {
  background: var(--ch-color-accent);
}

.approval-question + .approval-question {
  margin-top: 10px;
}

.approval-question-prompt {
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 500;
  line-height: 1.5;
  color: var(--ch-color-text);
  overflow-wrap: anywhere;
}

.approval-question-note {
  margin: 0 0 6px;
  font-size: 11px;
  color: var(--ch-color-text-subtle);
}

.approval-options {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* A tappable answer with its tradeoff underneath, not a chip: the tradeoff is
   the part that decides, so it cannot live in a tooltip or a truncated pill. */
.approval-option {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  width: 100%;
  padding: 9px 11px;
  text-align: left;
  font: inherit;
  color: var(--ch-color-text);
  background: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  cursor: pointer;
}

.approval-option:hover:not(:disabled) {
  border-color: var(--ch-color-border-hover);
}

.approval-option:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
}

.approval-option-index {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  margin-top: 1px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-chip-bg);
  font-size: 11px;
  color: var(--ch-color-text-subtle);
  font-variant-numeric: tabular-nums;
}

.approval-option-body {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.approval-option-label {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
}

.approval-option-text {
  font-size: 13px;
  font-weight: 500;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.approval-option-recommended {
  flex: none;
  padding: 0 5px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 999px;
  font-size: 10px;
  letter-spacing: 0.04em;
  line-height: 1.5;
  text-transform: uppercase;
  color: var(--ch-color-text-muted);
}

.approval-option-note {
  font-size: 12px;
  line-height: 1.45;
  color: var(--ch-color-text-muted);
  overflow-wrap: anywhere;
}

.approval-option--selected {
  border-color: var(--ch-color-accent);
  background: color-mix(in srgb, var(--ch-color-accent) 12%, transparent);
}

.approval-option:disabled {
  cursor: default;
}

.approval-option--selected:disabled {
  opacity: 0.7;
}

.approval-custom-answer {
  width: 100%;
  margin-top: 6px;
  padding: 7px 10px;
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 13px;
}

.approval-custom-answer::placeholder {
  color: var(--ch-color-text-subtle);
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

.approval-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}

.approval-secondary {
  padding: 6px 12px;
  font: inherit;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  background: transparent;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  cursor: pointer;
}

.approval-secondary:hover:not(:disabled) {
  border-color: var(--ch-color-border-hover);
  color: var(--ch-color-text);
}

.approval-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.approval-submit-btn {
  padding: 7px 14px;
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  color: var(--ch-color-on-accent);
  background: var(--ch-color-accent-strong);
  border: none;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
}

.approval-submit-btn:hover:not(:disabled) {
  background: var(--ch-color-accent-fill-hover);
}

.approval-submit-btn:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
}

.approval-submit-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.approval-key-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--ch-color-text-subtle);
}

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
}

/* Answers are thumb targets on a phone: the whole row is the control, and the
   number keys are meaningless without a hardware keyboard. */
@media (max-width: 640px), (pointer: coarse) {
  .approval-card {
    padding: 10px 11px;
  }

  .approval-option {
    min-height: 44px;
    padding: 11px 12px;
  }

  .approval-custom-answer {
    min-height: 44px;
  }

  .approval-submit-btn,
  .approval-secondary {
    min-height: 44px;
    padding-right: 16px;
    padding-left: 16px;
  }

  .approval-option-index,
  .approval-key-hint {
    display: none;
  }
}
</style>
