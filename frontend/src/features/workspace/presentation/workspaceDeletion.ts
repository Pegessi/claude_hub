import type {
  ManagedSession,
  Workspace,
  WorkspaceTask,
} from '@/types'

export interface WorkspaceDeletionContext {
  // Only the active workspace's board is loaded, so resource signals are
  // available for it; other workspaces get the generic wording.
  isActive: boolean
  // Qualitative, never an exact count: the board's task list is paginated and
  // its session set can change between render and confirmation, so a number
  // could understate what the backend force-teardown will stop.
  hasOpenTasks?: boolean
  hasLiveSessions?: boolean
}

// Single source of truth for the delete confirmation wording used by every
// workspace delete entry point. Mirrors the workspace editor dialog
// ("Delete workspace \"<name>\"? This cannot be undone.") and, for the
// currently selected workspace, warns qualitatively about the open tasks and
// live sessions the existing backend deletion flow will stop and remove.
export function workspaceDeleteConfirmMessage(
  workspace: Pick<Workspace, 'id' | 'name'>,
  context: WorkspaceDeletionContext = { isActive: false },
): string {
  const name = workspace.name.trim() || 'this workspace'
  const base = `Delete workspace "${name}"? This cannot be undone.`
  if (!context.isActive) return base
  if (!context.hasOpenTasks && !context.hasLiveSessions) return base

  const resources = [
    context.hasOpenTasks ? 'open tasks' : null,
    context.hasLiveSessions ? 'live sessions' : null,
  ].filter((value): value is string => value !== null)

  return `${base} This workspace still has ${resources.join(' and ')}; its sessions will be stopped and everything will be permanently deleted.`
}

// Whether any board task is still actionable. Mirrors the board column
// semantics (failed rides with Working) without claiming an exact count: the
// board payload is paginated, so the result is "at least one".
export function boardHasOpenTasks(tasks: Iterable<WorkspaceTask>): boolean {
  for (const task of tasks) {
    if (task.status !== 'done' && task.status !== 'failed') return true
  }
  return false
}

// Whether any managed session bound to the workspace is currently live.
export function boardHasLiveSessions(sessions: Iterable<ManagedSession>): boolean {
  for (const session of sessions) {
    if (session.runtime_status !== 'offline') return true
  }
  return false
}
