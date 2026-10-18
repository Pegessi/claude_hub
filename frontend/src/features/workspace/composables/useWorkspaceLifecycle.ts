import {
  watch,
  onMounted,
  onUnmounted,
} from 'vue'
import {
  resetDraftAttachments,
} from '@/utils/draftAttachments'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'
import type { useWorkspaceTaskActions } from './useWorkspaceTaskActions'
import type { useWorkspaceNavigation } from './useWorkspaceNavigation'
import type { useWorkspaceArtifacts } from './useWorkspaceArtifacts'
import type { useWorkspaceEditor } from './useWorkspaceEditor'

type WorkspaceLifecycleInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'tasks'
  | 'selectedTaskId'
  | 'workspaceStore'
  | 'activeWorkspaceId'
  | 'selectedWorkspaceId'
  | 'workspaceSessionView'
  | 'taskCreateFlow'
  | 'terminalStore'
  | 'refreshBoard'
  | 'elapsedClockMs'
>
  & Pick<ReturnType<typeof useWorkspaceTaskActions>,
  'closeTaskDetail'
  | 'detailAttachments'
>
  & Pick<ReturnType<typeof useWorkspaceNavigation>,
  'handleWorkspaceDocumentPointerDown'
>
  & Pick<ReturnType<typeof useWorkspaceArtifacts>,
  'handleLightboxKeydown'
>
  & Pick<ReturnType<typeof useWorkspaceEditor>,
  'fetchRemoteProfiles'
>

export function useWorkspaceLifecycle(inputs: WorkspaceLifecycleInputs) {
  const {
    tasks,
    selectedTaskId,
    workspaceStore,
    activeWorkspaceId,
    selectedWorkspaceId,
    workspaceSessionView,
    taskCreateFlow,
    terminalStore,
    refreshBoard,
    elapsedClockMs,
    closeTaskDetail,
    detailAttachments,
    handleWorkspaceDocumentPointerDown,
    handleLightboxKeydown,
    fetchRemoteProfiles,
  } = inputs

  let boardPollTimer: number | null = null

  let elapsedClockTimer: number | null = null

  watch(tasks, value => {
    if (selectedTaskId.value && !value.some(task => task.id === selectedTaskId.value)) {
      closeTaskDetail()
    }
  })

  // Hydrate the detail panel's full report history on demand. The board payload
  // only carries the latest report per task, so opening a task fetches its
  // complete history; switching away drops the prior task's cache.
  watch(selectedTaskId, (taskId, prevTaskId) => {
    if (prevTaskId && prevTaskId !== taskId) {
      workspaceStore.clearTaskReports(prevTaskId)
    }
    if (taskId && activeWorkspaceId.value) {
      workspaceStore.fetchTaskReports(activeWorkspaceId.value, taskId).catch(() => {
        // Error state is owned by the workspace store.
      })
    }
  })

  watch(activeWorkspaceId, value => {
    if (value) void workspaceStore.fetchTaskCapabilities(value).catch(() => {})
  }, { immediate: true })

  watch(activeWorkspaceId, value => {
    selectedWorkspaceId.value = value || ''
    workspaceSessionView.value = 'agents'
    taskCreateFlow.onWorkspaceChanged(value)
    workspaceStore.clearTaskReports()
    closeTaskDetail()
  })

  onMounted(async () => {
    document.addEventListener('pointerdown', handleWorkspaceDocumentPointerDown)
    document.addEventListener('keydown', handleLightboxKeydown)
    await fetchRemoteProfiles()
    await workspaceStore.fetchWorkspaces()
    terminalStore.startAgentStatusPolling()
    boardPollTimer = window.setInterval(refreshBoard, 2500)
    elapsedClockTimer = window.setInterval(() => {
      elapsedClockMs.value = Date.now()
    }, 30000)
  })

  onUnmounted(() => {
    document.removeEventListener('pointerdown', handleWorkspaceDocumentPointerDown)
    document.removeEventListener('keydown', handleLightboxKeydown)
    if (boardPollTimer !== null) {
      window.clearInterval(boardPollTimer)
      boardPollTimer = null
    }
    if (elapsedClockTimer !== null) {
      window.clearInterval(elapsedClockTimer)
      elapsedClockTimer = null
    }
    taskCreateFlow.dispose()
    resetDraftAttachments(detailAttachments.value)
    terminalStore.stopAgentStatusPolling()
  })
}
