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

export interface ManagedChatRouteTab extends ChatSurfaceTab {
  workspace_id?: string | null
}

/**
 * Matches backend `_managed_chat_session_for_tab`: a direct Chat carrying
 * `workspace_id` purely for grouping is NOT a managed Chat.
 */
export function isManagedChatTab(
  tab?: Pick<ChatSurfaceTab, 'session_kind' | 'workspace_role' | 'managed_session_id'> | null,
): boolean {
  return Boolean(tab && tab.session_kind === 'chat' && (tab.managed_session_id || tab.workspace_role))
}

/** Resolve the unarchive endpoint, routing managed Chat tabs to Workspace session controls. */
export function resolveTabUnarchivePath(
  apiBase: string,
  tabId: string,
  tab?: ManagedChatRouteTab | null,
): string {
  if (isManagedChatTab(tab)) {
    const workspaceId = tab?.workspace_id?.trim()
    const sessionId = tab?.managed_session_id?.trim()
    if (!workspaceId || !sessionId) {
      throw new Error(
        'Failed to unarchive tab: Managed Chat backing tab is missing its Workspace session identity',
      )
    }
    return `${apiBase}/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(sessionId)}/unarchive`
  }
  return `${apiBase}/tabs/${encodeURIComponent(tabId)}/unarchive`
}

/** Resolve the permanent-delete endpoint, routing managed Chat tabs to Workspace session controls. */
export function resolveTabDeletePath(
  apiBase: string,
  tabId: string,
  tab?: ManagedChatRouteTab | null,
): string {
  if (isManagedChatTab(tab)) {
    const sessionId = tab?.managed_session_id?.trim()
    if (!sessionId) {
      throw new Error(
        'Failed to delete tab: Managed Chat backing tab is missing its managed session identity',
      )
    }
    return `${apiBase}/workspaces/sessions/${encodeURIComponent(sessionId)}`
  }
  return `${apiBase}/tabs/${encodeURIComponent(tabId)}`
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
    return target === 'local' && ['claude', 'codex', 'cursor', 'maso', 'antigravity'].includes(agentType)
  }
  return agentType !== 'maso' && agentType !== 'antigravity'
}
