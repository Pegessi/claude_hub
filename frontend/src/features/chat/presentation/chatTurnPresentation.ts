import { formatElapsedDuration } from '@/utils/duration'
import type { AgentStreamHistoryDetailEntry } from '@/utils/agentStreamHistoryCache'
import type { TimelinePart, TimelineTool, TimelineTurn } from '../timeline/types'
import {
  foldTurnParts,
  splitTurnProcess,
  subagentRegionErrorCount,
  turnElapsedMs,
  turnProcessLabel,
} from '../timeline/turnProcess'
import { presentThinkingParts, type ThinkingDisplayPart, type ThinkingState } from '@/utils/thinkingPresentation'

/** Header for a turn whose detail has not been read yet.
 *
 *  Only the descriptor (a raw event count) is available here, and a count of
 *  transport events is not something a reader can use, so the label stays at
 *  what is known for free: that there was activity, and how long it took. The
 *  human counts arrive with the detail, once the reader asks for it. */
function processMetaForDetail(turn: TimelineTurn): string {
  const elapsed = turnElapsedMs(turn)
  return elapsed === null ? 'Activity' : `Activity · ${formatElapsedDuration(elapsed)}`
}

function lazyProcessPart(turn: TimelineTurn, detail: AgentStreamHistoryDetailEntry): TimelinePart {
  return {
    kind: 'process',
    key: `process-${turn.key}`,
    meta: processMetaForDetail(turn),
    expanded: false,
    turnId: turn.turnId,
    detailStatus: detail.status,
    ...(detail.errorMessage ? { detailError: detail.errorMessage } : {}),
  }
}

/** Viewer overrides belong to the pane; the shared timeline is never mutated. */
export function isTurnProcessExpanded(
  turn: TimelineTurn,
  _latestTurnKey: string | null,
  overrides: ReadonlyMap<string, boolean>,
): boolean {
  return overrides.get(turn.key) ?? false
}

/** Fold only completed turns that actually contain a process region. */
export function isTurnFoldable(turn: TimelineTurn, _latestTurnKey: string | null): boolean {
  return splitTurnProcess(turn) !== null
}

function isSummaryVisiblePart(part: TimelinePart): boolean {
  switch (part.kind) {
    case 'text':
      return !(part.fromPlan && part.planKind === 'progress')
    case 'status':
    case 'approval':
    case 'error':
      return true
    case 'subagents':
      return subagentRegionErrorCount(part) > 0 ||
        part.threads.some(child => child.lifecycleStatus === 'failed' || child.lifecycleStatus === 'cancelled')
    default:
      return false
  }
}

function compactSummaryPart(part: TimelinePart): TimelinePart {
  if (part.kind === 'subagents') return { ...part, key: `${part.key}-summary`, compact: true }
  return part
}

function summaryPartsForPendingDetail(turn: TimelineTurn): TimelinePart[] {
  return turn.parts.filter(isSummaryVisiblePart).map(compactSummaryPart)
}

export function presentTurnParts(
  turn: TimelineTurn,
  latestTurnKey: string | null,
  overrides: ReadonlyMap<string, boolean>,
  detail?: AgentStreamHistoryDetailEntry,
): ThinkingDisplayPart<TimelinePart>[] {
  const isExpanded = isTurnProcessExpanded(turn, latestTurnKey, overrides)
  const parts = detail && detail.status !== 'loaded'
    ? [lazyProcessPart(turn, detail), ...summaryPartsForPendingDetail(turn)]
    : isTurnFoldable(turn, latestTurnKey)
      ? foldTurnParts(turn, isExpanded)
      : turn.parts
  const terminal: Exclude<ThinkingState, 'active'> | null =
    turn.errors.length > 0 || ['failed', 'cancelled', 'interrupted'].includes(turn.completionStatus ?? '')
      ? 'interrupted' : turn.completed ? 'done' : null
  return presentThinkingParts(parts, terminal)
}

/** Memoize unaffected turns while reflecting the visible fold/detail state and label. */
export function turnProcessFoldSignature(
  turn: TimelineTurn,
  latestTurnKey: string | null,
  overrides: ReadonlyMap<string, boolean>,
  detail?: AgentStreamHistoryDetailEntry,
): string {
  if (detail && detail.status !== 'loaded') {
    return `lazy|${detail.status}|${detail.errorMessage ?? ''}|${processMetaForDetail(turn)}`
  }
  if (!isTurnFoldable(turn, latestTurnKey)) return ''
  const split = splitTurnProcess(turn)
  const label = split ? turnProcessLabel(turn, split.before) : ''
  return `${isTurnProcessExpanded(turn, latestTurnKey, overrides) ? 'open' : 'folded'}|${label}`
}

/** Aggregate status for a tool group: 'running' if any tool is still running,
 *  'failed' if any tool failed (and none running), 'cancelled' if every tool
 *  was cancelled (e.g. a dismissed AskUserQuestion), else 'completed'. */
export function toolGroupStatus(
  tools: TimelineTool[],
): 'running' | 'completed' | 'failed' | 'cancelled' {
  if (tools.some(t => t.status === 'running')) return 'running'
  if (tools.some(t => t.status === 'failed')) return 'failed'
  if (tools.length > 0 && tools.every(t => t.status === 'cancelled')) return 'cancelled'
  return 'completed'
}
