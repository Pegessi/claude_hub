import type { AgentType } from './taskExecution'

export type ScheduledTaskKind = 'chat_turn' | 'tab_message' | 'new_session' | 'hub_task'

export type ScheduledTaskStatus =
  | 'ok'
  | 'error'
  | 'queued'
  | 'waiting'
  | 'dispatching'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'uncertain'
  | 'cancelled'

export interface ScheduledTask {
  id: string
  name: string
  kind: ScheduledTaskKind
  enabled: boolean
  run_at?: string | null
  cron?: string | null
  interval_seconds?: number | null
  tab_id?: string | null
  workspace_id?: string | null
  agent_type: AgentType
  message?: string | null
  task_title?: string | null
  env_preset?: string | null
  last_run_at?: string | null
  next_run_at?: string | null
  last_status?: ScheduledTaskStatus | null
  last_error?: string | null
  last_run_id?: string | null
  run_count: number
  created_at: string
  updated_at: string
  // Live derived counts for chat_turn tasks (not persisted).
  active_run_count?: number
  queued_run_count?: number
  in_flight_run_count?: number
  in_flight_run_id?: string | null
  in_flight_since?: string | null
}

export interface ScheduledTaskRun {
  id: string
  scheduled_task_id: string
  tab_id: string
  client_turn_id: string
  message: string
  status: ScheduledTaskStatus
  scheduled_for: string
  queued_at: string
  dispatched_at?: string | null
  completed_at?: string | null
  waiting_reason?: string | null
  error?: string | null
}

export interface ScheduledTaskCreate {
  name: string
  kind: ScheduledTaskKind
  enabled?: boolean
  run_at?: string | null
  cron?: string | null
  interval_seconds?: number | null
  tab_id?: string | null
  workspace_id?: string | null
  agent_type?: AgentType
  message?: string | null
  task_title?: string | null
  env_preset?: string | null
}

export interface ScheduledTaskUpdate {
  name?: string
  enabled?: boolean
  run_at?: string | null
  cron?: string | null
  interval_seconds?: number | null
  tab_id?: string | null
  workspace_id?: string | null
  agent_type?: AgentType | null
  message?: string | null
  task_title?: string | null
  env_preset?: string | null
}

export interface ScheduledTaskRunResult {
  id: string
  last_run_id?: string | null
  last_run_at?: string | null
  last_status?: ScheduledTaskStatus | null
  last_error?: string | null
}
