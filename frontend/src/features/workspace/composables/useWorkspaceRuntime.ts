import {
  useAppStore,
} from '@/stores/appStore'
import {
  useTerminalStore,
} from '@/stores/terminalStore'
import {
  useWorkspaceStore,
} from '@/stores/workspaceStore'
import {
  useLaunchEnvPresets,
} from '@/composables/useLaunchEnvPresets'
import {
  usePendingActions,
} from '@/composables/usePendingActions'
import {
  useTaskCreateFlow,
} from '@/composables/useTaskCreateFlow'
import {
  storeToRefs,
} from 'pinia'
import {
  computed,
  ref,
  reactive,
} from 'vue'
import {
  type WorkspaceTaskStatus,
  type ManagedSession,
  type TerminalAgentStatus,
  type WorkspaceTask,
  type WorkspaceAttachment,
  type AgentRuntimeStatus,
} from '@/types'
import {
  formatAttachmentSize,
} from '@/utils/draftAttachments'
import type { WorkspaceSessionView } from '../types'

export function useWorkspaceRuntime() {
  const appStore = useAppStore()

  const terminalStore = useTerminalStore()

  const workspaceStore = useWorkspaceStore()

  // Dismiss all error-type notifications from workspace store.
  function dismissWorkspaceErrors() {
    const ids = workspaceStore.notifications
      .filter(n => n.type === 'error')
      .map(n => n.id)
    for (const id of ids) workspaceStore.dismissNotification(id)
  }

  const {
    envPresets,
    getPresetText,
    defaultPresetTextForAgent,
  } = useLaunchEnvPresets()

  const {
    isPending,
    runPending,
  } = usePendingActions()

  const taskCreateFlow = useTaskCreateFlow()

  const {
    colorScheme,
  } = storeToRefs(appStore)

  const {
    workspaces,
    activeWorkspaceId,
    board,
    tasks,
    boardTasksPagination,
    boardOlderTasksRemaining,
    boardLoadMoreLoading,
    boardLoadMoreError,
    activeFeedbackLessons,
    workspaceAgents,
    reviewerAgents,
    temporaryReviewers,
    dispatcherAgent,
    residentAgent,
    isLoading,
    error,
    notifications: wsNotifications,
    taskCapabilities,
  } = storeToRefs(workspaceStore)

  // Combine workspace and terminal store notifications so toasts fire regardless
  // of which store pushed them (terminalStore.switchEnv pushes to itself; most
  // workspace actions push to workspaceStore).
  const terminalNotificationsRefs = storeToRefs(terminalStore)

  const workspaceNotifications = computed(() => [
    ...wsNotifications.value,
    ...terminalNotificationsRefs.notifications.value,
  ])

  function dismissToast(n: { id: string }) {
    // Route dismiss to whichever store owns the notification (ws-* prefix is
    // assigned by workspaceStore; everything else goes to terminalStore).
    if (n.id.startsWith('ws-')) {
      workspaceStore.dismissNotification(n.id)
    } else {
      terminalStore.dismissNotification(n.id)
    }
  }

  const selectedWorkspaceId = ref(activeWorkspaceId.value || '')

  const selectedTaskId = ref<string | null>(null)

  const workspaceSessionView = ref<WorkspaceSessionView>('agents')

  const elapsedClockMs = ref(Date.now())

  const mobileCollapsedColumns = reactive<Record<WorkspaceTaskStatus, boolean>>({
    todo: false,
    queued: false,
    working: false,
    review: false,
    done: false,
    failed: false,
  })

  const columns: { status: WorkspaceTaskStatus; label: string }[] = [
    { status: 'todo', label: 'Todo' },
    { status: 'queued', label: 'Queued' },
    { status: 'working', label: 'Working' },
    { status: 'review', label: 'Review' },
    { status: 'done', label: 'Done' },
  ]

  // Show a graceful skeleton over the board while a workspace switch is in
  // flight, or on the very first board load (board still null) for the active
  // workspace. Background polling refreshes do not trigger this — once a board
  // has been rendered the columns stay visible and update in place.
  const boardLoading = computed(
    () =>
      !!activeWorkspaceId.value &&
      (isPending('workspace:switch') ||
        (board.value === null && isLoading.value)),
  )

  const activeWorkspace = computed(() =>
    workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
  )

  const mobileWorkspaceSummary = computed(() => {
    if (!activeWorkspaceId.value) return 'Create a workspace to begin'

    const agentCount = workspaceAgents.value.length
    const reviewerCount = reviewerAgents.value.length + temporaryReviewers.value.length
    const workingCount = workspaceAgents.value.filter(agent => agent.runtime_status === 'working').length
    const queuedCount = tasksByStatus('queued').length
    return `${agentCount} agents · ${reviewerCount} reviewers · ${workingCount} working · ${queuedCount} queued`
  })

  const selectedTask = computed(() =>
    tasks.value.find(task => task.id === selectedTaskId.value) || null
  )

  const reviewerSessions = computed<ManagedSession[]>(() => [
    ...reviewerAgents.value,
    ...temporaryReviewers.value,
  ])

  const managedWorkspaceSessions = computed<ManagedSession[]>(() => [
    ...workspaceAgents.value,
    ...reviewerSessions.value,
    ...(dispatcherAgent.value ? [dispatcherAgent.value] : []),
    ...(residentAgent.value ? [residentAgent.value] : []),
  ])

  const visibleWorkspaceSessions = computed<ManagedSession[]>(() =>
    workspaceSessionView.value === 'reviewers'
      ? reviewerSessions.value
      : [
          ...workspaceAgents.value,
          ...(residentAgent.value ? [residentAgent.value] : []),
        ]
  )

  const visibleWorkspaceSessionsEmptyText = computed(() =>
    workspaceSessionView.value === 'reviewers'
      ? 'No reviewers in this workspace.'
      : 'No agents in this workspace.'
  )

  const terminalStatusByTabId = computed<Record<string, TerminalAgentStatus>>(() => {
    const map: Record<string, TerminalAgentStatus> = {}
    for (const status of terminalStore.agentStatuses) {
      map[status.tab_id] = status
    }
    return map
  })

  function taskActionKey(action: string, taskId: string | null | undefined) {
    return `task:${taskId || 'none'}:${action}`
  }

  function agentActionKey(action: string, agentId: string | null | undefined) {
    return `agent:${agentId || 'none'}:${action}`
  }

  function sessionActionKey(action: string, sessionId: string | null | undefined) {
    return `session:${sessionId || 'none'}:${action}`
  }

  function tasksByStatus(status: WorkspaceTaskStatus) {
    if (status === 'working') {
      return tasks.value.filter(task => task.status === 'working' || task.status === 'failed')
    }
    return tasks.value.filter(task => task.status === status)
  }

  function taskCountByStatus(status: WorkspaceTaskStatus) {
    const counts = boardTasksPagination.value?.status_counts
    if (counts != null) {
      if (status === 'working') {
        return (counts.working ?? 0) + (counts.failed ?? 0)
      }
      return counts[status] ?? 0
    }
    return tasksByStatus(status).length
  }

  async function handleLoadOlderTasks() {
    if (!activeWorkspaceId.value) return
    await runPending('workspace:load-older', () =>
      workspaceStore.loadMoreBoardTasks(activeWorkspaceId.value!),
    )
  }

  function sessionForTask(task: WorkspaceTask) {
    return workspaceStore.sessionForTask(task)
  }

  function latestReportForTask(task: WorkspaceTask) {
    return workspaceStore.latestReportForTask(task)
  }

  function reviewerTitle(sessionId?: string | null) {
    if (!sessionId) return 'none'
    return managedWorkspaceSessions.value.find(agent => agent.id === sessionId)?.title || sessionId
  }

  function agentRoleLabel(agent: ManagedSession) {
    if (agent.role === 'dispatcher') return 'Dispatcher'
    if (agent.role === 'resident') return 'Resident'
    if (agent.role === 'reviewer') return agent.ephemeral ? 'Temporary Reviewer' : 'Reviewer'
    return 'Agent'
  }

  function taskTitle(taskId?: string | null) {
    if (!taskId) return 'none'
    return tasks.value.find(task => task.id === taskId)?.title || taskId
  }

  function persistedAttachmentMeta(attachment: WorkspaceAttachment) {
    return `${attachment.filename} (${attachment.mime_type}, ${formatAttachmentSize(attachment.size_bytes)})`
  }

  function terminalStatusForAgent(agent: ManagedSession) {
    return terminalStatusByTabId.value[agent.tab_id] || null
  }

  function agentRuntimeStatus(agent: ManagedSession): AgentRuntimeStatus {
    return terminalStatusForAgent(agent)?.status || agent.runtime_status
  }

  function agentRuntimeText(agent: ManagedSession) {
    return terminalStatusForAgent(agent)?.status_text || agent.runtime_status
  }

  function agentRuntimeDetail(agent: ManagedSession) {
    const status = terminalStatusForAgent(agent)
    if (status?.detail) return status.detail
    if (agent.current_task_id) return `Current task: ${taskTitle(agent.current_task_id)}`
    return agent.workspace_path
  }

  function agentTaskLabel(agent: ManagedSession) {
    if (!agent.current_task_id) return 'no task'
    return taskTitle(agent.current_task_id)
  }

  function openTasksForAgent(agent: ManagedSession) {
    return tasks.value.filter(
      task =>
        (task.session_id === agent.id || task.review_session_id === agent.id) &&
        task.status !== 'done'
    )
  }

  function agentDeleteDisabledReason(agent: ManagedSession) {
    const openTasks = openTasksForAgent(agent)
    if (openTasks.length === 0) return ''
    if (openTasks.length === 1) {
      return `Cannot delete while "${openTasks[0].title}" is still assigned`
    }
    return `Cannot delete while ${openTasks.length} tasks are still assigned`
  }

  function canDeleteAgent(agent: ManagedSession) {
    return agentDeleteDisabledReason(agent) === ''
  }

  function canSwitchAgentEnv(agent: ManagedSession) {
    // Switch-env supports Claude and Codex, requires a live local tmux session
    // (not offline/stopped), and is only available on local targets (remote
    // sessions aren't supported by the backend respawn flow).
    return (agent.agent_type === 'claude' || agent.agent_type === 'codex')
      && agent.target === 'local'
      && agent.runtime_status !== 'offline'
  }

  function agentDeleteTitle(agent: ManagedSession) {
    return agentDeleteDisabledReason(agent) || `Delete ${agent.title}`
  }

  function isMobileColumnCollapsed(status: WorkspaceTaskStatus) {
    return mobileCollapsedColumns[status]
  }

  function toggleMobileColumn(status: WorkspaceTaskStatus) {
    mobileCollapsedColumns[status] = !mobileCollapsedColumns[status]
  }

  function formatTime(value: string) {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(value))
  }

  async function handleWorkspaceChange() {
    if (!selectedWorkspaceId.value) return
    const workspaceId = selectedWorkspaceId.value
    await runPending('workspace:switch', async () => {
      workspaceStore.setActiveWorkspace(workspaceId)
      await workspaceStore.fetchBoard(workspaceId, { reset: true })
    })
  }

  function isTextEntryFocused(): boolean {
    // While the user is actively typing in a text field (most importantly the
    // task-detail compose textarea), a background board poll would replace the
    // entire `board` object and force Vue to re-render the large detail subtree
    // that hosts the focused input. On mobile that periodic re-render competes
    // with keystroke handling and is felt as input lag, so we skip the poll tick
    // until the field is blurred — the next tick (or any explicit action) then
    // refreshes the board normally.
    const active = document.activeElement as HTMLElement | null
    if (!active) return false
    const tag = active.tagName
    if (tag === 'TEXTAREA') return true
    if (tag === 'INPUT') {
      const type = (active as HTMLInputElement).type
      return type !== 'checkbox' && type !== 'radio' && type !== 'range'
    }
    return active.isContentEditable
  }

  async function refreshBoard() {
    // Defer background refreshes while the user is typing to keep input smooth.
    if (isTextEntryFocused()) return
    try {
      await workspaceStore.fetchBoard()
      // Keep the open task's full timeline live without polling its whole history:
      // the trimmed board carries the latest report per task, so refetch the
      // on-demand history only when that latest report id has changed.
      const task = selectedTask.value
      const workspaceId = activeWorkspaceId.value
      if (task && workspaceId) {
        const boardLatestId = workspaceStore.latestReportForTask(task)?.id ?? null
        const cached = workspaceStore.reportsForTaskId(task.id)
        const cachedLatestId = cached.length ? cached[cached.length - 1].id : null
        if (boardLatestId !== cachedLatestId) {
          await workspaceStore.fetchTaskReports(workspaceId, task.id)
        }
      }
    } catch {
      // Error state is owned by the workspace store.
    }
  }

  async function refreshAgentStatuses() {
    await runPending('workspace:refresh-statuses', () =>
      Promise.all([
        workspaceStore.fetchBoard(),
        terminalStore.fetchAgentStatuses(),
      ])
    )
  }

  async function openSession(session: ManagedSession) {
    await runPending(sessionActionKey('open', session.id), async () => {
      await terminalStore.fetchTabs()
      appStore.setMode('terminal')
      terminalStore.setActiveTab(session.tab_id)
    })
  }

  async function deleteAgent(agent: ManagedSession) {
    const confirmed = window.confirm(`Delete agent "${agent.title}"?`)
    if (!confirmed) return
    await runPending(agentActionKey('delete', agent.id), async () => {
      await workspaceStore.deleteSession(agent.id)
      await terminalStore.fetchTabs({ force: true })
    })
  }

  function openTaskModal(): void {
    const workspaceId = activeWorkspaceId.value
    if (workspaceId) taskCreateFlow.open(workspaceId)
  }

  return {
    activeWorkspace,
    mobileWorkspaceSummary,
    isPending,
    selectedWorkspaceId,
    handleWorkspaceChange,
    workspaces,
    activeWorkspaceId,
    openTaskModal,
    colorScheme,
    error,
    dismissWorkspaceErrors,
    workspaceAgents,
    reviewerAgents,
    temporaryReviewers,
    tasksByStatus,
    activeFeedbackLessons,
    columns,
    taskCountByStatus,
    workspaceSessionView,
    reviewerSessions,
    refreshAgentStatuses,
    visibleWorkspaceSessions,
    sessionActionKey,
    openSession,
    agentRuntimeStatus,
    agentRoleLabel,
    agentRuntimeDetail,
    agentTaskLabel,
    agentRuntimeText,
    canSwitchAgentEnv,
    canDeleteAgent,
    agentDeleteTitle,
    agentActionKey,
    deleteAgent,
    visibleWorkspaceSessionsEmptyText,
    boardLoading,
    isMobileColumnCollapsed,
    toggleMobileColumn,
    selectedTaskId,
    latestReportForTask,
    tasks,
    taskActionKey,
    sessionForTask,
    boardTasksPagination,
    boardOlderTasksRemaining,
    boardLoadMoreLoading,
    handleLoadOlderTasks,
    boardLoadMoreError,
    persistedAttachmentMeta,
    reviewerTitle,
    board,
    taskCapabilities,
    formatTime,
    selectedTask,
    isLoading,
    dispatcherAgent,
    residentAgent,
    taskTitle,
    envPresets,
    workspaceNotifications,
    dismissToast,
    taskCreateFlow,
    defaultPresetTextForAgent,
    workspaceStore,
    runPending,
    elapsedClockMs,
    getPresetText,
    terminalStore,
    appStore,
    refreshBoard,
  }
}
