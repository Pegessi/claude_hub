import type {
  AgentReport,
  ManagedSession,
  TaskLifecycle,
  WorkspaceTask,
  WorkspaceTaskEvent,
} from '@/types'
import { formatElapsedDuration, parseTimestampMs } from '@/utils/duration'
import { evidenceLine, evidenceLinks, evidenceReport, taskEvidence } from './evidence'
import {
  buildKeyMilestones,
  buildMacroStageBar,
  extractDeliverables,
  parseStructuredDigest,
  parseStructuredValidation,
  type DeliverableItem,
  type MacroStagePill,
  type MilestoneStep,
  type StructuredDigest,
  type StructuredValidation,
} from './triageDigest'

export type {
  DeliverableItem,
  MacroStagePill,
  MilestoneStep,
  StructuredDigest,
  StructuredValidation,
}

export type TriageBucket = 'needs_you' | 'working' | 'review' | 'done'
export type TriageTone = 'attention' | 'working' | 'review' | 'done' | 'failed' | 'neutral'
export type SpineNodeTone = 'neutral' | 'warn' | 'pass' | 'live'
export type SpineActorRole = 'orchestrator' | 'worker' | 'reviewer'

export interface TriageArtifactLink {
  label: string
  url: string
  kind: 'mr' | 'doc' | 'arena' | 'infowall' | 'commit' | 'link'
  meta: string
}

export interface TriageConsensusInfo {
  implementer: string
  reviewer: string
  roundsLabel: string
  verdictLabel: string
  verdictTone: 'pass' | 'live' | 'warn' | 'auto'
  metricHighlight: string
}

export interface TriageSpineNode {
  id: string
  stage: string
  actor: string
  actorRole: SpineActorRole
  tone: SpineNodeTone
  timeLabel: string
  deltaLabel: string
  epochLabel?: string
  badge?: string
  filesTouched?: string
  receipt?: string
  summary: string
  reportId?: string
  sessionId?: string | null
  tabId?: string | null
}

export interface TriageTaskViewModel {
  task: WorkspaceTask
  id: string
  code: string
  title: string
  workspaceName: string
  branchLabel: string
  bucket: TriageBucket
  statusLabel: string
  statusTone: TriageTone
  elapsedLabel: string
  updatedAgoLabel: string
  outcomeSummary: string
  decisionLine: string
  liveStep: string
  checksLabel: string
  changedFilesCount: number
  consensus: TriageConsensusInfo
  links: TriageArtifactLink[]
  spine: TriageSpineNode[]
  digest: StructuredDigest
  deliverables: DeliverableItem[]
  validation: StructuredValidation
  milestones: MilestoneStep[]
  macroStages: MacroStagePill[]
  hasIndependentReview: boolean
  reviewSummaryChip: string | null
  internalMeta: {
    control: string
    epoch: number
    mode: string
    seat: string
  }
}

export interface TriageGroup {
  key: TriageBucket
  label: string
  items: TriageTaskViewModel[]
}

const GROUP_LABELS: Record<TriageBucket, string> = {
  needs_you: 'NEEDS YOU',
  working: 'WORKING',
  review: 'REVIEW',
  done: 'DONE',
}

export function deriveTaskCode(task: WorkspaceTask, sources: readonly (string | null | undefined)[]): string {
  for (const src of [task.title, ...sources]) {
    if (!src) continue
    const mr = src.match(/(?:MR|PR)\s*[!#]?\s*(\d{2,6})/i) || src.match(/merge_requests\/(\d{2,6})/) || src.match(/\/pull\/(\d{2,6})/)
    if (mr) return `MR-${mr[1]}`
  }
  const clean = task.id.replace(/[^a-zA-Z0-9]/g, '').slice(0, 6).toUpperCase()
  return `#${clean || 'TASK'}`
}

export function classifyTriageBucket(
  task: WorkspaceTask,
  lifecycle?: TaskLifecycle,
): TriageBucket {
  if (task.status === 'done' || lifecycle?.phase === 'done') return 'done'
  if (task.status === 'failed') return 'needs_you'
  if (lifecycle?.phase === 'needs_user' || lifecycle?.phase === 'orphaned') {
    return 'needs_you'
  }
  if (task.latest_progress?.state === 'needs_input' || task.latest_progress?.state === 'blocked') {
    return 'needs_you'
  }
  if (task.status === 'review' || lifecycle?.phase === 'awaiting_acceptance') {
    return 'review'
  }
  if (lifecycle?.delivery_mr_state === 'open' && lifecycle?.owner?.state === 'idle') {
    return 'review'
  }
  if (lifecycle?.attention) {
    return 'needs_you'
  }
  return 'working'
}

export function classifyStatusTone(
  task: WorkspaceTask,
  bucket: TriageBucket,
): TriageTone {
  if (task.status === 'failed') return 'failed'
  if (bucket === 'needs_you') return 'attention'
  if (bucket === 'review') return 'review'
  if (bucket === 'done') return 'done'
  if (task.status === 'todo') return 'neutral'
  return 'working'
}

export function deriveStatusLabel(
  task: WorkspaceTask,
  reports: readonly AgentReport[],
  lifecycle?: TaskLifecycle,
): string {
  if (task.status === 'failed') return 'Failed'
  if (task.status === 'done' || lifecycle?.phase === 'done') {
    const reason = task.closed_reason || lifecycle?.reason
    if (task.human_accepted_at || reason === 'accepted') return 'Accepted'
    if (reason === 'chat_archived') return 'Archived'
    if (reason === 'mr_merged') return 'MR Merged'
    return 'Done'
  }
  const reviewReports = reports.filter(r => r.state.startsWith('review_'))
  const latestReview = reviewReports[reviewReports.length - 1]
  if (task.status === 'review') {
    if (latestReview?.state === 'review_passed') {
      const rounds = Math.max(1, task.review_attempts || reviewReports.filter(r => r.state === 'review_passed' || r.state === 'review_failed').length)
      return rounds > 1 ? `R${rounds} Review PASS` : 'Review PASS'
    }
    if (latestReview?.state === 'review_failed') return 'Changes requested'
    if (latestReview?.state === 'review_needs_input') return 'Reviewer question'
    if (task.review_skipped_at) return 'Ready to accept'
    if (lifecycle?.delivery_mr_state === 'open' && lifecycle.delivery_mr_label) {
      return `${lifecycle.delivery_mr_label} · Open`
    }
    return 'In review'
  }
  if (lifecycle?.phase === 'orphaned') return 'Session lost'
  if (lifecycle?.phase === 'awaiting_acceptance') return 'Ready to accept'
  if (lifecycle?.phase === 'needs_user') return 'Needs input'
  if (lifecycle?.attention && lifecycle.reason_text) {
    return lifecycle.reason_text.length <= 20 ? lifecycle.reason_text : 'Needs attention'
  }
  if (task.latest_progress?.state === 'needs_input') return 'Needs input'
  if (task.latest_progress?.state === 'blocked') return 'Blocked'
  if (task.status === 'queued') return 'Queued'
  if (task.status === 'todo') return 'Todo'
  if (latestReview?.state === 'review_started') {
    return `Reviewing · R${Math.max(1, (task.review_attempts || 0) + 1)}`
  }
  if (lifecycle?.delivery_mr_state === 'open' && lifecycle.delivery_mr_label && lifecycle.owner?.state === 'idle') {
    return `${lifecycle.delivery_mr_label} · Open`
  }
  return 'Working'
}

function formatClockLabel(iso: string | null | undefined): string {
  const ms = parseTimestampMs(iso)
  if (ms === null) return '--:--'
  const date = new Date(ms)
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

function classifyLinkKind(url: string): TriageArtifactLink['kind'] {
  if (/merge_requests?\/|\/pull\//i.test(url)) return 'mr'
  if (/larkoffice\.com|feishu\.cn|\/docx\/|\/wiki\//i.test(url)) return 'doc'
  if (/evaluation\/arena|trial_id|jobs\//i.test(url)) return 'arena'
  if (/infowall/i.test(url)) return 'infowall'
  if (/\/commit\//i.test(url)) return 'commit'
  return 'link'
}

function linkHostMeta(url: string, kind: TriageArtifactLink['kind']): string {
  try {
    const parsed = new URL(url)
    if (kind === 'mr') {
      const parts = parsed.pathname.split('/').filter(Boolean)
      return parts.slice(0, 2).join('/') || parsed.hostname
    }
    if (kind === 'doc') return 'Feishu Document'
    if (kind === 'arena') return 'Seed Evaluation / Job'
    return parsed.hostname
  } catch {
    return url
  }
}

function formatReportStageTitle(report: AgentReport, reviewOrdinal: number): string {
  switch (report.state) {
    case 'ready_for_review':
      return 'Implementation submitted for review'
    case 'completed':
      return 'Execution completed'
    case 'review_started':
      return `Cross-review round ${reviewOrdinal} started`
    case 'review_passed':
      return `Cross-review round ${reviewOrdinal} · Verdict PASS`
    case 'review_failed':
      return `Cross-review round ${reviewOrdinal} · Changes requested`
    case 'review_needs_input':
      return `Cross-review round ${reviewOrdinal} · Needs human input`
    case 'blocked':
      return 'Execution blocked'
    default:
      return 'Progress checkpoint'
  }
}

export function buildTaskSpine(
  task: WorkspaceTask,
  reports: readonly AgentReport[],
  sessionsById: ReadonlyMap<string, ManagedSession>,
): TriageSpineNode[] {
  const nodes: TriageSpineNode[] = []
  const createdMs = parseTimestampMs(task.created_at)
  let previousMs = createdMs

  const stepDelta = (iso: string | null | undefined): string => {
    const ms = parseTimestampMs(iso)
    if (ms === null || previousMs === null) return ''
    const delta = Math.max(0, ms - previousMs)
    previousMs = ms
    return delta < 1000 ? '0s' : `+${formatElapsedDuration(delta)}`
  }

  const workerSession = task.session_id ? sessionsById.get(task.session_id) : undefined
  nodes.push({
    id: `created-${task.id}`,
    stage: task.started_at ? 'Task created & dispatched' : 'Task registered',
    actor: task.source?.kind === 'human' ? 'Operator' : (task.source?.kind || 'Hub'),
    actorRole: 'orchestrator',
    tone: task.status === 'todo' ? 'neutral' : 'pass',
    timeLabel: formatClockLabel(task.started_at || task.created_at),
    deltaLabel: '0s',
    epochLabel: `EPOCH ${task.execution_epoch ?? 1} · ${task.task_mode.toUpperCase()} EXECUTION`,
    badge: workerSession?.agent_type ? workerSession.agent_type.toUpperCase() : task.agent_type.toUpperCase(),
    summary: evidenceLine(task.goal_packet?.objective || task.prompt).slice(0, 220) || task.title,
    sessionId: workerSession?.id ?? task.session_id ?? null,
    tabId: workerSession?.tab_id ?? task.source?.tab_id ?? null,
  })
  if (task.started_at) {
    previousMs = parseTimestampMs(task.started_at) ?? previousMs
  }

  let reviewRound = 0
  reports.forEach((report, idx) => {
    const isReview = report.state.startsWith('review_')
    if (report.state === 'review_started' || (isReview && reviewRound === 0)) {
      reviewRound += 1
    } else if ((report.state === 'review_passed' || report.state === 'review_failed' || report.state === 'review_needs_input') && idx > 0 && reports[idx - 1]?.state !== 'review_started') {
      reviewRound += 1
    }
    const session = report.session_id ? sessionsById.get(report.session_id) : undefined
    const actor = session?.title || (isReview ? (task.review_session_id || 'Reviewer') : (workerSession?.title || task.agent_type))
    const actorRole: SpineActorRole = isReview ? 'reviewer' : 'worker'
    let tone: SpineNodeTone = 'neutral'
    let badge: string | undefined
    if (report.state === 'review_passed' || report.state === 'completed') {
      tone = 'pass'
      badge = report.state === 'review_passed' ? 'VERDICT PASS' : 'COMPLETED'
    } else if (report.state === 'review_failed' || report.state === 'review_needs_input' || report.state === 'blocked') {
      tone = 'warn'
      badge = report.state === 'review_failed' ? 'CHANGES REQUESTED' : report.state.replace(/_/g, ' ').toUpperCase()
    } else if (report.state === 'review_started') {
      tone = 'live'
      badge = `ROUND ${Math.max(1, reviewRound)}`
    } else if (report.changed_files?.length) {
      badge = `${report.changed_files.length} files`
    }

    const msg = evidenceLine(report.message_zh || report.message || report.message_en)
    const filesTouched = report.changed_files?.length
      ? (report.changed_files.length <= 2
          ? report.changed_files.join(', ')
          : `${report.changed_files.slice(0, 2).join(', ')} (+${report.changed_files.length - 2} more)`)
      : undefined
    const receipt = report.validation ? evidenceLine(report.validation).slice(0, 160) : undefined

    nodes.push({
      id: `report-${report.id}`,
      stage: formatReportStageTitle(report, Math.max(1, reviewRound)),
      actor,
      actorRole,
      tone,
      timeLabel: formatClockLabel(report.created_at),
      deltaLabel: stepDelta(report.created_at),
      badge,
      filesTouched,
      receipt,
      summary: msg.slice(0, 280) || report.state,
      reportId: report.id,
      sessionId: report.session_id || null,
      tabId: session?.tab_id ?? null,
    })
  })

  // Initiator progress fallback when no managed session reports exist (or newer progress arrived)
  if (task.latest_progress && reports.length === 0) {
    const prog = task.latest_progress
    const pTone: SpineNodeTone =
      prog.state === 'completed'
        ? 'pass'
        : prog.state === 'failed' || prog.state === 'blocked' || prog.state === 'needs_input'
          ? 'warn'
          : 'live'
    nodes.push({
      id: `progress-${task.id}-${task.progress_revision ?? 0}`,
      stage: `Initiator progress · ${prog.state.replace(/_/g, ' ')}`,
      actor: task.execution_ref?.provider || task.agent_type || 'Initiator',
      actorRole: 'worker',
      tone: pTone,
      timeLabel: formatClockLabel(prog.reported_at || task.updated_at),
      deltaLabel: stepDelta(prog.reported_at || task.updated_at),
      badge: prog.state.toUpperCase(),
      receipt: prog.validation ? evidenceLine(prog.validation).slice(0, 160) : undefined,
      summary: evidenceLine(prog.summary).slice(0, 280),
      tabId: task.source?.tab_id ?? null,
    })
  }

  if (task.status === 'done' && task.completed_at) {
    nodes.push({
      id: `done-${task.id}`,
      stage: task.human_accepted_at ? 'Accepted & closed' : 'Completed',
      actor: task.human_accepted_at ? 'Operator' : 'Hub',
      actorRole: 'orchestrator',
      tone: 'pass',
      timeLabel: formatClockLabel(task.completed_at),
      deltaLabel: stepDelta(task.completed_at),
      badge: 'DONE',
      summary: task.human_accepted_at
        ? 'Human acceptance recorded and task finalized.'
        : 'Task reached terminal done state.',
    })
  } else if (task.status === 'failed' && task.failure_reason) {
    nodes.push({
      id: `failed-${task.id}`,
      stage: 'Task stopped with failure',
      actor: 'Hub Monitor',
      actorRole: 'orchestrator',
      tone: 'warn',
      timeLabel: formatClockLabel(task.failed_at || task.updated_at),
      deltaLabel: stepDelta(task.failed_at || task.updated_at),
      badge: 'FAILED',
      summary: evidenceLine(task.failure_reason).slice(0, 260),
    })
  }

  return nodes
}

export function buildTriageTaskViewModel(input: {
  task: WorkspaceTask
  workspaceName: string
  defaultBranch: string
  reports: readonly AgentReport[]
  events?: readonly WorkspaceTaskEvent[]
  markdownDocs?: readonly { path: string; report_id?: string | null }[]
  sessionsById: ReadonlyMap<string, ManagedSession>
  lifecycle?: TaskLifecycle
  nowMs: number
}): TriageTaskViewModel {
  const {
    task,
    workspaceName,
    defaultBranch,
    reports,
    events = [],
    markdownDocs = [],
    sessionsById,
    lifecycle,
    nowMs,
  } = input
  const ev = taskEvidence(task, reports)
  const bucket = classifyTriageBucket(task, lifecycle)
  const statusTone = classifyStatusTone(task, bucket)
  const statusLabel = deriveStatusLabel(task, reports, lifecycle)

  const workerSession = task.session_id ? sessionsById.get(task.session_id) : undefined
  const reviewerSession = task.review_session_id ? sessionsById.get(task.review_session_id) : undefined

  const createdMs = parseTimestampMs(task.started_at || task.created_at)
  const endMs =
    parseTimestampMs(task.completed_at) ??
    parseTimestampMs(task.failed_at) ??
    (bucket === 'done' ? parseTimestampMs(task.updated_at) ?? nowMs : nowMs)
  const elapsedLabel = createdMs !== null ? formatElapsedDuration(Math.max(0, endMs - createdMs)) : '—'

  const updatedMs = parseTimestampMs(task.updated_at)
  const updatedAgoLabel =
    updatedMs !== null
      ? nowMs - updatedMs < 45_000
        ? 'just now'
        : `${formatElapsedDuration(Math.max(0, nowMs - updatedMs))} ago`
      : ''

  const allTextSources = [
    task.prompt,
    ev.outcome,
    ev.validation,
    ...reports.map(r => r.message),
  ]
  const code = deriveTaskCode(task, allTextSources)

  const mergedLinkMap = new Map<string, { label: string; url: string }>()
  const linkCandidates = [
    ...ev.links,
    ...evidenceLinks([
      task.prompt,
      ...reports.map(r => r.message_zh || r.message || r.message_en || ''),
      ...reports.flatMap(r => r.artifact_refs ?? []),
    ]),
  ]
  for (const link of linkCandidates) {
    if (!mergedLinkMap.has(link.url)) {
      mergedLinkMap.set(link.url, link)
    }
  }
  const links: TriageArtifactLink[] = [...mergedLinkMap.values()].map(link => {
    const kind = classifyLinkKind(link.url)
    return {
      label: link.label,
      url: link.url,
      kind,
      meta: linkHostMeta(link.url, kind),
    }
  })

  const passedChecks = ev.acceptance.filter(c => c.status === 'passed').length
  const closeReason = task.closed_reason || lifecycle?.reason
  const checksLabel =
    ev.acceptance.length > 0
      ? `${passedChecks}/${ev.acceptance.length} criteria passed`
      : ev.validation
        ? evidenceLine(ev.validation).slice(0, 48)
        : (task.status === 'done' || lifecycle?.phase === 'done')
          ? (closeReason === 'chat_archived'
              ? 'Closed by archive'
              : closeReason === 'mr_merged'
                ? 'MR merged'
                : 'Verified')
          : 'Pending verification'

  const implementer = workerSession?.title || task.execution_ref?.provider || task.agent_type || 'Agent'
  const reviewer =
    reviewerSession?.title ||
    (task.task_mode === 'reviewed' ? 'Independent Reviewer' : task.task_mode === 'subagent' ? 'Controller Gate' : 'Direct')

  const reviewVerdicts = reports.filter(r => r.state === 'review_passed' || r.state === 'review_failed' || r.state === 'review_needs_input')
  const latestVerdict = reviewVerdicts[reviewVerdicts.length - 1]
  let verdictLabel = 'IN PROGRESS'
  let verdictTone: TriageConsensusInfo['verdictTone'] = 'live'
  if (task.status === 'failed') {
    verdictLabel = 'FAILED'
    verdictTone = 'warn'
  } else if (latestVerdict?.state === 'review_passed' || task.status === 'done' || lifecycle?.phase === 'done') {
    verdictLabel = latestVerdict?.state === 'review_passed'
      ? 'PASS · VERIFIED'
      : closeReason === 'chat_archived'
        ? 'CLOSED BY ARCHIVE'
        : closeReason === 'mr_merged'
          ? 'MR MERGED'
          : 'COMPLETED'
    verdictTone = closeReason === 'chat_archived' ? 'auto' : 'pass'
  } else if (latestVerdict?.state === 'review_failed' || latestVerdict?.state === 'review_needs_input') {
    verdictLabel = 'CHANGES REQUESTED'
    verdictTone = 'warn'
  } else if (task.status === 'review') {
    verdictLabel = 'AWAITING ACCEPTANCE'
    verdictTone = 'pass'
  } else if (task.status === 'todo') {
    verdictLabel = 'NOT STARTED'
    verdictTone = 'auto'
  }

  const roundsCount = Math.max(task.review_attempts || 0, reviewVerdicts.length)
  const roundsLabel =
    roundsCount > 0
      ? `${roundsCount} review round${roundsCount > 1 ? 's' : ''} · ${statusLabel}`
      : task.task_mode === 'reviewed'
        ? 'Independent cross-review policy active'
        : `${task.task_mode} mode`

  const metricHighlight =
    ev.changedFiles.length > 0
      ? `${ev.changedFiles.length} changed file${ev.changedFiles.length > 1 ? 's' : ''} · ${checksLabel}`
      : checksLabel

  const outcomeSummary =
    ev.outcome ||
    evidenceLine(task.goal_packet?.objective) ||
    evidenceLine(task.prompt).slice(0, 240) ||
    'No summary reported yet.'

  const decisionLine =
    bucket === 'needs_you'
      ? (lifecycle?.attention && lifecycle.reason_text
          ? lifecycle.reason_text
          : task.status === 'failed'
            ? (task.failure_reason || 'Task stopped with an error; steer to retry or abort.')
            : 'Waiting for your answer or steering instructions.')
      : bucket === 'review'
        ? (latestVerdict?.state === 'review_passed'
            ? 'Independent review passed. Ready for acceptance or merge.'
            : lifecycle?.delivery_mr_state === 'open' && lifecycle.delivery_mr_label
              ? `${lifecycle.delivery_mr_label} is open — closes automatically once merged.`
              : 'Deliverable ready for review or acceptance.')
        : ''

  const liveStep =
    bucket === 'working'
      ? (evidenceLine(task.latest_progress?.summary) ||
         evidenceLine(reports[reports.length - 1]?.message) ||
         (task.status === 'todo' ? 'Ready to start when dispatched' : 'Agent working…'))
      : ''

  const spine = buildTaskSpine(task, reports, sessionsById)

  const newestReport = evidenceReport(reports)
  const rawDigestSource =
    (task.status === 'failed' && task.failure_reason ? task.failure_reason : '') ||
    (newestReport ? (newestReport.message_zh || newestReport.message || newestReport.message_en || '') : '') ||
    (task.latest_progress?.summary || '')
  const digest = parseStructuredDigest(rawDigestSource, task, lifecycle)
  const deliverables = extractDeliverables(task, reports, markdownDocs)
  const validation = parseStructuredValidation(ev.validation, ev.risks, ev.acceptance)
  const milestones = buildKeyMilestones({
    task,
    reports,
    events,
    sessionsById,
    currentHeadline: digest.headline,
  })
  const macroStages = buildMacroStageBar(task, milestones, bucket)
  const hasIndependentReview = Boolean(reviewerSession) || reviewVerdicts.length > 0
  const reviewSummaryChip = hasIndependentReview
    ? `${implementer} → ${reviewer} · ${roundsCount > 0 ? `R${roundsCount} ` : ''}${
        latestVerdict?.state === 'review_passed'
          ? 'PASS'
          : latestVerdict?.state === 'review_failed'
            ? 'Changes requested'
            : statusLabel
      }`
    : null

  return {
    task,
    id: task.id,
    code,
    title: task.title,
    workspaceName,
    branchLabel: workerSession?.branch || defaultBranch || 'main',
    bucket,
    statusLabel,
    statusTone,
    elapsedLabel,
    updatedAgoLabel,
    outcomeSummary,
    decisionLine,
    liveStep,
    checksLabel,
    changedFilesCount: ev.changedFiles.length,
    consensus: {
      implementer,
      reviewer,
      roundsLabel,
      verdictLabel,
      verdictTone,
      metricHighlight,
    },
    links,
    spine,
    digest,
    deliverables,
    validation,
    milestones,
    macroStages,
    hasIndependentReview,
    reviewSummaryChip,
    internalMeta: {
      control: task.execution_control || 'workspace',
      epoch: task.execution_epoch ?? 1,
      mode: task.task_mode,
      seat: workerSession ? `${workerSession.title}${workerSession.ephemeral ? ' · ephemeral' : ''}` : 'unassigned',
    },
  }
}

export function groupTriageTasks(items: readonly TriageTaskViewModel[]): TriageGroup[] {
  const buckets: Record<TriageBucket, TriageTaskViewModel[]> = {
    needs_you: [],
    working: [],
    review: [],
    done: [],
  }
  for (const item of items) {
    buckets[item.bucket].push(item)
  }
  const byUpdatedDesc = (a: TriageTaskViewModel, b: TriageTaskViewModel) =>
    (parseTimestampMs(b.task.updated_at) ?? 0) - (parseTimestampMs(a.task.updated_at) ?? 0)

  buckets.needs_you.sort(byUpdatedDesc)
  buckets.working.sort(byUpdatedDesc)
  buckets.review.sort(byUpdatedDesc)
  buckets.done.sort(byUpdatedDesc)

  return (['needs_you', 'working', 'review', 'done'] as TriageBucket[]).map(key => ({
    key,
    label: GROUP_LABELS[key],
    items: buckets[key],
  }))
}
