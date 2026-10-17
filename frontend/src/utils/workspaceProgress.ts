import type { AgentReport, WorkspaceTask } from '@/types'
import { formatElapsedDuration, parseTimestampMs } from '@/utils/duration'

export interface ProgressTimelineItem {
  id: string
  label: string
  timestampMs: number
  elapsedLabel: string
  deltaLabel: string
  tone: 'task' | 'report' | 'terminal' | 'live'
}

export function latestReportAgeLabel(reports: readonly AgentReport[], nowMs: number): string {
  const latestReport = reports[reports.length - 1]
  if (!latestReport) return 'none'
  const latestMs = parseTimestampMs(latestReport.created_at)
  if (latestMs === null) return 'unknown'
  return `${formatElapsedDuration(nowMs - latestMs)} ago`
}

export function progressTimeline(
  task: WorkspaceTask | null,
  reports: readonly AgentReport[],
  nowMs: number,
): ProgressTimelineItem[] {
  if (!task) return []

  const rawItems: Array<Omit<ProgressTimelineItem, 'elapsedLabel' | 'deltaLabel'>> = []
  addTimelineItem(rawItems, 'task-created', 'Created', task.created_at, 'task')
  addTimelineItem(rawItems, 'task-queued', 'Queued', task.queued_at, 'task')
  addTimelineItem(rawItems, 'task-started', 'Started', task.started_at, 'task')
  reports.forEach((report) => {
    addTimelineItem(
      rawItems,
      `report-${report.id}`,
      report.state.replace(/_/g, ' '),
      report.created_at,
      'report',
    )
  })
  addTimelineItem(rawItems, 'task-reviewed', 'Review', task.reviewed_at, 'task')
  addTimelineItem(rawItems, 'task-failed', 'Failed', task.failed_at, 'terminal')
  addTimelineItem(rawItems, 'task-done', 'Done', task.completed_at, 'terminal')
  addTimelineItem(rawItems, 'task-aborted', 'Aborted', task.manual_aborted_at, 'terminal')

  if (
    task.status === 'queued' ||
    task.status === 'working' ||
    task.status === 'review' ||
    task.status === 'failed'
  ) {
    rawItems.push({
      id: 'task-now',
      label: 'Now',
      timestampMs: nowMs,
      tone: 'live',
    })
  }

  return rawItems
    .sort((a, b) => a.timestampMs - b.timestampMs)
    .map((item, index, items) => {
      const previous = items[index - 1]
      return {
        ...item,
        elapsedLabel: formatElapsedDuration(item.timestampMs - items[0].timestampMs),
        deltaLabel: previous ? formatElapsedDuration(item.timestampMs - previous.timestampMs) : '',
      }
    })
}

function addTimelineItem(
  items: Array<Omit<ProgressTimelineItem, 'elapsedLabel' | 'deltaLabel'>>,
  id: string,
  label: string,
  value: string | null | undefined,
  tone: ProgressTimelineItem['tone'],
) {
  const timestampMs = parseTimestampMs(value)
  if (timestampMs === null) return
  items.push({ id, label, timestampMs, tone })
}

function taskTimingEndMs(task: WorkspaceTask, nowMs: number): number {
  return (
    parseTimestampMs(task.completed_at) ??
    parseTimestampMs(task.manual_aborted_at) ??
    (
      task.status === 'queued' ||
      task.status === 'working' ||
      task.status === 'review' ||
      task.status === 'failed'
        ? nowMs
        : parseTimestampMs(task.updated_at) ?? nowMs
    )
  )
}

export function taskTotalElapsedLabel(task: WorkspaceTask, nowMs: number): string {
  const startedMs = parseTimestampMs(task.created_at)
  if (startedMs === null) return 'unknown'
  return formatElapsedDuration(taskTimingEndMs(task, nowMs) - startedMs)
}

function taskWorkingStartedMs(task: WorkspaceTask, reports: readonly AgentReport[]): number | null {
  return (
    parseTimestampMs(task.started_at) ??
    parseTimestampMs(task.autonomous_run?.iterations?.[0]?.started_at) ??
    parseTimestampMs(reports[0]?.created_at)
  )
}

export function taskWorkingElapsedLabel(
  task: WorkspaceTask,
  reports: readonly AgentReport[],
  nowMs: number,
): string {
  const startedMs = taskWorkingStartedMs(task, reports)
  if (startedMs === null) return 'not started'
  return formatElapsedDuration(taskTimingEndMs(task, nowMs) - startedMs)
}

function reportReferenceForIndex(
  index: number,
  task: WorkspaceTask | null,
  reports: readonly AgentReport[],
): { label: string; timestampMs: number } | null {
  const previousReportMs = parseTimestampMs(reports[index - 1]?.created_at)
  if (previousReportMs !== null) return { label: 'previous report', timestampMs: previousReportMs }

  const reportMs = parseTimestampMs(reports[index]?.created_at)
  if (!task || reportMs === null) return null

  const startedMs = parseTimestampMs(task.started_at)
  if (startedMs !== null && startedMs <= reportMs) return { label: 'start', timestampMs: startedMs }

  const queuedMs = parseTimestampMs(task.queued_at)
  if (queuedMs !== null && queuedMs <= reportMs) return { label: 'queue', timestampMs: queuedMs }

  const createdMs = parseTimestampMs(task.created_at)
  if (createdMs !== null) return { label: 'creation', timestampMs: createdMs }

  return null
}

export function reportElapsedLabel(
  report: AgentReport,
  index: number,
  task: WorkspaceTask | null,
  reports: readonly AgentReport[],
): string {
  const reportMs = parseTimestampMs(report.created_at)
  const reference = reportReferenceForIndex(index, task, reports)
  if (reportMs === null || !reference) return ''

  const elapsed = formatElapsedDuration(reportMs - reference.timestampMs)
  return index === 0 ? `${reference.label} +${elapsed}` : `+${elapsed}`
}

export function reportElapsedTitle(
  report: AgentReport,
  index: number,
  task: WorkspaceTask | null,
  reports: readonly AgentReport[],
): string {
  const reportMs = parseTimestampMs(report.created_at)
  const reference = reportReferenceForIndex(index, task, reports)
  if (reportMs === null || !reference) return 'Elapsed time unavailable'

  return `Elapsed since ${reference.label}: ${formatElapsedDuration(reportMs - reference.timestampMs)}`
}
