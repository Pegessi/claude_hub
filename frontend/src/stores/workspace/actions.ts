import type { Ref } from 'vue'
import type {
  AgentReportCreate,
  AgentType,
  ContinueTaskRequest,
  EnsureWorkspaceAgentRequest,
  ManualTaskControlRequest,
  RequestTaskReviewRequest,
  StartTaskRequest,
  WorkspaceArtifactPreview,
  WorkspaceAttachmentCreate,
  WorkspaceTask,
  WorkspaceTaskCreate,
  WorkspaceTaskHandoffRequest,
  WorkspaceTaskManualProgressRequest,
  WorkspaceTaskMutationResult,
  WorkspaceTaskStatus,
  WorkspaceTaskUpdate,
} from '@/types'

const API_BASE = '/api'

async function readError(response: Response): Promise<string> {
  try {
    const data = await response.json()
    return data.detail || response.statusText
  } catch {
    return response.statusText
  }
}

function taskMutationMessage(status: number, action: string): string {
  if (status === 400 || status === 422) return `The Task ${action} was rejected. Review the fields before trying again.`
  if (status === 401 || status === 403) return `You do not have permission to ${action} this Task.`
  if (status === 409) return `The Task changed or this request conflicts with an earlier request. Refresh before changing the request.`
  if (status >= 500) return `The Task ${action} may have been saved, but the response was not confirmed. Retry only with the same request.`
  return `Failed to ${action} the Task.`
}

async function taskMutationError(response: Response, action: string): Promise<Error> {
  return new Error(taskMutationMessage(response.status, action))
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function invalidTaskResponse(action: string): Error {
  return new Error(
    `The Task ${action} may have succeeded, but the server response was invalid. ` +
    'Refresh before deciding whether to retry.',
  )
}

async function parseTaskCreateResponse(
  response: Response,
  workspaceId: string,
  requireExecutionContract: boolean,
): Promise<WorkspaceTask> {
  let value: unknown
  try {
    value = await response.json()
  } catch {
    throw invalidTaskResponse('create request')
  }
  if (
    !isObject(value) ||
    typeof value.id !== 'string' ||
    !value.id ||
    value.workspace_id !== workspaceId ||
    (requireExecutionContract && (
      typeof value.execution_epoch !== 'number' ||
      !Number.isInteger(value.execution_epoch) || value.execution_epoch < 1 ||
      typeof value.progress_revision !== 'number' ||
      !Number.isInteger(value.progress_revision) || value.progress_revision < 0 ||
      typeof value.execution_control !== 'string' ||
      !['workspace', 'initiator'].includes(value.execution_control)
    ))
  ) {
    throw invalidTaskResponse('create request')
  }
  return value as unknown as WorkspaceTask
}

async function parseTaskMutationResponse(
  response: Response,
  workspaceId: string,
  taskId: string,
  requestCallId: string,
  action: string,
): Promise<WorkspaceTaskMutationResult> {
  let value: unknown
  try {
    value = await response.json()
  } catch {
    throw invalidTaskResponse(action)
  }
  if (!isObject(value) || !isObject(value.task) || !isObject(value.event)) {
    throw invalidTaskResponse(action)
  }
  const task = value.task
  const event = value.event
  const expectedEventCallId = `task-execution:${taskId}:${requestCallId}`
  if (
    typeof task.id !== 'string' ||
    task.id !== taskId ||
    task.workspace_id !== workspaceId ||
    typeof task.execution_epoch !== 'number' ||
    !Number.isInteger(task.execution_epoch) ||
    (task.execution_epoch as number) < 1 ||
    typeof task.progress_revision !== 'number' ||
    !Number.isInteger(task.progress_revision) ||
    (task.progress_revision as number) < 0 ||
    !['workspace', 'initiator'].includes(String(task.execution_control)) ||
    typeof event.call_id !== 'string' ||
    event.call_id !== expectedEventCallId ||
    event.task_id !== taskId ||
    typeof value.replayed !== 'boolean'
  ) {
    throw invalidTaskResponse(action)
  }
  return value as unknown as WorkspaceTaskMutationResult
}

interface WorkspaceActionsRuntime {
  activeWorkspaceId: Ref<string | null>
  isLoading: Ref<boolean>
  notifyError: (message: string) => void
  fetchBoard: (workspaceId?: string, options?: { reset?: boolean }) => Promise<void>
}

export function createWorkspaceActions(runtime: WorkspaceActionsRuntime) {
  const { activeWorkspaceId, isLoading, notifyError, fetchBoard } = runtime
  async function createTask(payload: WorkspaceTaskCreate, targetWorkspaceId?: string): Promise<WorkspaceTask> {
    const workspaceId = targetWorkspaceId ?? activeWorkspaceId.value
    if (!workspaceId) throw new Error('Select a workspace before creating a Task.')
    isLoading.value = true
    try {
      const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/tasks`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      })
      if (!response.ok) throw await taskMutationError(response, 'create')
      return await parseTaskCreateResponse(response, workspaceId, typeof payload.request_key === 'string')
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : 'Failed to create the Task.'
      notifyError(message)
      throw cause
    } finally { isLoading.value = false }
  }

  async function reportTaskProgressManually(
    workspaceId: string,
    taskId: string,
    payload: WorkspaceTaskManualProgressRequest,
  ): Promise<WorkspaceTaskMutationResult> {
    const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/tasks/${taskId}/progress/manual`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    })
    if (!response.ok) throw await taskMutationError(response, 'update progress for')
    return await parseTaskMutationResponse(response, workspaceId, taskId, payload.call_id, 'progress update')
  }

  async function handoffTask(
    workspaceId: string,
    taskId: string,
    payload: WorkspaceTaskHandoffRequest,
  ): Promise<WorkspaceTaskMutationResult> {
    const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/tasks/${taskId}/handoff`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    })
    if (!response.ok) throw await taskMutationError(response, 'hand off')
    return await parseTaskMutationResponse(response, workspaceId, taskId, payload.call_id, 'handoff request')
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
    createTask,
    reportTaskProgressManually,
    handoffTask,
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
}
