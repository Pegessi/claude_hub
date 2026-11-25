import type { FeedbackLesson, FeedbackSummaryRun, WorkspaceTask } from '@/types'

function feedbackTokens(value: string): Set<string> {
  const text = value.toLowerCase()
  const tokens = new Set<string>()
  const asciiMatches = text.match(/[a-z0-9][a-z0-9._/-]*/g) || []
  asciiMatches.forEach((raw) => {
    const cleaned = raw.replace(/^[._/-]+|[._/-]+$/g, '')
    if (cleaned.length >= 2) tokens.add(cleaned)
    cleaned.split(/[._/-]+/).forEach((part) => {
      if (part.length >= 2) tokens.add(part)
    })
  })
  const cjkChunks = text.match(/\p{Script=Han}{2,}/gu) || []
  cjkChunks.forEach((chunk) => {
    for (let index = 0; index <= chunk.length - 2; index += 1) {
      tokens.add(chunk.slice(index, index + 2))
    }
  })
  return tokens
}

function feedbackLessonText(lesson: FeedbackLesson): string {
  return [
    lesson.id,
    lesson.summary,
    lesson.do || '',
    lesson.avoid || '',
    ...(lesson.applies_when || []),
    ...(lesson.tags || []),
  ].join(' ')
}

function feedbackLessonScore(lesson: FeedbackLesson, task: WorkspaceTask): number {
  const queryTokens = feedbackTokens(`${task.title} ${task.prompt}`)
  if (queryTokens.size === 0) return 0
  const lessonTokens = feedbackTokens(feedbackLessonText(lesson))
  let score = 0
  queryTokens.forEach((token) => {
    if (lessonTokens.has(token)) score += 1
  })
  return score
}

export function matchingFeedbackLessons(
  task: WorkspaceTask | null,
  activeLessons: readonly FeedbackLesson[],
): FeedbackLesson[] {
  if (!task) return []
  return activeLessons
    .map(lesson => ({ lesson, score: feedbackLessonScore(lesson, task) }))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score)
    .map(item => item.lesson)
    .slice(0, 6)
}

export function lessonTitle(lesson: FeedbackLesson): string {
  return lesson.title?.trim() || lesson.summary.split(/[。.!?]/)[0]?.trim() || lesson.id
}

export function lessonDescription(lesson: FeedbackLesson): string {
  return lesson.summary || lesson.do || lesson.avoid || lesson.id
}

export function lessonTags(lesson: FeedbackLesson): string[] {
  return (lesson.tags || []).slice(0, 4)
}

export function feedbackSummaryTone(run: FeedbackSummaryRun): 'queued' | 'skipped' | 'done' {
  if (run.running || run.task_id) return 'queued'
  if (run.error || run.cache_hit || run.skipped_reason) return 'skipped'
  return 'done'
}

export function feedbackSummaryTitle(run: FeedbackSummaryRun): string {
  if (run.running) return 'Silent AI summary running'
  if (run.task_id) return 'Internal AI summary queued'
  if (run.error) return 'Summary failed'
  if (run.skipped_reason === 'no_new_task_records') return 'No new task records'
  if (run.skipped_reason) return 'Summary skipped'
  return 'Summary recorded'
}

export function feedbackSummaryDescription(run: FeedbackSummaryRun): string {
  if (run.running) {
    return 'A silent background Feedback Reaper run was started. It will distill lessons in the background without adding a task card to the board.'
  }
  if (run.task_id) {
    return 'A legacy Feedback Reaper task was started. It will update lessons and write audit evidence when it finishes.'
  }
  if (run.error) {
    return `The background feedback summary failed without modifying the board: ${run.error}.`
  }
  if (run.skipped_reason === 'no_new_task_records') {
    return 'No completed task records are available or changed, so no background AI summary was started. Force AI run can reprocess cached records once this workspace has records.'
  }
  if (run.skipped_reason) {
    return `No background AI summary was started: ${run.skipped_reason}.`
  }
  return 'The feedback summary run was recorded.'
}

export function feedbackLessonMatchSummary(
  tasks: readonly WorkspaceTask[],
  activeLessons: readonly FeedbackLesson[],
): string {
  const matchedTaskCount = tasks.filter(task => matchingFeedbackLessons(task, activeLessons).length > 0).length
  if (activeLessons.length === 0) return 'No active lessons indexed'
  if (matchedTaskCount === 0) return 'No current task matches'
  return `${matchedTaskCount} current tasks match active lessons`
}
