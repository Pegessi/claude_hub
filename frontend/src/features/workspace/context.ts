import {
  inject,
  provide,
  type InjectionKey,
} from 'vue'
import type { useWorkspaceRuntime } from './composables/useWorkspaceRuntime'
import type { useWorkspaceEditor } from './composables/useWorkspaceEditor'
import type { useWorkspaceAgentManager } from './composables/useWorkspaceAgentManager'
import type { useWorkspaceDirectoryBrowser } from './composables/useWorkspaceDirectoryBrowser'
import type { useWorkspaceArtifacts } from './composables/useWorkspaceArtifacts'
import type { useWorkspaceTaskPresentation } from './composables/useWorkspaceTaskPresentation'
import type { useWorkspaceTaskActions } from './composables/useWorkspaceTaskActions'
import type { useWorkspaceLessons } from './composables/useWorkspaceLessons'
import type { useWorkspaceNavigation } from './composables/useWorkspaceNavigation'

type WorkspaceRuntimeContext = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'activeWorkspace'
  | 'mobileWorkspaceSummary'
  | 'isPending'
  | 'selectedWorkspaceId'
  | 'handleWorkspaceChange'
  | 'workspaces'
  | 'activeWorkspaceId'
  | 'openTaskModal'
  | 'colorScheme'
  | 'error'
  | 'dismissWorkspaceErrors'
  | 'workspaceAgents'
  | 'reviewerAgents'
  | 'temporaryReviewers'
  | 'tasksByStatus'
  | 'activeFeedbackLessons'
  | 'columns'
  | 'taskCountByStatus'
  | 'workspaceSessionView'
  | 'reviewerSessions'
  | 'refreshAgentStatuses'
  | 'visibleWorkspaceSessions'
  | 'sessionActionKey'
  | 'openSession'
  | 'agentRuntimeStatus'
  | 'agentRoleLabel'
  | 'agentRuntimeDetail'
  | 'agentTaskLabel'
  | 'agentRuntimeText'
  | 'canSwitchAgentEnv'
  | 'canDeleteAgent'
  | 'agentDeleteTitle'
  | 'agentActionKey'
  | 'deleteAgent'
  | 'visibleWorkspaceSessionsEmptyText'
  | 'boardLoading'
  | 'isMobileColumnCollapsed'
  | 'toggleMobileColumn'
  | 'selectedTaskId'
  | 'latestReportForTask'
  | 'tasks'
  | 'taskActionKey'
  | 'sessionForTask'
  | 'boardTasksPagination'
  | 'boardOlderTasksRemaining'
  | 'boardLoadMoreLoading'
  | 'handleLoadOlderTasks'
  | 'boardLoadMoreError'
  | 'persistedAttachmentMeta'
  | 'reviewerTitle'
  | 'board'
  | 'taskCapabilities'
  | 'formatTime'
  | 'selectedTask'
  | 'isLoading'
  | 'dispatcherAgent'
  | 'residentAgent'
  | 'taskTitle'
  | 'envPresets'
  | 'workspaceNotifications'
  | 'dismissToast'
>
const runtimeKey: InjectionKey<WorkspaceRuntimeContext> = Symbol('WorkspaceRuntimeContext')
export function provideWorkspaceRuntimeContext(value: WorkspaceRuntimeContext) { provide(runtimeKey, value) }
export function useWorkspaceRuntimeContext(): WorkspaceRuntimeContext {
  const value = inject(runtimeKey)
  if (!value) throw new Error('WorkspaceRuntimeContext requires WorkspacePage')
  return value
}

type WorkspaceEditorContext = Pick<ReturnType<typeof useWorkspaceEditor>,
  'openWorkspaceModal'
  | 'openEditWorkspaceModal'
  | 'isResidentAgent'
  | 'isResidentPaused'
  | 'isResidentMaster'
  | 'residentLastRunLabel'
  | 'residentNextRunLabel'
  | 'latestResidentReport'
  | 'toggleResidentPaused'
  | 'residentRunPending'
  | 'handleRunResidentNow'
  | 'showWorkspaceModal'
  | 'closeWorkspaceModal'
  | 'workspaceModalMode'
  | 'handleSubmitWorkspace'
  | 'workspaceForm'
  | 'remoteProfilesLoading'
  | 'remoteProfiles'
  | 'selectedRemoteProfile'
  | 'residentSummaryLabel'
  | 'openResidentAgentModal'
  | 'handleDeleteWorkspace'
  | 'showResidentAgentModal'
  | 'closeResidentAgentModal'
  | 'enabledPeriodicTaskCount'
  | 'removePeriodicTask'
  | 'addPeriodicTask'
  | 'handleResidentTargetChange'
  | 'isResidentCreateMode'
  | 'residentExists'
  | 'handleDeleteResident'
  | 'handleToggleResidentPause'
  | 'handleCreateResident'
  | 'handleSaveResidentAndRunNow'
>
const editorKey: InjectionKey<WorkspaceEditorContext> = Symbol('WorkspaceEditorContext')
export function provideWorkspaceEditorContext(value: WorkspaceEditorContext) { provide(editorKey, value) }
export function useWorkspaceEditorContext(): WorkspaceEditorContext {
  const value = inject(editorKey)
  if (!value) throw new Error('WorkspaceEditorContext requires WorkspacePage')
  return value
}

type WorkspaceAgentManagerContext = Pick<ReturnType<typeof useWorkspaceAgentManager>,
  'openAgentOptionsModal'
  | 'openSwitchEnvModal'
  | 'showAgentOptionsModal'
  | 'closeAgentOptionsModal'
  | 'agentManagerView'
  | 'agentManagerSessions'
  | 'agentManagerEmptyText'
  | 'handleCreateAdvancedAgent'
  | 'agentOptionsForm'
  | 'handleAgentSurfaceChange'
  | 'handleAgentTargetChange'
  | 'selectedAgentRemoteProfile'
  | 'isAgentOptionsCreateDisabled'
  | 'showAgentFileBrowser'
  | 'switchEnvForm'
  | 'showSwitchEnvManager'
  | 'closeSwitchEnvPresetManager'
  | 'showSwitchEnvModal'
  | 'closeSwitchEnvModal'
  | 'switchEnvAgent'
  | 'handleSwitchEnv'
  | 'applySwitchEnvPreset'
  | 'openSwitchEnvPresetManager'
>
const agentsKey: InjectionKey<WorkspaceAgentManagerContext> = Symbol('WorkspaceAgentManagerContext')
export function provideWorkspaceAgentManagerContext(value: WorkspaceAgentManagerContext) { provide(agentsKey, value) }
export function useWorkspaceAgentManagerContext(): WorkspaceAgentManagerContext {
  const value = inject(agentsKey)
  if (!value) throw new Error('WorkspaceAgentManagerContext requires WorkspacePage')
  return value
}

type WorkspaceDirectoryBrowserContext = Pick<ReturnType<typeof useWorkspaceDirectoryBrowser>,
  'openAgentDirectoryBrowser'
  | 'browserPlacement'
  | 'navigateAgentBrowserHome'
  | 'agentBrowserParentPath'
  | 'navigateAgentBrowserParent'
  | 'agentBrowserPathInput'
  | 'loadAgentDirectory'
  | 'refreshAgentDirectory'
  | 'agentBrowserItems'
  | 'handleAgentFileItemClick'
  | 'agentBrowserLoading'
  | 'agentBrowserError'
  | 'selectAgentCurrentDirectory'
>
const directoryKey: InjectionKey<WorkspaceDirectoryBrowserContext> = Symbol('WorkspaceDirectoryBrowserContext')
export function provideWorkspaceDirectoryBrowserContext(value: WorkspaceDirectoryBrowserContext) { provide(directoryKey, value) }
export function useWorkspaceDirectoryBrowserContext(): WorkspaceDirectoryBrowserContext {
  const value = inject(directoryKey)
  if (!value) throw new Error('WorkspaceDirectoryBrowserContext requires WorkspacePage')
  return value
}

type WorkspaceArtifactsContext = Pick<ReturnType<typeof useWorkspaceArtifacts>,
  'openMarkdownPreviewModal'
  | 'openImageLightbox'
  | 'isArtifactPreviewLoading'
  | 'toggleArtifactPreview'
  | 'artifactPreviewButtonLabel'
  | 'expandedArtifactKey'
  | 'artifactPreviewErrors'
  | 'artifactPreviews'
  | 'isMarkdownDocumentPreviewLoading'
  | 'toggleMarkdownDocumentPreview'
  | 'markdownDocumentPreviewButtonLabel'
  | 'markdownPreviewModalPath'
  | 'closeMarkdownPreviewModal'
  | 'markdownPreviewModalError'
  | 'markdownPreviewModalLoading'
  | 'markdownPreviewModalContent'
  | 'imageLightboxUrl'
  | 'closeImageLightbox'
  | 'imageLightboxAlt'
>
const artifactsKey: InjectionKey<WorkspaceArtifactsContext> = Symbol('WorkspaceArtifactsContext')
export function provideWorkspaceArtifactsContext(value: WorkspaceArtifactsContext) { provide(artifactsKey, value) }
export function useWorkspaceArtifactsContext(): WorkspaceArtifactsContext {
  const value = inject(artifactsKey)
  if (!value) throw new Error('WorkspaceArtifactsContext requires WorkspacePage')
  return value
}

type WorkspaceTaskPresentationContext = Pick<ReturnType<typeof useWorkspaceTaskPresentation>,
  'activeReviewBadge'
  | 'canUseManagedTaskActions'
  | 'reportMessageForLang'
  | 'canEditTask'
  | 'canMarkDoneTask'
  | 'canRequestReviewTask'
  | 'canAbortTask'
  | 'taskModeLabel'
  | 'executionComplexityLabel'
  | 'selectedSession'
  | 'reviewStatusLabel'
  | 'autonomousRunPhaseLabel'
  | 'formatAutonomousScore'
  | 'taskTotalElapsedLabel'
  | 'taskWorkingElapsedLabel'
  | 'latestSelectedReportAgeLabel'
  | 'selectedReports'
  | 'hasBilingualReport'
  | 'reportLang'
  | 'setReportLang'
  | 'selectedProgressTimeline'
  | 'isLatestSelectedReport'
  | 'reportMessagePreview'
  | 'reportElapsedTitle'
  | 'reportElapsedLabel'
  | 'selectedMarkdownDocuments'
  | 'selectedTaskSendKey'
>
const presentationKey: InjectionKey<WorkspaceTaskPresentationContext> = Symbol('WorkspaceTaskPresentationContext')
export function provideWorkspaceTaskPresentationContext(value: WorkspaceTaskPresentationContext) { provide(presentationKey, value) }
export function useWorkspaceTaskPresentationContext(): WorkspaceTaskPresentationContext {
  const value = inject(presentationKey)
  if (!value) throw new Error('WorkspaceTaskPresentationContext requires WorkspacePage')
  return value
}

type WorkspaceTaskActionsContext = Pick<ReturnType<typeof useWorkspaceTaskActions>,
  'selectTask'
  | 'startOptionsFor'
  | 'startTask'
  | 'openEditTaskModal'
  | 'markTask'
  | 'requestReview'
  | 'abortTask'
  | 'deleteTask'
  | 'isDetailActionsExpanded'
  | 'toggleDetailActions'
  | 'sendDetailMessage'
  | 'detailAttachments'
  | 'detailMessage'
  | 'closeTaskDetail'
  | 'showEditTaskModal'
  | 'closeEditTaskModal'
  | 'handleUpdateTask'
  | 'editTaskForm'
  | 'editingTaskId'
>
const tasksKey: InjectionKey<WorkspaceTaskActionsContext> = Symbol('WorkspaceTaskActionsContext')
export function provideWorkspaceTaskActionsContext(value: WorkspaceTaskActionsContext) { provide(tasksKey, value) }
export function useWorkspaceTaskActionsContext(): WorkspaceTaskActionsContext {
  const value = inject(tasksKey)
  if (!value) throw new Error('WorkspaceTaskActionsContext requires WorkspacePage')
  return value
}

type WorkspaceLessonsContext = Pick<ReturnType<typeof useWorkspaceLessons>,
  'openLessonsModal'
  | 'showLessonsModal'
  | 'closeLessonsModal'
  | 'handleSummarizeLessons'
  | 'refreshFeedbackLessons'
  | 'lastFeedbackSummaryRun'
  | 'shortId'
  | 'feedbackLessonMatchSummary'
  | 'lessonActionKey'
  | 'deleteLesson'
  | 'handleCreateLesson'
  | 'lessonForm'
  | 'resetLessonForm'
>
const lessonsKey: InjectionKey<WorkspaceLessonsContext> = Symbol('WorkspaceLessonsContext')
export function provideWorkspaceLessonsContext(value: WorkspaceLessonsContext) { provide(lessonsKey, value) }
export function useWorkspaceLessonsContext(): WorkspaceLessonsContext {
  const value = inject(lessonsKey)
  if (!value) throw new Error('WorkspaceLessonsContext requires WorkspacePage')
  return value
}

type WorkspaceNavigationContext = Pick<ReturnType<typeof useWorkspaceNavigation>,
  'goToTerminalMode'
  | 'closeWorkspaceMobileMenu'
  | 'openWorkspaceModalFromMenu'
  | 'openEditWorkspaceModalFromMenu'
  | 'openLessonsModalFromMenu'
  | 'openAgentOptionsModalFromMenu'
  | 'refreshAgentStatusesFromMenu'
  | 'toggleThemeFromMenu'
  | 'workspaceMobileMenuRef'
>
const navigationKey: InjectionKey<WorkspaceNavigationContext> = Symbol('WorkspaceNavigationContext')
export function provideWorkspaceNavigationContext(value: WorkspaceNavigationContext) { provide(navigationKey, value) }
export function useWorkspaceNavigationContext(): WorkspaceNavigationContext {
  const value = inject(navigationKey)
  if (!value) throw new Error('WorkspaceNavigationContext requires WorkspacePage')
  return value
}
