import type {
  WorkspaceTask,
  WorkspaceTaskCapabilities,
  WorkspaceTaskCreate,
  WorkspaceTaskExecutionControl,
  WorkspaceTaskHandoffRequest,
  WorkspaceTaskManualProgressRequest,
  WorkspaceTaskProgressState,
  WorkspaceTaskSource,
} from '@/types'

export type ManualTaskProgressState = Exclude<WorkspaceTaskProgressState, 'released'>

export interface StoredTaskCreateAttempt {
  workspace_id: string
  payload: WorkspaceTaskCreate
}

export interface StoredTaskHandoffAttempt {
  workspace_id: string
  task_id: string
  source: WorkspaceTaskSource | null
  request: WorkspaceTaskHandoffRequest
}

const MANUAL_PROGRESS_STATES = new Set<WorkspaceTaskProgressState>([
  'started',
  'working',
  'blocked',
  'needs_input',
  'completed',
  'failed',
])

// This cache is populated only when sessionStorage persistence fails. A
// successful persistent write removes the corresponding volatile copy.
const volatileReporterKeys = new Map<string, string>()

export function executionControlForTask(
  task: Pick<WorkspaceTask, 'execution_control'>,
): WorkspaceTaskExecutionControl {
  return task.execution_control ?? 'workspace'
}

export function isWorkspaceControlledTask(
  task: Pick<WorkspaceTask, 'execution_control'>,
): boolean {
  return executionControlForTask(task) === 'workspace'
}

export function taskSourceLabel(source: WorkspaceTaskSource | null | undefined): string {
  if (!source) return 'Source unknown'
  if (source.kind === 'chat') return 'From Chat'
  if (source.kind === 'agent') return 'From Agent'
  return 'Created manually'
}

export function taskExecutionLabel(
  task: Pick<WorkspaceTask, 'execution_control'>,
): string {
  return isWorkspaceControlledTask(task) ? 'Workspace managed' : 'Initiator managed'
}

export function progressStateLabel(state: WorkspaceTaskProgressState): string {
  if (state === 'needs_input') return 'Needs input'
  if (state === 'completed') return 'Done'
  return state.charAt(0).toUpperCase() + state.slice(1)
}

export function manualProgressStates(
  capabilities: WorkspaceTaskCapabilities | null | undefined,
): ManualTaskProgressState[] {
  if (!capabilities) return []
  return capabilities.progress_states.filter(
    (state): state is ManualTaskProgressState => MANUAL_PROGRESS_STATES.has(state),
  )
}

export function generateTaskReporterKey(): string {
  const bytes = new Uint8Array(32)
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
}

export function generateTaskCallId(): string {
  return globalThis.crypto.randomUUID()
}

function validReporterKey(value: unknown): value is string {
  return typeof value === 'string' &&
    value.length >= 32 &&
    value.length <= 256 &&
    !Array.from(value).some(character => {
      const code = character.charCodeAt(0)
      return code < 33 || code > 126
    })
}

function safeOrigin(): string {
  try {
    return globalThis.location?.origin ?? ''
  } catch {
    return ''
  }
}

function sourceIdentity(source: WorkspaceTaskSource | null | undefined): string {
  return JSON.stringify([
    source?.kind ?? null,
    source?.tab_id ?? null,
    source?.agent_id ?? null,
  ])
}

function storageOrSession(storage?: Storage): Storage {
  return storage ?? globalThis.sessionStorage
}

export function taskReporterStorageKey(
  workspaceId: string,
  task: Pick<WorkspaceTask, 'id' | 'source' | 'execution_epoch'>,
  apiOrigin = safeOrigin(),
): string {
  return [
    'claude-hub:task-reporter',
    encodeURIComponent(apiOrigin),
    encodeURIComponent(workspaceId),
    encodeURIComponent(task.id),
    String(task.execution_epoch ?? 'legacy'),
    encodeURIComponent(sourceIdentity(task.source)),
  ].join(':')
}

export function saveTaskReporterKey(
  workspaceId: string,
  task: Pick<WorkspaceTask, 'id' | 'source' | 'execution_epoch'>,
  key: string,
  storage?: Storage,
): boolean {
  if (!validReporterKey(key)) {
    throw new Error('Reporter key must contain at least 32 and at most 256 printable characters')
  }
  const storageKey = taskReporterStorageKey(workspaceId, task)
  try {
    storageOrSession(storage).setItem(storageKey, key)
    volatileReporterKeys.delete(storageKey)
    return true
  } catch {
    volatileReporterKeys.set(storageKey, key)
    return false
  }
}

export function loadTaskReporterKey(
  workspaceId: string,
  task: Pick<WorkspaceTask, 'id' | 'source' | 'execution_epoch'>,
  storage?: Storage,
): string | null {
  const storageKey = taskReporterStorageKey(workspaceId, task)
  const volatile = volatileReporterKeys.get(storageKey)
  if (volatile) return volatile
  try {
    return storageOrSession(storage).getItem(storageKey)
  } catch {
    return null
  }
}

export function removeTaskReporterKey(
  workspaceId: string,
  task: Pick<WorkspaceTask, 'id' | 'source' | 'execution_epoch'>,
  storage?: Storage,
): void {
  const storageKey = taskReporterStorageKey(workspaceId, task)
  volatileReporterKeys.delete(storageKey)
  try {
    storageOrSession(storage).removeItem(storageKey)
  } catch {
    // Local cleanup failure does not change server state.
  }
}

export function taskCreateAttemptStorageKey(
  workspaceId: string,
  apiOrigin = safeOrigin(),
): string {
  return `claude-hub:task-create:${encodeURIComponent(apiOrigin)}:${encodeURIComponent(workspaceId)}`
}

function validCreateAttempt(value: unknown, workspaceId: string): value is StoredTaskCreateAttempt {
  if (!value || typeof value !== 'object') return false
  const attempt = value as StoredTaskCreateAttempt
  const requestKey = attempt.payload?.request_key
  if (attempt.workspace_id !== workspaceId) return false
  if (typeof requestKey !== 'string' || requestKey.length < 1 || requestKey.length > 128) {
    return false
  }
  if (
    attempt.payload.execution_control !== 'workspace' &&
    attempt.payload.execution_control !== 'initiator'
  ) return false
  if (
    attempt.payload.execution_control === 'initiator' &&
    !validReporterKey(attempt.payload.reporter_key)
  ) return false
  if (
    attempt.payload.execution_control === 'workspace' &&
    attempt.payload.reporter_key !== undefined
  ) return false
  return true
}

export function saveTaskCreateAttempt(
  value: StoredTaskCreateAttempt,
  storage?: Storage,
): void {
  if (!validCreateAttempt(value, value.workspace_id)) {
    throw new Error('Invalid Task create attempt')
  }
  try {
    storageOrSession(storage).setItem(
      taskCreateAttemptStorageKey(value.workspace_id),
      JSON.stringify(value),
    )
  } catch {
    throw new Error('The Task create attempt could not be stored safely')
  }
}

export function loadTaskCreateAttempt(
  workspaceId: string,
  storage?: Storage,
): StoredTaskCreateAttempt | null {
  try {
    const raw = storageOrSession(storage).getItem(taskCreateAttemptStorageKey(workspaceId))
    if (!raw) return null
    const value = JSON.parse(raw) as unknown
    return validCreateAttempt(value, workspaceId) ? value : null
  } catch {
    return null
  }
}

export function removeTaskCreateAttempt(
  workspaceId: string,
  options: RemoveAttemptOptions = {},
): boolean {
  try {
    const target = storageOrSession(options.storage)
    const key = taskCreateAttemptStorageKey(workspaceId)
    if (options.expectedRequestKey !== undefined) {
      const raw = target.getItem(key)
      if (!raw) return false
      const value = JSON.parse(raw) as { payload?: { request_key?: unknown } }
      if (value.payload?.request_key !== options.expectedRequestKey) return false
    }
    target.removeItem(key)
    return true
  } catch {
    return false
  }
}

function validTaskSource(value: unknown): value is WorkspaceTaskSource | null {
  if (value === null) return true
  if (!value || typeof value !== 'object') return false
  const source = value as WorkspaceTaskSource
  return ['human', 'chat', 'agent'].includes(source.kind) &&
    (source.tab_id === null || typeof source.tab_id === 'string') &&
    (source.agent_id === null || typeof source.agent_id === 'string')
}

function validExecutionRef(value: unknown): boolean {
  if (value === undefined || value === null) return true
  if (typeof value !== 'object' || Array.isArray(value)) return false
  const ref = value as Record<string, unknown>
  const text = ['provider', 'session_id', 'thread_id', 'turn_id']
  if (text.some(key => ref[key] !== undefined && ref[key] !== null && typeof ref[key] !== 'string')) {
    return false
  }
  return ref.run_epoch === undefined || ref.run_epoch === null ||
    (typeof ref.run_epoch === 'number' && Number.isInteger(ref.run_epoch) && ref.run_epoch >= 0)
}

function validHandoffRequest(value: unknown): value is WorkspaceTaskHandoffRequest {
  if (!value || typeof value !== 'object') return false
  const request = value as WorkspaceTaskHandoffRequest
  if (
    typeof request.call_id !== 'string' ||
    !request.call_id.trim() ||
    request.call_id.length > 128
  ) {
    return false
  }
  if (
    !Number.isInteger(request.expected_execution_epoch) ||
    request.expected_execution_epoch < 1
  ) {
    return false
  }
  if (
    !Number.isInteger(request.expected_progress_revision) ||
    request.expected_progress_revision < 0
  ) {
    return false
  }
  if (request.execution_control === 'initiator') {
    return validReporterKey(request.new_reporter_key) && validExecutionRef(request.execution_ref)
  }
  if (request.execution_control === 'workspace') {
    return request.new_reporter_key === undefined && request.execution_ref === undefined
  }
  return false
}

export function taskHandoffAttemptStorageKey(
  workspaceId: string,
  taskId: string,
  apiOrigin = safeOrigin(),
): string {
  return `claude-hub:task-handoff:${encodeURIComponent(apiOrigin)}:${encodeURIComponent(workspaceId)}:${encodeURIComponent(taskId)}`
}

export function saveTaskHandoffAttempt(
  value: StoredTaskHandoffAttempt,
  storage?: Storage,
): void {
  if (
    !value.workspace_id ||
    !value.task_id ||
    !validTaskSource(value.source) ||
    !validHandoffRequest(value.request)
  ) {
    throw new Error('Invalid Task handoff attempt')
  }
  try {
    storageOrSession(storage).setItem(
      taskHandoffAttemptStorageKey(value.workspace_id, value.task_id),
      JSON.stringify(value),
    )
  } catch {
    throw new Error('The Task handoff attempt could not be stored safely')
  }
}

export function loadTaskHandoffAttempt(
  workspaceId: string,
  taskId: string,
  storage?: Storage,
): StoredTaskHandoffAttempt | null {
  try {
    const raw = storageOrSession(storage).getItem(
      taskHandoffAttemptStorageKey(workspaceId, taskId),
    )
    if (!raw) return null
    const value = JSON.parse(raw) as unknown
    if (!value || typeof value !== 'object') return null
    const attempt = value as StoredTaskHandoffAttempt
    return attempt.workspace_id === workspaceId &&
      attempt.task_id === taskId &&
      validTaskSource(attempt.source) &&
      validHandoffRequest(attempt.request)
      ? attempt
      : null
  } catch {
    return null
  }
}

export function removeTaskHandoffAttempt(
  workspaceId: string,
  taskId: string,
  options: RemoveAttemptOptions = {},
): boolean {
  try {
    const target = storageOrSession(options.storage)
    const key = taskHandoffAttemptStorageKey(workspaceId, taskId)
    if (options.expectedCallId !== undefined) {
      const raw = target.getItem(key)
      if (!raw) return false
      const value = JSON.parse(raw) as { request?: { call_id?: unknown } }
      if (value.request?.call_id !== options.expectedCallId) return false
    }
    target.removeItem(key)
    return true
  } catch {
    return false
  }
}

export function handoffReporterKeyApplies(
  attempt: StoredTaskHandoffAttempt,
  task: WorkspaceTask,
): boolean {
  return attempt.request.execution_control === 'initiator' &&
    task.id === attempt.task_id &&
    task.workspace_id === attempt.workspace_id &&
    task.execution_control === 'initiator' &&
    task.execution_epoch === attempt.request.expected_execution_epoch + 1
}

export function createReporterKeyApplies(
  workspaceId: string,
  task: WorkspaceTask,
): boolean {
  return task.workspace_id === workspaceId &&
    task.execution_control === 'initiator' &&
    task.execution_epoch === 1
}

export interface StoredTaskProgressAttempt {
  workspace_id: string
  task_id: string
  request: WorkspaceTaskManualProgressRequest
}

function validManualProgressRequest(value: unknown): value is WorkspaceTaskManualProgressRequest {
  if (!value || typeof value !== 'object') return false
  const request = value as WorkspaceTaskManualProgressRequest
  const raw = value as Record<string, unknown>
  return typeof request.call_id === 'string' &&
    Boolean(request.call_id.trim()) &&
    request.call_id.length <= 128 &&
    Number.isInteger(request.expected_execution_epoch) &&
    request.expected_execution_epoch >= 1 &&
    Number.isInteger(request.expected_progress_revision) &&
    request.expected_progress_revision >= 0 &&
    MANUAL_PROGRESS_STATES.has(request.state) &&
    typeof request.summary === 'string' &&
    Boolean(request.summary.trim()) &&
    request.summary.length <= 2_000 &&
    (request.validation === undefined || request.validation === null ||
      (typeof request.validation === 'string' && request.validation.length <= 4_000)) &&
    (request.risks === undefined || request.risks === null ||
      (typeof request.risks === 'string' && request.risks.length <= 2_000)) &&
    (request.artifact_refs === undefined ||
      (Array.isArray(request.artifact_refs) &&
       request.artifact_refs.length <= 32 &&
       request.artifact_refs.every(item =>
         typeof item === 'string' && Boolean(item.trim()) && item.length <= 2_048))) &&
    raw.execution_ref === undefined
}

export function taskProgressAttemptStorageKey(
  workspaceId: string,
  taskId: string,
  apiOrigin = safeOrigin(),
): string {
  return `claude-hub:task-progress:${encodeURIComponent(apiOrigin)}:${encodeURIComponent(workspaceId)}:${encodeURIComponent(taskId)}`
}

export function saveTaskProgressAttempt(
  value: StoredTaskProgressAttempt,
  storage?: Storage,
): void {
  if (!value.workspace_id || !value.task_id || !validManualProgressRequest(value.request)) {
    throw new Error('Invalid Task progress attempt')
  }
  try {
    storageOrSession(storage).setItem(
      taskProgressAttemptStorageKey(value.workspace_id, value.task_id),
      JSON.stringify(value),
    )
  } catch {
    throw new Error('The Task progress attempt could not be stored safely')
  }
}

export function loadTaskProgressAttempt(
  workspaceId: string,
  taskId: string,
  storage?: Storage,
): StoredTaskProgressAttempt | null {
  try {
    const raw = storageOrSession(storage).getItem(
      taskProgressAttemptStorageKey(workspaceId, taskId),
    )
    if (!raw) return null
    const value = JSON.parse(raw) as unknown
    if (!value || typeof value !== 'object') return null
    const attempt = value as StoredTaskProgressAttempt
    return attempt.workspace_id === workspaceId &&
      attempt.task_id === taskId &&
      validManualProgressRequest(attempt.request)
      ? attempt
      : null
  } catch {
    return null
  }
}

export function removeTaskProgressAttempt(
  workspaceId: string,
  taskId: string,
  options: RemoveAttemptOptions = {},
): boolean {
  try {
    const target = storageOrSession(options.storage)
    const key = taskProgressAttemptStorageKey(workspaceId, taskId)
    if (options.expectedCallId !== undefined) {
      const raw = target.getItem(key)
      if (!raw) return false
      const value = JSON.parse(raw) as { request?: { call_id?: unknown } }
      if (value.request?.call_id !== options.expectedCallId) return false
    }
    target.removeItem(key)
    return true
  } catch {
    return false
  }
}

export interface RemoveAttemptOptions {
  expectedCallId?: string
  expectedRequestKey?: string
  storage?: Storage
}
