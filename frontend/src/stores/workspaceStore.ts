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
  // Full per-task report history, fetched on demand when a task detail panel is
  // opened. The board response only carries the latest report per task, so the
  // detail panel hydrates from here instead of the (trimmed) board payload.
  const taskReports = ref<Record<string, AgentReport[]>>({})
  const taskReportFetches = new Map<string, Promise<void>>()

  const activeWorkspace = computed(() =>
    workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
  )
  const tasks = computed(() => board.value?.tasks || [])
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
        await fetchBoard(activeWorkspaceId.value)
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

  async function fetchBoard(workspaceId = activeWorkspaceId.value) {
    if (!workspaceId) return
    const existing = boardFetches.get(workspaceId)
    if (existing) return existing

    const request = (async () => {
      const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/board`)
      if (!response.ok) throw new Error(await readError(response))
      board.value = await response.json()
      await fetchFeedbackLessons(workspaceId)
    })()

    boardFetches.set(workspaceId, request)
    try {
      await request
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to fetch workspace board')
      throw e
    } finally {
      boardFetches.delete(workspaceId)
    }
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
      await fetchBoard(workspace.id)
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
      await fetchBoard()
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
    tasks,
    sessions,
    reports,
    workspaceAgents,
    reviewerAgents,
    temporaryReviewers,
    dispatcherAgent,
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
    fetchFeedbackLessons,
    createFeedbackLesson,
    deleteFeedbackLesson,
    summarizeFeedbackLessons,
    createWorkspace,
    updateWorkspace,
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
