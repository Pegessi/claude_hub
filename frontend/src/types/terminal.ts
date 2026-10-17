import type {
  AgentRuntimeStatus,
  AgentType,
  ExecutionTarget,
  LaunchEnv,
  SessionKind,
  WorkspaceSessionRole,
} from './taskExecution'

export interface TerminalTab {
  id: string
  name: string
  shell?: string
  cwd?: string
  solo_mode?: boolean
  agent_type?: AgentType
  session_kind: SessionKind
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
  managed_session_id?: string | null
  agent_session_id?: string | null
  cursor_transport?: string
  cursor_data_dir?: string | null
  cursor_cli_version?: string | null
  cursor_transcript_path?: string | null
  cursor_transcript_schema?: string | null
  archived?: boolean
  archived_at?: string | null
  last_viewed_at?: string | null
  is_unread?: boolean
}

export interface TerminalTabCreate {
  name: string
  shell?: string
  cwd?: string
  solo_mode?: boolean
  agent_type?: AgentType
  session_kind?: SessionKind
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

export type RemoteTransport = 'normal' | 'pty_gateway'

export interface RemoteProfile {
  id: string
  name: string
  ssh_host: string
  user?: string | null
  port: number
  default_cwd?: string | null
  /** @deprecated use transport + remoteInteractive instead */
  stdin_shell?: boolean
  transport?: RemoteTransport
  /** false/null-absent semantics: only explicit false means browse-only */
  interactive?: boolean | null
}

export interface NetworkAddress {
  address: string
  label: string
}

export interface NetworkAccessInfo {
  hostname: string
  addresses: NetworkAddress[]
}

/**
 * One quota window reported by a provider (`GET /api/quota`).
 *
 * `state` is an open string: known values are `within`, `near`, `over`,
 * `no_limit`, `no_data` and `unknown`. Percentages are null whenever the
 * provider did not report a number; null is not zero.
 */
export interface QuotaLimit {
  /** Codex uses a window name such as `weekly`; MASO uses the model name. */
  scope: string
  label: string
  state: string
  used_percent: number | null
  remaining_percent: number | null
  resets_at: string | null
}

/**
 * One provider's quota reading. `available: false` means the Hub could not read
 * the provider at all and `message` carries the reason; the request still
 * succeeds so one broken provider never hides the other.
 */
export interface ProviderQuota {
  provider: string
  available: boolean
  /** Same values as `QuotaLimit.state`, plus `unavailable`. */
  state: string
  message: string
  /** When this reading was taken, not when it was requested. */
  as_of: string | null
  stale: boolean
  limits: QuotaLimit[]
  /** Codex subscription plan, e.g. `pro`. */
  plan: string | null
  /** Codex prepaid balance as reported; the unit is not part of the contract. */
  credit_balance: string | null
  /** MASO: whether reaching a limit actually refuses requests. */
  enforcing: boolean | null
}

export interface QuotaResponse {
  providers: ProviderQuota[]
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
