/**
 * Done-tasks preview helpers.
 *
 * The AgentWorkspace view renders a potentially large number of done tasks.
 * To keep mode switches cheap, the Done column shows only the most recent
 * `DONE_TASKS_PREVIEW_LIMIT` tasks by default; the rest are revealed via a
 * "Show more" toggle. These helpers isolate the sorting + slicing logic so
 * it can be unit-tested without mounting the Vue component.
 */

export const DONE_TASKS_PREVIEW_LIMIT = 15

export interface DoneTaskLike {
  completed_at?: string | null
  created_at?: string | null
}

/**
 * Parse an ISO-8601 timestamp into epoch milliseconds.
 * Returns null when the value is absent or unparseable.
 */
export function parseTimestampMs(value: string | null | undefined): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isNaN(ms) ? null : ms
}

/**
 * Sort done tasks by completion time (most recent first).
 * Falls back to `created_at` when `completed_at` is absent, and finally to 0
 * (tasks without any timestamp sort last).
 */
export function sortDoneTasksByRecency<T extends DoneTaskLike>(tasks: T[]): T[] {
  return [...tasks].sort((a, b) => {
    const aMs = parseTimestampMs(a.completed_at) ?? parseTimestampMs(a.created_at) ?? 0
    const bMs = parseTimestampMs(b.completed_at) ?? parseTimestampMs(b.created_at) ?? 0
    return bMs - aMs
  })
}

/**
 * Return the done tasks that should be rendered in the Done column.
 *
 * When `showAll` is false, only the most recent `DONE_TASKS_PREVIEW_LIMIT`
 * tasks are returned. When `showAll` is true, all done tasks are returned
 * (still sorted by recency).
 */
export function previewDoneTasks<T extends DoneTaskLike>(
  tasks: T[],
  showAll: boolean,
): T[] {
  const sorted = sortDoneTasksByRecency(tasks)
  if (showAll) return sorted
  return sorted.slice(0, DONE_TASKS_PREVIEW_LIMIT)
}

/**
 * Number of done tasks hidden behind the "Show more" button.
 * Zero when there are no hidden tasks (i.e. the button should not render).
 */
export function hiddenDoneTaskCount(total: number, showAll: boolean): number {
  if (showAll) return 0
  return Math.max(0, total - DONE_TASKS_PREVIEW_LIMIT)
}
