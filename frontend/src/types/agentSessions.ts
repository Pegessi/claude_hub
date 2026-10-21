import type {
  AgentReportState,
  AgentRuntimeStatus,
  AgentType,
  ExecutionTarget,
  LaunchEnv,
  ManagedSessionStatus,
  ReviewDecision,
  ReviewProfile,
  SessionKind,
  WorkspaceSessionRole,
} from './taskExecution'
import type {
  AcceptanceCheck,
  EvaluationReport,
  GoalPacket,
  ReviewProfileResult,
  WorkspaceAttachmentCreate,
} from './workspace'

export interface EnsureWorkspaceAgentRequest {
  agent_type: AgentType
  session_kind?: SessionKind
  title?: string | null
  role?: WorkspaceSessionRole
  reuse_existing?: boolean
  cwd?: string | null
  solo_mode?: boolean
  target?: ExecutionTarget | null
  remote_profile_id?: string | null
  remote_cwd?: string | null
  remote_reconnect?: boolean | null
  ephemeral?: boolean
  env?: LaunchEnv
}

export interface ManagedSession {
  id: string
  workspace_id: string
  task_id?: string | null
  tab_id: string
  role: WorkspaceSessionRole
  agent_type: AgentType
  session_kind: SessionKind
  status: ManagedSessionStatus
  runtime_status: AgentRuntimeStatus
  current_task_id?: string | null
  queued_count: number
  title: string
  branch?: string | null
  workspace_path: string
  tmux_session: string
  target: ExecutionTarget
  remote_profile_id?: string | null
  remote_cwd?: string | null
  remote_reconnect: boolean
  solo_mode: boolean
  ephemeral: boolean
  env?: LaunchEnv
  remote_forward_port?: number | null
  auto_continue_task_id?: string | null
  auto_continue_attempts?: number
  last_auto_continue_at?: string | null
  prompt_retry_task_id?: string | null
  prompt_retry_attempted_at?: string | null
  created_at: string
  updated_at: string
  last_activity_at?: string | null
}

// ── Structured Chat stream (Layer B observation plane) ───────────────────────

export interface StreamModeOption {
  id: string
  label: string
  description?: string | null
}

export interface StreamModelOption {
  id: string
  label: string
  description?: string | null
  provider_model_id?: string | null
  default_reasoning_effort?: string | null
  supported_reasoning_efforts: StreamReasoningEffortOption[]
}

export interface StreamReasoningEffortOption {
  id: string
  label?: string | null
  description?: string | null
  provider_model_id?: string | null
}

export interface StreamCapabilities {
  structured: boolean
  unavailable_reason?: string | null
  /** False when capability discovery is incomplete and must not be reused. */
  metadata_cacheable?: boolean
  adapter_id: string
  schema_version: number
  sources: string[]
  supports_approval_ui: boolean
  supports_tool_timeline: boolean
  supports_images: boolean
  supports_dynamic_modes: boolean
  supports_goals: boolean
  goal_execution_owner: 'provider_native' | 'hub_managed' | null
  goal_usage_quality: ChatGoalUsageQuality
  available_modes: StreamModeOption[]
  current_mode: string | null
  available_models: StreamModelOption[]
  current_model: string | null
  current_reasoning_effort: string | null
}

// ── Chat Goal control plane ────────────────────────────────────────────────

export type ChatGoalStatus =
  | 'active'
  | 'paused'
  | 'blocked'
  | 'complete'
  | 'cancelled'
  | 'failed'

export type ChatGoalUsageQuality = 'exact' | 'estimated' | 'unavailable'
export type ChatGoalDispatchState = 'idle' | 'pending' | 'dispatched' | 'uncertain'

export interface ChatGoalCheckpoint {
  turn_id: string
  verified_progress: Array<{ item: string; evidence: string }>
  decisions: string[]
  remaining: string[]
  blocker?: string | null
  next_step?: string | null
  created_at: string
}

export interface ChatGoal {
  id: string
  tab_id: string
  objective: string
  status: ChatGoalStatus
  token_usage: number | null
  usage_quality: ChatGoalUsageQuality
  turns_completed: number
  dispatch_state: ChatGoalDispatchState
  pending_step_id?: string | null
  current_turn_id?: string | null
  completed_turn_ids?: string[]
  checkpoint?: ChatGoalCheckpoint | null
  checkpoint_history?: ChatGoalCheckpoint[]
  checkpoint_warning?: string | null
  status_message?: string | null
  pending_image_count?: number
  pending_input?: string | null
  resume_after_turn?: boolean
  created_at: string
  updated_at: string
  paused_at?: string | null
  completed_at?: string | null
}

export interface ChatGoalCreate {
  attachments?: (WorkspaceAttachmentCreate & { preview_is_original?: boolean })[]
  objective: string
  client_request_id: string
}

export type AgentStreamEventType =
  | 'turn_started'
  | 'turn_completed'
  | 'text_delta'
  | 'thinking_delta'
  | 'tool_call_started'
  | 'tool_call_completed'
  | 'approval_required'
  | 'approval_resolved'
  | 'error'
  | 'status'

export interface AgentStreamEvent {
  stream_sequence: number
  session_id: string
  tab_id: string
  agent_type: AgentType
  type: AgentStreamEventType
  run_epoch?: number | null
  turn_id?: string | null
  message_id?: string | null
  call_id?: string | null
  payload: Record<string, unknown>
  created_at: string
  redacted: boolean
}

export interface AgentStreamHistoryDetail {
  turn_id: string
  first_sequence: number
  last_sequence: number
  event_count: number
}

export interface AgentStreamEventPage {
  events: AgentStreamEvent[]
  next_sequence: number
  has_more: boolean
  history_details?: AgentStreamHistoryDetail[]
  attributed_tool_ids?: string[]
}

export interface AgentReport {
  id: string
  workspace_id: string
  task_id?: string | null
  session_id: string
  state: AgentReportState
  message: string
  message_en?: string | null
  message_zh?: string | null
  changed_files: string[]
  validation?: string | null
  risks?: string | null
  acceptance_check?: AcceptanceCheck[]
  evaluation_report?: EvaluationReport | null
  review_profiles?: ReviewProfile[]
  profile_results?: ReviewProfileResult[]
  artifact_refs?: string[]
  confidence?: number | null
  requires_human_judgment?: boolean
  review_decision: ReviewDecision
  review_reason?: string | null
  risk_level?: string | null
  created_at: string
}

export interface WorkspaceArtifactPreview {
  path: string
  filename: string
  content: string
  size_bytes: number
  truncated: boolean
}

export type WorkspaceMarkdownDocumentSource = 'artifact' | 'changed_file' | 'snapshot' | 'discovered'

export interface WorkspaceMarkdownDocument {
  id: string
  path: string
  label: string
  source: WorkspaceMarkdownDocumentSource
  task_id?: string | null
  report_id?: string | null
  session_id?: string | null
  size_bytes?: number | null
  updated_at?: string | null
}

export interface AgentReportCreate {
  state: AgentReportState
  message: string
  message_en?: string | null
  message_zh?: string | null
  task_id?: string | null
  changed_files?: string[]
  validation?: string | null
  risks?: string | null
  acceptance_check?: AcceptanceCheck[]
  goal_packet?: GoalPacket | null
  evaluation_report?: EvaluationReport | null
  review_profiles?: ReviewProfile[]
  profile_results?: ReviewProfileResult[]
  artifact_refs?: string[]
  confidence?: number | null
  requires_human_judgment?: boolean
  review_decision?: ReviewDecision
  review_reason?: string | null
  risk_level?: string | null
}
