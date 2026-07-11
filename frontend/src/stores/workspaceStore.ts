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
  RemoteProfile,
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
  // Last board ETag per workspace. Sent back as If-None-Match so an unchanged
  // board resolves to a bodyless 304 and we skip re-parsing the payload.
  const boardETags = new Map<string, string>()
  // Full per-task report history, fetched on demand when a task detail panel is
  // opened. The board response only carries the latest report per task, so the
  // detail panel hydrates from here instead of the (trimmed) board payload.
  const taskReports = ref<Record<string, AgentReport[]>>({})
  const taskReportFetches = new Map<string, Promise<void>>()
  // Full per-task detail, fetched on demand when a task detail panel is opened.
  // The board list payload strips detail-only heavy task fields (goal-packet
  // prose arrays, autonomous evaluation reports); the detail panel hydrates the
  // complete task from here so those sections render.
  const taskDetails = ref<Record<string, WorkspaceTask>>({})
  const taskDetailFetches = new Map<string, Promise<void>>()
  // Feedback-lesson fetch deduplication + cadence throttle (PR-03).
  //
  // `feedbackLessons` is a single shared ref, not per-workspace, so a throttle
  // that only looked at wall-clock time per workspace could render the wrong
  // workspace's lessons after a rapid A→B→A switch. We mirror the boardETag
  // identity guard at fetchBoard:297 (`board.value?.workspace.id ===
  // workspaceId`) with a scalar `loadedLessonsWorkspaceId` that records which
  // workspace the current feedbackLessons.value belongs to; the throttle only
  // short-circuits when the requested id matches AND the last fetch is fresh.
  const lessonFetches = new Map<string, Promise<void>>()
  const lastLessonsFetchAt = new Map<string, number>()
  const LESSONS_REFETCH_INTERVAL_MS = 30_000
  let loadedLessonsWorkspaceId: string | null = null
  // Remote execution profiles (PR-09): cached list + in-flight promise dedup so
  // concurrent modal opens / target-toggle watchers do not fire duplicate GETs
  // against /api/remote/profiles. The resource is global (no workspace id in the
  // URL), so a singleton in-flight reference (rather than a keyed Map) suffices.
  // Pass { force: true } to bypass the cache and refetch.
  const remoteProfiles = ref<RemoteProfile[]>([])
  const remoteProfilesLoading = ref(false)
  let remoteProfilesFetch: Promise<void> | null = null

  const activeWorkspace = computed(() =>
    workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
  )
  const tasks = computed(() => board.value?.tasks || [])
  const sessions = computed(() => board.value?.sessions || [])
  const reports = computed(() => board.value?.reports || [])
  /**
   * Latest report for the workspace's resident agent session
   * (board.resident_report), populated server-side. The board poll truncates
   * message bodies; this is the authoritative source for the resident status
   * chip so the UI never has to scan reports[] for task_id=null entries.
   */
  const residentReport = computed<AgentReport | null>(
    () => board.value?.resident_report ?? null,
  )
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
  // RS-05: precomputed working-agent count — replaces inline
  // `workspaceAgents.filter(a => a.runtime_status === 'working').length` at the
  // desktop summary strip and in mobileWorkspaceSummary (PR-02 precomputed-derived idiom).
  const workingAgentCount = computed(() =>
    workspaceAgents.value.filter(agent => agent.runtime_status === 'working').length
  )

  // Stable per-status task arrays. Derived once per board update instead of
  // being re-filtered on every template expression (the board polls every 2.5s
  // and each column used to call .filter() ~6 times per render).
  const tasksByStatusMap = computed<Record<WorkspaceTaskStatus, WorkspaceTask[]>>(() => {
    const map: Record<WorkspaceTaskStatus, WorkspaceTask[]> = {
      todo: [],
      queued: [],
      working: [],
      review: [],
      done: [],
    }
    for (const task of tasks.value) {
      const arr = map[task.status]
      if (arr) arr.push(task)
    }
    return map
  })

  // Stable task-id -> latest report map, derived once per board update.
  // Without this, latestReportForTask() filters the full reports array for
  // every task card multiple times per render.
  const latestReportByTaskId = computed<Record<string, AgentReport>>(() => {
    const map: Record<string, AgentReport> = {}
    for (const report of reports.value) {
      if (report.task_id) map[report.task_id] = report
    }
    return map
  })

  // Stable task-id -> session map, derived once per board update.
  // Mirrors latestReportByTaskId so sessionForTask() becomes an O(1) lookup
  // instead of an O(S) sessions.value.find(...) per call inside the card
  // v-for (PR-02).
  const sessionByTaskId = computed<Record<string, ManagedSession | null>>(() => {
    const sessionById: Record<string, ManagedSession> = {}
    for (const session of sessions.value) {
      sessionById[session.id] = session
    }
    const map: Record<string, ManagedSession | null> = {}
    for (const task of tasks.value) {
      if (!task.session_id) {
        map[task.id] = null
        continue
      }
      map[task.id] = sessionById[task.session_id] || null
    }
    return map
  })

  // Stable task-id -> reports[] map + latest-review-report map, both derived
  // in a single O(R) pass after each board replacement. Without this,
  // reportsForTask() does an O(R) Array.filter per call, and AWV's
  // latestReviewReportForTask() stacked a second .filter(review_*) on
  // top — O(N·R) per render for N cards (PR-02).
  const reportsByTaskId = computed<Record<string, AgentReport[]>>(() => {
    const map: Record<string, AgentReport[]> = {}
    for (const report of reports.value) {
      if (!report.task_id) continue
      const arr = map[report.task_id]
      if (arr) {
        arr.push(report)
      } else {
        map[report.task_id] = [report]
      }
    }
    return map
  })

  const latestReviewReportByTaskId = computed<Record<string, AgentReport>>(() => {
    const map: Record<string, AgentReport> = {}
    for (const report of reports.value) {
      if (!report.task_id) continue
      if (report.state.startsWith('review_')) {
        map[report.task_id] = report
      }
    }
    return map
  })

  function sessionForTask(task: WorkspaceTask): ManagedSession | null {
    if (!task.session_id) return null
    return sessionByTaskId.value[task.id] ?? null
  }

  function reportsForTask(task: WorkspaceTask): AgentReport[] {
    return reportsByTaskId.value[task.id] || []
  }

  function latestReportForTask(task: WorkspaceTask): AgentReport | null {
    return latestReportByTaskId.value[task.id] ?? null
  }

  function latestReviewReportForTask(task: WorkspaceTask): AgentReport | null {
    return latestReviewReportByTaskId.value[task.id] ?? null
  }

  // ---- On-demand full report history (detail panel) ----
  function reportsForTaskId(taskId: string): AgentReport[] {
    return taskReports.value[taskId] || []
  }

  // ---- On-demand full task detail (detail panel / edit modal) ----
  function taskDetailForId(taskId: string): WorkspaceTask | null {
    return taskDetails.value[taskId] || null
  }

  function clearTaskDetail(taskId?: string) {
    if (!taskId) {
      taskDetails.value = {}
      return
    }
    if (taskId in taskDetails.value) {
      const next = { ...taskDetails.value }
      delete next[taskId]
      taskDetails.value = next
    }
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
      const headers: Record<string, string> = {}
      const knownETag = boardETags.get(workspaceId)
      if (knownETag && board.value?.workspace.id === workspaceId) {
        headers['If-None-Match'] = knownETag
      }
      const response = await fetch(`${API_BASE}/workspaces/${workspaceId}/board`, { headers })
      if (response.status === 304) {
        // Board unchanged since the last fetch — keep the existing board.value.
        await fetchFeedbackLessons(workspaceId)
        return
      }
      if (!response.ok) throw new Error(await readError(response))
      const etag = response.headers.get('ETag')
      if (etag) boardETags.set(workspaceId, etag)
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

  async function fetchTaskDetail(
    workspaceId = activeWorkspaceId.value,
    taskId?: string,
  ) {
    if (!workspaceId || !taskId) return
    const key = `${workspaceId}:${taskId}`
    const existing = taskDetailFetches.get(key)
    if (existing) return existing

    const request = (async () => {
      const response = await fetch(
        `${API_BASE}/workspaces/${workspaceId}/tasks/${taskId}`,
      )
      if (!response.ok) throw new Error(await readError(response))
      // Reassign (spread) so the keyed object is a new reference and Pinia
      // reactivity reliably re-derives the detail-panel computed.
      taskDetails.value = { ...taskDetails.value, [taskId]: await response.json() }
    })()

    taskDetailFetches.set(key, request)
    try {
      await request
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to fetch task detail')
      throw e
    } finally {
      taskDetailFetches.delete(key)
    }
  }

  async function fetchRemoteProfiles(opts: { force?: boolean } = {}) {
    // In-flight coalesce: concurrent callers (modal opens, target-toggle
    // watchers, etc.) share one in-flight GET against /api/remote/profiles.
    // Mirrors the boardFetches / taskDetailFetches Map pattern but uses a
    // singleton Promise ref because remote profiles are workspace-global (no
    // key dimension). Cached early-return when profiles are already loaded
    // unless the caller explicitly passes { force: true }.
    if (!opts.force && remoteProfiles.value.length > 0) return
    if (remoteProfilesFetch) return remoteProfilesFetch

    const request = (async () => {
      remoteProfilesLoading.value = true
      try {
        const response = await fetch(`${API_BASE}/remote/profiles`)
        if (!response.ok) throw new Error(await readError(response))
        remoteProfiles.value = await response.json()
      } finally {
        remoteProfilesLoading.value = false
      }
    })()

    remoteProfilesFetch = request
    try {
      await request
    } catch (e) {
      // Note: mirroring the pre-PR-09 AWV behavior we notify but do NOT
      // re-throw: every caller today is fire-and-forget (modal open,
      // target-toggle watcher) and would surface an unhandled-promise
      // rejection otherwise. Callers that need to observe failure can
      // await the returned promise and check remoteProfiles.value.length.
      notifyError(e instanceof Error ? e.message : 'Failed to load remote profiles')
    } finally {
      remoteProfilesFetch = null
    }
  }

  async function fetchFeedbackLessons(workspaceId = activeWorkspaceId.value) {
    if (!workspaceId) {
      feedbackLessons.value = []
      loadedLessonsWorkspaceId = null
      return
    }
    // In-flight coalesce: concurrent calls for the same workspace share one
    // in-flight GET, mirroring boardFetches / taskReportFetches / taskDetailFetches.
    const existing = lessonFetches.get(workspaceId)
    if (existing) return existing

    // Cadence throttle with identity guard (mirrors fetchBoard:297's
    // `board.value?.workspace.id === workspaceId` ETag guard): only skip the
    // fetch when (a) we hold lessons for THIS workspace and (b) the last
    // fetch completed within the throttle window. Switching workspaces forces
    // a fresh fetch regardless of age, so we never render ws-B's lessons
    // against ws-A's UI.
    const lastAt = lastLessonsFetchAt.get(workspaceId)
    if (
      loadedLessonsWorkspaceId === workspaceId
      && lastAt !== undefined
      && Date.now() - lastAt < LESSONS_REFETCH_INTERVAL_MS
    ) {
      return
    }

    const request = (async () => {
      // Capture requested id at the top of the IIFE so we can detect a
      // workspace switch mid-flight and drop the late response (torn-write
      // guard) instead of overwriting the new workspace's lessons.
      const requestedId = workspaceId
      const response = await fetch(`${API_BASE}/workspaces/${requestedId}/lessons?limit=50`)
      if (!response.ok) throw new Error(await readError(response))
      const data = await response.json() as FeedbackLesson[]
      if (requestedId !== activeWorkspaceId.value) {
        // User switched workspaces while this was in flight — discard. The
        // new workspace's own fetch (already queued or about to fire) will
        // populate feedbackLessons with the correct data.
        return
      }
      feedbackLessons.value = data
      loadedLessonsWorkspaceId = requestedId
      lastLessonsFetchAt.set(requestedId, Date.now())
    })()

    lessonFetches.set(workspaceId, request)
    try {
      await request
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to fetch feedback lessons')
      throw e
    } finally {
      lessonFetches.delete(workspaceId)
    }
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
      // Force refresh after mutation: bypass both the in-flight map (defensive
      // — we just awaited the POST, nothing else should be in flight for this
      // id) and the cadence throttle.
      lastLessonsFetchAt.delete(activeWorkspaceId.value)
      lessonFetches.delete(activeWorkspaceId.value)
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
      lastLessonsFetchAt.delete(activeWorkspaceId.value)
      lessonFetches.delete(activeWorkspaceId.value)
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
      lastLessonsFetchAt.delete(activeWorkspaceId.value)
      lessonFetches.delete(activeWorkspaceId.value)
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
          await fetchBoard(next.id)
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
    residentAgent,
    residentReport,
    workspaceAgent,
    workingAgentCount,
    tasksByStatusMap,
    latestReportByTaskId,
    sessionByTaskId,
    reportsByTaskId,
    latestReviewReportByTaskId,
    isLoading,
    error,
    notifications,
    pushNotification,
    dismissNotification,
    remoteProfiles,
    remoteProfilesLoading,
    fetchRemoteProfiles,
    sessionForTask,
    reportsForTask,
    latestReportForTask,
    latestReviewReportForTask,
    taskReports,
    reportsForTaskId,
    clearTaskReports,
    fetchTaskReports,
    taskDetails,
    taskDetailForId,
    clearTaskDetail,
    fetchTaskDetail,
    fetchWorkspaces,
    setActiveWorkspace,
    fetchBoard,
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
