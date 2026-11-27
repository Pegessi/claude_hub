import {
  ref,
  reactive,
} from 'vue'
import {
  type DraftAttachment,
  resetDraftAttachments,
  serializeDraftAttachments,
} from '@/utils/draftAttachments'
import {
  type WorkspaceTask,
  type WorkspaceTaskUpdate,
  type WorkspaceTaskStatus,
} from '@/types'
import {
  DEFAULT_ABORT_REASON,
  resolveAbortReason,
} from '@/utils/taskAbort'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'
import type { useWorkspaceArtifacts } from './useWorkspaceArtifacts'
import type { useWorkspaceTaskPresentation } from './useWorkspaceTaskPresentation'

type WorkspaceTaskActionsInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'selectedTaskId'
  | 'runPending'
  | 'taskActionKey'
  | 'workspaceStore'
  | 'terminalStore'
  | 'selectedTask'
>
  & Pick<ReturnType<typeof useWorkspaceArtifacts>,
  'expandedArtifactKey'
  | 'closeMarkdownPreviewModal'
>
  & Pick<ReturnType<typeof useWorkspaceTaskPresentation>,
  'canEditTask'
  | 'canUseManagedTaskActions'
  | 'selectedSession'
>

export function useWorkspaceTaskActions(inputs: WorkspaceTaskActionsInputs) {
  const {
    selectedTaskId,
    runPending,
    taskActionKey,
    workspaceStore,
    terminalStore,
    selectedTask,
    expandedArtifactKey,
    closeMarkdownPreviewModal,
    canEditTask,
    canUseManagedTaskActions,
    selectedSession,
  } = inputs

  const detailMessage = ref('')

  const detailAttachments = ref<DraftAttachment[]>([])

  const isDetailActionsExpanded = ref(false)

  const showEditTaskModal = ref(false)

  const editingTaskId = ref<string | null>(null)

  const initialEditContract = reactive({
    goal: null as string | null,
    done_when: [] as string[],
    out_of_scope: [] as string[],
  })

  const editTaskForm = reactive({
    title: '',
    prompt: '',
    goal: '',
    done_when: '',
    out_of_scope: '',
    related_task_id: '',
  })

  function isInteractiveElement(target: EventTarget | null) {
    return target instanceof HTMLElement && Boolean(target.closest(
      'button, a, input, select, textarea, summary, details, label'
    ))
  }

  function selectTask(event: MouseEvent, taskId: string) {
    if (isInteractiveElement(event.target)) return
    if (selectedTaskId.value !== taskId) {
      detailMessage.value = ''
      resetDraftAttachments(detailAttachments.value)
      detailAttachments.value = []
      isDetailActionsExpanded.value = false
      expandedArtifactKey.value = null
    }
    selectedTaskId.value = taskId
  }

  function toggleDetailActions() {
    isDetailActionsExpanded.value = !isDetailActionsExpanded.value
  }

  function closeTaskDetail() {
    selectedTaskId.value = null
    detailMessage.value = ''
    resetDraftAttachments(detailAttachments.value)
    detailAttachments.value = []
    isDetailActionsExpanded.value = false
    expandedArtifactKey.value = null
    closeMarkdownPreviewModal()
  }

  function normalizeLines(raw: string): string[] {
    return raw.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  }

  function sameStringList(a: string[], b: string[]): boolean {
    return a.length === b.length && a.every((val, idx) => val === b[idx])
  }

  function openEditTaskModal(task: WorkspaceTask) {
    if (!canEditTask(task)) return
    const packet = task.goal_packet
    const effectiveGoal = (task.goal ?? packet?.objective ?? '').trim()
    const effectiveDoneWhen = (
      task.done_when && task.done_when.length > 0
        ? task.done_when
        : (packet?.acceptance_criteria ?? [])
    ).map(item => item.trim()).filter(Boolean)
    const effectiveOutOfScope = (
      task.out_of_scope && task.out_of_scope.length > 0
        ? task.out_of_scope
        : (packet?.out_of_scope ?? [])
    ).map(item => item.trim()).filter(Boolean)

    editingTaskId.value = task.id
    editTaskForm.title = task.title
    editTaskForm.prompt = task.prompt
    editTaskForm.goal = effectiveGoal
    editTaskForm.done_when = effectiveDoneWhen.join('\n')
    editTaskForm.out_of_scope = effectiveOutOfScope.join('\n')
    initialEditContract.goal = effectiveGoal || null
    initialEditContract.done_when = [...effectiveDoneWhen]
    initialEditContract.out_of_scope = [...effectiveOutOfScope]
    editTaskForm.related_task_id = task.related_task_id || ''
    showEditTaskModal.value = true
  }

  function closeEditTaskModal() {
    showEditTaskModal.value = false
    editingTaskId.value = null
    editTaskForm.title = ''
    editTaskForm.prompt = ''
    editTaskForm.goal = ''
    editTaskForm.done_when = ''
    editTaskForm.out_of_scope = ''
    initialEditContract.goal = null
    initialEditContract.done_when = []
    initialEditContract.out_of_scope = []
    editTaskForm.related_task_id = ''
  }

  async function handleUpdateTask() {
    const taskId = editingTaskId.value
    if (
      !taskId ||
      !editTaskForm.title.trim() ||
      !editTaskForm.prompt.trim()
    ) {
      return
    }
    await runPending(taskActionKey('edit', taskId), async () => {
      const nextGoal = editTaskForm.goal.trim() || null
      const nextDoneWhen = normalizeLines(editTaskForm.done_when)
      const nextOutOfScope = normalizeLines(editTaskForm.out_of_scope)
      const contractChanged = (
        nextGoal !== initialEditContract.goal
        || !sameStringList(nextDoneWhen, initialEditContract.done_when)
        || !sameStringList(nextOutOfScope, initialEditContract.out_of_scope)
      )
      const payload: WorkspaceTaskUpdate = {
        title: editTaskForm.title.trim(),
        prompt: editTaskForm.prompt.trim(),
        // The update API uses an empty string to clear; null means unchanged.
        related_task_id: editTaskForm.related_task_id,
      }
      if (contractChanged) {
        payload.goal = nextGoal
        payload.done_when = nextDoneWhen
        payload.out_of_scope = nextOutOfScope
      }
      await workspaceStore.updateTask(taskId, payload)
      closeEditTaskModal()
    })
  }

  async function startTask(task: WorkspaceTask) {
    if (!canUseManagedTaskActions(task)) return
    await runPending(taskActionKey('start', task.id), async () => {
      await workspaceStore.startTask(task.id)
      await terminalStore.fetchTabs({ force: true })
    })
  }

  async function sendDetailMessage() {
    if (!selectedTask.value || !canUseManagedTaskActions(selectedTask.value)) return
    if (
      !selectedTask.value ||
      !selectedSession.value ||
      (!detailMessage.value.trim() && detailAttachments.value.length === 0)
    ) {
      return
    }
    const task = selectedTask.value
    const session = selectedSession.value
    const message = detailMessage.value.trim()
    const attachments = serializeDraftAttachments(detailAttachments.value)
    await runPending(taskActionKey('send', task.id), async () => {
      if (task.status === 'review' || task.status === 'failed') {
        await workspaceStore.continueTask(task.id, { message, attachments })
      } else {
        await workspaceStore.sendMessage(session.id, message, attachments)
      }
      detailMessage.value = ''
      resetDraftAttachments(detailAttachments.value)
    })
  }

  async function markTask(taskId: string, status: WorkspaceTaskStatus) {
    await runPending(taskActionKey(`mark-${status}`, taskId), () =>
      workspaceStore.updateTaskStatus(taskId, status)
    )
  }

  async function runEphemeralSessionCleanup(taskId: string, workspaceId: string) {
    try {
      const result = await workspaceStore.cleanupTaskSession(taskId, workspaceId)
      if (result.action === 'deleted') {
        await terminalStore.fetchTabs({ force: true })
        return
      }
      const benignReasons = new Set([
        'session no longer exists',
        'task has no session_id',
        'session is not caller-owned ephemeral',
        'initiator task does not own managed sessions',
      ])
      if (result.action === 'skipped' && result.reason && !benignReasons.has(result.reason)) {
        workspaceStore.pushNotification({
          type: 'warning',
          message: `Task accepted, but temporary session cleanup was deferred (${result.reason}). Use "Release session" when idle.`,
          autoDismissMs: 9000,
        })
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'request failed'
      workspaceStore.pushNotification({
        type: 'warning',
        message: `Task accepted, but temporary session cleanup failed (${msg}). Use "Release session" to retry.`,
        autoDismissMs: 9000,
      })
    }
  }

  async function acceptAndCleanupTask(task: WorkspaceTask) {
    const shouldCleanupSession = Boolean(workspaceStore.sessionForTask(task)?.ephemeral)
    const originWorkspaceId = task.workspace_id
    await runPending(taskActionKey('mark-done', task.id), async () => {
      await workspaceStore.updateTaskStatus(task.id, 'done')
      if (shouldCleanupSession) {
        await runEphemeralSessionCleanup(task.id, originWorkspaceId)
      }
    })
  }

  async function cleanupCompletedTaskSession(task: WorkspaceTask) {
    await runPending(taskActionKey('cleanup-session', task.id), async () => {
      await runEphemeralSessionCleanup(task.id, task.workspace_id)
    })
  }

  async function requestReview(task: WorkspaceTask) {
    const message = window.prompt(
      'Tell the reviewer what to check:',
      detailMessage.value.trim(),
    )
    if (!message || !message.trim()) return
    detailMessage.value = ''
    await runPending(taskActionKey('request-review', task.id), () =>
      workspaceStore.requestTaskReview(task.id, { message: message.trim() })
    )
  }

  async function abortTask(task: WorkspaceTask) {
    const reason = window.prompt(
      `Abort task "${task.title}" and return it to todo? Enter a reason:`,
      DEFAULT_ABORT_REASON,
    )
    const auditReason = resolveAbortReason(reason)
    if (auditReason === null) return
    await runPending(taskActionKey('abort', task.id), async () => {
      await workspaceStore.abortTask(task.id, { reason: auditReason })
      if (selectedTaskId.value === task.id) {
        closeTaskDetail()
      }
    })
  }

  async function deleteTask(task: WorkspaceTask) {
    if (!canUseManagedTaskActions(task)) return
    const confirmed = window.confirm(`Delete task "${task.title}"?`)
    if (!confirmed) return
    await runPending(taskActionKey('delete', task.id), async () => {
      await workspaceStore.deleteTask(task.id)
      if (selectedTaskId.value === task.id) {
        closeTaskDetail()
      }
    })
  }

  return {
    selectTask,
    startTask,
    openEditTaskModal,
    markTask,
    acceptAndCleanupTask,
    cleanupCompletedTaskSession,
    requestReview,
    abortTask,
    deleteTask,
    isDetailActionsExpanded,
    toggleDetailActions,
    sendDetailMessage,
    detailAttachments,
    detailMessage,
    closeTaskDetail,
    showEditTaskModal,
    closeEditTaskModal,
    handleUpdateTask,
    editTaskForm,
    editingTaskId,
  }
}
