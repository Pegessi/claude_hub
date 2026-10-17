import {
  computed,
  ref,
} from 'vue'
import {
  type AgentReport,
  type WorkspaceMarkdownDocument,
  type WorkspaceTask,
  type WorkspaceTaskMode,
  type WorkspaceTaskExecutionComplexity,
} from '@/types'
import {
  taskMarkdownDocuments,
  reportMessagePreview as projectReportMessagePreview,
  primaryExpandedReportId as projectPrimaryExpandedReportId,
  type ReportLang,
  reportMessageForLang as projectReportMessageForLang,
  hasBilingualReport as projectHasBilingualReport,
} from '@/features/workspace/presentation/reports'
import {
  latestReportAgeLabel,
  progressTimeline,
  taskTotalElapsedLabel as projectTaskTotalElapsedLabel,
  taskWorkingElapsedLabel as projectTaskWorkingElapsedLabel,
  reportElapsedLabel as projectReportElapsedLabel,
  reportElapsedTitle as projectReportElapsedTitle,
} from '@/features/workspace/presentation/progress'
import {
  awaitingHumanAcceptance as taskAcceptanceAwaiting,
  canMarkDoneTask as taskAcceptanceCanMarkDone,
} from '@/utils/taskAcceptance'
import {
  isWorkspaceControlledTask,
} from '@/utils/taskExecution'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'

type WorkspaceTaskPresentationInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'selectedTask'
  | 'workspaceStore'
  | 'taskActionKey'
  | 'board'
  | 'elapsedClockMs'
  | 'reviewerTitle'
>

export function useWorkspaceTaskPresentation(inputs: WorkspaceTaskPresentationInputs) {
  const {
    selectedTask,
    workspaceStore,
    taskActionKey,
    board,
    elapsedClockMs,
    reviewerTitle,
  } = inputs

  const selectedSession = computed(() =>
    selectedTask.value ? workspaceStore.sessionForTask(selectedTask.value) : null
  )

  const selectedTaskSendKey = computed(() =>
    selectedTask.value ? taskActionKey('send', selectedTask.value.id) : 'task:none:send'
  )

  const selectedReports = computed<AgentReport[]>(() =>
    selectedTask.value ? workspaceStore.reportsForTaskId(selectedTask.value.id) : []
  )

  const selectedMarkdownDocuments = computed<WorkspaceMarkdownDocument[]>(() =>
    taskMarkdownDocuments(selectedTask.value, selectedReports.value, board.value?.markdown_documents || [])
  )

  const latestSelectedReportAgeLabel = computed(() =>
    latestReportAgeLabel(selectedReports.value, elapsedClockMs.value)
  )

  const selectedProgressTimeline = computed(() =>
    progressTimeline(selectedTask.value, selectedReports.value, elapsedClockMs.value)
  )

  function reportMessagePreview(report: AgentReport): string {
    return projectReportMessagePreview(report, reportLang.value)
  }

  function latestReviewReportForTask(task: WorkspaceTask) {
    const reviewReports = workspaceStore
      .reportsForTask(task)
      .filter(report => report.state.startsWith('review_'))
    return reviewReports[reviewReports.length - 1] || null
  }

  function taskModeLabel(mode: WorkspaceTaskMode) {
    if (mode === 'autonomous') return 'Autonomous'
    if (mode === 'direct') return 'Direct'
    if (mode === 'subagent') return 'Subagent'
    return 'Reviewed'
  }

  function executionComplexityLabel(complexity: WorkspaceTaskExecutionComplexity) {
    if (complexity === 'complex') return 'Complex'
    if (complexity === 'simple') return 'Simple'
    return 'Auto'
  }

  function autonomousRunPhaseLabel(phase: string) {
    return phase
      .split('_')
      .map(part => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ')
  }

  function formatAutonomousScore(score: number | null | undefined) {
    if (score === null || score === undefined) return 'none'
    return `${Math.round(score * 100)}%`
  }

  function reviewStatusLabel(task: WorkspaceTask) {
    if (task.human_accepted_at) return 'Human accepted'
    if (task.goal_packet?.status === 'pending_review') {
      if (task.review_requested_at && !task.review_completed_at) return 'Pending Goal Packet approval'
      if (task.review_completed_at) return 'Goal Packet review needs input'
    }
    if (task.goal_packet?.status === 'rejected' && task.status === 'working') {
      return 'Goal Packet changes requested'
    }
    if (task.goal_packet?.status === 'approved' && task.status === 'working') {
      return 'Goal Packet approved; implementation active'
    }
    const latestReviewReport = latestReviewReportForTask(task)
    if (latestReviewReport?.state === 'review_passed') return 'AI review passed, awaiting human acceptance'
    if (latestReviewReport?.state === 'review_failed') return 'Changes requested'
    if (latestReviewReport?.state === 'review_needs_input') return 'Review needs input'
    if (latestReviewReport?.state === 'review_started') return 'Reviewing'
    if (task.review_skipped_at) return 'AI review skipped, awaiting human acceptance'
    if (task.review_requested_at && !task.review_completed_at) return 'Pending review'
    if (task.review_attempts > 0) return `Review attempts ${task.review_attempts}`
    return ''
  }

  function activeReviewBadge(
    task: WorkspaceTask,
  ): { kind: 'active' | 'pending' | 'attention'; label: string; title: string } | null {
    if (!canUseManagedTaskActions(task)) return null
    const latestReviewReport = latestReviewReportForTask(task)
    if (
      task.goal_packet?.status === 'pending_review' &&
      task.review_requested_at &&
      !task.review_completed_at
    ) {
      const reviewerName = task.review_session_id
        ? reviewerTitle(task.review_session_id)
        : 'AI reviewer'
      return {
        kind: latestReviewReport?.state === 'review_started' ? 'active' : 'pending',
        label: latestReviewReport?.state === 'review_started'
          ? 'Packet reviewing'
          : 'Packet approval',
        title: `${reviewerName} is reviewing the Goal Packet before implementation starts`,
      }
    }
    if (task.goal_packet?.status === 'rejected' && task.status === 'working') {
      return {
        kind: 'attention',
        label: 'Packet changes',
        title: 'Goal Packet review requested changes before implementation can start',
      }
    }
    if (latestReviewReport?.state === 'review_failed') {
      return {
        kind: 'attention',
        label: 'Changes requested',
        title: 'Reviewer requested changes before this task can be accepted',
      }
    }
    if (latestReviewReport?.state === 'review_needs_input') {
      const reviewerName = task.review_session_id
        ? reviewerTitle(task.review_session_id)
        : 'AI reviewer'
      return {
        kind: 'attention',
        label: 'Review needs input',
        title: `${reviewerName} needs input to continue the review`,
      }
    }
    if (awaitingHumanAcceptance(task)) {
      return {
        kind: 'pending',
        label: 'Awaiting human acceptance',
        title: 'AI review is complete or skipped; human acceptance is required',
      }
    }
    if (task.review_skipped_at) return null
    if (task.review_completed_at) return null
    const reviewerName = task.review_session_id
      ? reviewerTitle(task.review_session_id)
      : 'AI reviewer'
    if (latestReviewReport?.state === 'review_started') {
      return {
        kind: 'active',
        label: 'AI reviewing',
        title: `${reviewerName} is reviewing this task`,
      }
    }
    if (task.review_requested_at) {
      return {
        kind: 'pending',
        label: 'Awaiting AI review',
        title: `Queued for review by ${reviewerName}`,
      }
    }
    return null
  }

  // Thin store-backed wrappers over the pure gate logic in
  // utils/taskAcceptance.ts. They resolve the latest report / latest review
  // report for the task and delegate the decision so it stays unit-testable.
  function awaitingHumanAcceptance(task: WorkspaceTask) {
    return taskAcceptanceAwaiting(
      task,
      workspaceStore.latestReportForTask(task),
      latestReviewReportForTask(task),
    )
  }

  function canMarkDoneTask(task: WorkspaceTask) {
    if (!canUseManagedTaskActions(task)) return false
    return taskAcceptanceCanMarkDone(
      task,
      workspaceStore.latestReportForTask(task),
      latestReviewReportForTask(task),
    )
  }

  function canRequestReviewTask(task: WorkspaceTask) {
    if (!canUseManagedTaskActions(task) || task.status !== 'review') return false
    const latestReviewReport = latestReviewReportForTask(task)
    return awaitingHumanAcceptance(task) ||
      task.review_skipped_at ||
      latestReviewReport?.state === 'review_failed' ||
      latestReviewReport?.state === 'review_needs_input'
  }

  function canAbortTask(task: WorkspaceTask) {
    if (!canUseManagedTaskActions(task)) return false
    return (
      task.status === 'queued' ||
      task.status === 'working' ||
      task.status === 'review' ||
      task.status === 'failed'
    )
  }

  function canEditTask(task: WorkspaceTask) {
    if (!canUseManagedTaskActions(task)) return false
    return task.status === 'todo'
  }

  function primaryExpandedReportId(): string | null {
    return projectPrimaryExpandedReportId(selectedReports.value)
  }

  function isLatestSelectedReport(report: AgentReport) {
    return primaryExpandedReportId() === report.id
  }

  function taskTotalElapsedLabel(task: WorkspaceTask) {
    return projectTaskTotalElapsedLabel(task, elapsedClockMs.value)
  }

  function taskWorkingElapsedLabel(task: WorkspaceTask) {
    return projectTaskWorkingElapsedLabel(task, selectedReports.value, elapsedClockMs.value)
  }

  function reportElapsedLabel(report: AgentReport, index: number) {
    return projectReportElapsedLabel(report, index, selectedTask.value, selectedReports.value)
  }

  function reportElapsedTitle(report: AgentReport, index: number) {
    return projectReportElapsedTitle(report, index, selectedTask.value, selectedReports.value)
  }

  const REPORT_LANG_STORAGE_KEY = 'claude-hub:report-lang'

  const reportLang = ref<ReportLang>(
    (typeof localStorage !== 'undefined' && (localStorage.getItem(REPORT_LANG_STORAGE_KEY) as ReportLang | null)) || 'en'
  )

  function setReportLang(lang: ReportLang) {
    reportLang.value = lang
    try {
      localStorage.setItem(REPORT_LANG_STORAGE_KEY, lang)
    } catch {
      /* ignore quota / disabled storage */
    }
  }

  function reportMessageForLang(report: AgentReport): string {
    return projectReportMessageForLang(report, reportLang.value)
  }

  const hasBilingualReport = computed(() => projectHasBilingualReport(selectedReports.value))

  function canUseManagedTaskActions(task: WorkspaceTask): boolean {
    return isWorkspaceControlledTask(task)
  }

  return {
    activeReviewBadge,
    canUseManagedTaskActions,
    reportMessageForLang,
    canEditTask,
    canMarkDoneTask,
    canRequestReviewTask,
    canAbortTask,
    taskModeLabel,
    executionComplexityLabel,
    selectedSession,
    reviewStatusLabel,
    autonomousRunPhaseLabel,
    formatAutonomousScore,
    taskTotalElapsedLabel,
    taskWorkingElapsedLabel,
    latestSelectedReportAgeLabel,
    selectedReports,
    hasBilingualReport,
    reportLang,
    setReportLang,
    selectedProgressTimeline,
    isLatestSelectedReport,
    reportMessagePreview,
    reportElapsedTitle,
    reportElapsedLabel,
    selectedMarkdownDocuments,
    selectedTaskSendKey,
  }
}
