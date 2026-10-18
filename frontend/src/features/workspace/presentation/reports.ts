import type {
  AcceptanceCheck, AcceptanceCheckStatus, AgentReport, AgentReportState, GoalPacket,
  ReviewProfile, ReviewProfileResult, WorkspaceMarkdownDocument, WorkspaceMarkdownDocumentSource,
  WorkspaceTask,
} from '@/types'

export type ReportLang = 'en' | 'zh'

export function goalPacketSections(goalPacket: GoalPacket) {
  return [
    {
      key: 'acceptance_criteria',
      label: 'Acceptance Criteria',
      items: goalPacketItems(goalPacket.acceptance_criteria),
    },
    {
      key: 'validation_plan',
      label: 'Validation Plan',
      items: goalPacketItems(goalPacket.validation_plan),
    },
    {
      key: 'assumptions',
      label: 'Assumptions',
      items: goalPacketItems(goalPacket.assumptions),
    },
    {
      key: 'out_of_scope',
      label: 'Out of Scope',
      items: goalPacketItems(goalPacket.out_of_scope),
    },
    {
      key: 'handoff_requirements',
      label: 'Handoff Requirements',
      items: goalPacketItems(goalPacket.handoff_requirements),
    },
  ]
}

function goalPacketItems(items: string[] | undefined): string[] {
  return Array.isArray(items) ? items : []
}

export function acceptanceChecksFor(report: AgentReport): AcceptanceCheck[] {
  return Array.isArray(report.acceptance_check) ? report.acceptance_check : []
}

const FINAL_REPORT_STATES = new Set<AgentReportState>([
  'completed',
  'ready_for_review',
  'review_passed',
  'review_failed',
])

function isFinalReport(report: AgentReport): boolean {
  return FINAL_REPORT_STATES.has(report.state)
}

function acceptanceSummary(report: AgentReport): string | null {
  const checks = acceptanceChecksFor(report)
  if (checks.length === 0) return null
  const counts: Record<string, number> = {}
  for (const check of checks) {
    counts[check.status] = (counts[check.status] || 0) + 1
  }
  const order: AcceptanceCheckStatus[] = ['passed', 'partial', 'failed', 'not_checked']
  const parts: string[] = []
  for (const status of order) {
    if (counts[status]) parts.push(`${counts[status]} ${status}`)
  }
  return parts.join(' · ')
}

function isSubstantiveReport(report: AgentReport): boolean {
  return Boolean(
    (report.changed_files && report.changed_files.length > 0) ||
    report.validation ||
    (report.acceptance_check && report.acceptance_check.length > 0) ||
    (report.profile_results && report.profile_results.length > 0) ||
    (report.artifact_refs && report.artifact_refs.length > 0) ||
    report.risks ||
    report.evaluation_report
  )
}

export function reportSummaryLabel(report: AgentReport): string {
  const parts: string[] = []
  const acceptance = acceptanceSummary(report)
  if (acceptance) parts.push(acceptance)
  if (report.changed_files?.length) {
    parts.push(`${report.changed_files.length} file${report.changed_files.length === 1 ? '' : 's'}`)
  }
  if (report.validation) parts.push('validated')
  if (report.risks) parts.push('risks noted')
  if (report.profile_results?.length) parts.push(`${report.profile_results.length} review profile${report.profile_results.length === 1 ? '' : 's'}`)
  if (report.artifact_refs?.length) parts.push(`${report.artifact_refs.length} artifact${report.artifact_refs.length === 1 ? '' : 's'}`)
  return parts.join(' · ')
}

export function reportMessagePreview(report: AgentReport, lang: ReportLang): string {
  const text = reportMessageForLang(report, lang).trim()
  if (text.length <= 120) return text
  return text.slice(0, 117) + '…'
}

export function profileResultsFor(report: AgentReport): ReviewProfileResult[] {
  return Array.isArray(report.profile_results) ? report.profile_results : []
}

export function reviewProfileLabel(profile: ReviewProfile): string {
  const labels: Record<ReviewProfile, string> = {
    general: 'General',
    code: 'Code',
    ui: 'UI',
    artifact: 'Artifact',
    delivery: 'Delivery',
    boundary: 'Boundary',
  }
  return labels[profile] || profile
}

export function taskReviewProfiles(task: WorkspaceTask): string {
  const profiles = Array.isArray(task.review_profiles) ? task.review_profiles : []
  if (profiles.length > 0) return profiles.map(reviewProfileLabel).join(', ')
  const policyProfiles = task.autonomy_policy?.review_profiles || []
  if (policyProfiles.length > 0) return policyProfiles.map(reviewProfileLabel).join(', ')
  return task.autonomy_policy?.require_artifact_review ? 'Inferred + Artifact' : 'Inferred'
}

export function profileResultSummary(results: ReviewProfileResult[]): string {
  return results
    .map(result => `${reviewProfileLabel(result.profile)} ${result.status}`)
    .join(' · ')
}

export function isMarkdownArtifact(artifact: string): boolean {
  const value = artifact.trim().split(/[?#]/)[0] || ''
  return /\.(md|markdown|mdown|mkd)(?::\d+)?$/i.test(value)
}

function isWorkspaceMaintenanceMarkdown(path: string): boolean {
  return path.trim().split(/[?#]/)[0].split('/').pop()?.toLowerCase() === 'changelog.md'
}

export function markdownArtifactRefs(report: AgentReport): string[] {
  return (report.artifact_refs || []).filter(isMarkdownArtifact)
}

export function artifactPreviewKey(report: AgentReport, artifact: string): string {
  return `${report.id}:${artifact}`
}

export function markdownDocumentPreviewKey(document: WorkspaceMarkdownDocument): string {
  return `doc:${document.id}`
}

export function markdownDocumentSourceLabel(source: WorkspaceMarkdownDocumentSource): string {
  const labels: Record<WorkspaceMarkdownDocumentSource, string> = {
    artifact: 'Official artifact',
    changed_file: 'Changed Markdown',
    snapshot: 'Workspace snapshot',
    discovered: 'Discovered',
  }
  return labels[source]
}

export function goalPacketStatusLabel(status: GoalPacket['status']) {
  if (status === 'pending_review') return 'Pending review'
  if (status === 'approved') return 'Approved'
  if (status === 'rejected') return 'Rejected'
  if (status === 'frozen') return 'Frozen'
  if (status === 'superseded') return 'Superseded'
  return 'Draft'
}

export function goalPacketGateLabel(task: WorkspaceTask) {
  const status = task.goal_packet?.status
  if (!status) return 'Not recorded'
  if (status === 'pending_review') {
    if (task.review_requested_at && !task.review_completed_at) {
      return 'Awaiting Goal Packet approval'
    }
    if (task.review_completed_at) return 'Review needs input'
    return 'Pending approval'
  }
  if (status === 'approved') return 'Approved for implementation'
  if (status === 'rejected') return 'Changes requested before implementation'
  return goalPacketStatusLabel(status)
}

export function primaryExpandedReportId(reports: readonly AgentReport[]): string | null {
  if (reports.length === 0) return null
  const finalSubstantive = [...reports].reverse().find(
    (r) => isFinalReport(r) && isSubstantiveReport(r)
  )
  if (finalSubstantive) return finalSubstantive.id
  const substantive = [...reports].reverse().find(isSubstantiveReport)
  if (substantive) return substantive.id
  return reports[reports.length - 1].id
}

export function reportMessageForLang(report: AgentReport, lang: ReportLang): string {
  const preferred = lang === 'zh' ? report.message_zh : report.message_en
  const fallback = lang === 'zh' ? report.message_en : report.message_zh
  return preferred || fallback || report.message
}

export function hasBilingualReport(reports: readonly AgentReport[]): boolean {
  return reports.some((r) => (r.message_en && r.message_en.trim()) || (r.message_zh && r.message_zh.trim()))
}

export function taskMarkdownDocuments(
  task: WorkspaceTask | null,
  reports: readonly AgentReport[],
  documents: readonly WorkspaceMarkdownDocument[],
): WorkspaceMarkdownDocument[] {
  if (!task) return []
  const selectedReportIds = new Set(reports.map(report => report.id))
  return documents.filter(document =>
    !isWorkspaceMaintenanceMarkdown(document.path) && (
      document.task_id === task.id ||
      (document.report_id ? selectedReportIds.has(document.report_id) : false)
    )
  )
}
