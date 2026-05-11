export type AgentType = 'claude' | 'codex' | 'cursor'
export type AgentRuntimeStatus = 'idle' | 'working' | 'attention' | 'offline'

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
