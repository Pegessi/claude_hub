import type {
  AgentType,
  AutonomousRunPhase,
  EvaluationDecision,
  EvaluationStrictness,
  ExecutionTarget,
  HumanCheckpointPolicy,
  ReviewProfile,
  ReviewProfileResultStatus,
  SessionKind,
  WorkspaceTaskExecutionComplexity,
  WorkspaceTaskExecutionControl,
  WorkspaceTaskExecutionRef,
  WorkspaceTaskMode,
  WorkspaceTaskOrigin,
  WorkspaceTaskProgress,
  WorkspaceTaskRuntimeObservation,
  WorkspaceTaskSource,
  WorkspaceTaskStatus,
} from './taskExecution'

export interface ResidentPeriodicTask {
  id: string
  text: string
  enabled: boolean
}

export interface Workspace {
  id: string
  name: string
  path: string
  default_branch: string
  session_prefix: string
  dispatcher_session_id?: string | null
  target: ExecutionTarget
  remote_profile_id?: string | null
  remote_cwd?: string | null
  remote_reconnect: boolean
  resident_agent_enabled: boolean
  resident_agent_paused: boolean
  resident_agent_interval_minutes: number
  resident_agent_session_id?: string | null
  resident_agent_directive?: string | null
  resident_agent_periodic_tasks: ResidentPeriodicTask[]
  resident_agent_last_run_at?: string | null
  resident_agent_run_requested_at?: string | null
  resident_agent_next_run_at?: string | null
  resident_agent_type: AgentType
  resident_agent_env: Record<string, string>
  resident_agent_solo_mode: boolean
  resident_agent_master_mode: boolean
  resident_agent_title?: string | null
  resident_agent_target: ExecutionTarget
  resident_agent_remote_profile_id?: string | null
  resident_agent_cwd?: string | null
  resident_agent_remote_reconnect: boolean
  created_at: string
  updated_at: string
}

export interface WorkspaceCreate {
  name: string
  path: string
  default_branch?: string
  session_prefix?: string
  target?: ExecutionTarget
  remote_profile_id?: string | null
  remote_cwd?: string | null
  remote_reconnect?: boolean
  resident_agent_enabled?: boolean
  resident_agent_paused?: boolean
  resident_agent_interval_minutes?: number
  resident_agent_directive?: string
  resident_agent_periodic_tasks?: ResidentPeriodicTask[]
  resident_agent_type?: AgentType
  resident_agent_env?: Record<string, string>
  resident_agent_solo_mode?: boolean
  resident_agent_master_mode?: boolean
  resident_agent_title?: string
  resident_agent_target?: ExecutionTarget
  resident_agent_remote_profile_id?: string | null
  resident_agent_cwd?: string
  resident_agent_remote_reconnect?: boolean
}

export interface WorkspaceUpdate {
  name?: string
  path?: string
  default_branch?: string
  remote_cwd?: string | null
  remote_reconnect?: boolean
  resident_agent_enabled?: boolean
  resident_agent_paused?: boolean
  resident_agent_interval_minutes?: number
  resident_agent_directive?: string
  resident_agent_periodic_tasks?: ResidentPeriodicTask[]
  resident_agent_type?: AgentType
  resident_agent_env?: Record<string, string>
  resident_agent_solo_mode?: boolean
  resident_agent_master_mode?: boolean
  resident_agent_title?: string | null
  resident_agent_target?: ExecutionTarget
  resident_agent_remote_profile_id?: string | null
  resident_agent_cwd?: string | null
  resident_agent_remote_reconnect?: boolean
}

export interface WorkspaceAttachment {
  id: string
  filename: string
  mime_type: string
  path: string
  size_bytes: number
}

export interface WorkspaceAttachmentCreate {
  filename: string
  mime_type: string
  data_url: string
  /** Stored display image: the original when it is within 8 MiB and 8192 px,
   *  otherwise a browser re-encode that fits those bounds.
   *  Only the preview is persisted to durable attachment history; the
   *  original ``data_url`` bytes are forwarded to the provider. Codex may
   *  stage originals transiently in the isolated runtime temp directory.
   *  Optional for workspace task attachments and compatibility clients. */
  preview_data_url?: string
}

export type GoalPacketStatus =
  | 'draft'
  | 'pending_review'
  | 'approved'
  | 'rejected'
  | 'frozen'
  | 'superseded'

export type AcceptanceCheckStatus = 'passed' | 'failed' | 'partial' | 'not_checked'

export interface GoalPacket {
  objective: string
  acceptance_criteria?: string[]
  validation_plan?: string[]
  assumptions?: string[]
  out_of_scope?: string[]
  handoff_requirements?: string[]
  source?: string
  status?: GoalPacketStatus
  created_at?: string | null
  updated_at?: string | null
}

export interface AcceptanceCheck {
  criterion: string
  status: AcceptanceCheckStatus
  evidence: string
}

export interface ReviewProfileResult {
  profile: ReviewProfile
  status: ReviewProfileResultStatus
  evidence?: string
  blocking_findings?: string[]
  non_blocking_findings?: string[]
}

export interface AutonomyPolicy {
  max_iterations: number
  evaluation_strictness: EvaluationStrictness
  allow_web_research: boolean
  require_artifact_review: boolean
  review_profiles?: ReviewProfile[]
  human_checkpoint_policy: HumanCheckpointPolicy
  allowed_agent_types?: AgentType[]
  stop_on_repeated_failure: boolean
}

export interface RubricCriterion {
  id: string
  name: string
  description?: string
  weight?: number
  pass_condition?: string
  evaluation_method?: string
  blocking_threshold?: number | null
}

export interface CriterionResult {
  criterion_id?: string | null
  criterion: string
  score?: number | null
  passed?: boolean | null
  evidence?: string
}

export interface EvaluationReport {
  id: string
  run_id?: string | null
  task_id?: string | null
  iteration: number
  evaluator_session_id?: string | null
  overall_score?: number | null
  decision: EvaluationDecision
  criterion_results?: CriterionResult[]
  profile_results?: ReviewProfileResult[]
  blocking_issues?: string[]
  suggested_fixes?: string[]
  artifact_refs?: string[]
  validation_reviewed?: string | null
  risks?: string | null
  confidence?: number | null
  requires_human_judgment?: boolean
  created_at?: string | null
}

export interface AutonomousIteration {
  iteration: number
  worker_session_id?: string | null
  evaluator_session_id?: string | null
  worker_report_id?: string | null
  evaluation_report_id?: string | null
  revision_prompt?: string | null
  controller_decision?: string | null
  started_at?: string | null
  completed_at?: string | null
}

export interface AutonomousRun {
  id: string
  task_id?: string | null
  phase: AutonomousRunPhase
  iteration: number
  max_iterations: number
  status_summary: string
  active_session_ids?: string[]
  pass_threshold: number
  current_score?: number | null
  next_action: string
  paused_at?: string | null
  exhausted_at?: string | null
  completed_at?: string | null
  rubric?: RubricCriterion[]
  evaluation_reports?: EvaluationReport[]
  iterations?: AutonomousIteration[]
}

export interface WorkspaceTask {
  id: string
  workspace_id: string
  title: string
  prompt: string
  attachments: WorkspaceAttachment[]
  goal_packet?: GoalPacket | null
  review_profiles?: ReviewProfile[]
  agent_type: AgentType
  session_kind: SessionKind
  task_mode: WorkspaceTaskMode
  execution_complexity: WorkspaceTaskExecutionComplexity
  origin?: WorkspaceTaskOrigin
  source?: WorkspaceTaskSource | null
  execution_control?: WorkspaceTaskExecutionControl
  execution_ref?: WorkspaceTaskExecutionRef | null
  execution_epoch?: number
  progress_revision?: number
  execution_released?: boolean
  latest_progress?: WorkspaceTaskProgress | null
  runtime_observation?: WorkspaceTaskRuntimeObservation | null
  agent_tag?: string | null
  autonomy_policy?: AutonomyPolicy | null
  autonomous_run?: AutonomousRun | null
  status: WorkspaceTaskStatus
  session_id?: string | null
  related_task_id?: string | null
  parent_task_id?: string | null
  root_task_id?: string | null
  path?: string
  consumer_ack_sequence?: number
  clear_context?: boolean | null
  dispatch_reason?: string | null
  dispatch_pending: boolean
  system_internal?: boolean
  internal_kind?: string | null
  feedback_lesson_ids?: string[]
  review_session_id?: string | null
  review_attempts: number
  review_requested_at?: string | null
  review_completed_at?: string | null
  review_skipped_at?: string | null
  review_skip_reason?: string | null
  manual_aborted_at?: string | null
  manual_abort_reason?: string | null
  timeout_seconds?: number | null
  failure_reason?: string | null
  failed_at?: string | null
  human_acceptance_requested_at?: string | null
  human_accepted_at?: string | null
  queued_at?: string | null
  started_at?: string | null
  reviewed_at?: string | null
  completed_at?: string | null
  created_at: string
  updated_at: string
}

export interface WorkspaceTaskCreate {
  execution_control?: WorkspaceTaskExecutionControl
  session_kind?: SessionKind
  reporter_key?: string
  request_key?: string | null
  source?: WorkspaceTaskSource | null
  execution_ref?: WorkspaceTaskExecutionRef | null
  title: string
  prompt: string
  agent_type?: AgentType
  task_mode?: WorkspaceTaskMode
  execution_complexity?: WorkspaceTaskExecutionComplexity
  related_task_id?: string | null
  parent_task_id?: string | null
  attachments?: WorkspaceAttachmentCreate[]
  goal_packet?: GoalPacket | null
  review_profiles?: ReviewProfile[]
  autonomy_policy?: AutonomyPolicy | null
  session_id?: string | null
  clear_context?: boolean | null
  agent_tag?: string | null
}

export interface WorkspaceTaskUpdate {
  title?: string
  session_kind?: SessionKind
  prompt?: string
  status?: WorkspaceTaskStatus
  add_attachments?: WorkspaceAttachmentCreate[]
  removed_attachment_ids?: string[]
  related_task_id?: string | null
  parent_task_id?: string | null
  clear_context?: boolean | null
  session_id?: string | null
  agent_tag?: string | null
}

export interface StartTaskRequest {
  agent_type?: AgentType
  target_session_id?: string | null
  clear_context?: boolean | null
  related_task_id?: string | null
}

export interface ContinueTaskRequest {
  message?: string | null
  attachments?: WorkspaceAttachmentCreate[]
}

export interface RequestTaskReviewRequest {
  message?: string | null
}

export interface ManualTaskControlRequest {
  reason: string
}
