import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type {
  AgentReport,
  AgentReportCreate,
  AgentType,
  ContinueTaskRequest,
  EnsureWorkspaceAgentRequest,
  ManagedSession,
  StartTaskRequest,
  Workspace,
  WorkspaceAttachmentCreate,
  WorkspaceBoard,
  WorkspaceCreate,
  WorkspaceTask,
  WorkspaceTaskCreate,
  WorkspaceTaskStatus,
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
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  const boardFetches = new Map<string, Promise<void>>()

  const activeWorkspace = computed(() =>
    workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
  )
  const tasks = computed(() => board.value?.tasks || [])
  const sessions = computed(() => board.value?.sessions || [])
  const reports = computed(() => board.value?.reports || [])
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

  async function fetchWorkspaces() {
    isLoading.value = true
    error.value = null
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
      error.value = e instanceof Error ? e.message : 'Failed to fetch workspaces'
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
      error.value = null
    })()

    boardFetches.set(workspaceId, request)
    try {
      await request
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to fetch workspace board'
      throw e
    } finally {
      boardFetches.delete(workspaceId)
    }
  }

  async function createWorkspace(payload: WorkspaceCreate) {
    isLoading.value = true
    error.value = null
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
      error.value = e instanceof Error ? e.message : 'Failed to create workspace'
    } finally {
      isLoading.value = false
    }
  }

  async function createTask(payload: WorkspaceTaskCreate) {
    if (!activeWorkspaceId.value) return
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/workspaces/${activeWorkspaceId.value}/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to create task'
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
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to delete task'
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
    error.value = null
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
      error.value = e instanceof Error ? e.message : 'Failed to start workspace agent'
    } finally {
      isLoading.value = false
    }
  }

  async function deleteSession(sessionId: string) {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/workspaces/sessions/${sessionId}`, {
        method: 'DELETE',
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to delete agent'
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function startTask(taskId: string, payload: StartTaskRequest = {}) {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to start task'
    } finally {
      isLoading.value = false
    }
  }

  async function continueTask(taskId: string, payload: ContinueTaskRequest = {}) {
    isLoading.value = true
    error.value = null
    try {
      const response = await fetch(`${API_BASE}/workspaces/tasks/${taskId}/continue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await readError(response))
      await fetchBoard()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to continue task'
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

  return {
    workspaces,
    activeWorkspaceId,
    activeWorkspace,
    board,
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
    sessionForTask,
    reportsForTask,
    latestReportForTask,
    fetchWorkspaces,
    setActiveWorkspace,
    fetchBoard,
    createWorkspace,
    createTask,
    updateTaskStatus,
    deleteTask,
    ensureWorkspaceAgent,
    deleteSession,
    startTask,
    continueTask,
    dispatchWorkspace,
    sendMessage,
    createReport,
  }
})
