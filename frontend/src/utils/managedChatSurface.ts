import type { AgentType, ExecutionTarget, SessionKind } from '@/types'
import type { StreamSource } from '@/composables/useAgentStream'

export interface ChatStreamIdentity {
  source: StreamSource
  sourceId: string
  basePath: string
}

export interface ChatSurfaceTab {
  session_kind: SessionKind
  workspace_role?: string | null
  managed_session_id?: string | null
}

/** Resolve the one authoritative transport identity for a visible Chat pane. */
export function chatStreamIdentity(tabId: string, managedSessionId?: string): ChatStreamIdentity {
  const source: StreamSource = managedSessionId ? 'managed-session' : 'terminal-tab'
  const sourceId = managedSessionId ?? tabId
  const collection = managedSessionId ? 'sessions' : 'tabs'
  return {
    source,
    sourceId,
    basePath: `/api/workspaces/${collection}/${encodeURIComponent(sourceId)}/stream`,
  }
}

/** Managed Chat fails closed when its managed-session identity is absent. */
export function tabOwnsStructuredChat(tab: ChatSurfaceTab): boolean {
  if (tab.session_kind !== 'chat') return false
  return !tab.workspace_role || Boolean(tab.managed_session_id)
}

/** Provider support shared by Workspace worker and reviewer launch controls. */
export function providerSupportsManagedSurface(
  agentType: AgentType,
  sessionKind: SessionKind,
  target: ExecutionTarget,
): boolean {
  if (sessionKind === 'chat') {
    return target === 'local' && ['claude', 'codex', 'cursor', 'maso'].includes(agentType)
  }
  return agentType !== 'maso'
}
