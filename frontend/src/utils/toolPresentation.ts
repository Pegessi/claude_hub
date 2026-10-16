import type { TimelineTool } from './agentStreamTimeline'

const SUMMARY_NAME_LIMIT = 3
const SUMMARY_NAME_LENGTH = 48
const TOOL_STATUS_LABELS: Record<TimelineTool['status'], string> = {
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

/** Bound work and summary text even when a tool group grows throughout a turn. */
export function toolGroupNames(tools: readonly Pick<TimelineTool, 'name'>[]): string {
  const names = tools.slice(0, SUMMARY_NAME_LIMIT).map(({ name }) =>
    name.length > SUMMARY_NAME_LENGTH ? `${name.slice(0, SUMMARY_NAME_LENGTH - 1)}…` : name,
  )
  const remaining = tools.length - names.length
  return `${names.join(', ')}${remaining > 0 ? ` · +${remaining} more` : ''}`
}

export function toolStatusLabel(status: TimelineTool['status']): string {
  return TOOL_STATUS_LABELS[status]
}
