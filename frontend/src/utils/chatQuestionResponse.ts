export interface QuestionOption {
  id: string
  label: string
  description?: string
  /** The provider suggested this option; shown as a "Recommended" marker. */
  recommended?: boolean
}

export interface StructuredQuestion {
  id: string
  prompt: string
  allowMultiple: boolean
  isSecret?: boolean
  options: QuestionOption[]
}

export interface QuestionAnswerMap {
  [questionId: string]: string[]
}

/** Provider convention for marking a suggested option: no question tool has a
 *  recommendation field, so agents append this suffix to the label itself.
 *  Anchored at the end and case-insensitive so "(Recommended)" is recognized
 *  both in the ASCII and full-width forms agents produce. */
const RECOMMENDED_LABEL = /^(.+?)\s*[（(]\s*recommended\s*[)）]\s*$/i

/** Peel the provider's "(Recommended)" suffix off a label.
 *
 *  The marker is a display hint, not part of the choice: the label is what the
 *  user reads back on the resolved card and in the answer they check, so it
 *  must not carry the suffix. Option ids stay untouched (providers reuse the
 *  label as the id), which keeps the submitted answer self-describing.
 *  A label that is only the suffix keeps its text and is not marked.
 */
export function splitRecommendedLabel(label: string): { label: string; recommended: boolean } {
  const match = label.match(RECOMMENDED_LABEL)
  const trimmed = (match?.[1] ?? '').trim()
  if (!match || !trimmed) return { label, recommended: false }
  return { label: trimmed, recommended: true }
}

export function parseStructuredQuestions(raw: unknown): StructuredQuestion[] {
  if (!Array.isArray(raw)) return []
  const questions: StructuredQuestion[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const id = typeof record.id === 'string' ? record.id.trim() : ''
    const prompt = typeof record.prompt === 'string' ? record.prompt.trim() : ''
    if (!id || !prompt) continue
    const optionsRaw = record.options
    const options: QuestionOption[] = []
    if (Array.isArray(optionsRaw)) {
      for (const option of optionsRaw) {
        if (!option || typeof option !== 'object') continue
        const opt = option as Record<string, unknown>
        const optId = typeof opt.id === 'string' ? opt.id.trim() : ''
        const rawLabel = typeof opt.label === 'string' ? opt.label.trim() : ''
        if (!optId || !rawLabel) continue
        const { label, recommended } = splitRecommendedLabel(rawLabel)
        const description = typeof opt.description === 'string' ? opt.description.trim() : ''
        options.push({
          id: optId,
          label,
          ...(description ? { description } : {}),
          ...(recommended ? { recommended: true } : {}),
        })
      }
    }
    // Modern providers can ask a free-text question without suggestions.
    // Its prompt and ID are sufficient to render and route an answer.
    questions.push({
      id,
      prompt,
      allowMultiple: record.allow_multiple === true,
      ...(record.is_secret === true ? { isSecret: true } : {}),
      options,
    })
  }
  return questions
}

export function formatAskQuestionResponse(answers: QuestionAnswerMap): string {
  const payload = {
    type: 'ask_question_response',
    answers: Object.entries(answers).map(([questionId, selected]) => ({
      questionId,
      selected,
    })),
  }
  return JSON.stringify(payload)
}

export function isQuestionAnswerComplete(
  questions: readonly StructuredQuestion[],
  answers: QuestionAnswerMap,
): boolean {
  return questions.length > 0 && questions.every(question => isQuestionAnswered(question, answers))
}

/** Whether this one question has an answer so far.
 *
 *  Split out from {@link isQuestionAnswerComplete} because a multi-question
 *  card advances one question at a time: "can I move on" is asked per step,
 *  while submit asks it for the whole card. Blank (whitespace-only) typed
 *  text is not an answer.
 */
export function isQuestionAnswered(
  question: StructuredQuestion,
  answers: QuestionAnswerMap,
): boolean {
  return (answers[question.id] ?? []).some(answer => answer.trim().length > 0)
}

/** The user's answers as one readable line, for the calm resolved state.
 *
 *  Selected option ids are preferred over their labels (they are equal for
 *  Claude/Codex, but Cursor sends real ids); typed answers have no label and
 *  are shown verbatim. Questions are joined with a middle dot, options within
 *  a question with a comma, so "You chose: Fast · Safe" reads as answers to
 *  two questions and "Fast, Safe" as one multi-select answer.
 */
export function describeQuestionAnswers(
  questions: readonly StructuredQuestion[],
  answers: QuestionAnswerMap,
): string {
  const groups: string[] = []
  for (const question of questions) {
    const labels = (answers[question.id] ?? [])
      .filter(answer => answer.trim().length > 0)
      .map(answer => question.options.find(option => option.id === answer)?.label ?? answer)
    if (labels.length > 0) groups.push(labels.join(', '))
  }
  return groups.join(' · ')
}

/** Key of the approval the user still has to answer, or ``null``.
 *
 *  Approvals arrive in stream order, so the last unresolved one is the
 *  decision the conversation is waiting on. Only that card claims the
 *  number-key shortcuts; older cards stay quiet so a keystroke can never
 *  answer a question the user has scrolled past.
 */
export function latestPendingApprovalKey(
  approvals: readonly { key: string; resolved: boolean }[],
): string | null {
  let pending: string | null = null
  for (const approval of approvals) {
    if (!approval.resolved) pending = approval.key
  }
  return pending
}

/** DOM id for one option's tradeoff line.
 *
 *  Claude and Codex reuse the option *label* as the option id, so ``option.id``
 *  is ordinary prose ("Inbox in the top bar"). Neither an ``id`` (no ASCII
 *  whitespace allowed) nor an ``aria-describedby`` reference (a
 *  whitespace-separated IDREF list) can carry that: the reference splits into
 *  several dangling tokens and the tradeoff line stops being announced. Keying
 *  on the question/option *positions* and a sanitized card key keeps the
 *  attribute one valid token whatever the provider puts in the labels.
 */
export function questionOptionNoteId(
  approvalKey: string,
  questionIndex: number,
  optionIndex: number,
): string {
  const scope = approvalKey.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
  return `approval-note-${scope || 'approval'}-q${questionIndex}-o${optionIndex}`
}
