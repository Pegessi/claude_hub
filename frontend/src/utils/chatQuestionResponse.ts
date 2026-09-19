export interface QuestionOption {
  id: string
  label: string
  description?: string
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
        const label = typeof opt.label === 'string' ? opt.label.trim() : ''
        if (!optId || !label) continue
        const description = typeof opt.description === 'string' ? opt.description.trim() : ''
        options.push({ id: optId, label, ...(description ? { description } : {}) })
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
  return questions.length > 0 && questions.every((question) => {
    const selected = answers[question.id] ?? []
    return selected.some(answer => answer.trim().length > 0)
  })
}
