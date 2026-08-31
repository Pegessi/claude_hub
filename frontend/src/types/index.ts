export type AgentType = 'claude' | 'codex' | 'cursor' | 'terminal'
export type ExecutionTarget = 'local' | 'remote'
export type AgentRuntimeStatus = 'idle' | 'working' | 'attention' | 'offline'
export type AppMode = 'terminal' | 'workspace'
export type ColorScheme = 'dark' | 'light'
export type WorkspaceTaskStatus = 'todo' | 'queued' | 'working' | 'review' | 'done' | 'failed'
export type WorkspaceTaskMode = 'direct' | 'reviewed' | 'autonomous' | 'subagent'
export type WorkspaceTaskExecutionComplexity = 'auto' | 'simple' | 'complex'
export type WorkspaceTaskOrigin = 'human' | 'resident'
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

export interface TerminalTab {
  id: string
  name: string
  shell?: string
  cwd?: string
  solo_mode?: boolean
  agent_type?: AgentType
  target?: ExecutionTarget
  remote_profile_id?: string | null
  remote_cwd?: string | null
  remote_reconnect?: boolean
  env?: LaunchEnv
  port: number
  created_at: string
  is_active: boolean
  workspace_id?: string | null
  workspace_name?: string | null
  workspace_role?: WorkspaceSessionRole | null
  agent_session_id?: string | null
  cursor_transport?: string
  cursor_data_dir?: string | null
  cursor_cli_version?: string | null
  cursor_transcript_path?: string | null
  cursor_transcript_schema?: string | null
}

export interface TerminalTabCreate {
  name: string
  shell?: string
  cwd?: string
  solo_mode?: boolean
  agent_type?: AgentType
  target?: ExecutionTarget
  remote_profile_id?: string | null
  remote_cwd?: string | null
  remote_reconnect?: boolean
  env?: LaunchEnv
  agent_session_id?: string
}

export interface TerminalTabUpdate {
  name?: string
  shell?: string
  cwd?: string
  solo_mode?: boolean
  agent_type?: AgentType
  target?: ExecutionTarget
  remote_profile_id?: string | null
  remote_cwd?: string | null
  remote_reconnect?: boolean
  env?: LaunchEnv
}

export interface SwitchEnvRequest {
  env: LaunchEnv
  solo_mode?: boolean
}

export interface RemoteProfile {
  id: string
  name: string
  ssh_host: string
  user?: string | null
  port: number
  default_cwd?: string | null
}

export interface NetworkAddress {
  address: string
  label: string
}

export interface NetworkAccessInfo {
  hostname: string
  addresses: NetworkAddress[]
}

export interface TerminalAgentStatus {
  tab_id: string
  tab_name: string
  agent_type: AgentType
  status: AgentRuntimeStatus
  status_text: string
  detail?: string | null
  tmux_session: string
  last_changed_at?: string | null
  sampled_at: string
}

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
  task_mode: WorkspaceTaskMode
  execution_complexity: WorkspaceTaskExecutionComplexity
  origin?: WorkspaceTaskOrigin
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

export interface EnsureWorkspaceAgentRequest {
  agent_type: AgentType
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

// ── Structured agent-stream (Layer B observation plane) ─────────────────────

export interface StreamCapabilities {
  structured: boolean
  adapter_id: string
  schema_version: number
  sources: string[]
  supports_approval_ui: boolean
  supports_tool_timeline: boolean
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
  call_id?: string | null
  payload: Record<string, unknown>
  created_at: string
  redacted: boolean
}

export interface AgentStreamEventPage {
  events: AgentStreamEvent[]
  next_sequence: number
  has_more: boolean
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

export interface BoardTasksPagination {
  total_count: number
  has_more: boolean
  next_cursor?: string | null
  limit?: number | null
  status_counts?: Record<string, number>
}

export interface WorkspaceBoard {
  workspace: Workspace
  tasks: WorkspaceTask[]
  sessions: ManagedSession[]
  reports: AgentReport[]
  markdown_documents?: WorkspaceMarkdownDocument[]
  snapshot_path?: string | null
  tasks_pagination?: BoardTasksPagination | null
}

export type FeedbackLessonScope = 'workspace' | 'family' | 'global'
export type FeedbackLessonStatus = 'draft' | 'active' | 'archived' | 'rejected'

export interface FeedbackLesson {
  id: string
  workspace_id: string
  title?: string
  fingerprint?: string
  scope: FeedbackLessonScope
  status: FeedbackLessonStatus
  summary: string
  applies_when?: string[]
  do?: string
  avoid?: string
  tags?: string[]
  evidence_task_ids?: string[]
  source_draft_ids?: string[]
  source_record_ids?: string[]
  merged_from_ids?: string[]
  superseded_by_id?: string | null
  hit_count?: number
  success_count?: number
  confidence?: number | null
  last_seen_at?: string | null
  last_used_at?: string | null
  last_validated_at?: string | null
  created_at: string
  updated_at: string
}

export interface FeedbackLessonCreate {
  id?: string | null
  title?: string | null
  fingerprint?: string | null
  summary: string
  applies_when?: string[]
  do?: string
  avoid?: string
  tags?: string[]
  scope?: FeedbackLessonScope
  confidence?: number | null
}

export type FeedbackSummaryMode = 'incremental' | 'full'

export interface FeedbackSummaryRequest {
  mode?: FeedbackSummaryMode
  limit?: number
  force?: boolean
  clear_context?: boolean
}

export interface FeedbackSummaryRun {
  id: string
  workspace_id: string
  task_id?: string | null
  mode: FeedbackSummaryMode
  input_record_ids: string[]
  cache_hit: boolean
  prompt_version: number
  created_lesson_ids: string[]
  merged_lesson_ids: string[]
  skipped_reason?: string | null
  created_at: string
  completed_at?: string | null
}

export type LayoutType = '1x1' | '2x1' | '1x2' | '3x1' | '1x3' | '2x2' | '3x3'

export interface Pane {
  id: string
  tabId: string | null
  isActive: boolean
}

export interface LayoutConfig {
  type: LayoutType
  rows: number
  cols: number
}

export interface User {
  open_id: string
  name: string
  email: string
  avatar_url?: string
}

export interface AuthCheckResponse {
  authenticated: boolean
  auth_required: boolean
  user: User | null
}

// Namespaced globals hung off window.__claudeHub to avoid top-level pollution.
// See: F8 cleanup — consolidate stray window globals.
export interface TerminalKeyItem {
  key: string
  ctrl: boolean
  shift: boolean
}

export interface TerminalKeyState {
  iframes: Record<string, HTMLIFrameElement | null>
  ready: Record<string, boolean>
  queues: Record<string, TerminalKeyItem[]>
  inputRing: Record<string, unknown>
}

/** Returns false only when no terminal target is available to accept/queue the key. */
export type TerminalKeySender = (key: string, ctrl?: boolean, shift?: boolean) => boolean
export type TerminalHistoryRefresher = (tabId?: string) => void
export type TerminalIframeRegistrar = (el: HTMLIFrameElement | null, tabId: string) => void
export type TerminalSelectModeSetter = (enabled: boolean, tabId?: string) => void

export interface ClaudeHubNamespace {
  activePaneTabId?: string | null
  terminalState?: TerminalKeyState
  registerTerminalIframe?: TerminalIframeRegistrar
  refreshTerminalHistory?: TerminalHistoryRefresher
  sendTerminalKey?: TerminalKeySender
  setTerminalSelectMode?: TerminalSelectModeSetter
}

declare global {
  interface Window {
    __claudeHub: ClaudeHubNamespace
  }
}

// Cross-store notification (toast) shape — replaces single mutable `error: string`
// anti-pattern so concurrent failures stack instead of overwrite each other.
export type NotificationType = 'error' | 'success' | 'warning' | 'info'

export interface StoreNotification {
  id: string
  type: NotificationType
  message: string
  /** If set, the toast auto-dismisses after this many milliseconds. */
  autoDismissMs?: number
}
