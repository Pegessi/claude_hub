import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type {
  AgentReport,
  AgentReportCreate,
  AgentType,
  ContinueTaskRequest,
  EnsureWorkspaceAgentRequest,
  FeedbackLesson,
  FeedbackLessonCreate,
  FeedbackSummaryRequest,
  FeedbackSummaryRun,
  ManualTaskControlRequest,
  ManagedSession,
  RequestTaskReviewRequest,
  StartTaskRequest,
  Workspace,
  WorkspaceArtifactPreview,
  WorkspaceAttachmentCreate,
  WorkspaceBoard,
  WorkspaceCreate,
  WorkspaceTask,
  WorkspaceTaskCreate,
  WorkspaceTaskStatus,
  WorkspaceTaskUpdate,
  WorkspaceUpdate,
  StoreNotification,
  NotificationType,
} from '@/types'

import {
  applyBoardPayloadState,
  BOARD_TASKS_PAGE_SIZE,
  boardOlderRemainingCount,
  boardTasksLimitForPoll,
  isStaleBoardGeneration,
  nextBoardFetchGeneration,
  runBoardLoadMoreAttempt,
  shouldCoalesceBoardFetch,
} from '@/utils/boardPagination'

const API_BASE = '/api'
const STORAGE_KEY_ACTIVE_WORKSPACE = 'claude_hub_active_workspace_id'

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json()
    return data.detail || response.statusText
  } catch {
    return response.statusText
  }
}

export const useWorkspaceStore = defineStore('workspace', () => {
  const workspaces = ref<Workspace[]>([])
  const activeWorkspaceId = ref<string | null>(localStorage.getItem(STORAGE_KEY_ACTIVE_WORKSPACE))
  const board = ref<WorkspaceBoard | null>(null)
  const feedbackLessons = ref<FeedbackLesson[]>([])
  const isLoading = ref(false)
  // ---- Notification / toast stack (F5: replaces single error string) ----
  const notifications = ref<StoreNotification[]>([])
  let _wsNotifIdSeq = 0

  function pushNotification(partial: Omit<StoreNotification, 'id'>) {
    const id = `ws-${Date.now().toString(36)}-${(_wsNotifIdSeq++).toString(36)}`
    const n: StoreNotification = { id, ...partial }
    notifications.value.push(n)
    if (n.autoDismissMs && n.autoDismissMs > 0) {
      window.setTimeout(() => dismissNotification(id), n.autoDismissMs)
    }
  }

  function dismissNotification(id: string) {
    const i = notifications.value.findIndex(n => n.id === id)
    if (i >= 0) notifications.value.splice(i, 1)
  }

  function notifyError(message: string) {
    pushNotification({ type: 'error', message, autoDismissMs: 10000 })
  }

  // Backward compat: the most recent error-type message (for single banner UI)
  const error = computed<string | null>(() =>
    notifications.value.find(n => n.type === ('error' as NotificationType))?.message ?? null
  )
  const boardFetches = new Map<string, Promise<void>>()
  const boardFetchControllers = new Map<string, AbortController>()
  const boardFetchGeneration = new Map<string, number>()
  const boardLoadMoreError = ref<string | null>(null)
  const boardLoadMoreLoading = ref(false)
  // Last board ETag per workspace + query signature. Sent back as If-None-Match so
  // an unchanged paginated board resolves to a bodyless 304.
  const boardETags = new Map<string, string>()
  // Full per-task report history, fetched on demand when a task detail panel is
  // opened. The board response only carries the latest report per task, so the
  // detail panel hydrates from here instead of the (trimmed) board payload.
  const taskReports = ref<Record<string, AgentReport[]>>({})
  const taskReportFetches = new Map<string, Promise<void>>()

  const activeWorkspace = computed(() =>
    workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
  )
  const tasks = computed(() => board.value?.tasks || [])
  const boardTasksPagination = computed(() => board.value?.tasks_pagination ?? null)
  const boardOlderTasksRemaining = computed(() =>
    boardOlderRemainingCount(tasks.value, boardTasksPagination.value),
  )
  const sessions = computed(() => board.value?.sessions || [])
  const reports = computed(() => board.value?.reports || [])
  const activeFeedbackLessons = computed(() =>
    feedbackLessons.value.filter(lesson => lesson.status === 'active')
  )
  const workspaceAgents = computed(() =>
    sessions.value.filter(session => session.role === 'orchestrator' || session.role === 'worker')
  )
  const reviewerAgents = computed(() =>
    sessions.value.filter(session => session.role === 'reviewer' && !session.ephemeral)
  )
  const temporaryReviewers = computed(() =>
    sessions.value.filter(session => session.role === 'reviewer' && session.ephemeral)
  )
  const dispatcherAgent = computed(() =>
    sessions.value.find(session => session.role === 'dispatcher') || null
  )
  const residentAgent = computed(() =>
    sessions.value.find(session => session.role === 'resident') || null
  )
  const workspaceAgent = computed(() =>
    workspaceAgents.value[0] || null
  )

  function sessionForTask(task: WorkspaceTask): ManagedSession | null {
    if (!task.session_id) return null
    return sessions.value.find(session => session.id === task.session_id) || null
  }

  function reportsForTask(task: WorkspaceTask): AgentReport[] {
    return reports.value.filter(report => report.task_id === task.id)
  }

  function latestReportForTask(task: WorkspaceTask): AgentReport | null {
    const taskReports = reportsForTask(task)
    return taskReports.length > 0 ? taskReports[taskReports.length - 1] : null
  }

  // ---- On-demand full report history (detail panel) ----
  function reportsForTaskId(taskId: string): AgentReport[] {
    return taskReports.value[taskId] || []
  }

  function clearTaskReports(taskId?: string) {
    if (!taskId) {
      taskReports.value = {}
      return
    }
    if (taskId in taskReports.value) {
      const next = { ...taskReports.value }
      delete next[taskId]
      taskReports.value = next
    }
  }

  async function fetchWorkspaces() {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces`)
      if (!response.ok) throw new Error(await readError(response))
      workspaces.value = await response.json()
      if (
        activeWorkspaceId.value &&
        !workspaces.value.some(workspace => workspace.id === activeWorkspaceId.value)
      ) {
        activeWorkspaceId.value = null
        localStorage.removeItem(STORAGE_KEY_ACTIVE_WORKSPACE)
      }
      if (!activeWorkspaceId.value && workspaces.value.length > 0) {
        setActiveWorkspace(workspaces.value[0].id)
      }
      if (activeWorkspaceId.value) {
        await fetchBoard(activeWorkspaceId.value, { reset: true })
      }
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to fetch workspaces')
    } finally {
      isLoading.value = false
    }
  }

  function setActiveWorkspace(workspaceId: string) {
    activeWorkspaceId.value = workspaceId
    localStorage.setItem(STORAGE_KEY_ACTIVE_WORKSPACE, workspaceId)
  }

  function boardEtagKey(workspaceId: string, query: string): string {
    return `${workspaceId}:${query}`
  }

  function beginBoardFetch(workspaceId: string, reset: boolean): {
    generation: number
    signal: AbortSignal
  } {
    if (reset) {
      boardFetchControllers.get(workspaceId)?.abort()
    }
    const generation = boardFetchGeneration.get(workspaceId) ?? 0
    const controller = new AbortController()
    boardFetchControllers.set(workspaceId, controller)
    return { generation, signal: controller.signal }
  }

  function isStaleBoardFetch(workspaceId: string, generation: number): boolean {
    return isStaleBoardGeneration(boardFetchGeneration.get(workspaceId), generation)
  }

  function buildBoardQuery(limit: number, cursor?: string | null): string {
    const params = new URLSearchParams({ tasks_limit: String(limit) })
    if (cursor) {
      params.set('tasks_cursor', cursor)
    }
    return params.toString()
  }

  function applyBoardPayload(
    workspaceId: string,
    payload: WorkspaceBoard,
    append: boolean,
    pollRefresh = false,
  ) {
    const next = applyBoardPayloadState(
      activeWorkspaceId.value,
      workspaceId,
      board.value,
      payload,
      append,
      pollRefresh,
    )
    if (next !== null) {
      board.value = next
    }
  }

  async function fetchBoard(
    workspaceId = activeWorkspaceId.value,
    options?: { reset?: boolean },
  ) {
    if (!workspaceId) return
    const reset = options?.reset ?? false
    const existing = boardFetches.get(workspaceId)
    if (shouldCoalesceBoardFetch(reset, existing !== undefined)) {
      return existing
    }

    if (reset) {
      boardFetchControllers.get(workspaceId)?.abort()
      boardFetchGeneration.set(
        workspaceId,
        nextBoardFetchGeneration(boardFetchGeneration.get(workspaceId), true),
      )
    }

    const request = (async () => {
      const loadedCount = board.value?.workspace.id === workspaceId ? board.value.tasks.length : 0
      const limit = reset
        ? BOARD_TASKS_PAGE_SIZE
        : boardTasksLimitForPoll(loadedCount || BOARD_TASKS_PAGE_SIZE)
      const query = buildBoardQuery(limit)
      const { generation, signal } = beginBoardFetch(workspaceId, reset)

      const headers: Record<string, string> = {}
      const etagKey = boardEtagKey(workspaceId, query)
      const knownETag = boardETags.get(etagKey)
      if (knownETag && board.value?.workspace.id === workspaceId && !reset) {
        headers['If-None-Match'] = knownETag
      }

      const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/board?${query}`, {
        headers,
        signal,
      })
      if (isStaleBoardFetch(workspaceId, generation)) {
        return
      }
      if (response.status === 304) {
        await fetchFeedbackLessons(workspaceId)
        return
      }
      if (!response.ok) throw new Error(await readError(response))
      const etag = response.headers.get('ETag')
      if (etag) boardETags.set(etagKey, etag)
      const payload = (await response.json()) as WorkspaceBoard
      if (isStaleBoardFetch(workspaceId, generation)) {
        return
      }
      applyBoardPayload(workspaceId, payload, false, !reset)
      boardLoadMoreError.value = null
      await fetchFeedbackLessons(workspaceId)
    })()

    boardFetches.set(workspaceId, request)
    try {
      await request
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') {
        return
      }
      notifyError(e instanceof Error ? e.message : 'Failed to fetch workspace board')
      throw e
    } finally {
      if (boardFetches.get(workspaceId) === request) {
        boardFetches.delete(workspaceId)
      }
    }
  }

  async function loadMoreBoardTasks(workspaceId = activeWorkspaceId.value) {
    if (!workspaceId || boardLoadMoreLoading.value) return
    const pagination = board.value?.tasks_pagination
    if (!pagination?.has_more || !pagination.next_cursor) {
      return
    }

    const generation = boardFetchGeneration.get(workspaceId) ?? 0
    const query = buildBoardQuery(BOARD_TASKS_PAGE_SIZE, pagination.next_cursor)
    const loadMoreState = {
      get loading() {
        return boardLoadMoreLoading.value
      },
      set loading(value: boolean) {
        boardLoadMoreLoading.value = value
      },
      get error() {
        return boardLoadMoreError.value
      },
      set error(value: string | null) {
        boardLoadMoreError.value = value
      },
    }

    await runBoardLoadMoreAttempt(
      loadMoreState,
      async () => {
        const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/board?${query}`)
        if (isStaleBoardFetch(workspaceId, generation)) {
          return null
        }
        if (!response.ok) throw new Error(await readError(response))
        const payload = (await response.json()) as WorkspaceBoard
        if (isStaleBoardFetch(workspaceId, generation)) {
          return null
        }
        return payload
      },
      payload => {
        applyBoardPayload(workspaceId, payload, true)
      },
    )
  }

  async function fetchTaskReports(
    workspaceId = activeWorkspaceId.value,
    taskId?: string,
  ) {
    if (!workspaceId || !taskId) return
    const key = `${workspaceId}:${taskId}`
    const existing = taskReportFetches.get(key)
    if (existing) return existing

    const request = (async () => {
      const response = await fetch(
        `${API_BASE}/workspaces/${workspaceId}/tasks/${taskId}/reports`,
      )
      if (!response.ok) throw new Error(await readError(response))
      // Reassign (spread) so the keyed object is a new reference and Pinia
      // reactivity reliably re-derives selectedReports.
      taskReports.value = { ...taskReports.value, [taskId]: await response.json() }
    })()

    taskReportFetches.set(key, request)
    try {
      await request
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to fetch task reports')
      throw e
    } finally {
      taskReportFetches.delete(key)
    }
  }

  async function fetchFeedbackLessons(workspaceId = activeWorkspaceId.value) {
    if (!workspaceId) {
      feedbackLessons.value = []
      return
    }
    const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/lessons?limit=50`)
    if (!response.ok) throw new Error(await readError(response))
    feedbackLessons.value = await response.json()
  }

  async function createFeedbackLesson(payload: FeedbackLessonCreate) {
    if (!activeWorkspaceId.value) return
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${activeWorkspaceId.value}/lessons`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchFeedbackLessons()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to create lesson')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function deleteFeedbackLesson(lessonId: string) {
    if (!activeWorkspaceId.value) return
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${activeWorkspaceId.value}/lessons/${encodeURIComponent(lessonId)}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchFeedbackLessons()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to delete lesson')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function summarizeFeedbackLessons(
    payload: FeedbackSummaryRequest = {}
  ): Promise<FeedbackSummaryRun | undefined> {
    if (!activeWorkspaceId.value) return
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${activeWorkspaceId.value}/lessons/summarize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      const run = await response.json() as FeedbackSummaryRun
      await fetchBoard()
      await fetchFeedbackLessons()
      return run
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to summarize lessons')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function createWorkspace(payload: WorkspaceCreate) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      const workspace: Workspace = await response.json()
      workspaces.value.push(workspace)
      setActiveWorkspace(workspace.id)
      await fetchBoard(workspace.id, { reset: true })
      return workspace
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to create workspace')
    } finally {
      isLoading.value = false
    }
  }

  async function updateWorkspace(workspaceId: string, payload: WorkspaceUpdate) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${workspaceId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      const workspace: Workspace = await response.json()
      const index = workspaces.value.findIndex(item => item.id === workspaceId)
      if (index >= 0) {
        workspaces.value[index] = workspace
      } else {
        workspaces.value.push(workspace)
      }
      if (board.value && board.value.workspace.id === workspaceId) {
        board.value = { ...board.value, workspace }
      }
      return workspace
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to update workspace')
    } finally {
      isLoading.value = false
    }
  }

  async function runResidentNow(workspaceId: string) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/resident/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      })
      if (!response.ok) throw new Error(await readError(response))
      const workspace: Workspace = await response.json()
      const index = workspaces.value.findIndex(item => item.id === workspaceId)
      if (index >= 0) {
        workspaces.value[index] = workspace
      } else {
        workspaces.value.push(workspace)
      }
      if (board.value && board.value.workspace.id === workspaceId) {
        board.value = { ...board.value, workspace }
      }
      return workspace
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to run resident agent')
    } finally {
      isLoading.value = false
    }
  }

  async function deleteWorkspace(workspaceId: string) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${workspaceId}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error(await readError(response))
      const index = workspaces.value.findIndex(item => item.id === workspaceId)
      if (index >= 0) {
        workspaces.value.splice(index, 1)
      }
      if (activeWorkspaceId.value === workspaceId) {
        const next = workspaces.value[0]
        if (next) {
          setActiveWorkspace(next.id)
          await fetchBoard(next.id, { reset: true })
        } else {
          activeWorkspaceId.value = null
          localStorage.removeItem(STORAGE_KEY_ACTIVE_WORKSPACE)
          board.value = null
        }
      }
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to delete workspace')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function createTask(payload: WorkspaceTaskCreate) {
    if (!activeWorkspaceId.value) return
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${activeWorkspaceId.value}/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to create task')
    } finally {
      isLoading.value = false
    }
  }

  async function updateTask(taskId: string, payload: WorkspaceTaskUpdate) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to update task')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function updateTaskStatus(taskId: string, status: WorkspaceTaskStatus) {
    const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    })
    if (!response.ok) throw new Error(await readError(response))
    await fetchBoard()
  }

  async function deleteTask(taskId: string) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard(undefined, { reset: true })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to delete task')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function ensureWorkspaceAgent(
    payload: EnsureWorkspaceAgentRequest | AgentType = 'codex'
  ) {
    if (!activeWorkspaceId.value) return
    isLoading.value = true
    const body = typeof payload === 'string' ? { agent_type: payload } : payload
    try {
      const response = await fetch(`${API_BASE}/workspaces/${activeWorkspaceId.value}/agent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to start workspace agent')
    } finally {
      isLoading.value = false
    }
  }

  async function deleteSession(sessionId: string) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/sessions/${sessionId}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to delete agent')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function startTask(taskId: string, payload: StartTaskRequest = {}) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to start task')
    } finally {
      isLoading.value = false
    }
  }

  async function continueTask(taskId: string, payload: ContinueTaskRequest = {}) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}/continue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to continue task')
    } finally {
      isLoading.value = false
    }
  }

  async function requestTaskReview(
    taskId: string,
    payload: RequestTaskReviewRequest = {},
  ) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}/request-review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to request review')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function abortTask(taskId: string, payload: ManualTaskControlRequest) {
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}/abort`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to abort task')
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function dispatchWorkspace() {
    if (!activeWorkspaceId.value) return
    const response = await fetch(`${API_BASE}/workspaces/${activeWorkspaceId.value}/dispatch`, {
      method: 'POST',
    })
    if (!response.ok) throw new Error(await readError(response))
    await fetchBoard()
  }

  async function sendMessage(
    sessionId: string,
    message: string,
    attachments: WorkspaceAttachmentCreate[] = []
  ) {
    const response = await fetch(`${API_BASE}/workspaces/sessions/${sessionId}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, attachments }),
    })
    if (!response.ok) throw new Error(await readError(response))
  }

  async function createReport(sessionId: string, payload: AgentReportCreate) {
    const response = await fetch(`${API_BASE}/workspaces/sessions/${sessionId}/reports`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!response.ok) throw new Error(await readError(response))
    await fetchBoard()
  }

  async function fetchArtifactPreview(
    workspaceId: string,
    artifactPath: string,
    reportId?: string,
  ): Promise<WorkspaceArtifactPreview> {
    const params = new URLSearchParams({ path: artifactPath })
    if (reportId) params.set('report_id', reportId)
    const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/artifacts/preview?${params}`)
    if (!response.ok) throw new Error(await readError(response))
    return response.json()
  }

  return {
    workspaces,
    activeWorkspaceId,
    activeWorkspace,
    board,
    feedbackLessons,
    activeFeedbackLessons,
    boardTasksPagination,
    boardOlderTasksRemaining,
    boardLoadMoreLoading,
    boardLoadMoreError,
    tasks,
    sessions,
    reports,
    workspaceAgents,
    reviewerAgents,
    temporaryReviewers,
    dispatcherAgent,
    residentAgent,
    workspaceAgent,
    isLoading,
    error,
    notifications,
    pushNotification,
    dismissNotification,
    sessionForTask,
    reportsForTask,
    latestReportForTask,
    taskReports,
    reportsForTaskId,
    clearTaskReports,
    fetchTaskReports,
    fetchWorkspaces,
    setActiveWorkspace,
    fetchBoard,
    loadMoreBoardTasks,
    fetchFeedbackLessons,
    createFeedbackLesson,
    deleteFeedbackLesson,
    summarizeFeedbackLessons,
    createWorkspace,
    updateWorkspace,
    runResidentNow,
    deleteWorkspace,
    createTask,
    updateTask,
    updateTaskStatus,
    deleteTask,
    ensureWorkspaceAgent,
    deleteSession,
    startTask,
    continueTask,
    requestTaskReview,
    abortTask,
    dispatchWorkspace,
    sendMessage,
    createReport,
    fetchArtifactPreview,
  }
})
