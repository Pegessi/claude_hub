import type { TimelinePart, TimelineTool, TimelineTurn } from '../timeline/types'
import {
  foldTurnParts,
  splitTurnProcess,
  turnProcessLabel,
} from '../timeline/turnProcess'
import { presentThinkingParts, type ThinkingDisplayPart, type ThinkingState } from '@/utils/thinkingPresentation'

/** Viewer overrides belong to the pane; the shared timeline is never mutated. */
export function isTurnProcessExpanded(
  turn: TimelineTurn,
  latestTurnKey: string | null,
  overrides: ReadonlyMap<string, boolean>,
): boolean {
  const override = overrides.get(turn.key)
  if (override !== undefined) return override
  return turn.key === latestTurnKey
}

/** Keep the newest turn open; running or unsafe process regions cannot fold. */
export function isTurnFoldable(turn: TimelineTurn, latestTurnKey: string | null): boolean {
  return turn.key !== latestTurnKey && splitTurnProcess(turn) !== null
}

export function presentTurnParts(
  turn: TimelineTurn,
  latestTurnKey: string | null,
  overrides: ReadonlyMap<string, boolean>,
): ThinkingDisplayPart<TimelinePart>[] {
  const parts = isTurnFoldable(turn, latestTurnKey)
    ? foldTurnParts(turn, isTurnProcessExpanded(turn, latestTurnKey, overrides)) : turn.parts
  const terminal: Exclude<ThinkingState, 'active'> | null =
    turn.errors.length > 0 || ['failed', 'cancelled', 'interrupted'].includes(turn.completionStatus ?? '')
      ? 'interrupted' : turn.completed ? 'done' : null
  return presentThinkingParts(parts, terminal)
}

/** Memoize unaffected turns while reflecting the visible fold state and label. */
export function turnProcessFoldSignature(
  turn: TimelineTurn,
  latestTurnKey: string | null,
  overrides: ReadonlyMap<string, boolean>,
): string {
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
