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
import type { TaskStartOptions } from '../types'
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

  const startOptions = reactive<Record<string, TaskStartOptions>>({})

  const editTaskForm = reactive({
    title: '',
    prompt: '',
    session_id: '',
    related_task_id: '',
    clear_context: false,
  })

  function startOptionsFor(task: WorkspaceTask): TaskStartOptions {
    if (!startOptions[task.id]) {
      startOptions[task.id] = {
        target_session_id: task.session_id || '',
        related_task_id: task.related_task_id || '',
        clear_context: Boolean(task.clear_context),
      }
    }
    return startOptions[task.id]
  }

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
    isDetailActionsExpanded.value = false
    expandedArtifactKey.value = null
    closeMarkdownPreviewModal()
  }

  function openEditTaskModal(task: WorkspaceTask) {
    if (!canEditTask(task)) return
    editingTaskId.value = task.id
    editTaskForm.title = task.title
    editTaskForm.prompt = task.prompt
    editTaskForm.session_id = task.session_id || ''
    editTaskForm.related_task_id = task.related_task_id || ''
    editTaskForm.clear_context = Boolean(task.clear_context)
    showEditTaskModal.value = true
  }

  function closeEditTaskModal() {
    showEditTaskModal.value = false
    editingTaskId.value = null
    editTaskForm.title = ''
    editTaskForm.prompt = ''
    editTaskForm.session_id = ''
    editTaskForm.related_task_id = ''
    editTaskForm.clear_context = false
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
      const payload: WorkspaceTaskUpdate = {
        title: editTaskForm.title.trim(),
        prompt: editTaskForm.prompt.trim(),
        session_id: editTaskForm.session_id || null,
        related_task_id: editTaskForm.related_task_id || null,
        clear_context: editTaskForm.clear_context ? true : null,
      }
      await workspaceStore.updateTask(taskId, payload)
      // Clear cached start options so they re-read from the updated task
      delete startOptions[taskId]
      closeEditTaskModal()
    })
  }

  async function startTask(task: WorkspaceTask) {
    if (!canUseManagedTaskActions(task)) return
    await runPending(taskActionKey('start', task.id), async () => {
      const options = startOptionsFor(task)
      await workspaceStore.startTask(task.id, {
        target_session_id: options.target_session_id || null,
        related_task_id: options.related_task_id || null,
        clear_context: options.clear_context ? true : null,
      })
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
    startOptionsFor,
    startTask,
    openEditTaskModal,
    markTask,
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
