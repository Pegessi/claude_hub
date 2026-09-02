export interface QuestionOption {
  id: string
  label: string
}

export interface StructuredQuestion {
  id: string
  prompt: string
  allowMultiple: boolean
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
        options.push({ id: optId, label })
      }
    }
    if (options.length === 0) continue
    questions.push({
      id,
      prompt,
      allowMultiple: record.allow_multiple === true,
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
  return questions.every((question) => {
    const selected = answers[question.id] ?? []
    return selected.length > 0
  })
}
