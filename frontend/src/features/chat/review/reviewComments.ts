import { describeToolActivity } from '@/utils/toolActivity'
import type { TimelineTurn } from '../timeline/types'

/**
 * Review comments a reader leaves on a diff row, and the one message they
 * become.
 *
 * The comment anchors on the row it was written against so the tray can scroll
 * back to it; the message names the file and line instead, because the agent
 * reads the transcript, not the local UI state.
 */
export interface ReviewAnchor {
  /** Row identity inside the diff (``part:tool:row``); the scroll target. */
  key: string
  /** Turn key that owns the row, so a collapsed card can be reopened. */
  turn: string
  /** Tool-group key inside that turn. */
  group: string
  /** Tool call key inside that group. */
  tool: string
  /** Row index inside the tool's diff. */
  row: number
  /** Path the comment is about; '' when the call names no file. */
  file: string
  /** Preferred line number: the new side, else the original side. */
  line: number | null
  /** Last line of a shift-click range, when it spans several rows. */
  endLine: number | null
  /** The code row the reviewer pointed at, bounded for one-line display. */
  code: string
}

export interface ReviewComment extends ReviewAnchor {
  id: string
  text: string
  /** The row this comment points at is gone (a history reload renumbers the
   *  turn's parts). Learned by trying to reveal it; never persisted. */
  stale?: boolean
}

export const REVIEW_CODE_LIMIT = 160
export const REVIEW_NOTE_LIMIT = 800

/** Collapse to one line and bound it: a message item reads as one line. */
function oneLine(text: string, limit: number): string {
  const line = text.replace(/\s+/g, ' ').trim()
  return line.length > limit ? `${line.slice(0, limit - 1)}…` : line
}

export function reviewCode(text: string): string {
  return oneLine(text, REVIEW_CODE_LIMIT)
}

export function reviewNote(text: string): string {
  return oneLine(text, REVIEW_NOTE_LIMIT)
}

/** Backticks are reserved for the code span; a row that contains one is quoted
 *  instead so the note cannot swallow the rest of the line. */
export function reviewQuotedCode(text: string): string {
  const code = reviewCode(text)
  if (!code) return ''
  return code.includes('`') ? `"${code}"` : `\`${code}\``
}

function withLines(path: string, anchor: ReviewAnchor): string {
  if (anchor.line === null) return path
  return anchor.endLine && anchor.endLine !== anchor.line
    ? `${path}:${anchor.line}-${anchor.endLine}`
    : `${path}:${anchor.line}`
}

/** ``path:42-45``, ``path:42``, or just the path when the diff carried no
 *  line numbers (a Claude old/new string edit, for example). */
export function reviewLocation(anchor: ReviewAnchor): string {
  return withLines(anchor.file || 'diff', anchor)
}

/** The same for a narrow tray: the file's tail and its lines, so the line
 *  number survives truncation of a long path. */
export function reviewShortLocation(anchor: ReviewAnchor): string {
  const tail = anchor.file ? anchor.file.split(/[\\/]/).filter(Boolean).slice(-2).join('/') : ''
  return withLines(tail || 'diff', anchor)
}

export function reviewCountLabel(count: number): string {
  return `${count} ${count === 1 ? 'comment' : 'comments'}`
}

/** How many rows a stored anchor could point at today, or null when the turn,
 *  group or tool it named is gone.
 *
 *  Turn and part keys are re-derived when a warm cache reloads history, so a
 *  comment saved against one revision can outlive the row it was written on.
 *  The check is on demand: only the entry being revealed pays for it, and a
 *  turn outside the loaded history window is stale rather than a false hit. */
export function reviewAnchorRowCount(
  anchor: ReviewAnchor,
  turns: readonly TimelineTurn[],
): number | null {
  const turn = turns.find(item => item.key === anchor.turn)
  const part = turn?.parts.find(item => item.key === anchor.group)
  if (!part || part.kind !== 'tool_group') return null
  const tool = part.tools.find(item => item.key === anchor.tool)
  if (!tool) return null
  return describeToolActivity(tool).diff?.length ?? 0
}

/** Whether the anchor still addresses a rendered row. */
export function reviewAnchorIsLive(anchor: ReviewAnchor, turns: readonly TimelineTurn[]): boolean {
  const rows = reviewAnchorRowCount(anchor, turns)
  return rows !== null && anchor.row >= 0 && anchor.row < rows
}

/** The whole tray as one message, so the agent gets a single review turn
 *  instead of one turn per note. Empty when there is nothing to send. */
export function formatReviewMessage(comments: readonly ReviewComment[]): string {
  if (!comments.length) return ''
  const items = comments.map((comment, index) => {
    const code = reviewQuotedCode(comment.code)
    const note = reviewNote(comment.text)
    const body = code ? `${reviewLocation(comment)} — ${code} — ${note}` : `${reviewLocation(comment)} — ${note}`
    return `${index + 1}. ${body}`
  })
  return [`Review comments (${comments.length}):`, ...items].join('\n')
}
