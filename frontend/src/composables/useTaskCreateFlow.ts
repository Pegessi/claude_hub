import { computed, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { usePendingActions } from '@/composables/usePendingActions'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import { writeClipboard } from '@/utils/clipboard'
import {
  handleAttachmentPaste, removeDraftAttachment, resetDraftAttachments,
  serializeDraftAttachments, type DraftAttachment,
} from '@/utils/draftAttachments'
import {
  createReporterKeyApplies, generateTaskCallId, generateTaskReporterKey,
  loadTaskCreateAttempt, removeTaskCreateAttempt, saveTaskCreateAttempt,
  saveTaskReporterKey, type StoredTaskCreateAttempt,
} from '@/utils/taskExecution'
import type {
  AutonomyPolicy, WorkspaceTask, WorkspaceTaskCapabilitiesStatus,
  WorkspaceTaskCreate, WorkspaceTaskExecutionComplexity,
  WorkspaceTaskExecutionControl, WorkspaceTaskMode,
} from '@/types'

export interface CreatedReporterRecovery {
  workspaceId: string
  task: WorkspaceTask
  key: string
  attempt: StoredTaskCreateAttempt
}

/** Parent-owned creation flow; hiding the dialog does not discard recovery state. */
export function useTaskCreateFlow() {
  const workspaceStore = useWorkspaceStore()
  const {
    activeWorkspaceId, isLoading, taskCapabilities, taskCapabilitiesWorkspaceId,
    taskCapabilitiesStatus, taskCapabilitiesError, workspaceAgents, tasks,
  } = storeToRefs(workspaceStore)
  const { isPending, runPending } = usePendingActions()
  let disposed = false

  const showTaskModal = ref(false)

  const taskModalWorkspaceId = ref<string | null>(null)

  const pendingTaskCreateAttempt = ref<StoredTaskCreateAttempt | null>(null)

  const createdReporterRecovery = ref<CreatedReporterRecovery | null>(null)

  const revealCreatedReporterKey = ref(false)

  let taskCreateUiGeneration = 0

  const taskModalCapabilitiesStatus = computed<WorkspaceTaskCapabilitiesStatus>(() => {
    const workspaceId = taskModalWorkspaceId.value
    if (!workspaceId || taskCapabilitiesWorkspaceId.value !== workspaceId) return 'idle'
    return taskCapabilitiesStatus.value
  })

  const taskCreateCapabilitiesReady = computed(() =>
    taskModalCapabilitiesStatus.value === 'supported' ||
    taskModalCapabilitiesStatus.value === 'unsupported',
  )

  const taskCreateSubmitBlocked = computed(() =>
    !taskCreateCapabilitiesReady.value ||
    (Boolean(pendingTaskCreateAttempt.value) &&
      taskModalCapabilitiesStatus.value !== 'supported'),
  )

  async function retryTaskCapabilities(): Promise<void> {
    const workspaceId = taskModalWorkspaceId.value ?? activeWorkspaceId.value
    if (!workspaceId || workspaceId !== activeWorkspaceId.value) return
    try {
      await workspaceStore.fetchTaskCapabilities(workspaceId)
    } catch {
      // Store owns the fixed error shown in this modal.
    }
  }

  const taskForm = reactive({
    execution_control: 'workspace' as WorkspaceTaskExecutionControl,
    title: '',
    prompt: '',
    task_mode: 'reviewed' as WorkspaceTaskMode,
    execution_complexity: 'auto' as WorkspaceTaskExecutionComplexity,
    max_iterations: 3,
    evaluation_strictness: 'balanced' as AutonomyPolicy['evaluation_strictness'],
    allow_web_research: false,
    require_artifact_review: false,
    session_id: '',
    clear_context: false,
    related_task_id: '',
    attachments: [] as DraftAttachment[],
  })

  function removeFrozenCreateAttempt(attempt: StoredTaskCreateAttempt): boolean {
    const requestKey = attempt.payload.request_key
    return typeof requestKey === 'string' && removeTaskCreateAttempt(
      attempt.workspace_id, { expectedRequestKey: requestKey },
    )
  }

  function taskCreateActionKey(workspaceId = taskModalWorkspaceId.value): string {
    return `task:create:${workspaceId || 'none'}`
  }

  function sameCreateUi(workspaceId: string, generation: number): boolean {
    return !disposed && showTaskModal.value &&
      taskModalWorkspaceId.value === workspaceId &&
      taskCreateUiGeneration === generation
  }

  function samePendingCreate(attempt: StoredTaskCreateAttempt): boolean {
    return pendingTaskCreateAttempt.value?.workspace_id === attempt.workspace_id &&
      pendingTaskCreateAttempt.value.payload.request_key === attempt.payload.request_key
  }

  function openTaskModal() {
    const workspaceId = activeWorkspaceId.value
    if (!workspaceId) return
    taskCreateUiGeneration += 1
    taskModalWorkspaceId.value = workspaceId
    if (
      taskCapabilitiesWorkspaceId.value !== workspaceId ||
      taskModalCapabilitiesStatus.value === 'idle' ||
      taskModalCapabilitiesStatus.value === 'error'
    ) {
      void retryTaskCapabilities()
    }
    revealCreatedReporterKey.value = false

    if (createdReporterRecovery.value?.workspaceId === workspaceId) {
      pendingTaskCreateAttempt.value = loadTaskCreateAttempt(workspaceId)
    } else {
      createdReporterRecovery.value = null
      pendingTaskCreateAttempt.value = loadTaskCreateAttempt(workspaceId)
      if (!pendingTaskCreateAttempt.value) resetTaskForm()
    }
    showTaskModal.value = true
  }

  function closeTaskModal() {
    showTaskModal.value = false
    taskCreateUiGeneration += 1
    resetDraftAttachments(taskForm.attachments)
    // Persisted attempts and a same-page credential recovery deliberately remain.
  }

  function discardPendingTaskCreateAttempt() {
    const workspaceId = taskModalWorkspaceId.value
    if (!workspaceId || isPending(taskCreateActionKey(workspaceId))) return
    const attempt = pendingTaskCreateAttempt.value
    if (attempt && attempt.workspace_id === workspaceId) removeFrozenCreateAttempt(attempt)
    pendingTaskCreateAttempt.value = null
    createdReporterRecovery.value = null
    revealCreatedReporterKey.value = false
  }

  function handleTaskFormMutation() {
    if (pendingTaskCreateAttempt.value) discardPendingTaskCreateAttempt()
  }

  async function refreshCreatedTaskBoard(
    workspaceId: string,
    notify: boolean,
  ): Promise<void> {
    try {
      await workspaceStore.fetchBoard(workspaceId)
    } catch {
      if (notify) {
        workspaceStore.pushNotification({
          type: 'error',
          message: 'The Task was created, but the board could not be refreshed.',
          autoDismissMs: 10_000,
        })
      }
    }
  }

  async function copyCreatedReporterCredential() {
    const recovery = createdReporterRecovery.value
    if (!recovery) return
    try {
      await writeClipboard(recovery.key)
    } catch {
      workspaceStore.pushNotification({
        type: 'error',
        message: 'Could not copy the reporter credential. Show it and copy it manually.',
        autoDismissMs: 10_000,
      })
    }
  }

  function persistCreatedReporterCredential() {
    const recovery = createdReporterRecovery.value
    if (!recovery || !createReporterKeyApplies(recovery.workspaceId, recovery.task)) return
    if (!saveTaskReporterKey(recovery.workspaceId, recovery.task, recovery.key)) return

    removeFrozenCreateAttempt(recovery.attempt)
    if (taskModalWorkspaceId.value === recovery.workspaceId) {
      pendingTaskCreateAttempt.value = null
      createdReporterRecovery.value = null
      revealCreatedReporterKey.value = false
      resetTaskForm()
      showTaskModal.value = false
      taskCreateUiGeneration += 1
    }
  }

  async function handleCreateTask() {
    const workspaceId = taskModalWorkspaceId.value
    if (!workspaceId || workspaceId !== activeWorkspaceId.value) return
    if (createdReporterRecovery.value) return
    if (taskCreateSubmitBlocked.value) return
    if (
      !pendingTaskCreateAttempt.value &&
      (!taskForm.title.trim() ||
        (!taskForm.prompt.trim() && taskForm.attachments.length === 0))
    ) {
      return
    }

    const generation = taskCreateUiGeneration
    await runPending(taskCreateActionKey(workspaceId), async () => {
      let attempt = pendingTaskCreateAttempt.value
      if (attempt && attempt.workspace_id !== workspaceId) {
        workspaceStore.pushNotification({
          type: 'error',
          message: 'The saved Task request belongs to another Workspace.',
          autoDismissMs: 10_000,
        })
        return
      }

      if (!attempt) {
        const autonomyPolicy: AutonomyPolicy | null = taskForm.task_mode === 'autonomous'
          ? {
              max_iterations: Math.max(1, Number(taskForm.max_iterations) || 3),
              evaluation_strictness: taskForm.evaluation_strictness,
              allow_web_research: taskForm.allow_web_research,
              require_artifact_review: taskForm.require_artifact_review,
              human_checkpoint_policy: 'final_only',
              allowed_agent_types: [],
              stop_on_repeated_failure: true,
            }
          : null
        const hasTaskContract = taskModalCapabilitiesStatus.value === 'supported'
        const recordOnly = hasTaskContract && taskForm.execution_control === 'initiator'
        if (
          recordOnly &&
          !capabilities.value?.supported_execution_controls.includes('initiator')
        ) {
          workspaceStore.pushNotification({
            type: 'error',
            message: 'This Hub does not support initiator-managed Tasks.',
            autoDismissMs: 10_000,
          })
          return
        }

        const payload: WorkspaceTaskCreate = {
          ...(hasTaskContract
            ? {
                request_key: generateTaskCallId(),
                execution_control: taskForm.execution_control,
                source: { kind: 'human', tab_id: null, agent_id: null },
              }
            : {}),
          ...(recordOnly ? { reporter_key: generateTaskReporterKey() } : {}),
          title: taskForm.title.trim(),
          prompt: taskForm.prompt.trim(),
          task_mode: taskForm.task_mode,
          execution_complexity: taskForm.execution_complexity,
          autonomy_policy: autonomyPolicy,
          ...(recordOnly
            ? {}
            : {
                session_id: taskForm.session_id || null,
                clear_context: taskForm.clear_context || null,
                related_task_id: taskForm.related_task_id || null,
              }),
          attachments: serializeDraftAttachments(taskForm.attachments),
        }

        if (hasTaskContract) {
          attempt = { workspace_id: workspaceId, payload }
          try {
            saveTaskCreateAttempt(attempt)
          } catch {
            workspaceStore.pushNotification({
              type: 'error',
              message: 'This Task could not be staged safely for retry. Reduce large attachments and try again.',
              autoDismissMs: 10_000,
            })
            return
          }
          if (sameCreateUi(workspaceId, generation)) {
            pendingTaskCreateAttempt.value = attempt
          }
        } else {
          // The compatibility path is intentionally not retryable: the old API has
          // no request_key contract.
          attempt = { workspace_id: workspaceId, payload }
        }
      }

      const frozenAttempt = attempt
      let created: WorkspaceTask
      try {
        created = await workspaceStore.createTask(
          frozenAttempt.payload,
          frozenAttempt.workspace_id,
        )
      } catch {
        // A new-contract attempt stays frozen for an explicit identical recovery.
        return
      }

      const reporterKey = frozenAttempt.payload.execution_control === 'initiator'
        ? frozenAttempt.payload.reporter_key
        : undefined

      if (reporterKey) {
        if (!createReporterKeyApplies(frozenAttempt.workspace_id, created)) {
          removeFrozenCreateAttempt(frozenAttempt)
          if (sameCreateUi(workspaceId, generation) && samePendingCreate(frozenAttempt)) {
            pendingTaskCreateAttempt.value = null
            resetTaskForm()
            showTaskModal.value = false
            taskCreateUiGeneration += 1
          }
          await refreshCreatedTaskBoard(
            frozenAttempt.workspace_id,
            sameCreateUi(workspaceId, generation),
          )
          workspaceStore.pushNotification({
            type: 'warning',
            message: 'The recovered Task had already moved to another execution epoch. The original reporter credential was not saved.',
            autoDismissMs: 10_000,
          })
          return
        }

        const persisted = saveTaskReporterKey(
          frozenAttempt.workspace_id,
          created,
          reporterKey,
        )
        if (!persisted) {
          if (sameCreateUi(workspaceId, generation) && samePendingCreate(frozenAttempt)) {
            createdReporterRecovery.value = {
              workspaceId: frozenAttempt.workspace_id,
              task: created,
              key: reporterKey,
              attempt: frozenAttempt,
            }
            revealCreatedReporterKey.value = false
          }
          await refreshCreatedTaskBoard(
            frozenAttempt.workspace_id,
            sameCreateUi(workspaceId, generation),
          )
          return
        }
      }

      removeFrozenCreateAttempt(frozenAttempt)
      await refreshCreatedTaskBoard(
        frozenAttempt.workspace_id,
        sameCreateUi(workspaceId, generation),
      )

      if (!sameCreateUi(workspaceId, generation)) return
      if (samePendingCreate(frozenAttempt)) pendingTaskCreateAttempt.value = null
      createdReporterRecovery.value = null
      revealCreatedReporterKey.value = false
      resetTaskForm()
      showTaskModal.value = false
      taskCreateUiGeneration += 1
    })
  }

  function resetTaskForm() {
    taskForm.execution_control = 'workspace'
    taskForm.title = ''
    taskForm.prompt = ''
    taskForm.task_mode = 'reviewed'
    taskForm.execution_complexity = 'auto'
    taskForm.max_iterations = 3
    taskForm.evaluation_strictness = 'balanced'
    taskForm.allow_web_research = false
    taskForm.require_artifact_review = false
    taskForm.session_id = ''
    taskForm.clear_context = false
    taskForm.related_task_id = ''
    resetDraftAttachments(taskForm.attachments)
  }

  const capabilities = computed(() =>
    taskModalWorkspaceId.value && taskCapabilitiesWorkspaceId.value === taskModalWorkspaceId.value
      ? taskCapabilities.value : null,
  )
  const capabilitiesError = computed(() =>
    taskModalWorkspaceId.value && taskCapabilitiesWorkspaceId.value === taskModalWorkspaceId.value
      ? taskCapabilitiesError.value : null,
  )
  const submitting = computed(() => isPending(taskCreateActionKey()))
  const targetAvailable = computed(() =>
    Boolean(taskModalWorkspaceId.value && taskModalWorkspaceId.value === activeWorkspaceId.value),
  )
  const submitDisabled = computed(() =>
    taskCreateSubmitBlocked.value || !targetAvailable.value || isLoading.value ||
    (!pendingTaskCreateAttempt.value &&
      (!taskForm.title.trim() || (!taskForm.prompt.trim() && taskForm.attachments.length === 0))),
  )

  function open(workspaceId: string): void {
    if (!disposed && workspaceId === activeWorkspaceId.value) openTaskModal()
  }

  function onWorkspaceChanged(value: string | null): void {
    if (showTaskModal.value && taskModalWorkspaceId.value !== value) {
      showTaskModal.value = false
      taskModalWorkspaceId.value = null
      pendingTaskCreateAttempt.value = null
      createdReporterRecovery.value = null
      revealCreatedReporterKey.value = false
      taskCreateUiGeneration += 1
      resetTaskForm()
    }
  }

  function dispose(): void {
    disposed = true
    showTaskModal.value = false
    taskCreateUiGeneration += 1
    resetDraftAttachments(taskForm.attachments)
  }

  return {
    visible: showTaskModal, workspaceId: taskModalWorkspaceId, form: taskForm,
    pendingAttempt: pendingTaskCreateAttempt, reporterRecovery: createdReporterRecovery,
    revealReporterKey: revealCreatedReporterKey, capabilities,
    capabilitiesStatus: taskModalCapabilitiesStatus, capabilitiesError,
    submitBlocked: taskCreateSubmitBlocked, submitDisabled, submitting, targetAvailable,
    workspaceAgents, tasks, open, close: closeTaskModal, onWorkspaceChanged, dispose,
    retryCapabilities: retryTaskCapabilities,
    discardPendingAttempt: discardPendingTaskCreateAttempt,
    onFormMutation: handleTaskFormMutation,
    pasteAttachments: (event: ClipboardEvent) => handleAttachmentPaste(event, taskForm.attachments),
    removeAttachment: (attachment: DraftAttachment) => removeDraftAttachment(taskForm.attachments, attachment),
    toggleReporterKey: () => { revealCreatedReporterKey.value = !revealCreatedReporterKey.value },
    copyReporterCredential: copyCreatedReporterCredential,
    persistReporterCredential: persistCreatedReporterCredential,
    submit: handleCreateTask,
  }
}

export type TaskCreateFlow = ReturnType<typeof useTaskCreateFlow>
export type TaskCreateForm = TaskCreateFlow['form']
