import { subagentProviderLabel } from '@/utils/subagentTool'
import type { TimelineSubagentLifecycle, TimelineSubthread, TimelineTurn } from '../timeline/types'
import { subthreadHeadline, subthreadReportHeadline } from './chatSubagentPresentation'

/**
 * Dock rows for provider-native sub-agents (Claude ``Agent``, Cursor ``Task``,
 * TraeX ``spawnAgent``).
 *
 * The Hub dock lists `claude-hub agent run` children from the durable
 * `/api/subagents` record; a native child has no Chat tab and no backend
 * record, only the ``subagents`` part inside its parent turn. This module
 * projects that part into the same row vocabulary so one list can render both
 * sources, and the pane chrome keeps a single control.
 */

/** Child lifecycles mapped onto the Hub chip states so one dot style serves both. */
export type NativeSubagentState = 'running' | 'completed' | 'failed' | 'cancelled' | 'unknown'

const STATE_LABELS: Record<NativeSubagentState, string> = {
  running: 'Active',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  unknown: 'Unknown',
}

/** Bounds the list the same way the Hub chips are bounded, newest first. */
export const NATIVE_SUBAGENT_ROW_LIMIT = 30

export interface NativeSubagentRow {
  /** Discriminant for the shared dock list; Hub rows are ``'hub'``. */
  kind: 'native'
  /** ``turnKey:threadId`` — the turn key is stable for the pane visit. */
  key: string
  turnKey: string
  turnId: string | null
  threadId: string
  /** Agent type when the provider names one, else the provider thread id. */
  name: string
  /** Provider label shown beside the name (e.g. ``Claude sub-agent``). */
  agentType: string
  state: NativeSubagentState
  stateLabel: string
  /** Latest child report/instruction line, else its delegated prompt. */
  summary: string
  /** The parent turn's start, used for the row's relative time. */
  createdAt: string
  group: 'active' | 'earlier'
  /** Native children have no Chat tab: they open a detail window instead. */
  archived: false
  canJump: false
  canOpen: true
}

const READABLE_MASO_THREAD_ID_RE = /^[a-zA-Z0-9_]+-[0-9a-f]{4,8}$/

function rowState(lifecycle: TimelineSubagentLifecycle): NativeSubagentState {
  return lifecycle === 'active' ? 'running' : lifecycle
}

function threadName(thread: TimelineSubthread): string {
  if (
    (thread.launchTool?.subagent?.provider === 'maso' || thread.provider === 'maso') &&
    READABLE_MASO_THREAD_ID_RE.test(thread.threadId)
  ) {
    return thread.threadId
  }
  const agentType = thread.launchTool?.subagent?.agentType
  if (agentType) return agentType
  if (READABLE_MASO_THREAD_ID_RE.test(thread.threadId)) return thread.threadId
  // Claude addresses children by tool-use id, TraeX by thread id: show a short
  // form so the row still names the child the provider knows.
  return thread.threadId.length > 12 ? `Thread ${thread.threadId.slice(0, 8)}` : thread.threadId
}

function threadProvider(thread: TimelineSubthread): string {
  const provider = thread.launchTool?.subagent?.provider ?? thread.provider
  return provider ? subagentProviderLabel(provider) : 'Native sub-agent'
}

export function nativeSubagentRow(turn: TimelineTurn, thread: TimelineSubthread): NativeSubagentRow {
  const state = rowState(thread.lifecycleStatus)
  return {
    kind: 'native',
    key: `${turn.key}:${thread.threadId}`,
    turnKey: turn.key,
    turnId: turn.turnId,
    threadId: thread.threadId,
    name: threadName(thread),
    agentType: threadProvider(thread),
    state,
    stateLabel: STATE_LABELS[state],
    summary: subthreadHeadline(thread),
    createdAt: turn.startedAt ?? '',
    group: state === 'running' ? 'active' : 'earlier',
    archived: false,
    canJump: false,
    canOpen: true,
  }
}

export interface NativeSubagentRowMemo {
  /** The turn instance the rows were built from. `renderRevision` restarts at
   *  0 whenever the reducer rebuilds from scratch, so the number alone cannot
   *  tell a rebuilt turn from the one it replaced. */
  turn: TimelineTurn
  revision: number
  rows: NativeSubagentRow[]
}

function turnHasNonMasoLaunchForThread(turn: TimelineTurn, threadId: string): boolean {
  for (const part of turn.parts) {
    if (part.kind === 'subagent' && part.tool.subagent && part.tool.subagent.provider !== 'maso') {
      if ((part.tool.callId || part.tool.key) === threadId) return true
    }
    if (part.kind !== 'subagents') continue
    for (const thread of part.threads) {
      if (thread.threadId !== threadId) continue
      if (thread.launchTool && thread.launchTool.subagent?.provider !== 'maso') {
        return true
      }
    }
  }
  return false
}

function standaloneToolToSubthread(tool: TimelineTurn['tools'][number]): TimelineSubthread {
  const threadId = tool.callId || tool.key
  const lifecycleStatus: TimelineSubagentLifecycle =
    tool.status === 'running'
      ? 'active'
      : tool.status === 'failed'
        ? 'failed'
        : tool.status === 'cancelled'
          ? 'cancelled'
          : 'completed'
  return {
    key: `subthread-${threadId}`,
    threadId,
    parts: [],
    launchTool: tool,
    lifecycleStatus,
    provider: tool.subagent?.provider,
    renderRevision: 1,
  }
}

function turnSubthreads(turn: TimelineTurn): TimelineSubthread[] {
  const threads: TimelineSubthread[] = []
  const seen = new Set<string>()
  for (const part of turn.parts) {
    if (part.kind !== 'subagents') continue
    for (const thread of part.threads) {
      seen.add(thread.threadId)
      threads.push(thread)
    }
  }
  for (const part of turn.parts) {
    if (part.kind !== 'subagent' || !part.tool.subagent) continue
    const threadId = part.tool.callId || part.tool.key
    if (seen.has(threadId)) continue
    seen.add(threadId)
    threads.push(standaloneToolToSubthread(part.tool))
  }
  return threads
}

function turnReportHeadlineForThread(turn: TimelineTurn, threadId: string): string | null {
  for (const part of turn.parts) {
    if (part.kind !== 'subagents') continue
    for (const thread of part.threads) {
      if (thread.threadId !== threadId) continue
      const report = subthreadReportHeadline(thread)
      if (report) return report
    }
  }
  return null
}

/** Whether a turn's children are fully known yet.
 *
 *  A historical turn keeps its child events behind its execution-detail stream,
 *  but the shallow conversation page still carries a failed/cancelled child
 *  *status* (see `conversation_event_visible`). The reducer therefore mounts a
 *  stub child (no launch call, no prose) for it. Projecting that stub would put
 *  a row in the dock that the window cannot fill in, and would hide the turn's
 *  completed siblings. `StructuredPane` supplies the real check (the same one
 *  `presentTurnParts` uses); the default projects everything. */
export type NativeSubagentRowReadiness = (turn: TimelineTurn) => boolean

/**
 * All native children the pane's timeline holds, newest turn first.
 *
 * Child rows are read from ``turn.parts``, never from the presentation layer:
 * a folded turn keeps its ``subagents`` part, so a rail row does not depend on
 * the transcript being expanded. A historical turn's children are behind its
 * execution-detail stream, so a turn whose detail has not been requested yet is
 * skipped entirely (``isReady``) — the rail never invents a row it cannot open,
 * and the stub a shallow page may carry for a failed child never surfaces.
 *
 * ``memo`` keeps this O(changed turns) per stream batch: the reducer bumps
 * ``turn.renderRevision`` for visible child mutations, so an unchanged turn
 * reuses its rows. Identity is checked as well, because a rebuilt turn restarts
 * the revision count from zero.
 */
export function nativeSubagentRows(
  turns: readonly TimelineTurn[],
  memo?: Map<string, NativeSubagentRowMemo>,
  isReady: NativeSubagentRowReadiness = () => true,
): NativeSubagentRow[] {
  const rows: NativeSubagentRow[] = []
  const byThreadId = new Map<string, number>()
  const hasReportByThreadId = new Map<string, boolean>()
  const liveKeys = new Set<string>()
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index]
    liveKeys.add(turn.key)
    if (!isReady(turn)) continue
    let turnRows: NativeSubagentRow[]
    const cached = memo?.get(turn.key)
    if (cached && cached.turn === turn && cached.revision === turn.renderRevision) {
      turnRows = cached.rows
    } else {
      const fresh: NativeSubagentRow[] = []
      const subthreads = turnSubthreads(turn)
      // Threads are appended in creation order; the list reads newest first.
      for (let threadIndex = subthreads.length - 1; threadIndex >= 0; threadIndex -= 1) {
        fresh.push(nativeSubagentRow(turn, subthreads[threadIndex]))
      }
      memo?.set(turn.key, { turn, revision: turn.renderRevision, rows: fresh })
      turnRows = fresh
    }
    for (const candidate of turnRows) {
      const existingIndex = byThreadId.get(candidate.threadId)
      const current = existingIndex !== undefined ? rows[existingIndex] : undefined
      const isDistinctSyntheticSpawn =
        current !== undefined &&
        current.agentType !== 'MASO sub-agent' &&
        candidate.agentType !== 'MASO sub-agent' &&
        turnHasNonMasoLaunchForThread(turn, candidate.threadId) &&
        turns.some(
          t => t.key === current.turnKey && turnHasNonMasoLaunchForThread(t, current.threadId),
        )
      const candidateReport = turnReportHeadlineForThread(turn, candidate.threadId)
      if (existingIndex === undefined || isDistinctSyntheticSpawn) {
        if (rows.length >= NATIVE_SUBAGENT_ROW_LIMIT) continue
        byThreadId.set(candidate.threadId, rows.length)
        hasReportByThreadId.set(candidate.threadId, candidateReport !== null)
        rows.push(candidate)
      } else if (current) {
        // Long-lived sub-agents (e.g. MASO Task/SendMessage across turns) keep
        // one dock entry anchored at their latest turn while inheriting spawn
        // metadata or lifecycle state from earlier turns when needed.
        const nextState = current.state !== 'unknown' ? current.state : candidate.state
        const nextAgentType =
          current.agentType !== 'Native sub-agent' ? current.agentType : candidate.agentType
        const nextName =
          current.agentType !== 'Native sub-agent' ? current.name : candidate.name
        const currentHasReport = hasReportByThreadId.get(candidate.threadId) === true
        let nextSummary = current.summary
        if (!currentHasReport && candidateReport !== null && nextState !== 'running') {
          nextSummary = candidateReport
          hasReportByThreadId.set(candidate.threadId, true)
        } else if (current.summary === 'Waiting for sub-agent activity') {
          nextSummary = candidate.summary
        }
        if (
          nextState !== current.state ||
          nextAgentType !== current.agentType ||
          nextName !== current.name ||
          nextSummary !== current.summary
        ) {
          rows[existingIndex] = {
            ...current,
            name: nextName,
            agentType: nextAgentType,
            state: nextState,
            stateLabel: STATE_LABELS[nextState],
            group: nextState === 'running' ? 'active' : 'earlier',
            summary: nextSummary,
          }
        }
      }
    }
  }
  if (memo) {
    // Turns scrolled out of the pane's history would otherwise pin their rows.
    for (const key of memo.keys()) {
      if (!liveKeys.has(key)) memo.delete(key)
    }
  }
  return rows.slice(0, NATIVE_SUBAGENT_ROW_LIMIT)
}

export interface NativeSubthreadLookup {
  turn: TimelineTurn
  thread: TimelineSubthread
  /** 1-based position in the transcript, for the window's context line. */
  ordinal: number
}

/** Resolve the live child a dock row points at; ``null`` once it left the pane. */
export function findNativeSubthread(
  turns: readonly TimelineTurn[],
  turnKey: string,
  threadId: string,
): NativeSubthreadLookup | null {
  const matches: Array<{ turn: TimelineTurn; thread: TimelineSubthread; ordinal: number }> = []
  for (let index = 0; index < turns.length; index += 1) {
    const turn = turns[index]
    const thread = turnSubthreads(turn).find(candidate => candidate.threadId === threadId)
    if (thread) {
      matches.push({ turn, thread, ordinal: index + 1 })
    }
  }
  if (matches.length === 0) return null
  const targetIndex = matches.findIndex(item => item.turn.key === turnKey)
  if (targetIndex < 0) return null
  const target = matches[targetIndex]
  if (
    matches.length === 1 ||
    (target.thread.provider !== 'maso' &&
      target.thread.launchTool?.subagent?.provider !== 'maso' &&
      turnHasNonMasoLaunchForThread(target.turn, threadId))
  ) {
    return target
  }
  let launchTool = target.thread.launchTool
  let lifecycleStatus = target.thread.lifecycleStatus
  let provider = target.thread.provider
  let renderRevision = 0
  const mergedParts: TimelineSubthread['parts'] = []
  for (let i = 0; i <= targetIndex; i += 1) {
    const item = matches[i]
    if (!launchTool && item.thread.launchTool) launchTool = item.thread.launchTool
    if (item.thread.lifecycleStatus !== 'unknown') lifecycleStatus = item.thread.lifecycleStatus
    if (!provider && item.thread.provider) provider = item.thread.provider
    renderRevision += item.thread.renderRevision + 1
    for (const part of item.thread.parts) {
      mergedParts.push(
        i === targetIndex ? part : { ...part, key: `${item.turn.key}:${part.key}` },
      )
    }
  }
  return {
    turn: target.turn,
    ordinal: target.ordinal,
    thread: {
      key: target.thread.key,
      threadId: target.thread.threadId,
      parts: mergedParts,
      launchTool,
      lifecycleStatus,
      renderRevision,
      ...(provider ? { provider } : {}),
    },
  }
}
