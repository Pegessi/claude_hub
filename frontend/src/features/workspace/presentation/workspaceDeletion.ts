import type {
  ManagedSession,
  Workspace,
  WorkspaceTask,
} from '@/types'

export interface WorkspaceDeletionContext {
  // Only the active workspace's board is loaded, so live task/session counts
  // are factual for it; other workspaces get the generic wording.
  isActive: boolean
  openTasks?: Iterable<WorkspaceTask>
  sessions?: Iterable<ManagedSession>
}

// Single source of truth for the delete confirmation wording used by every
// workspace delete entry point. Mirrors the workspace editor dialog
// ("Delete workspace \"<name>\"? This cannot be undone.") and, for the
// currently selected workspace, adds the concrete resources the backend
// force-teardown will stop and remove.
export function workspaceDeleteConfirmMessage(
  workspace: Pick<Workspace, 'id' | 'name'>,
  context: WorkspaceDeletionContext = { isActive: false },
): string {
  const name = workspace.name.trim() || 'this workspace'
  const base = `Delete workspace "${name}"? This cannot be undone.`
  if (!context.isActive) return base

  let openTaskCount = 0
  for (const task of context.openTasks ?? []) {
    if (task.status !== 'done' && task.status !== 'failed') openTaskCount += 1
  }
  let liveSessionCount = 0
  for (const session of context.sessions ?? []) {
    if (session.runtime_status !== 'offline') liveSessionCount += 1
  }

  const fragments: string[] = []
  if (openTaskCount > 0) {
    fragments.push(`${openTaskCount} open ${openTaskCount === 1 ? 'task' : 'tasks'}`)
  }
  if (liveSessionCount > 0) {
    fragments.push(`${liveSessionCount} live ${liveSessionCount === 1 ? 'session' : 'sessions'}`)
  }
  if (fragments.length === 0) return base
  return `${base} This workspace still has ${fragments.join(' and ')}; its sessions will be stopped and everything will be permanently deleted.`
}
