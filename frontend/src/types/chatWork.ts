/** A durable work item associated with a Chat, independent of its transcript. */
export interface ChatWork {
  id: string
  source_tab_id: string
  workspace_id: string
  title: string
  kind: 'task' | 'monitor'
  status: 'running' | 'waiting' | 'paused' | 'stopped' | 'completed' | 'failed' | 'review'
  agent_type: string
  model: string | null
  cwd: string
  interval_seconds: number | null
  next_run_at: string | null
  run_count: number
  active_task_id: string | null
  created_at: string
  updated_at: string
  latest_result: {
    kind: 'progress' | 'anomaly' | 'completed' | 'decision' | 'failed'
    summary: string
    task_id: string | null
    created_at: string
    validation: string | null
    report_id: string | null
  } | null
  executions: {
    task_id: string
    status: string
    created_at: string
    updated_at: string
    summary: string | null
    outcome: string | null
  }[]
}

export interface ChatWorkUpdate {
  action?: 'pause' | 'resume' | 'stop'
  interval_seconds?: number
}
