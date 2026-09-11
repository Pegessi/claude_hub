import { ref, type Ref } from 'vue'
import {
  isQuestionAnswerComplete,
  type QuestionAnswerMap,
} from '@/utils/chatQuestionResponse'
import type { TimelineApproval, TimelineQuestion } from '@/utils/agentStreamTimeline'

/**
 * Per-approval interaction state for AskUserQuestion / AskQuestion /
 * request_user_input approval cards.
 *
 * The selection state lives here (component-scoped) rather than on the
 * timeline turn because the ``IncrementalTimelineReducer`` rebuilds turn
 * objects on every stream batch; storing selections on the turn would wipe
 * them out on the next delta. The approval is keyed by ``approval.key``
 * (``approval-<callId|messageId|sequence>``), which is stable across batches.
 *
 * ``StructuredPane`` memoizes each turn's DOM with ``v-memo``. The memo deps
 * must include a signature of this state (see ``approvalStateSignature``) or a
 * click that toggles a selection updates the reactive state but never
 * re-renders the turn — the chip shows no selected state and the submit button
 * stays disabled.
 */
export function useQuestionAnswers() {
  /** approvalKey -> (questionId -> selected option ids) */
  const questionAnswers: Ref<Record<string, QuestionAnswerMap>> = ref({})
  /** approval keys the user has already submitted (visual ack). */
  const resolvedApprovalKeys: Ref<Set<string>> = ref(new Set())
  /**
   * approvalKey -> (questionId -> free-text answer).
   *
   * Kept apart from ``questionAnswers`` rather than folded into it. Storing the
   * typed text as just another selection means telling the two apart by "is
   * this value one of the option ids?" — and in this codebase an option's id
   * IS its label, so typing an option's own text would read back as "the user
   * ticked that option", emptying the box. A separate field answers the
   * question directly: a selection is a selection, typed text is typed text.
   */
  const customAnswers: Ref<Record<string, Record<string, string>>> = ref({})

  function isQuestionOptionSelected(
    approvalKey: string,
    questionId: string,
    optionId: string,
  ): boolean {
    return (questionAnswers.value[approvalKey]?.[questionId] ?? []).includes(optionId)
  }

  function toggleQuestionOption(
    approvalKey: string,
    questionId: string,
    optionId: string,
    allowMultiple: boolean,
  ): void {
    const current = { ...(questionAnswers.value[approvalKey] ?? {}) }
    const selected = new Set(current[questionId] ?? [])
    if (allowMultiple) {
      if (selected.has(optionId)) selected.delete(optionId)
      else selected.add(optionId)
    } else {
      // Single-select: clicking an option replaces any prior selection — and
      // any typed answer, so the two can never be submitted together.
      selected.clear()
      selected.add(optionId)
      clearCustomAnswer(approvalKey, questionId)
    }
    current[questionId] = [...selected]
    questionAnswers.value = { ...questionAnswers.value, [approvalKey]: current }
  }

  /**
   * The free-text answer the user has typed for a question, or ``''``.
   */
  function customAnswer(approvalKey: string, question: TimelineQuestion): string {
    return customAnswers.value[approvalKey]?.[question.id] ?? ''
  }

  /**
   * Set (or clear) a question's free-text answer.
   *
   * Typing on a single-select question clears its ticked option — one answer,
   * not two. On a multi-select the typed answer joins whatever is ticked.
   * Blank input clears the answer, so an emptied box leaves the question
   * unanswered and the submit button disabled rather than submitting
   * whitespace.
   */
  function setCustomAnswer(
    approvalKey: string,
    question: TimelineQuestion,
    text: string,
  ): void {
    customAnswers.value = {
      ...customAnswers.value,
      [approvalKey]: {
        ...(customAnswers.value[approvalKey] ?? {}),
        [question.id]: text,
      },
    }
    if (text.trim() && !question.allowMultiple) {
      const selections = { ...(questionAnswers.value[approvalKey] ?? {}) }
      selections[question.id] = []
      questionAnswers.value = { ...questionAnswers.value, [approvalKey]: selections }
    }
  }

  /** Drop a question's typed answer — the ticked option is the answer now. */
  function clearCustomAnswer(approvalKey: string, questionId: string): void {
    const current = customAnswers.value[approvalKey]
    if (!current?.[questionId]) return
    const next = { ...current }
    delete next[questionId]
    customAnswers.value = { ...customAnswers.value, [approvalKey]: next }
  }

  function isApprovalResolved(approval: TimelineApproval): boolean {
    return approval.resolved || resolvedApprovalKeys.value.has(approval.key)
  }

  function canSubmitQuestion(approval: TimelineApproval): boolean {
    return isQuestionAnswerComplete(approval.questions, answersFor(approval.key))
  }

  /** The answers as they will be submitted: ticked options plus typed text. */
  function answersFor(approvalKey: string): QuestionAnswerMap {
    const merged: QuestionAnswerMap = {}
    for (const [questionId, selected] of Object.entries(questionAnswers.value[approvalKey] ?? {})) {
      merged[questionId] = [...selected]
    }
    for (const [questionId, text] of Object.entries(customAnswers.value[approvalKey] ?? {})) {
      if (!text.trim()) continue
      merged[questionId] = [...(merged[questionId] ?? []), text]
    }
    return merged
  }

  function markResolved(approvalKey: string): void {
    resolvedApprovalKeys.value = new Set(resolvedApprovalKeys.value).add(approvalKey)
  }

  function reset(): void {
    questionAnswers.value = {}
    customAnswers.value = {}
    resolvedApprovalKeys.value = new Set()
  }

  return {
    questionAnswers,
    resolvedApprovalKeys,
    customAnswers,
    customAnswer,
    setCustomAnswer,
    isQuestionOptionSelected,
    toggleQuestionOption,
    isApprovalResolved,
    canSubmitQuestion,
    answersFor,
    markResolved,
    reset,
  }
}

/**
 * Pure signature of one approval card's render-affecting interaction state:
 * its resolved flag plus the selected option id(s) for every question.
 *
 * ``StructuredPane`` folds this into each turn's ``v-memo`` deps so a toggle
 * re-renders only the turn owning that approval. Kept pure (state passed in)
 * so it can be unit-tested without mounting a component.
 */
export function approvalStateSignature(
  approval: TimelineApproval,
  questionAnswers: Record<string, QuestionAnswerMap>,
  resolvedKeys: Set<string>,
  customAnswers: Record<string, Record<string, string>> = {},
): string {
  const answers = questionAnswers[approval.key]
  const custom = customAnswers[approval.key] ?? {}
  // The typed text is part of the signature: it decides whether submit is
  // enabled, so a keystroke has to invalidate the turn's memo.
  const answerSig = approval.questions
    .map(question => `${question.id}=${(answers?.[question.id] ?? []).join('|')}+${custom[question.id] ?? ''}`)
    .join(',')
  return `${approval.resolved || resolvedKeys.has(approval.key) ? 'r' : 'o'}:${answerSig}`
}
