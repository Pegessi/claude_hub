import type {
  AgentReport,
  BoardTasksPagination,
  WorkspaceBoard,
  WorkspaceTask,
} from '@/types'

export const BOARD_TASKS_PAGE_SIZE = 15
export const MAX_BOARD_TASKS_LIMIT = 100

/** Drop board payloads that belong to a workspace that is no longer active. */
export function shouldAcceptBoardPayload(
  activeWorkspaceId: string | null | undefined,
  payloadWorkspaceId: string,
): boolean {
  return activeWorkspaceId === payloadWorkspaceId
}

/** True when a board fetch started under an older generation (reset/switch). */
export function isStaleBoardGeneration(
  storedGeneration: number | undefined,
  requestGeneration: number,
): boolean {
  return (storedGeneration ?? 0) !== requestGeneration
}

/** Bump stored generation when a reset supersedes in-flight work. */
export function nextBoardFetchGeneration(
  storedGeneration: number | undefined,
  reset: boolean,
): number {
  return (storedGeneration ?? 0) + (reset ? 1 : 0)
}

/** Reset fetch supersedes an in-flight poll; non-reset polls coalesce. */
export function shouldCoalesceBoardFetch(reset: boolean, hasInFlight: boolean): boolean {
  return hasInFlight && !reset
}

/** Apply a board payload only when workspace + generation guards pass. */
export function applyBoardPayloadState(
  activeWorkspaceId: string | null | undefined,
  targetWorkspaceId: string,
  currentBoard: WorkspaceBoard | null,
  payload: WorkspaceBoard,
  append: boolean,
  pollRefresh = false,
): WorkspaceBoard | null {
  if (!shouldAcceptBoardPayload(activeWorkspaceId, targetWorkspaceId)) {
    return currentBoard
  }
  if (currentBoard?.workspace.id === targetWorkspaceId) {
    const mergedTasks = append
      ? mergeBoardTasks(currentBoard.tasks, payload.tasks, true)
      : pollRefresh
        ? mergeBoardPollTasks(currentBoard.tasks, payload.tasks)
        : payload.tasks
    const tasksPagination = pollRefresh
      ? mergeBoardPollPagination(
          currentBoard.tasks_pagination,
          payload.tasks_pagination,
          loadedDoneTaskCount(mergedTasks),
        )
      : payload.tasks_pagination ?? currentBoard.tasks_pagination ?? null
    return {
      ...payload,
      tasks: mergedTasks,
      reports: append
        ? mergeBoardReports(currentBoard.reports, payload.reports)
        : pollRefresh
          ? mergeBoardPollReports(currentBoard.reports, payload.reports, mergedTasks)
          : payload.reports,
      tasks_pagination: tasksPagination,
    }
  }
  return payload
}

export function mergeBoardTasks(
  existing: WorkspaceTask[],
  incoming: WorkspaceTask[],
  append: boolean,
): WorkspaceTask[] {
  if (!append) {
    return incoming
  }
  const seen = new Set(existing.map(task => task.id))
  const appended = incoming.filter(task => !seen.has(task.id))
  return [...existing, ...appended]
}

/** Keep older load-more tail when poll window is capped below loaded count. */
export function mergeBoardPollTasks(
  existing: WorkspaceTask[],
  incoming: WorkspaceTask[],
): WorkspaceTask[] {
  const incomingIds = new Set(incoming.map(task => task.id))
  const preservedTail = existing.filter(task => !incomingIds.has(task.id))
  return [...incoming, ...preservedTail]
}

export function mergeBoardPollPagination(
  existingPagination: BoardTasksPagination | null | undefined,
  incomingPagination: BoardTasksPagination | null | undefined,
  mergedDoneCount: number,
): BoardTasksPagination | null {
  if (!incomingPagination) {
    return existingPagination ?? null
  }
  const totalCount = incomingPagination.total_count
  const hasMore = mergedDoneCount < totalCount
  const nextCursor =
    hasMore && existingPagination?.next_cursor
      ? existingPagination.next_cursor
      : incomingPagination.next_cursor
  return {
    ...incomingPagination,
    has_more: hasMore,
    next_cursor: hasMore ? nextCursor ?? null : null,
  }
}

export function mergeBoardPollReports(
  existing: AgentReport[],
  incoming: AgentReport[],
  mergedTasks: WorkspaceTask[],
): AgentReport[] {
  const mergedTaskIds = new Set(mergedTasks.map(task => task.id))
  const refreshedTaskIds = new Set(
    incoming
      .map(report => report.task_id)
      .filter((taskId): taskId is string => taskId != null),
  )
  const preserved = existing.filter(
    report =>
      report.task_id != null &&
      mergedTaskIds.has(report.task_id) &&
      !refreshedTaskIds.has(report.task_id),
  )
  return mergeBoardReports(preserved, incoming)
}

export function mergeBoardReports(
  existing: AgentReport[],
  incoming: AgentReport[],
): AgentReport[] {
  const byId = new Map<string, AgentReport>()
  for (const report of existing) {
    byId.set(report.id, report)
  }
  for (const report of incoming) {
    byId.set(report.id, report)
  }
  return [...byId.values()]
}

export function loadedDoneTaskCount(tasks: WorkspaceTask[]): number {
  return tasks.filter(task => task.status === 'done').length
}

export function boardTasksLimitForPoll(loadedDoneCount: number): number {
  const window = Math.max(loadedDoneCount, BOARD_TASKS_PAGE_SIZE)
  return Math.min(window, MAX_BOARD_TASKS_LIMIT)
}

export function boardOlderRemainingCount(
  tasks: WorkspaceTask[],
  pagination: BoardTasksPagination | null | undefined,
): number {
  if (!pagination) return 0
  return Math.max(0, pagination.total_count - loadedDoneTaskCount(tasks))
}

export type BoardLoadMoreAttemptState = {
  loading: boolean
  error: string | null
}

/** Shared load-more attempt lifecycle: loading/error flags + stale-safe apply. */
export async function runBoardLoadMoreAttempt<T>(
  state: BoardLoadMoreAttemptState,
  fetchPage: () => Promise<T | null>,
  onSuccess: (payload: T) => void,
): Promise<void> {
  state.loading = true
  state.error = null
  try {
    const payload = await fetchPage()
    if (payload == null) {
      return
    }
    onSuccess(payload)
    state.error = null
  } catch (error) {
    state.error =
      error instanceof Error ? error.message : 'Failed to load older workspace tasks'
    throw error
  } finally {
    state.loading = false
  }
}
