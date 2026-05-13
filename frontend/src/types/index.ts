export type AgentType = 'claude' | 'codex' | 'cursor'
export type AgentRuntimeStatus = 'idle' | 'working' | 'attention' | 'offline'
export type AppMode = 'terminal' | 'workspace'
export type WorkspaceTaskStatus = 'todo' | 'assigned' | 'working' | 'review' | 'done'
export type ManagedSessionStatus =
  | 'spawning'
  | 'working'
  | 'idle'
  | 'needs_input'
  | 'done'
  | 'stopped'
  | 'error'
export type WorkspaceSessionRole = 'worker' | 'orchestrator'
export type AgentReportState =
  | 'started'
  | 'working'
  | 'blocked'
  | 'needs_input'
  | 'ready_for_review'
  | 'completed'

export interface TerminalTab {
  id: string
  name: string
  shell?: string
  cwd?: string
  solo_mode?: boolean
  agent_type?: AgentType
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
}

export interface TerminalTabUpdate {
  name?: string
  shell?: string
  cwd?: string
  solo_mode?: boolean
  agent_type?: AgentType
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
  agent_session_id?: string | null
  created_at: string
  updated_at: string
}

export interface WorkspaceCreate {
  name: string
  path: string
  default_branch?: string
  session_prefix?: string
}

export interface WorkspaceTask {
  id: string
  workspace_id: string
  title: string
  prompt: string
  agent_type: AgentType
  status: WorkspaceTaskStatus
  session_id?: string | null
  created_at: string
  updated_at: string
}

export interface WorkspaceTaskCreate {
  title: string
  prompt: string
  agent_type: AgentType
}

export interface ManagedSession {
  id: string
  workspace_id: string
  task_id?: string | null
  tab_id: string
  role: WorkspaceSessionRole
  agent_type: AgentType
  status: ManagedSessionStatus
  title: string
  branch?: string | null
  workspace_path: string
  tmux_session: string
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
