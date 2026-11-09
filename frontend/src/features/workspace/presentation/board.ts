import type { AgentReportState, WorkspaceTask, WorkspaceTaskStatus } from '@/types'

/**
 * Failed tasks keep their place in the Working column on purpose: the board has
 * no Failed column, and a failed task is still a live record that needs a
 * decision (retry, hand off, delete) rather than history. They are rendered
 * first and marked with the danger tone so they cannot be mistaken for work in
 * progress.
 */
export function orderedColumnTasks(
  tasks: readonly WorkspaceTask[],
  status: WorkspaceTaskStatus,
): WorkspaceTask[] {
  if (status !== 'working') {
    return tasks.filter(task => task.status === status)
  }
  const failed = tasks
    .filter(task => task.status === 'failed')
    .sort((a, b) => timestampMs(b.failed_at ?? b.updated_at) - timestampMs(a.failed_at ?? a.updated_at))
  const active = tasks.filter(task => task.status === 'working')
  return [...failed, ...active]
}

function timestampMs(value: string | null | undefined): number {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}

export function taskStatusLabel(status: WorkspaceTaskStatus): string {
  if (typeof status !== 'string' || !status) return ''
  return status.charAt(0).toUpperCase() + status.slice(1)
}

const REPORT_STATE_LABELS: Record<AgentReportState, string> = {
  started: 'Started',
  working: 'Working',
  blocked: 'Blocked',
  needs_input: 'Needs input',
  ready_for_review: 'Ready for review',
  completed: 'Completed',
  review_started: 'Reviewing',
  review_passed: 'Review passed',
  review_failed: 'Review failed',
  review_needs_input: 'Review needs input',
}

export function reportStateLabel(state: AgentReportState | null | undefined): string {
  if (!state) return ''
  return REPORT_STATE_LABELS[state] ?? taskStatusLabel(state as WorkspaceTaskStatus)
}

const EMPTY_COLUMN_TEXT: Record<WorkspaceTaskStatus, string> = {
  todo: 'Nothing waiting to start',
  queued: 'Nothing queued',
  working: 'No work in progress',
  review: 'Nothing waiting for your review',
  done: 'Nothing finished yet',
  failed: 'No failed tasks',
}

export function emptyColumnText(status: WorkspaceTaskStatus): string {
  return EMPTY_COLUMN_TEXT[status]
}
