<template>
  <section class="workspace-view">
    <WorkspaceHeader />

    <WorkspaceSummary />

    <WorkspaceAgentStatus />

    <WorkspaceBoard />

    <TaskDetailDialog />

    <ArtifactPreviewDialogs />

    <WorkspaceEditorDialog />

    <!-- Resident self-driven agent config (nested popup over the workspace modal) -->
    <ResidentAgentDialog />

    <TaskCreateDialog :flow="taskCreateFlow" />

    <TaskEditDialog />

    <WorkspaceLessonsDialog />

    <WorkspaceAgentDialog />

    <AgentDirectoryDialog />
  </section>

  <SwitchEnvDialog />

  <WorkspaceNotifications />
</template>

<script setup lang="ts">
import TaskCreateDialog from '@/components/TaskCreateDialog.vue'
import WorkspaceHeader from './components/WorkspaceHeader.vue'
import WorkspaceSummary from './components/WorkspaceSummary.vue'
import WorkspaceAgentStatus from './components/WorkspaceAgentStatus.vue'
import WorkspaceBoard from './components/WorkspaceBoard.vue'
import TaskDetailDialog from './components/TaskDetailDialog.vue'
import ArtifactPreviewDialogs from './components/ArtifactPreviewDialogs.vue'
import WorkspaceEditorDialog from './components/WorkspaceEditorDialog.vue'
import ResidentAgentDialog from './components/ResidentAgentDialog.vue'
import TaskEditDialog from './components/TaskEditDialog.vue'
import WorkspaceLessonsDialog from './components/WorkspaceLessonsDialog.vue'
import WorkspaceAgentDialog from './components/WorkspaceAgentDialog.vue'
import AgentDirectoryDialog from './components/AgentDirectoryDialog.vue'
import SwitchEnvDialog from './components/SwitchEnvDialog.vue'
import WorkspaceNotifications from './components/WorkspaceNotifications.vue'
import {
  useWorkspaceRuntime,
} from './composables/useWorkspaceRuntime'
import {
  useWorkspaceEditor,
} from './composables/useWorkspaceEditor'
import {
  useWorkspaceAgentManager,
} from './composables/useWorkspaceAgentManager'
import {
  useWorkspaceDirectoryBrowser,
} from './composables/useWorkspaceDirectoryBrowser'
import {
  useWorkspaceArtifacts,
} from './composables/useWorkspaceArtifacts'
import {
  useWorkspaceTaskPresentation,
} from './composables/useWorkspaceTaskPresentation'
import {
  useWorkspaceTaskActions,
} from './composables/useWorkspaceTaskActions'
import {
  useWorkspaceLessons,
} from './composables/useWorkspaceLessons'
import {
  useWorkspaceNavigation,
} from './composables/useWorkspaceNavigation'
import {
  useWorkspaceLifecycle,
} from './composables/useWorkspaceLifecycle'
import {
  provideWorkspaceRuntimeContext,
  provideWorkspaceEditorContext,
  provideWorkspaceAgentManagerContext,
  provideWorkspaceDirectoryBrowserContext,
  provideWorkspaceArtifactsContext,
  provideWorkspaceTaskPresentationContext,
  provideWorkspaceTaskActionsContext,
  provideWorkspaceLessonsContext,
  provideWorkspaceNavigationContext,
} from './context'
const runtime = useWorkspaceRuntime()
const editor = useWorkspaceEditor({
  defaultPresetTextForAgent: runtime.defaultPresetTextForAgent,
  workspaceStore: runtime.workspaceStore,
  runPending: runtime.runPending,
  selectedWorkspaceId: runtime.selectedWorkspaceId,
  activeWorkspace: runtime.activeWorkspace,
  activeWorkspaceId: runtime.activeWorkspaceId,
  elapsedClockMs: runtime.elapsedClockMs,
})
const agents = useWorkspaceAgentManager({
  defaultPresetTextForAgent: runtime.defaultPresetTextForAgent,
  reviewerSessions: runtime.reviewerSessions,
  workspaceAgents: runtime.workspaceAgents,
  dispatcherAgent: runtime.dispatcherAgent,
  residentAgent: runtime.residentAgent,
  isLoading: runtime.isLoading,
  isPending: runtime.isPending,
  activeWorkspace: runtime.activeWorkspace,
  getPresetText: runtime.getPresetText,
  workspaceStore: runtime.workspaceStore,
  runPending: runtime.runPending,
  sessionActionKey: runtime.sessionActionKey,
  terminalStore: runtime.terminalStore,
  remoteProfiles: editor.remoteProfiles,
  fetchRemoteProfiles: editor.fetchRemoteProfiles,
})
const directory = useWorkspaceDirectoryBrowser({
  agentOptionsForm: agents.agentOptionsForm,
  showAgentFileBrowser: agents.showAgentFileBrowser,
  remoteProfiles: editor.remoteProfiles,
  runPending: runtime.runPending,
})
const artifacts = useWorkspaceArtifacts({
  selectedTask: runtime.selectedTask,
  activeWorkspaceId: runtime.activeWorkspaceId,
  workspaceStore: runtime.workspaceStore,
})
const presentation = useWorkspaceTaskPresentation({
  selectedTask: runtime.selectedTask,
  workspaceStore: runtime.workspaceStore,
  taskActionKey: runtime.taskActionKey,
  board: runtime.board,
  elapsedClockMs: runtime.elapsedClockMs,
  reviewerTitle: runtime.reviewerTitle,
})
const tasks = useWorkspaceTaskActions({
  selectedTaskId: runtime.selectedTaskId,
  runPending: runtime.runPending,
  taskActionKey: runtime.taskActionKey,
  workspaceStore: runtime.workspaceStore,
  terminalStore: runtime.terminalStore,
  selectedTask: runtime.selectedTask,
  expandedArtifactKey: artifacts.expandedArtifactKey,
  closeMarkdownPreviewModal: artifacts.closeMarkdownPreviewModal,
  canEditTask: presentation.canEditTask,
  canUseManagedTaskActions: presentation.canUseManagedTaskActions,
  selectedSession: presentation.selectedSession,
})
const lessons = useWorkspaceLessons({
  tasks: runtime.tasks,
  activeFeedbackLessons: runtime.activeFeedbackLessons,
  workspaceStore: runtime.workspaceStore,
  runPending: runtime.runPending,
  activeWorkspaceId: runtime.activeWorkspaceId,
})
const navigation = useWorkspaceNavigation({
  appStore: runtime.appStore,
  refreshAgentStatuses: runtime.refreshAgentStatuses,
  openWorkspaceModal: editor.openWorkspaceModal,
  openEditWorkspaceModal: editor.openEditWorkspaceModal,
  openAgentOptionsModal: agents.openAgentOptionsModal,
  openLessonsModal: lessons.openLessonsModal,
})
useWorkspaceLifecycle({
  tasks: runtime.tasks,
  selectedTaskId: runtime.selectedTaskId,
  workspaceStore: runtime.workspaceStore,
  activeWorkspaceId: runtime.activeWorkspaceId,
  selectedWorkspaceId: runtime.selectedWorkspaceId,
  workspaceSessionView: runtime.workspaceSessionView,
  taskCreateFlow: runtime.taskCreateFlow,
  terminalStore: runtime.terminalStore,
  refreshBoard: runtime.refreshBoard,
  elapsedClockMs: runtime.elapsedClockMs,
  closeTaskDetail: tasks.closeTaskDetail,
  detailAttachments: tasks.detailAttachments,
  handleWorkspaceDocumentPointerDown: navigation.handleWorkspaceDocumentPointerDown,
  handleLightboxKeydown: artifacts.handleLightboxKeydown,
  fetchRemoteProfiles: editor.fetchRemoteProfiles,
})
provideWorkspaceRuntimeContext({
  activeWorkspace: runtime.activeWorkspace,
  mobileWorkspaceSummary: runtime.mobileWorkspaceSummary,
  isPending: runtime.isPending,
  selectedWorkspaceId: runtime.selectedWorkspaceId,
  handleWorkspaceChange: runtime.handleWorkspaceChange,
  workspaces: runtime.workspaces,
  activeWorkspaceId: runtime.activeWorkspaceId,
  openTaskModal: runtime.openTaskModal,
  colorScheme: runtime.colorScheme,
  error: runtime.error,
  dismissWorkspaceErrors: runtime.dismissWorkspaceErrors,
  workspaceAgents: runtime.workspaceAgents,
  reviewerAgents: runtime.reviewerAgents,
  temporaryReviewers: runtime.temporaryReviewers,
  tasksByStatus: runtime.tasksByStatus,
  activeFeedbackLessons: runtime.activeFeedbackLessons,
  columns: runtime.columns,
  taskCountByStatus: runtime.taskCountByStatus,
  workspaceSessionView: runtime.workspaceSessionView,
  reviewerSessions: runtime.reviewerSessions,
  refreshAgentStatuses: runtime.refreshAgentStatuses,
  visibleWorkspaceSessions: runtime.visibleWorkspaceSessions,
  sessionActionKey: runtime.sessionActionKey,
  openSession: runtime.openSession,
  agentRuntimeStatus: runtime.agentRuntimeStatus,
  agentRoleLabel: runtime.agentRoleLabel,
  agentRuntimeDetail: runtime.agentRuntimeDetail,
  agentTaskLabel: runtime.agentTaskLabel,
  agentRuntimeText: runtime.agentRuntimeText,
  canSwitchAgentEnv: runtime.canSwitchAgentEnv,
  canDeleteAgent: runtime.canDeleteAgent,
  agentDeleteTitle: runtime.agentDeleteTitle,
  agentActionKey: runtime.agentActionKey,
  deleteAgent: runtime.deleteAgent,
  visibleWorkspaceSessionsEmptyText: runtime.visibleWorkspaceSessionsEmptyText,
  boardLoading: runtime.boardLoading,
  isMobileColumnCollapsed: runtime.isMobileColumnCollapsed,
  toggleMobileColumn: runtime.toggleMobileColumn,
  selectedTaskId: runtime.selectedTaskId,
  latestReportForTask: runtime.latestReportForTask,
  tasks: runtime.tasks,
  taskActionKey: runtime.taskActionKey,
  sessionForTask: runtime.sessionForTask,
  boardTasksPagination: runtime.boardTasksPagination,
  boardOlderTasksRemaining: runtime.boardOlderTasksRemaining,
  boardLoadMoreLoading: runtime.boardLoadMoreLoading,
  handleLoadOlderTasks: runtime.handleLoadOlderTasks,
  boardLoadMoreError: runtime.boardLoadMoreError,
  persistedAttachmentMeta: runtime.persistedAttachmentMeta,
  reviewerTitle: runtime.reviewerTitle,
  board: runtime.board,
  taskCapabilities: runtime.taskCapabilities,
  formatTime: runtime.formatTime,
  selectedTask: runtime.selectedTask,
  isLoading: runtime.isLoading,
  dispatcherAgent: runtime.dispatcherAgent,
  residentAgent: runtime.residentAgent,
  taskTitle: runtime.taskTitle,
  envPresets: runtime.envPresets,
  workspaceNotifications: runtime.workspaceNotifications,
  dismissToast: runtime.dismissToast,
})
provideWorkspaceEditorContext({
  openWorkspaceModal: editor.openWorkspaceModal,
  openEditWorkspaceModal: editor.openEditWorkspaceModal,
  isResidentAgent: editor.isResidentAgent,
  isResidentPaused: editor.isResidentPaused,
  isResidentMaster: editor.isResidentMaster,
  residentLastRunLabel: editor.residentLastRunLabel,
  residentNextRunLabel: editor.residentNextRunLabel,
  latestResidentReport: editor.latestResidentReport,
  toggleResidentPaused: editor.toggleResidentPaused,
  residentRunPending: editor.residentRunPending,
  handleRunResidentNow: editor.handleRunResidentNow,
  showWorkspaceModal: editor.showWorkspaceModal,
  closeWorkspaceModal: editor.closeWorkspaceModal,
  workspaceModalMode: editor.workspaceModalMode,
  handleSubmitWorkspace: editor.handleSubmitWorkspace,
  workspaceForm: editor.workspaceForm,
  remoteProfilesLoading: editor.remoteProfilesLoading,
  remoteProfiles: editor.remoteProfiles,
  selectedRemoteProfile: editor.selectedRemoteProfile,
  residentSummaryLabel: editor.residentSummaryLabel,
  openResidentAgentModal: editor.openResidentAgentModal,
  handleDeleteWorkspace: editor.handleDeleteWorkspace,
  showResidentAgentModal: editor.showResidentAgentModal,
  closeResidentAgentModal: editor.closeResidentAgentModal,
  enabledPeriodicTaskCount: editor.enabledPeriodicTaskCount,
  removePeriodicTask: editor.removePeriodicTask,
  addPeriodicTask: editor.addPeriodicTask,
  handleResidentTargetChange: editor.handleResidentTargetChange,
  isResidentCreateMode: editor.isResidentCreateMode,
  residentExists: editor.residentExists,
  handleDeleteResident: editor.handleDeleteResident,
  handleToggleResidentPause: editor.handleToggleResidentPause,
  handleCreateResident: editor.handleCreateResident,
  handleSaveResidentAndRunNow: editor.handleSaveResidentAndRunNow,
})
provideWorkspaceAgentManagerContext({
  openAgentOptionsModal: agents.openAgentOptionsModal,
  openSwitchEnvModal: agents.openSwitchEnvModal,
  showAgentOptionsModal: agents.showAgentOptionsModal,
  closeAgentOptionsModal: agents.closeAgentOptionsModal,
  agentManagerView: agents.agentManagerView,
  agentManagerSessions: agents.agentManagerSessions,
  agentManagerEmptyText: agents.agentManagerEmptyText,
  handleCreateAdvancedAgent: agents.handleCreateAdvancedAgent,
  agentOptionsForm: agents.agentOptionsForm,
  handleAgentSurfaceChange: agents.handleAgentSurfaceChange,
  handleAgentTargetChange: agents.handleAgentTargetChange,
  selectedAgentRemoteProfile: agents.selectedAgentRemoteProfile,
  isAgentOptionsCreateDisabled: agents.isAgentOptionsCreateDisabled,
  showAgentFileBrowser: agents.showAgentFileBrowser,
  switchEnvForm: agents.switchEnvForm,
  showSwitchEnvManager: agents.showSwitchEnvManager,
  closeSwitchEnvPresetManager: agents.closeSwitchEnvPresetManager,
  showSwitchEnvModal: agents.showSwitchEnvModal,
  closeSwitchEnvModal: agents.closeSwitchEnvModal,
  switchEnvAgent: agents.switchEnvAgent,
  handleSwitchEnv: agents.handleSwitchEnv,
  applySwitchEnvPreset: agents.applySwitchEnvPreset,
  openSwitchEnvPresetManager: agents.openSwitchEnvPresetManager,
})
provideWorkspaceDirectoryBrowserContext({
  openAgentDirectoryBrowser: directory.openAgentDirectoryBrowser,
  browserPlacement: directory.browserPlacement,
  navigateAgentBrowserHome: directory.navigateAgentBrowserHome,
  agentBrowserParentPath: directory.agentBrowserParentPath,
  navigateAgentBrowserParent: directory.navigateAgentBrowserParent,
  agentBrowserPathInput: directory.agentBrowserPathInput,
  loadAgentDirectory: directory.loadAgentDirectory,
  refreshAgentDirectory: directory.refreshAgentDirectory,
  agentBrowserItems: directory.agentBrowserItems,
  handleAgentFileItemClick: directory.handleAgentFileItemClick,
  agentBrowserLoading: directory.agentBrowserLoading,
  agentBrowserError: directory.agentBrowserError,
  selectAgentCurrentDirectory: directory.selectAgentCurrentDirectory,
})
provideWorkspaceArtifactsContext({
  openMarkdownPreviewModal: artifacts.openMarkdownPreviewModal,
  openImageLightbox: artifacts.openImageLightbox,
  isArtifactPreviewLoading: artifacts.isArtifactPreviewLoading,
  toggleArtifactPreview: artifacts.toggleArtifactPreview,
  artifactPreviewButtonLabel: artifacts.artifactPreviewButtonLabel,
  expandedArtifactKey: artifacts.expandedArtifactKey,
  artifactPreviewErrors: artifacts.artifactPreviewErrors,
  artifactPreviews: artifacts.artifactPreviews,
  isMarkdownDocumentPreviewLoading: artifacts.isMarkdownDocumentPreviewLoading,
  toggleMarkdownDocumentPreview: artifacts.toggleMarkdownDocumentPreview,
  markdownDocumentPreviewButtonLabel: artifacts.markdownDocumentPreviewButtonLabel,
  markdownPreviewModalPath: artifacts.markdownPreviewModalPath,
  closeMarkdownPreviewModal: artifacts.closeMarkdownPreviewModal,
  markdownPreviewModalError: artifacts.markdownPreviewModalError,
  markdownPreviewModalLoading: artifacts.markdownPreviewModalLoading,
  markdownPreviewModalContent: artifacts.markdownPreviewModalContent,
  imageLightboxUrl: artifacts.imageLightboxUrl,
  closeImageLightbox: artifacts.closeImageLightbox,
  imageLightboxAlt: artifacts.imageLightboxAlt,
})
provideWorkspaceTaskPresentationContext({
  activeReviewBadge: presentation.activeReviewBadge,
  canUseManagedTaskActions: presentation.canUseManagedTaskActions,
  reportMessageForLang: presentation.reportMessageForLang,
  canEditTask: presentation.canEditTask,
  canMarkDoneTask: presentation.canMarkDoneTask,
  canRequestReviewTask: presentation.canRequestReviewTask,
  canAbortTask: presentation.canAbortTask,
  taskModeLabel: presentation.taskModeLabel,
  executionComplexityLabel: presentation.executionComplexityLabel,
  selectedSession: presentation.selectedSession,
  reviewStatusLabel: presentation.reviewStatusLabel,
  autonomousRunPhaseLabel: presentation.autonomousRunPhaseLabel,
  formatAutonomousScore: presentation.formatAutonomousScore,
  taskTotalElapsedLabel: presentation.taskTotalElapsedLabel,
  taskWorkingElapsedLabel: presentation.taskWorkingElapsedLabel,
  latestSelectedReportAgeLabel: presentation.latestSelectedReportAgeLabel,
  selectedReports: presentation.selectedReports,
  hasBilingualReport: presentation.hasBilingualReport,
  reportLang: presentation.reportLang,
  setReportLang: presentation.setReportLang,
  selectedProgressTimeline: presentation.selectedProgressTimeline,
  isLatestSelectedReport: presentation.isLatestSelectedReport,
  reportMessagePreview: presentation.reportMessagePreview,
  reportElapsedTitle: presentation.reportElapsedTitle,
  reportElapsedLabel: presentation.reportElapsedLabel,
  selectedMarkdownDocuments: presentation.selectedMarkdownDocuments,
  selectedTaskSendKey: presentation.selectedTaskSendKey,
})
provideWorkspaceTaskActionsContext({
  selectTask: tasks.selectTask,
  startOptionsFor: tasks.startOptionsFor,
  startTask: tasks.startTask,
  openEditTaskModal: tasks.openEditTaskModal,
  markTask: tasks.markTask,
  requestReview: tasks.requestReview,
  abortTask: tasks.abortTask,
  deleteTask: tasks.deleteTask,
  isDetailActionsExpanded: tasks.isDetailActionsExpanded,
  toggleDetailActions: tasks.toggleDetailActions,
  sendDetailMessage: tasks.sendDetailMessage,
  detailAttachments: tasks.detailAttachments,
  detailMessage: tasks.detailMessage,
  closeTaskDetail: tasks.closeTaskDetail,
  showEditTaskModal: tasks.showEditTaskModal,
  closeEditTaskModal: tasks.closeEditTaskModal,
  handleUpdateTask: tasks.handleUpdateTask,
  editTaskForm: tasks.editTaskForm,
  editingTaskId: tasks.editingTaskId,
})
provideWorkspaceLessonsContext({
  openLessonsModal: lessons.openLessonsModal,
  showLessonsModal: lessons.showLessonsModal,
  closeLessonsModal: lessons.closeLessonsModal,
  handleSummarizeLessons: lessons.handleSummarizeLessons,
  refreshFeedbackLessons: lessons.refreshFeedbackLessons,
  lastFeedbackSummaryRun: lessons.lastFeedbackSummaryRun,
  shortId: lessons.shortId,
  feedbackLessonMatchSummary: lessons.feedbackLessonMatchSummary,
  lessonActionKey: lessons.lessonActionKey,
  deleteLesson: lessons.deleteLesson,
  handleCreateLesson: lessons.handleCreateLesson,
  lessonForm: lessons.lessonForm,
  resetLessonForm: lessons.resetLessonForm,
})
provideWorkspaceNavigationContext({
  goToTerminalMode: navigation.goToTerminalMode,
  closeWorkspaceMobileMenu: navigation.closeWorkspaceMobileMenu,
  openWorkspaceModalFromMenu: navigation.openWorkspaceModalFromMenu,
  openEditWorkspaceModalFromMenu: navigation.openEditWorkspaceModalFromMenu,
  openLessonsModalFromMenu: navigation.openLessonsModalFromMenu,
  openAgentOptionsModalFromMenu: navigation.openAgentOptionsModalFromMenu,
  refreshAgentStatusesFromMenu: navigation.refreshAgentStatusesFromMenu,
  toggleThemeFromMenu: navigation.toggleThemeFromMenu,
  workspaceMobileMenuRef: navigation.workspaceMobileMenuRef,
})
const {
  taskCreateFlow,
} = runtime
</script>

<style scoped>
.workspace-view {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
}
</style>

<style scoped>
@media (max-width: 760px) {
  .workspace-view {
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }
}
</style>
