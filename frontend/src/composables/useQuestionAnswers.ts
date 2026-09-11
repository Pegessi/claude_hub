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
      // Single-select: clicking an option replaces any prior selection.
      selected.clear()
      selected.add(optionId)
    }
    current[questionId] = [...selected]
    questionAnswers.value = { ...questionAnswers.value, [approvalKey]: current }
  }

  /**
   * The free-text answer the user has typed for a question, or ``''``.
   *
   * A typed answer is stored as an ordinary selection, so everything
   * downstream — the completion check, the response payload, the memo
   * signature — treats it like any other answer. It is told apart on the way
   * back out by not being one of the question's option ids.
   */
  function customAnswer(approvalKey: string, question: TimelineQuestion): string {
    const selected = questionAnswers.value[approvalKey]?.[question.id] ?? []
    const optionIds = new Set(question.options.map(option => option.id))
    return selected.find(value => !optionIds.has(value)) ?? ''
  }

  /**
   * Set (or clear) a question's free-text answer.
   *
   * A single-select question keeps only the typed answer; a multi-select keeps
   * it alongside the options already ticked. Blank input clears it rather than
   * selecting whitespace, so an empty box leaves the question unanswered and
   * the submit button disabled.
   */
  function setCustomAnswer(
    approvalKey: string,
    question: TimelineQuestion,
    text: string,
  ): void {
    const current = { ...(questionAnswers.value[approvalKey] ?? {}) }
    const optionIds = new Set(question.options.map(option => option.id))
    const ticked = (current[question.id] ?? []).filter(value => optionIds.has(value))
    current[question.id] = text.trim()
      ? (question.allowMultiple ? [...ticked, text] : [text])
      : ticked
    questionAnswers.value = { ...questionAnswers.value, [approvalKey]: current }
  }

  function isApprovalResolved(approval: TimelineApproval): boolean {
    return approval.resolved || resolvedApprovalKeys.value.has(approval.key)
  }

  function canSubmitQuestion(approval: TimelineApproval): boolean {
    const answers = questionAnswers.value[approval.key] ?? {}
    return isQuestionAnswerComplete(approval.questions, answers)
  }

  function answersFor(approvalKey: string): QuestionAnswerMap {
    return questionAnswers.value[approvalKey] ?? {}
  }

  function markResolved(approvalKey: string): void {
    resolvedApprovalKeys.value = new Set(resolvedApprovalKeys.value).add(approvalKey)
  }

  function reset(): void {
    questionAnswers.value = {}
    resolvedApprovalKeys.value = new Set()
  }

  return {
    questionAnswers,
    resolvedApprovalKeys,
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
): string {
  const answers = questionAnswers[approval.key]
  const answerSig = approval.questions
    .map(question => `${question.id}=${(answers?.[question.id] ?? []).join('|')}`)
    .join(',')
  return `${approval.resolved || resolvedKeys.has(approval.key) ? 'r' : 'o'}:${answerSig}`
}
