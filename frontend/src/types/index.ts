export type AgentType = 'claude' | 'codex' | 'cursor'
export type ExecutionTarget = 'local' | 'remote'
export type AgentRuntimeStatus = 'idle' | 'working' | 'attention' | 'offline'
export type AppMode = 'terminal' | 'workspace'
export type ColorScheme = 'dark' | 'light'
export type WorkspaceTaskStatus = 'todo' | 'queued' | 'working' | 'review' | 'done'
export type ManagedSessionStatus =
  | 'spawning'
  | 'working'
  | 'idle'
  | 'needs_input'
  | 'done'
  | 'stopped'
  | 'error'
export type WorkspaceSessionRole = 'worker' | 'orchestrator' | 'reviewer' | 'dispatcher'
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
  port: number
  created_at: string
  is_active: boolean
  workspace_id?: string | null
  workspace_name?: string | null
  workspace_role?: WorkspaceSessionRole | null
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

export interface WorkspaceTask {
  id: string
  workspace_id: string
  title: string
  prompt: string
  attachments: WorkspaceAttachment[]
  agent_type: AgentType
  status: WorkspaceTaskStatus
  session_id?: string | null
  related_task_id?: string | null
  clear_context?: boolean | null
  dispatch_reason?: string | null
  dispatch_pending: boolean
  review_session_id?: string | null
  review_attempts: number
  review_requested_at?: string | null
  review_completed_at?: string | null
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
  related_task_id?: string | null
  attachments?: WorkspaceAttachmentCreate[]
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
  remote_forward_port?: number | null
  created_at: string
  updated_at: string
  last_activity_at?: string | null
}

export interface AgentReport {
  id: string
  workspace_id: string
  task_id?: string | null
  session_id: string
  state: AgentReportState
  message: string
  changed_files: string[]
  validation?: string | null
  risks?: string | null
  created_at: string
}

export interface AgentReportCreate {
  state: AgentReportState
  message: string
  task_id?: string | null
  changed_files?: string[]
  validation?: string | null
  risks?: string | null
}

export interface WorkspaceBoard {
  workspace: Workspace
  tasks: WorkspaceTask[]
  sessions: ManagedSession[]
  reports: AgentReport[]
  snapshot_path?: string | null
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
