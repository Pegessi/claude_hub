import type { WorkspaceTask } from './workspace'

export type AgentType = 'claude' | 'codex' | 'traex' | 'maso' | 'cursor' | 'antigravity' | 'terminal'
export type SessionKind = 'chat' | 'terminal'
export type ExecutionTarget = 'local' | 'remote'
export type AgentRuntimeStatus = 'idle' | 'working' | 'attention' | 'offline'
export type AppMode = 'terminal' | 'workspace'
export type ColorScheme = 'dark' | 'light'
export type WorkspaceTaskStatus = 'todo' | 'queued' | 'working' | 'review' | 'done' | 'failed'
export type WorkspaceTaskMode = 'direct' | 'reviewed' | 'autonomous' | 'subagent'
export type WorkspaceTaskExecutionComplexity = 'auto' | 'simple' | 'complex'
export type WorkspaceTaskOrigin = 'human' | 'resident'
export type WorkspaceTaskExecutionControl = 'workspace' | 'initiator'
export type WorkspaceTaskSourceKind = 'human' | 'chat' | 'agent'
export type WorkspaceTaskProgressState =
  | 'started' | 'working' | 'blocked' | 'needs_input'
  | 'completed' | 'failed' | 'released'
export type WorkspaceTaskRuntimeStatus =
  | 'active' | 'idle' | 'error' | 'disconnected' | 'unknown'

export interface WorkspaceTaskSource {
  kind: WorkspaceTaskSourceKind
  tab_id: string | null
  agent_id: string | null
}

export interface WorkspaceTaskExecutionRef {
  provider: string | null
  session_id: string | null
  thread_id: string | null
  turn_id: string | null
  run_epoch: number | null
}

export interface WorkspaceTaskProgress {
  state: WorkspaceTaskProgressState
  summary: string
  validation: string | null
  risks: string | null
  artifact_refs: string[]
  call_id: string
  execution_epoch: number
  reported_at: string
}

export interface WorkspaceTaskRuntimeObservation {
  execution_epoch: number
  stream_sequence: number
  status: WorkspaceTaskRuntimeStatus
  observed_at: string
  turn_id: string | null
  detail: string | null
}

export type WorkspaceTaskCapabilitiesStatus =
  | 'idle' | 'loading' | 'supported' | 'unsupported' | 'error'

export interface WorkspaceTaskCapabilities {
  supported_execution_controls: WorkspaceTaskExecutionControl[]
  progress_states: WorkspaceTaskProgressState[]
  record_only_requires_reporter_key: boolean
  handoff_requires_release: boolean
}

export interface WorkspaceTaskManualProgressRequest {
  call_id: string
  expected_execution_epoch: number
  expected_progress_revision: number
  state: Exclude<WorkspaceTaskProgressState, 'released'>
  summary: string
  validation?: string | null
  risks?: string | null
  artifact_refs?: string[]
}

export interface WorkspaceTaskHandoffRequest {
  call_id: string
  expected_execution_epoch: number
  expected_progress_revision: number
  execution_control: WorkspaceTaskExecutionControl
  new_reporter_key?: string
  execution_ref?: WorkspaceTaskExecutionRef | null
}

export interface WorkspaceTaskEvent {
  sequence: number
  call_id: string
  fingerprint: string
  task_id: string
  actor_session_id: string | null
  actor_role: string
  type: string
  action: string
  target: string
  consumer_key: string
  payload: Record<string, unknown>
  created_at: string
  review_cycle: number | null
  report_id: string | null
}

export interface WorkspaceTaskMutationResult {
  task: WorkspaceTask
  event: WorkspaceTaskEvent
  replayed: boolean
}
export type EvaluationStrictness = 'lenient' | 'balanced' | 'strict'
export type HumanCheckpointPolicy = 'final_only' | 'after_rubric' | 'every_iteration'
export type AutonomousRunPhase =
  | 'intake'
  | 'rubric_research'
  | 'planning'
  | 'dispatching'
  | 'working'
  | 'evaluating'
  | 'revising'
  | 'waiting_for_human'
  | 'passed'
  | 'failed'
  | 'exhausted'
  | 'cancelled'
export type EvaluationDecision = 'pass' | 'revise' | 'needs_input' | 'fail' | 'escalate'
export type ManagedSessionStatus =
  | 'spawning'
  | 'working'
  | 'idle'
  | 'needs_input'
  | 'done'
  | 'stopped'
  | 'error'
export type WorkspaceSessionRole =
  | 'worker'
  | 'orchestrator'
  | 'reviewer'
  | 'dispatcher'
  | 'resident'
export type AgentReportState =
  | 'started'
  | 'working'
  | 'blocked'
  | 'needs_input'
  | 'ready_for_review'
  | 'completed'
  | 'review_started'
  | 'review_passed'
  | 'review_failed'
  | 'review_needs_input'
export type ReviewDecision = 'auto' | 'request' | 'skip'
export type ReviewProfile = 'general' | 'code' | 'ui' | 'artifact' | 'delivery' | 'boundary'
export type ReviewProfileResultStatus = 'passed' | 'failed' | 'partial' | 'not_checked'
export type LaunchEnv = Record<string, string>
