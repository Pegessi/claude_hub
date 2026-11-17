import type {
  AcceptanceCheckStatus,
  AgentReport,
  ManagedSession,
  TaskLifecycle,
  WorkspaceTask,
  WorkspaceTaskEvent,
} from '@/types'
import { formatElapsedDuration, parseTimestampMs } from '@/utils/duration'
import type { EvidenceCriterion } from './evidence'
import { evidenceLine, evidenceLinks } from './evidence'
import { isMarkdownArtifact } from './reports'

export interface DigestBullet {
  label?: string
  text: string
}

export interface StructuredDigest {
  headline: string
  bullets: DigestBullet[]
  actionPrompt: string | null
  rawText: string
}

export interface DeliverableItem {
  key: string
  kind: 'markdown' | 'mr' | 'doc' | 'arena' | 'infowall' | 'branch' | 'file' | 'link'
  title: string
  subtitle: string
  url?: string
  artifactPath?: string
  reportId?: string
  copyText?: string
}

export interface ValidationPill {
  label: string
  status: 'pass' | 'fail' | 'info'
}

export interface StructuredValidation {
  contextRef: string | null
  summaryLabel: string
  passedCount: number
  failedCount: number
  pills: ValidationPill[]
  notes: string[]
  riskBullets: string[]
}

export interface MilestoneStep {
  id: string
  phase: 'start' | 'plan' | 'work' | 'review' | 'decision' | 'done'
  tone: 'neutral' | 'live' | 'warn' | 'pass'
  title: string
  oneLiner: string
  timeLabel: string
  deltaLabel: string
  badge?: string
  detailText?: string
  collapsedCount?: number
  isCurrentEcho?: boolean
  sessionId?: string | null
  tabId?: string | null
}

export interface MacroStagePill {
  key: string
  label: string
  sublabel?: string
  state: 'done' | 'current' | 'warn' | 'upcoming'
}

function normalizeEventIso(iso: string | null | undefined): string | null | undefined {
  if (!iso) return iso
  const trimmed = iso.trim()
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(trimmed)) {
    return `${trimmed.replace(' ', 'T')}Z`
  }
  return trimmed
}

function formatClock(iso: string | null | undefined): string {
  const ms = parseTimestampMs(iso)
  if (ms === null) return '--:--'
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/**
 * Split dense Chinese/English prose into clean sentences without breaking
 * decimal numbers (14997.525s), versions (v7.3, M14.2), or filenames (.md).
 */
export function splitSmartSentences(raw: string): string[] {
  const normalized = raw.replace(/\r\n/g, '\n').trim()
  if (!normalized) return []

  const results: string[] = []
  for (const rawLine of normalized.split(/\n+/)) {
    const line = rawLine.trim()
    if (!line) continue
    // Split on Chinese sentence-ending punctuation or semicolons, or English ". " followed by uppercase/CJK
    const parts = line
      .split(/(?<=[。！？；])\s*|(?<=[^0-9A-Za-z_]|[a-z0-9)\]])\.\s+(?=[A-Z\u4e00-\u9fa5])/g)
      .map(s =>
        s
          .replace(/^[•\-*]\s*/, '')
          .replace(/[;；]+$/, '')
          .replace(/\s*[;；]?\s*no lessons needed[.。]?$/i, '')
          .trim(),
      )
      .filter(Boolean)
    results.push(...parts)
  }
  return results
}

const DECISION_SENTENCE_RE =
  /(?:remaining decision|decision needed|next decision|needs? your (?:input|decision|approval|confirmation)|whether to (?:merge|proceed|stop|continue)|awaiting human|需确认|待确认|请确认|需要你|待人工|阻塞点|必须修复|剩余决策|请拍板|是否继续|是否合入|[?？]$)/i

const LABELED_LINE_RE =
  /^(结论|摘要|验收汇总|必须修复|阻塞点?|风险|备注|交付|Verdict|Summary|Checks|Must fix|Blocker|Notes?)\s*[：:]\s*(.+)$/i

const NOISE_REPORT_LINE_RE =
  /^(?:结论\s*[：:]\s*review_\w+|verdict\s*[：:]\s*review_\w+|no lessons needed[.。]?)$/i

const SYSTEM_ECHO_REPORT_RE =
  /^(?:Goal Packet (?:reviewer requested changes|approved)\.|开始(?:修订)?目标包评审|开始实施证据评审|收到重复触发)/i

export function parseStructuredDigest(
  rawText: string,
  task: WorkspaceTask,
  lifecycle?: TaskLifecycle,
): StructuredDigest {
  const trimmed = rawText.trim()
  if (!trimmed) {
    const fallbackHeadline =
      task.status === 'todo'
        ? 'Queued in backlog — ready to start.'
        : task.status === 'done'
          ? 'Completed.'
          : 'In progress — waiting for next milestone report.'
    return {
      headline: fallbackHeadline,
      bullets: [],
      actionPrompt: lifecycle?.attention ? lifecycle.reason_text || null : null,
      rawText: '',
    }
  }

  // Case 1: Labeled multi-line report (e.g. 结论 / 摘要 / 验收汇总 / 必须修复 / 备注)
  const rawLines = trimmed.split(/\n+/).map(l => l.trim()).filter(Boolean)
  const labeledMatches = rawLines.map(l => l.match(LABELED_LINE_RE)).filter(Boolean) as RegExpMatchArray[]
  if (labeledMatches.length >= 2) {
    let headline = ''
    let actionPrompt: string | null = null
    const bullets: DigestBullet[] = []

    for (const line of rawLines) {
      if (NOISE_REPORT_LINE_RE.test(line)) continue
      const m = line.match(LABELED_LINE_RE)
      if (!m) {
        if (!headline) headline = line
        else bullets.push({ text: line })
        continue
      }
      const key = m[1].trim()
      const val = m[2].replace(/\s*[;；]?\s*no lessons needed[.。]?$/i, '').trim()
      if (!val || /^review_\w+$/i.test(val)) continue
      if (/^(摘要|Summary)$/i.test(key) && !headline) {
        headline = val
      } else if (/^(必须修复|阻塞点?|Must fix|Blocker)$/i.test(key) && !actionPrompt) {
        actionPrompt = val
      } else {
        bullets.push({ label: key, text: val })
      }
    }

    if (!headline && bullets.length > 0) {
      const first = bullets.shift()!
      headline = first.text
    }
    if (!actionPrompt && lifecycle?.attention && lifecycle.reason_text) {
      actionPrompt = lifecycle.reason_text
    }
    return { headline: headline || evidenceLine(trimmed), bullets, actionPrompt, rawText: trimmed }
  }

  // Case 2: Dense paragraph prose -> extract explicit conclusion, decision prompt, and crisp bullets
  const sentences = splitSmartSentences(trimmed).filter(s => !NOISE_REPORT_LINE_RE.test(s))
  if (sentences.length === 0) {
    return { headline: evidenceLine(trimmed), bullets: [], actionPrompt: null, rawText: trimmed }
  }

  let actionPrompt: string | null = null
  const bodySentences: string[] = []

  for (const s of sentences) {
    if (!actionPrompt && sentences.length > 1 && DECISION_SENTENCE_RE.test(s)) {
      actionPrompt = s.replace(/^(?:remaining decision is (?:the user's)?\s*[：:]?\s*)/i, 'Decision needed: ')
    } else {
      bodySentences.push(s)
    }
  }

  if (bodySentences.length === 0 && actionPrompt) {
    bodySentences.push(actionPrompt)
    actionPrompt = null
  }

  // Promote explicit "结论：..." sentence to headline if it appeared at the end of an investigation paragraph
  let headlineIdx = bodySentences.findIndex(s => /^(?:结论|Conclusion|Summary)\s*[：:]/i.test(s))
  if (headlineIdx < 0) headlineIdx = 0

  const rawHeadline = bodySentences[headlineIdx] || sentences[0]
  let headline = rawHeadline.replace(/^(?:结论|Conclusion|Summary)\s*[：:]\s*/i, '').trim()
  const bullets: DigestBullet[] = bodySentences
    .filter((_, idx) => idx !== headlineIdx)
    .map(text => ({ text }))

  // When the opening sentence is a long compound statement with a colon-delimited breakdown
  // (e.g. "A third review found 10 items: 1 blocking (...), 2 high (...)"), keep the concise
  // lead clause as the headline and move the breakdown into the first bullet for scannability.
  if (headline.length > 135) {
    const colonMatch = headline.match(/^([^：:]{24,130})[：:]\s*(.{20,})$/)
    if (colonMatch) {
      headline = colonMatch[1].trim()
      bullets.unshift({ label: 'Breakdown', text: colonMatch[2].trim() })
    }
  }

  if (!actionPrompt && lifecycle?.attention && lifecycle.reason_text) {
    actionPrompt = lifecycle.reason_text
  }

  return {
    headline,
    bullets,
    actionPrompt,
    rawText: trimmed,
  }
}

function classifyUrlKind(url: string): DeliverableItem['kind'] {
  if (/merge_requests?\/|\/pull\//i.test(url)) return 'mr'
  if (/larkoffice\.com|feishu\.cn|\/docx\/|\/wiki\//i.test(url)) return 'doc'
  if (/evaluation\/arena|trial_id|jobs\//i.test(url)) return 'arena'
  if (/infowall/i.test(url)) return 'infowall'
  return 'link'
}

function urlSubtitle(url: string, kind: DeliverableItem['kind']): string {
  try {
    const parsed = new URL(url)
    if (kind === 'mr') {
      const parts = parsed.pathname.split('/').filter(Boolean)
      const mrNum = url.match(/(?:merge_requests?|pull)\/(\d+)/i)?.[1]
      const repo = parts.slice(0, 2).join('/')
      return mrNum ? `${repo} · !${mrNum}` : repo || parsed.hostname
    }
    if (kind === 'doc') return 'Feishu Document'
    if (kind === 'arena') return 'Seed Arena Evaluation'
    if (kind === 'infowall') return 'InfoWall Record'
    return parsed.hostname
  } catch {
    return url
  }
}

function cleanFileLabel(pathStr: string): { name: string; dir: string } {
  const trimmed = pathStr.trim().replace(/[.,;)]+$/, '')
  const parts = trimmed.split('/').filter(Boolean)
  const name = parts[parts.length - 1] || trimmed
  const dir = parts.length >= 2 ? parts.slice(-3, -1).join('/') : ''
  return { name, dir }
}

function looksLikeFilesystemArtifact(ref: string): boolean {
  const s = ref.trim()
  if (!s || s.includes(' ') || /^https?:\/\//i.test(s)) return false
  return s.includes('/') || /\.(?:md|json|csv|py|ts|vue|patch|log|html|pdf)$/i.test(s)
}

export function extractDeliverables(
  task: WorkspaceTask,
  reports: readonly AgentReport[],
  markdownDocs: readonly { path: string; report_id?: string | null }[],
): DeliverableItem[] {
  const items: DeliverableItem[] = []
  const seen = new Set<string>()

  // 1. Markdown documents (previewable right inside the Hub!)
  const mdCandidates: { path: string; reportId?: string }[] = []
  for (const doc of markdownDocs) {
    mdCandidates.push({ path: doc.path, reportId: doc.report_id ?? undefined })
  }
  for (const ref of task.latest_progress?.artifact_refs ?? []) {
    if (looksLikeFilesystemArtifact(ref) && isMarkdownArtifact(ref)) {
      mdCandidates.push({ path: ref.trim() })
    }
  }
  for (let i = reports.length - 1; i >= 0; i -= 1) {
    const r = reports[i]
    for (const ref of [...(r.artifact_refs ?? []), ...(r.changed_files ?? [])]) {
      if (looksLikeFilesystemArtifact(ref) && isMarkdownArtifact(ref)) {
        mdCandidates.push({ path: ref.trim(), reportId: r.id })
      }
    }
  }

  for (const md of mdCandidates) {
    const { name, dir } = cleanFileLabel(md.path)
    const dedupKey = `md:${md.path}`
    if (seen.has(dedupKey)) continue
    seen.add(dedupKey)
    items.push({
      key: dedupKey,
      kind: 'markdown',
      title: name,
      subtitle: dir ? `${dir}/ · Previewable` : 'Markdown report · Previewable',
      artifactPath: md.path,
      reportId: md.reportId,
    })
  }

  // 2. External URLs (MRs, Feishu docs, Arena, InfoWall)
  const textSources = [
    task.latest_progress?.summary,
    task.latest_progress?.validation,
    ...reports.map(r => r.message_zh || r.message || r.message_en || ''),
    ...reports.flatMap(r => r.artifact_refs ?? []),
    task.prompt,
  ]
  for (const link of evidenceLinks(textSources)) {
    const dedupKey = `url:${link.url}`
    if (seen.has(dedupKey)) continue
    seen.add(dedupKey)
    const kind = classifyUrlKind(link.url)
    items.push({
      key: dedupKey,
      kind,
      title: link.label,
      subtitle: urlSubtitle(link.url, kind),
      url: link.url,
    })
  }

  // 3. Branch / Commit delivery references in latest summary (supports backticks e.g. `feat/foo` @ `2e743b4`)
  const latestBlob = [
    task.latest_progress?.summary || '',
    reports[reports.length - 1]?.message_zh || reports[reports.length - 1]?.message || '',
  ].join(' ')
  const branchCommitMatch =
    latestBlob.match(
      /`?\b((?:feat|fix|chore|refactor|style|docs|codex|wzh)\/[A-Za-z0-9._/-]+)`?\s*(?:@|at\b|,\s*HEAD)\s*\*?\*?`?([0-9a-f]{7,40})\b/i,
    ) ||
    latestBlob.match(/`?\b(main)`?\s*(?:=|@)\s*`?([0-9a-f]{7,40})\b/i)
  if (branchCommitMatch) {
    const branch = branchCommitMatch[1]
    const sha = branchCommitMatch[2].slice(0, 7)
    const dedupKey = `branch:${branch}@${sha}`
    if (!seen.has(dedupKey)) {
      seen.add(dedupKey)
      items.push({
        key: dedupKey,
        kind: 'branch',
        title: `${branch} @ ${sha}`,
        subtitle: 'Git branch & commit',
        copyText: `${branch} ${branchCommitMatch[2]}`,
      })
    }
  }

  // 4. Non-markdown file artifacts (JSON, CSV, scripts - click to copy path)
  const otherRefs = [
    ...(task.latest_progress?.artifact_refs ?? []),
    ...(reports[reports.length - 1]?.artifact_refs ?? []),
  ]
  for (const ref of otherRefs) {
    if (!looksLikeFilesystemArtifact(ref) || isMarkdownArtifact(ref)) continue
    const dedupKey = `file:${ref.trim()}`
    if (seen.has(dedupKey) || items.length >= 8) continue
    seen.add(dedupKey)
    const { name, dir } = cleanFileLabel(ref)
    items.push({
      key: dedupKey,
      kind: 'file',
      title: name,
      subtitle: dir ? `${dir}/` : ref.trim(),
      copyText: ref.trim(),
    })
  }

  return items.slice(0, 8)
}

export function parseStructuredValidation(
  validationRaw: string | null | undefined,
  risksRaw: string | null | undefined,
  acceptance: readonly EvidenceCriterion[],
): StructuredValidation {
  const pills: ValidationPill[] = []
  const notes: string[] = []
  let contextRef: string | null = null

  // Acceptance criteria checks first
  for (const item of acceptance) {
    if (item.status === 'unreported') continue
    const status: ValidationPill['status'] =
      item.status === ('passed' as AcceptanceCheckStatus)
        ? 'pass'
        : item.status === ('failed' as AcceptanceCheckStatus)
          ? 'fail'
          : 'info'
    pills.push({
      label: item.criterion.length > 44 ? `${item.criterion.slice(0, 42)}…` : item.criterion,
      status,
    })
  }

  const raw = (validationRaw || '').trim()
  if (raw) {
    let body = raw
    const prefixMatch = body.match(/^(At\s+[0-9a-f]{7,12}|Commit\s+[0-9a-f]{7,12})\s*:\s*/i)
    if (prefixMatch) {
      contextRef = prefixMatch[1]
      body = body.slice(prefixMatch[0].length)
    }

    const segments = body
      .split(/[;；\n]+/)
      .map(s => s.replace(/^[•\-*]\s*/, '').trim())
      .filter(Boolean)

    for (const seg of segments) {
      const isPass = /\b(?:PASS|passed|all_passed|clean|exited\s*0)\b|(?:\b0\s+failed\b)|通过/i.test(seg)
      const isFail =
        !isPass && (/\b(?:FAIL|failed|ERROR)\b|失败|未通过|阻塞/i.test(seg))

      if ((isPass || isFail) && seg.length <= 68) {
        const cleanLabel = seg
          .replace(/\bPASS\b\s*/gi, '')
          .replace(/\s+/g, ' ')
          .trim()
        pills.push({
          label: cleanLabel || seg,
          status: isFail ? 'fail' : 'pass',
        })
      } else {
        notes.push(...splitSmartSentences(seg))
      }
    }
  }

  const passedCount = pills.filter(p => p.status === 'pass').length
  const failedCount = pills.filter(p => p.status === 'fail').length

  let summaryLabel = 'No verification recorded yet'
  if (failedCount > 0) {
    summaryLabel = `${failedCount} failed · ${passedCount} passed`
  } else if (passedCount > 0) {
    summaryLabel = `${passedCount} ${passedCount === 1 ? 'check' : 'checks'} passed`
  } else if (notes.length > 0) {
    summaryLabel = 'Verification notes recorded'
  }

  const riskBullets = splitSmartSentences(risksRaw || '').slice(0, 5)

  return {
    contextRef,
    summaryLabel,
    passedCount,
    failedCount,
    pills,
    notes: notes.slice(0, 5),
    riskBullets,
  }
}

function summarizeReportOneLiner(report: AgentReport): string {
  const raw = (report.message_zh || report.message || report.message_en || '').trim()
  if (!raw) return report.state.replace(/_/g, ' ')
  const lines = raw.split(/\n+/).map(l => l.trim()).filter(l => l && !NOISE_REPORT_LINE_RE.test(l))
  for (const line of lines) {
    const m = line.match(LABELED_LINE_RE)
    if (m && /^(摘要|Summary|必须修复|Must fix)$/i.test(m[1])) {
      return m[2].trim()
    }
  }
  const first = splitSmartSentences(lines.join(' '))[0] || lines[0] || raw
  const cleaned = first.replace(LABELED_LINE_RE, '$2').trim()
  return cleaned.length > 96 ? `${cleaned.slice(0, 94)}…` : cleaned
}

function isPlanPhaseReport(report: AgentReport, idx: number, all: readonly AgentReport[]): boolean {
  const raw = `${report.message_zh || ''} ${report.message || ''}`
  if (/目标包|Goal Packet/i.test(raw)) return true
  // Early review before any ready_for_review submission is Goal Packet review
  const firstImplSubmit = all.findIndex(r => r.state === 'ready_for_review' || (r.changed_files?.length ?? 0) > 0)
  if (firstImplSubmit > 0 && idx < firstImplSubmit && report.state.startsWith('review_')) {
    return true
  }
  return false
}

export function buildKeyMilestones(input: {
  task: WorkspaceTask
  reports: readonly AgentReport[]
  events: readonly WorkspaceTaskEvent[]
  sessionsById: ReadonlyMap<string, ManagedSession>
  currentHeadline: string
}): MilestoneStep[] {
  const { task, reports, events, sessionsById, currentHeadline } = input
  const steps: MilestoneStep[] = []
  let prevMs = parseTimestampMs(task.started_at || task.created_at)

  const deltaFrom = (iso: string | null | undefined): string => {
    const ms = parseTimestampMs(iso)
    if (ms === null || prevMs === null) return ''
    const diff = Math.max(0, ms - prevMs)
    prevMs = ms
    return diff < 2000 ? '' : `+${formatElapsedDuration(diff)}`
  }

  const workerSession = task.session_id ? sessionsById.get(task.session_id) : undefined
  const promptPreview = splitSmartSentences(task.goal_packet?.objective || task.prompt)[0] || task.title
  steps.push({
    id: `start-${task.id}`,
    phase: 'start',
    tone: task.status === 'todo' ? 'neutral' : 'pass',
    title: task.started_at ? 'Task started' : 'Task queued',
    oneLiner: promptPreview.length > 92 ? `${promptPreview.slice(0, 90)}…` : promptPreview,
    timeLabel: formatClock(task.started_at || task.created_at),
    deltaLabel: '',
    badge: (workerSession?.agent_type || task.agent_type).toUpperCase(),
    detailText: task.prompt !== task.title ? task.prompt : undefined,
    sessionId: workerSession?.id ?? task.session_id ?? null,
    tabId: workerSession?.tab_id ?? task.source?.tab_id ?? null,
  })

  // Track A: Managed-session reports -> filter system echoes & cluster noisy phases
  const meaningfulReports = reports.filter(r => {
    if (r.state === 'review_started' || r.state === 'started') return false
    const msg = (r.message_zh || r.message || '').trim()
    if (SYSTEM_ECHO_REPORT_RE.test(msg)) return false
    if (/内部 Feedback Reaper/i.test(msg)) return false
    return true
  })

  if (meaningfulReports.length > 0) {
    // Cluster 1: Collapse all pre-implementation Goal Packet / Plan alignment turns into ONE milestone
    const planReports: AgentReport[] = []
    const execReports: AgentReport[] = []
    meaningfulReports.forEach((r, idx) => {
      if (isPlanPhaseReport(r, idx, meaningfulReports)) {
        planReports.push(r)
      } else {
        execReports.push(r)
      }
    })

    if (planReports.length > 0) {
      const lastPlan = planReports[planReports.length - 1]
      const planReviews = planReports.filter(r => r.state.startsWith('review_'))
      const passed = lastPlan.state === 'review_passed'
      steps.push({
        id: `plan-${lastPlan.id}`,
        phase: 'plan',
        tone: passed ? 'pass' : lastPlan.state === 'review_failed' ? 'warn' : 'live',
        title: passed
          ? `Plan aligned${planReviews.length > 1 ? ` (${planReviews.length} review rounds)` : ''}`
          : 'Plan under review',
        oneLiner: summarizeReportOneLiner(lastPlan),
        timeLabel: formatClock(lastPlan.created_at),
        deltaLabel: deltaFrom(lastPlan.created_at),
        badge: passed ? 'PLAN APPROVED' : 'PLAN REVIEW',
        collapsedCount: planReports.length > 1 ? planReports.length : undefined,
        detailText: planReports
          .map(r => `[${formatClock(r.created_at)} · ${r.state}] ${(r.message_zh || r.message || '').trim()}`)
          .join('\n\n'),
        sessionId: lastPlan.session_id,
      })
    }

    // Cluster 2: Collapse consecutive "working" updates before each review/completion checkpoint
    let workingBuffer: AgentReport[] = []
    let reviewOrdinal = 0

    const flushWorkingBuffer = () => {
      if (workingBuffer.length === 0) return
      const lastW = workingBuffer[workingBuffer.length - 1]
      const totalFiles = new Set(workingBuffer.flatMap(w => w.changed_files ?? [])).size
      steps.push({
        id: `work-${lastW.id}`,
        phase: 'work',
        tone: 'pass',
        title: workingBuffer.length > 1 ? `Implementation (${workingBuffer.length} updates)` : 'Implementation update',
        oneLiner: summarizeReportOneLiner(lastW),
        timeLabel: formatClock(lastW.created_at),
        deltaLabel: deltaFrom(lastW.created_at),
        badge: totalFiles > 0 ? `${totalFiles} files` : undefined,
        collapsedCount: workingBuffer.length > 1 ? workingBuffer.length : undefined,
        detailText: workingBuffer
          .map(r => `[${formatClock(r.created_at)}] ${(r.message_zh || r.message || '').trim()}`)
          .join('\n\n'),
        sessionId: lastW.session_id,
      })
      workingBuffer = []
    }

    execReports.forEach((r, idx) => {
      const isLast = idx === execReports.length - 1
      if (r.state === 'working') {
        workingBuffer.push(r)
        if (isLast) flushWorkingBuffer()
        return
      }
      flushWorkingBuffer()

      const isReview = r.state.startsWith('review_')
      if (isReview) reviewOrdinal += 1
      const oneLiner = summarizeReportOneLiner(r)
      const isCurrentEcho =
        isLast &&
        Boolean(currentHeadline) &&
        oneLiner.slice(0, 28) === currentHeadline.slice(0, 28)

      let title = 'Progress checkpoint'
      let tone: MilestoneStep['tone'] = 'neutral'
      let badge: string | undefined

      if (r.state === 'ready_for_review') {
        title = 'Submitted for review'
        tone = 'pass'
        badge = r.changed_files?.length ? `${r.changed_files.length} files` : 'SUBMITTED'
      } else if (r.state === 'review_passed') {
        title = `Cross-review R${reviewOrdinal} passed`
        tone = 'pass'
        badge = 'PASS'
      } else if (r.state === 'review_failed') {
        title = `Cross-review R${reviewOrdinal} requested changes`
        tone = 'warn'
        badge = 'REVISE'
      } else if (r.state === 'review_needs_input') {
        title = `Cross-review R${reviewOrdinal} needs your input`
        tone = 'warn'
        badge = 'NEEDS INPUT'
      } else if (r.state === 'completed') {
        title = 'Execution completed'
        tone = 'pass'
        badge = 'DONE'
      } else if (r.state === 'blocked' || r.state === 'needs_input') {
        title = 'Paused — needs input'
        tone = 'warn'
        badge = 'BLOCKED'
      }

      const session = r.session_id ? sessionsById.get(r.session_id) : undefined
      steps.push({
        id: `rep-${r.id}`,
        phase: isReview ? 'review' : r.state === 'completed' ? 'done' : 'work',
        tone,
        title,
        oneLiner: isCurrentEcho ? 'See current summary above' : oneLiner,
        timeLabel: formatClock(r.created_at),
        deltaLabel: deltaFrom(r.created_at),
        badge,
        isCurrentEcho,
        detailText: isCurrentEcho ? undefined : (r.message_zh || r.message || '').trim(),
        sessionId: r.session_id,
        tabId: session?.tab_id ?? null,
      })
    })
  } else {
    // Track B: Initiator-controlled progress events (/tasks/{id}/events) + latest_progress fallback
    const progressEvents = events.filter(
      ev =>
        (ev.type === 'progress' || ev.type === 'completed' || ev.type === 'failed') &&
        typeof ev.payload?.summary === 'string' &&
        String(ev.payload.summary).trim().length > 0,
    )

    if (progressEvents.length > 0) {
      const emitSingleEvent = (ev: WorkspaceTaskEvent, idx: number, isLast: boolean) => {
        const state = String(ev.payload.state || ev.type || 'working')
        const rawSummary = String(ev.payload.summary || '').trim()
        const eventIso = normalizeEventIso(ev.created_at)
        const parsed = parseStructuredDigest(rawSummary, task)
        const oneLiner =
          parsed.headline.length > 96 ? `${parsed.headline.slice(0, 94)}…` : parsed.headline
        const isCurrentEcho =
          isLast &&
          Boolean(currentHeadline) &&
          parsed.headline.slice(0, 24) === currentHeadline.slice(0, 24)

        const tone: MilestoneStep['tone'] =
          state === 'completed' || task.status === 'done'
            ? 'pass'
            : state === 'failed' || state === 'blocked' || state === 'needs_input'
              ? 'warn'
              : isLast
                ? 'live'
                : 'pass'

        const deriveEventTitle = (): string => {
          if (state === 'started') return 'Workspace & environment initialized'
          if (state === 'needs_input') return 'Paused for your decision'
          if (state === 'blocked') return 'Blocked'
          if (state === 'completed') return 'Delivered & verified'
          if (state === 'failed') return 'Execution failed'
          if (/cross-review|independent review|对抗|复审|审查/i.test(rawSummary)) {
            return 'Cross-review checkpoint'
          }
          if (/\bmerge\b|合入|集成/i.test(rawSummary)) {
            return 'Integration checkpoint'
          }
          if (/Arena|评测|benchmark|DONE/i.test(rawSummary)) {
            return 'Evaluation checkpoint'
          }
          return `Checkpoint ${idx + 1}`
        }

        steps.push({
          id: `ev-${ev.sequence}`,
          phase: state === 'completed' ? 'done' : state === 'needs_input' ? 'decision' : 'work',
          tone,
          title: deriveEventTitle(),
          oneLiner: isCurrentEcho ? 'Current state — details shown in Summary above' : oneLiner,
          timeLabel: formatClock(eventIso),
          deltaLabel: deltaFrom(eventIso),
          badge: state.replace(/_/g, ' ').toUpperCase(),
          isCurrentEcho,
          detailText: isCurrentEcho ? undefined : rawSummary,
          tabId: task.source?.tab_id ?? null,
        })
      }

      if (progressEvents.length <= 8) {
        progressEvents.forEach((ev, idx) => {
          emitSingleEvent(ev, idx, idx === progressEvents.length - 1)
        })
      } else {
        // Long-running iterative tasks (e.g. 50+ ablation updates): keep major state transitions
        // and fold intermediate working runs into at most 5 digestible phase batches.
        let workRun: WorkspaceTaskEvent[] = []
        let batchOrdinal = 0

        const flushWorkRun = (touchesEnd: boolean) => {
          if (workRun.length === 0) return
          const targetChunks = Math.min(4, Math.max(1, Math.ceil(workRun.length / 12)))
          const chunkSize = Math.ceil(workRun.length / targetChunks)

          for (let offset = 0; offset < workRun.length; offset += chunkSize) {
            const slice = workRun.slice(offset, offset + chunkSize)
            const isFinalSlice = touchesEnd && offset + chunkSize >= workRun.length
            if (isFinalSlice && slice.length > 1) {
              const prior = slice.slice(0, -1)
              const lastPrior = prior[prior.length - 1]
              const priorIso = normalizeEventIso(lastPrior.created_at)
              const priorParsed = parseStructuredDigest(String(lastPrior.payload.summary || ''), task)
              batchOrdinal += 1
              steps.push({
                id: `ev-batch-${lastPrior.sequence}`,
                phase: 'work',
                tone: 'pass',
                title: `Iteration phase ${batchOrdinal} (${prior.length} updates)`,
                oneLiner:
                  priorParsed.headline.length > 96
                    ? `${priorParsed.headline.slice(0, 94)}…`
                    : priorParsed.headline,
                timeLabel: formatClock(priorIso),
                deltaLabel: deltaFrom(priorIso),
                badge: `${prior.length} UPDATES`,
                collapsedCount: prior.length,
                detailText: prior
                  .map(e => `[${formatClock(normalizeEventIso(e.created_at))}] ${String(e.payload.summary || '').trim()}`)
                  .join('\n\n'),
                tabId: task.source?.tab_id ?? null,
              })
              emitSingleEvent(slice[slice.length - 1], progressEvents.length - 1, true)
            } else if (slice.length === 1) {
              emitSingleEvent(slice[0], batchOrdinal, isFinalSlice)
            } else {
              const lastEv = slice[slice.length - 1]
              const sliceIso = normalizeEventIso(lastEv.created_at)
              const sliceParsed = parseStructuredDigest(String(lastEv.payload.summary || ''), task)
              batchOrdinal += 1
              steps.push({
                id: `ev-batch-${lastEv.sequence}`,
                phase: 'work',
                tone: 'pass',
                title: `Iteration phase ${batchOrdinal} (${slice.length} updates)`,
                oneLiner:
                  sliceParsed.headline.length > 96
                    ? `${sliceParsed.headline.slice(0, 94)}…`
                    : sliceParsed.headline,
                timeLabel: formatClock(sliceIso),
                deltaLabel: deltaFrom(sliceIso),
                badge: `${slice.length} UPDATES`,
                collapsedCount: slice.length,
                detailText: slice
                  .map(e => `[${formatClock(normalizeEventIso(e.created_at))}] ${String(e.payload.summary || '').trim()}`)
                  .join('\n\n'),
                tabId: task.source?.tab_id ?? null,
              })
            }
          }
          workRun = []
        }

        progressEvents.forEach((ev, idx) => {
          const isLast = idx === progressEvents.length - 1
          const state = String(ev.payload.state || ev.type || 'working')
          if (state === 'working') {
            workRun.push(ev)
            if (isLast) flushWorkRun(true)
            return
          }
          flushWorkRun(false)
          emitSingleEvent(ev, idx, isLast)
        })
      }
    } else if (task.latest_progress) {
      // Single snapshot fallback — mark as current echo so we NEVER duplicate the 400-word summary!
      const prog = task.latest_progress
      const state = prog.state
      const tone: MilestoneStep['tone'] =
        state === 'completed' || task.status === 'done'
          ? 'pass'
          : state === 'failed' || state === 'blocked' || state === 'needs_input'
            ? 'warn'
            : 'live'
      const revCount = task.progress_revision ?? 1
      steps.push({
        id: `latest-${task.id}`,
        phase: state === 'completed' ? 'done' : state === 'needs_input' ? 'decision' : 'work',
        tone,
        title:
          state === 'needs_input'
            ? `Awaiting your decision${revCount > 1 ? ` (${revCount} checkpoints)` : ''}`
            : state === 'completed' || task.status === 'done'
              ? `Completed${revCount > 1 ? ` (${revCount} checkpoints)` : ''}`
              : `Latest checkpoint${revCount > 1 ? ` (#${revCount})` : ''}`,
        oneLiner: 'Current state — summarized above',
        timeLabel: formatClock(prog.reported_at || task.updated_at),
        deltaLabel: deltaFrom(prog.reported_at || task.updated_at),
        badge: state.replace(/_/g, ' ').toUpperCase(),
        isCurrentEcho: true,
        tabId: task.source?.tab_id ?? null,
      })
    }
  }

  if (task.status === 'done' && task.human_accepted_at) {
    steps.push({
      id: `accepted-${task.id}`,
      phase: 'done',
      tone: 'pass',
      title: 'Accepted & closed',
      oneLiner: 'Human acceptance recorded.',
      timeLabel: formatClock(task.human_accepted_at),
      deltaLabel: deltaFrom(task.human_accepted_at),
      badge: 'ACCEPTED',
    })
  } else if (
    task.status === 'done' &&
    task.completed_at &&
    steps[steps.length - 1]?.phase !== 'done'
  ) {
    steps.push({
      id: `completed-${task.id}`,
      phase: 'done',
      tone: 'pass',
      title: 'Completed',
      oneLiner: 'Task marked done.',
      timeLabel: formatClock(task.completed_at),
      deltaLabel: deltaFrom(task.completed_at),
      badge: 'DONE',
    })
  } else if (task.status === 'failed' && task.failure_reason) {
    steps.push({
      id: `failed-${task.id}`,
      phase: 'decision',
      tone: 'warn',
      title: 'Stopped with failure',
      oneLiner: evidenceLine(task.failure_reason).slice(0, 96),
      timeLabel: formatClock(task.failed_at || task.updated_at),
      deltaLabel: deltaFrom(task.failed_at || task.updated_at),
      badge: 'FAILED',
    })
  }

  return steps
}

export function buildMacroStageBar(
  task: WorkspaceTask,
  milestones: readonly MilestoneStep[],
  bucket: 'needs_you' | 'working' | 'review' | 'done',
): MacroStagePill[] {
  const hasStarted = task.status !== 'todo'
  const hasReview =
    task.task_mode === 'reviewed' ||
    bucket === 'review' ||
    milestones.some(m => m.phase === 'review' || m.phase === 'plan')
  const reviewRounds = milestones.filter(m => m.phase === 'review').length

  const stages: MacroStagePill[] = [
    {
      key: 'start',
      label: '1. Queued',
      state: hasStarted ? 'done' : 'current',
    },
    {
      key: 'exec',
      label: '2. Execution',
      state: !hasStarted
        ? 'upcoming'
        : bucket === 'done' || bucket === 'review' || task.status === 'review' || milestones.some(m => m.phase === 'review')
          ? 'done'
          : bucket === 'needs_you'
            ? 'warn'
            : 'current',
    },
  ]

  if (hasReview) {
    const inReview = task.status === 'review' || bucket === 'review'
    stages.push({
      key: 'review',
      label: reviewRounds > 0 ? `3. Review (R${reviewRounds})` : '3. Review',
      state:
        bucket === 'done'
          ? 'done'
          : inReview
            ? bucket === 'needs_you'
              ? 'warn'
              : 'current'
            : 'upcoming',
    })
  }

  const finalNum = stages.length + 1
  stages.push({
    key: 'final',
    label: `${finalNum}. ${bucket === 'done' ? 'Done' : bucket === 'needs_you' ? 'Your Decision' : 'Accept'}`,
    state:
      bucket === 'done'
        ? 'done'
        : bucket === 'needs_you'
          ? 'warn'
          : bucket === 'review'
            ? 'current'
            : 'upcoming',
  })

  return stages
}
