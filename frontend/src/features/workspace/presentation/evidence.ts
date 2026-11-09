import type {
  AcceptanceCheck,
  AcceptanceCheckStatus,
  AgentReport,
  WorkspaceTask,
} from '@/types'
import { isSubstantiveReport } from './reports'

/**
 * Evidence presented before a human accepts a task: the acceptance criteria
 * with the executor's per-criterion verdict, the reported validation, the
 * changed files, artifacts and risks. It summarizes the newest substantive
 * report plus the newest progress update; the full report history stays in
 * the Progress section and internal execution fields move to Debug info.
 */

export type EvidenceCriterionStatus = AcceptanceCheckStatus | 'unreported'

export interface EvidenceCriterion {
  criterion: string
  status: EvidenceCriterionStatus
  evidence: string | null
}

export interface EvidenceLink {
  label: string
  url: string
}

export interface TaskEvidence {
  /** Single-line outcome for the dialog header. */
  outcome: string
  outcomeIsFailure: boolean
  acceptance: EvidenceCriterion[]
  validation: string | null
  risks: string | null
  changedFiles: string[]
  artifactRefs: string[]
  links: EvidenceLink[]
  reportId: string | null
  reportedAt: string | null
  hasContent: boolean
}

const MAX_EVIDENCE_LINKS = 6

/** Newest report that carries evidence; falls back to the newest report. */
export function evidenceReport(reports: readonly AgentReport[]): AgentReport | null {
  if (reports.length === 0) return null
  for (let index = reports.length - 1; index >= 0; index -= 1) {
    if (isSubstantiveReport(reports[index])) return reports[index]
  }
  return reports[reports.length - 1]
}

export function normalizeEvidenceText(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, ' ')
}

/** Single-line, whitespace-collapsed text for headers and card summaries. */
export function evidenceLine(value: string | null | undefined): string {
  return (value ?? '').replace(/\s+/g, ' ').trim()
}

function acceptanceChecks(report: AgentReport | null): AcceptanceCheck[] {
  return Array.isArray(report?.acceptance_check) ? report!.acceptance_check! : []
}

/**
 * Goal Packet criteria are the review contract. When the executor reported
 * per-criterion checks, match them by normalized text so a criterion keeps its
 * ordinal position and an unreported check stays visibly unreported.
 */
export function evidenceAcceptance(
  task: WorkspaceTask,
  report: AgentReport | null,
): EvidenceCriterion[] {
  const criteria = Array.isArray(task.goal_packet?.acceptance_criteria)
    ? task.goal_packet!.acceptance_criteria!
    : []
  const checks = acceptanceChecks(report)
  const checksByCriterion = new Map<string, AcceptanceCheck>()
  for (const check of checks) {
    checksByCriterion.set(normalizeEvidenceText(check.criterion), check)
  }

  if (criteria.length > 0) {
    return criteria.map((criterion) => {
      const check = checksByCriterion.get(normalizeEvidenceText(criterion))
      return {
        criterion,
        status: check ? check.status : 'unreported',
        evidence: check?.evidence || null,
      }
    })
  }
  // No Goal Packet criteria: still show what the executor verified.
  return checks.map(check => ({
    criterion: check.criterion,
    status: check.status,
    evidence: check.evidence || null,
  }))
}

const LINK_PATTERN = /(https?:\/\/[^\s<>()[\]"'|]+)/g
const MARKDOWN_LINK_PATTERN = /\[([^\]\n]{1,120})\]\(([^)\s]+)\)/g
const TRAILING_PUNCTUATION = /[.,;:!?)\]]+$/

function linkLabel(url: string): string | null {
  const mergeRequest = url.match(/merge_requests?\/(\d+)/)
  if (mergeRequest) return `MR ${mergeRequest[1]}`
  const pullRequest = url.match(/\/pull\/(\d+)/)
  if (pullRequest) return `PR ${pullRequest[1]}`
  const pipeline = url.match(/pipelines?\/(\d+)/)
  if (pipeline) return `CI pipeline ${pipeline[1]}`
  const job = url.match(/(?:\/-\/)?jobs\/(\d+)/)
  if (job) return `CI job ${job[1]}`
  const workflowRun = url.match(/(?:actions\/runs|\/runs)\/(\d+)/)
  if (workflowRun) return `CI run ${workflowRun[1]}`
  return null
}

function shortUrlLabel(url: string): string {
  try {
    const parsed = new URL(url)
    const path = parsed.pathname.replace(/\/$/, '')
    const label = `${parsed.hostname}${path}`
    return label.length > 42 ? `${label.slice(0, 39)}…` : label
  } catch {
    return url.length > 42 ? `${url.slice(0, 39)}…` : url
  }
}

/**
 * Pull review/CI destinations out of free text so a reviewer can open the MR
 * or pipeline instead of re-reading the prose. Markdown link labels win over a
 * derived label because the executor already named the destination.
 */
export function evidenceLinks(sources: readonly (string | null | undefined)[]): EvidenceLink[] {
  const links: EvidenceLink[] = []
  const seen = new Set<string>()
  const add = (url: string, label?: string | null) => {
    const trimmed = url.trim().replace(TRAILING_PUNCTUATION, '')
    if (!/^https?:\/\//.test(trimmed) || seen.has(trimmed)) return
    seen.add(trimmed)
    const named = (label ?? '').trim()
    links.push({
      label: linkLabel(trimmed) ?? (named || shortUrlLabel(trimmed)),
      url: trimmed,
    })
  }

  for (const source of sources) {
    if (!source) continue
    for (const match of source.matchAll(MARKDOWN_LINK_PATTERN)) {
      add(match[2], match[1])
    }
    for (const match of source.matchAll(LINK_PATTERN)) {
      add(match[1])
    }
  }
  return links.slice(0, MAX_EVIDENCE_LINKS)
}

function uniqueStrings(values: readonly (string | null | undefined)[]): string[] {
  const result: string[] = []
  for (const value of values) {
    const trimmed = (value ?? '').trim()
    if (trimmed && !result.includes(trimmed)) result.push(trimmed)
  }
  return result
}

export function taskEvidence(
  task: WorkspaceTask,
  reports: readonly AgentReport[],
): TaskEvidence {
  const report = evidenceReport(reports)
  const progress = task.latest_progress ?? null
  const failureReason = evidenceLine(task.failure_reason)
  const progressSummary = evidenceLine(progress?.summary)
  const reportSummary = evidenceLine(report?.message_en || report?.message)
  const outcomeIsFailure = task.status === 'failed' && Boolean(failureReason)
  const outcome = outcomeIsFailure
    ? failureReason
    : progressSummary || reportSummary || failureReason

  const validation = progress?.validation?.trim() || report?.validation?.trim() || null
  const risks = progress?.risks?.trim() || report?.risks?.trim() || null
  const changedFiles = uniqueStrings((report?.changed_files ?? []).map(file => file))
  const artifactRefs = uniqueStrings([
    ...(progress?.artifact_refs ?? []),
    ...(report?.artifact_refs ?? []),
  ])
  const acceptance = evidenceAcceptance(task, report)
  const links = evidenceLinks([
    progress?.summary,
    progress?.validation,
    progress?.risks,
    ...(progress?.artifact_refs ?? []),
    report?.message_en,
    report?.message,
    report?.validation,
    report?.risks,
    ...(report?.artifact_refs ?? []),
  ])

  return {
    outcome,
    outcomeIsFailure,
    acceptance,
    validation,
    risks,
    changedFiles,
    artifactRefs,
    links,
    reportId: report?.id ?? null,
    reportedAt: progress?.reported_at ?? report?.created_at ?? null,
    hasContent: Boolean(
      acceptance.length
      || validation
      || risks
      || changedFiles.length
      || artifactRefs.length
      || links.length,
    ),
  }
}
