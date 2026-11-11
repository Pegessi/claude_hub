import { subagentProviderLabel } from '@/utils/subagentTool'
import type { TimelineSubagentLifecycle, TimelineSubthread, TimelineTurn } from '../timeline/types'
import { subthreadHeadline } from './chatSubagentPresentation'

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

function rowState(lifecycle: TimelineSubagentLifecycle): NativeSubagentState {
  return lifecycle === 'active' ? 'running' : lifecycle
}

function threadName(thread: TimelineSubthread): string {
  const agentType = thread.launchTool?.subagent?.agentType
  if (agentType) return agentType
  // Claude addresses children by tool-use id, TraeX by thread id: show a short
  // form so the row still names the child the provider knows.
  return thread.threadId.length > 12 ? `Thread ${thread.threadId.slice(0, 8)}` : thread.threadId
}

function threadProvider(thread: TimelineSubthread): string {
  const provider = thread.launchTool?.subagent?.provider
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
  const liveKeys = new Set<string>()
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index]
    liveKeys.add(turn.key)
    if (!isReady(turn)) continue
    const cached = memo?.get(turn.key)
    if (cached && cached.turn === turn && cached.revision === turn.renderRevision) {
      rows.push(...cached.rows)
    } else {
      const fresh: NativeSubagentRow[] = []
      for (const part of turn.parts) {
        if (part.kind !== 'subagents') continue
        // Threads are appended in creation order; the list reads newest first.
        for (let threadIndex = part.threads.length - 1; threadIndex >= 0; threadIndex -= 1) {
          fresh.push(nativeSubagentRow(turn, part.threads[threadIndex]))
        }
      }
      memo?.set(turn.key, { turn, revision: turn.renderRevision, rows: fresh })
      rows.push(...fresh)
    }
    if (rows.length >= NATIVE_SUBAGENT_ROW_LIMIT) break
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
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index]
    if (turn.key !== turnKey) continue
    for (const part of turn.parts) {
      if (part.kind !== 'subagents') continue
      const thread = part.threads.find(candidate => candidate.threadId === threadId)
      if (thread) return { turn, thread, ordinal: index + 1 }
    }
    return null
  }
  return null
}
