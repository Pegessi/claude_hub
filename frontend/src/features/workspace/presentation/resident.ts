import type { AgentReport, ResidentPeriodicTask, Workspace, WorkspaceUpdate } from '@/types'
import {
  formatElapsedDuration,
  parseTimestampMs,
} from '@/utils/duration'

// The modal owns this draft; projection never mutates it or the saved workspace.
type ResidentAgentDraft = Pick<Workspace,
  | 'resident_agent_enabled' | 'resident_agent_paused' | 'resident_agent_master_mode'
  | 'resident_agent_interval_minutes' | 'resident_agent_periodic_tasks' | 'resident_agent_type'
  | 'resident_agent_solo_mode' | 'resident_agent_target' | 'resident_agent_remote_reconnect'
> & {
  resident_agent_directive: string
  resident_agent_title: string
  resident_agent_cwd: string
  resident_agent_remote_profile_id: string
}

// Drop blank rows and trim text; keep ids/order so a save round-trips stable.
export function sanitizePeriodicTasks(tasks: readonly ResidentPeriodicTask[]): ResidentPeriodicTask[] {
  return tasks
    .map((task) => ({ ...task, text: (task.text ?? '').trim() }))
    .filter((task) => task.text.length > 0)
}

export function enabledPeriodicTaskCount(tasks: readonly ResidentPeriodicTask[]): number {
  return tasks.filter((task) => task.enabled && (task.text ?? '').trim().length > 0).length
}

export function residentUpdatePayload(
  draft: ResidentAgentDraft,
  env: Record<string, string>,
  overrides: Partial<WorkspaceUpdate> = {},
): WorkspaceUpdate {
  return {
    resident_agent_enabled: draft.resident_agent_enabled,
    resident_agent_paused: draft.resident_agent_paused,
    resident_agent_master_mode: draft.resident_agent_master_mode,
    resident_agent_interval_minutes: draft.resident_agent_interval_minutes,
    resident_agent_directive: draft.resident_agent_directive.trim() || undefined,
    resident_agent_periodic_tasks: sanitizePeriodicTasks(
      draft.resident_agent_periodic_tasks,
    ),
    resident_agent_type: draft.resident_agent_type,
    resident_agent_env: env,
    resident_agent_solo_mode: draft.resident_agent_solo_mode,
    resident_agent_title: draft.resident_agent_title.trim() || null,
    resident_agent_target: draft.resident_agent_target,
    resident_agent_remote_profile_id:
      draft.resident_agent_target === 'remote'
        ? draft.resident_agent_remote_profile_id || null
        : null,
    resident_agent_cwd: draft.resident_agent_cwd.trim() || null,
    resident_agent_remote_reconnect: draft.resident_agent_remote_reconnect,
    ...overrides,
  }
}

export function residentLastRunLabel(workspace: Workspace | null, nowMs: number): string {
  const lastRunMs = parseTimestampMs(workspace?.resident_agent_last_run_at)
  if (lastRunMs === null) return 'never'
  return `${formatElapsedDuration(nowMs - lastRunMs)} ago`
}

export function residentNextRunLabel(workspace: Workspace | null, nowMs: number): string {
  if (!workspace || !workspace.resident_agent_enabled) return ''
  if (workspace.resident_agent_run_requested_at) return 'queued'
  if (workspace.resident_agent_paused) return 'paused'
  const nextRunMs = parseTimestampMs(workspace.resident_agent_next_run_at)
  if (nextRunMs === null) return ''
  const remaining = nextRunMs - nowMs
  if (remaining <= 0) return 'due now'
  return `in ${formatElapsedDuration(remaining)}`
}

export function latestResidentReport(
  sessionId: string | null | undefined,
  reports: readonly AgentReport[],
): AgentReport | null {
  if (!sessionId) return null
  const residentReports = reports.filter(
    report => report.session_id === sessionId
  )
  if (residentReports.length === 0) return null
  return [...residentReports].sort(
    (a, b) => (parseTimestampMs(b.created_at) ?? 0) - (parseTimestampMs(a.created_at) ?? 0)
  )[0]
}
