import type {
  ManagedSession,
  Workspace,
  WorkspaceTask,
} from '@/types'

// Uncounted, always-true consequence shared by every workspace delete entry
// point: the backend force-tears down ALL sessions bound to the workspace,
// including for a non-active workspace whose board is not loaded.
const STOP_CLAUSE = 'Any sessions running in this workspace will be stopped.'

export interface WorkspaceDeletionContext {
  // Only the active workspace's board is loaded, so resource signals are
  // available for it; other workspaces get the generic wording.
  isActive: boolean
  // Qualitative, never an exact count: the board's task page is paginated and
  // its session set can change between render and confirmation, so a number
  // could understate what the backend force-teardown will stop.
  hasOpenTasks?: boolean
  hasLiveSessions?: boolean
}

// Single source of truth for the delete confirmation wording used by every
// workspace delete entry point. The stop clause is unconditional (it is true
// for any workspace); the open-tasks/live-sessions sentence is added only for
// the currently selected workspace, whose board is the one loaded client-side.
export function workspaceDeleteConfirmMessage(
  workspace: Pick<Workspace, 'id' | 'name'>,
  context: WorkspaceDeletionContext = { isActive: false },
): string {
  const name = workspace.name.trim() || 'this workspace'
  const base = `Delete workspace "${name}"? ${STOP_CLAUSE} This cannot be undone.`
  if (!context.isActive) return base
  if (!context.hasOpenTasks && !context.hasLiveSessions) return base

  const resources = [
    context.hasOpenTasks ? 'open tasks' : null,
    context.hasLiveSessions ? 'live sessions' : null,
  ].filter((value): value is string => value !== null)

  return `${base} This workspace still has ${resources.join(' and ')}.`
}

const CLOSED_TASK_STATUSES = new Set(['done', 'failed'])

// Whether any task is still actionable. Prefers the board's authoritative
// status_counts (covers pages beyond the loaded one); falls back to scanning
// the loaded tasks for boards that arrive without pagination metadata. The
// answer stays boolean: "at least one open task", never an exact count.
export function boardHasOpenTasks(
  tasks: Iterable<WorkspaceTask>,
  statusCounts?: Record<string, number> | null,
): boolean {
  if (statusCounts) {
    for (const [status, count] of Object.entries(statusCounts)) {
      if (!CLOSED_TASK_STATUSES.has(status) && count > 0) return true
    }
    return false
  }
  for (const task of tasks) {
    if (!CLOSED_TASK_STATUSES.has(task.status)) return true
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
