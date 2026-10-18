import { formatClockTime, formatElapsedDuration, parseTimestampMs } from '@/utils/duration'
import type { TimelinePart, TimelineTurn, TimelineTool } from './types'

// ── process folding ─────────────────────────────────────────────────────
//
// A finished turn is mostly a record of how the answer was reached: thinking,
// tool calls, and the narration between them. Once the answer is delivered the
// working region is collapsed to a single line so a long conversation reads as
// questions and answers, with the process one click away. The split is
// structural and pure; where the fold state lives, and which turns default to
// folded, is the renderer's decision (see ``StructuredPane``).

/** A turn's parts, split into its working region and its delivered answer. */
export interface TurnProcessSplit {
  /** The whole working region in arrival order — what the expanded view shows. */
  before: TimelinePart[]
  /** Members of ``before`` that stay visible even while folded. Only errors:
   *  an approval card folds with the rest of the record. */
  pinned: TimelinePart[]
  /** The delivered answer plus anything that arrived after it. */
  delivery: TimelinePart[]
}

/** Index of the delivered answer: the turn's final text segment.
 *
 *  Everything the model says before it is working narration ("我先看一下…"),
 *  so the LAST text part is the delivery, not the first. Codex's plan stream is
 *  skipped: it is prose too, but it describes work rather than answering. */
function deliveryIndex(parts: TimelinePart[], mode: TimelineTurn['mode'] = null): number {
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    const part = parts[i]
    if (part.kind === 'text' && part.text.trim() && (
      !part.fromPlan || (mode === 'plan' && part.planKind !== 'progress')
    )) return i
  }
  return -1
}

/** Whether a part records how the answer was reached rather than being output. */
function isProcessPart(part: TimelinePart): boolean {
  return (
    part.kind === 'thinking' ||
    part.kind === 'tool' ||
    part.kind === 'tool_group' ||
    part.kind === 'subagent' ||
    part.kind === 'subagents' ||
    part.kind === 'agent_image' ||
    (part.kind === 'text' && part.fromPlan === true)
  )
}

/** Split a completed turn into its working process and its delivered answer.
 *
 *  Returns ``null`` when there is nothing safe to fold:
 *
 *  * the turn is still running, so its answer is not final yet;
 *  * it never produced assistant text, so folding would leave an empty turn;
 *  * work continues past its last text — a turn cancelled mid-tool, or one
 *    that ran out of room, ends without a delivered answer. Folding there
 *    would leave the tools and thinking on screen under a header claiming to
 *    have hidden them, because "the last text and everything after it" is only
 *    an answer when the turn actually stopped there;
 *  An approval card inside the region does not stop the fold — it goes with the
 *  rest of the record. An error is reported as ``pinned`` and stays visible
 *  beside the header: folding it would bury the reason the turn failed.
 */
export function splitTurnProcess(turn: TimelineTurn): TurnProcessSplit | null {
  if (!turn.completed) return null
  const index = deliveryIndex(turn.parts, turn.mode)
  if (index <= 0) return null
  if (turn.parts.slice(index + 1).some(isProcessPart)) return null
  const before = turn.parts.slice(0, index)
  return {
    before,
    pinned: before.filter(isPinnedPart),
    delivery: turn.parts.slice(index),
  }
}

/** Whether a part must stay visible while the working region is folded.
 *
 *  Only an error, which is the reason the turn failed and would be buried by a
 *  fold. An approval card folds away with the rest of the record — an answered
 *  prompt collapses into the transcript in Codex the same way.
 *
 *  Pinning unanswered cards was the first attempt and it did not survive contact
 *  with how cards are actually answered: the agent asks, the tool returns the
 *  placeholder, and the user replies in the *next message* rather than through
 *  the card. That reply is ordinary text, so `approval_resolved` is never
 *  emitted and `resolved` stays false forever — making "pin the unanswered
 *  card" mean "never fold this turn", which is the bug this fixes.
 *
 *  Nothing live is hidden by folding them: the card that could still be
 *  pending belongs to the newest turn, and the newest completed turn is never
 *  folded (``StructuredPane.isTurnFoldable``). Anything folded here is history
 *  the reader has already moved past, one click away. */
export function subagentRegionErrorCount(
  region: Extract<TimelinePart, { kind: 'subagents' }>,
): number {
  const issues = new Set<string>()
  for (const child of region.threads) {
    if (child.launchTool?.status === 'failed') issues.add(`launch:${child.launchTool.key}`)
    for (const row of child.parts) {
      if (row.kind === 'status' && row.isError) issues.add(`status:${child.threadId}:${row.key}`)
      const tools = row.kind === 'tool_group' ? row.tools : row.kind === 'agent_image' ? [row.tool] : []
      for (const tool of tools) {
        if (tool.status === 'failed') issues.add(`tool:${child.threadId}:${tool.key}`)
      }
    }
  }
  return issues.size
}

function isPinnedPart(part: TimelinePart): boolean {
  return part.kind === 'error' || (part.kind === 'subagents' && (
    subagentRegionErrorCount(part) > 0 ||
    part.threads.some(child => child.lifecycleStatus === 'failed')
  ))
}

/** When the turn's last message began, or ``null`` when it never spoke.
 *
 *  What the turn's action row reports. Deliberately NOT routed through
 *  ``splitTurnProcess``: that answers "can this turn be folded", which is also
 *  false for a plain question-and-answer turn with no process to hide — the
 *  most common shape there is. This asks a different question, "what was the
 *  last thing it said", and every turn with a message has an answer. */
export function deliveryAt(turn: TimelineTurn): string | null {
  const index = deliveryIndex(turn.parts, turn.mode)
  if (index < 0) return null
  const part = turn.parts[index]
  return part.kind === 'text' ? part.at : null
}

/** Return an actionable plan only for a successful, explicitly planned turn.
 * Older history without mode metadata must not acquire an Implement action
 * merely because the composer was switched to Plan later. */
export function getCompletedPlanText(turn: TimelineTurn): string | null {
  if (turn.mode !== 'plan' || !turn.completed || turn.completionStatus !== 'completed') return null
  for (let index = turn.parts.length - 1; index >= 0; index -= 1) {
    const part = turn.parts[index]
    if (part.kind === 'text' && part.fromPlan && part.planKind !== 'progress' && part.text.trim()) {
      return part.text
    }
  }
  const index = deliveryIndex(turn.parts, turn.mode)
  const part = turn.parts[index]
  return part?.kind === 'text' ? part.text : null
}

/** Clock label for the message that opened the turn — when it was sent.
 *
 *  Its own function rather than an inline ``formatClockTime(turn.startedAt)``
 *  so the choice of *which* timestamp belongs to that row is a testable
 *  decision rather than a string in a template. */
export function messageClockLabel(turn: TimelineTurn): string {
  return formatClockTime(turn.startedAt)
}

/** Clock label for a turn's action row: the last message's own time where the
 *  turn has one, otherwise when the turn finished. ``''`` while a turn is still
 *  running, so the caller renders no label rather than an empty one. */
export function turnClockLabel(turn: TimelineTurn): string {
  return formatClockTime(deliveryAt(turn) ?? turn.completedAt)
}

/** Count what the agent actually did, for the folded label.
 *
 *  Tool calls rather than parts: "8 个工具调用" should mean eight actions, not
 *  eight render blocks. */
export function countProcessSteps(process: TimelinePart[]): number {
  let steps = 0
  for (const part of process) {
    if (part.kind === 'tool_group') {
      steps += part.tools.length
    } else if (part.kind === 'subagents') {
      const launches = new Set<TimelineTool>()
      for (const thread of part.threads) {
        if (thread.launchTool) launches.add(thread.launchTool)
        for (const child of thread.parts) {
          if (child.kind === 'tool_group') steps += child.tools.length
          else if (child.kind === 'agent_image') steps += 1
        }
      }
      steps += launches.size
    } else if (part.kind === 'tool' || part.kind === 'subagent' || part.kind === 'agent_image') {
      steps += 1
    }
  }
  return steps
}

/** Elapsed wall-clock the turn occupied, or ``null`` when it cannot be trusted.
 *
 *  A turn replayed from a provider transcript rather than streamed live has
 *  every ``created_at`` stamped with the import time, so its span collapses to
 *  ~0. The one-second floor drops those instead of labelling a long turn "0s". */
export function turnElapsedMs(turn: TimelineTurn): number | null {
  const started = parseTimestampMs(turn.startedAt)
  const completed = parseTimestampMs(turn.completedAt)
  if (started === null || completed === null) return null
  const elapsed = completed - started
  return elapsed >= 1000 ? elapsed : null
}

/** Label for the folded process line, e.g. ``过程 · 8 个工具调用 · 1m 23s``. */
export function turnProcessLabel(turn: TimelineTurn, process: TimelinePart[]): string {
  const segments = ['过程']
  const steps = countProcessSteps(process)
  if (steps > 0) segments.push(`${steps} 个工具调用`)
  // The header is the only thing left on screen, so it has to say when a card
  // is folded in with the rest — otherwise a question the user may never have
  // answered disappears without a trace.
  const cards = process.filter(part => part.kind === 'approval').length
  if (cards > 0) segments.push(`${cards} 张审批卡`)
  const elapsed = turnElapsedMs(turn)
  if (elapsed !== null) segments.push(formatElapsedDuration(elapsed))
  return segments.join(' · ')
}

/** Parts to render for a turn, with the working process folded away.
 *
 *  The header keeps its identity and its place in both states, and only the
 *  detail below it grows: expanding must not move the control out from under
 *  the pointer, and collapsing must not require scrolling back to the bottom
 *  of whatever was just revealed.
 *
 *  Returns ``turn.parts`` untouched when there is nothing to fold, so the turn
 *  being streamed — and any turn the renderer keeps open — renders exactly as
 *  it did before folding existed. */
export function foldTurnParts(turn: TimelineTurn, expanded: boolean): TimelinePart[] {
  const split = splitTurnProcess(turn)
  if (split === null) return turn.parts
  const header: TimelinePart = {
    kind: 'process',
    key: `process-${turn.key}`,
    meta: turnProcessLabel(turn, split.before),
    expanded,
  }
  // Expanded, the region replays in arrival order. Folded, the pinned parts
  // stay on screen next to the header — an error is not something a fold may
  // hide. Approval cards are not pinned: they fold with the record.
  return expanded
    ? [header, ...split.before, ...split.delivery]
    : [header, ...split.pinned, ...split.delivery]
}
