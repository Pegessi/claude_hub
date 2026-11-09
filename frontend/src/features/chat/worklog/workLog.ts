/** A turn's work log, in words a person can read.
 *
 *  Two questions, both answered from the same timeline parts:
 *
 *  * **What is it doing right now?** — one label for the live footer, so a
 *    20-minute turn is not an empty screen under a spinner.
 *  * **What has it done so far?** — counts of edits, commands, reads and
 *    failures, replacing raw event counts (``12782 events`` says nothing about
 *    what happened, and nothing at all about whether something failed).
 *
 *  Everything here is a pure projection of {@link TimelineTurn}: no Vue state
 *  and no timers, so the live footer can re-read it on every stream batch
 *  without owning any of it.
 */

import { describeToolActivity, toolActivityKind, type ToolActivityKind } from '@/utils/toolActivity'
import type { TimelinePart, TimelineSubthread, TimelineTool, TimelineTurn } from '../timeline/types'

/** What the turn has done so far. Every field is a count of *calls*, not of
 *  render blocks: a folded tool group holding three reads is three reads. */
export interface WorkLogCounts {
  commands: number
  /** Edits and writes together: the reader cares that files changed. */
  edits: number
  reads: number
  searches: number
  /** Fetched pages and web searches. */
  web: number
  /** Anything else that did work (MCP calls, provider-specific tools). */
  other: number
  /** Failed calls and turn-level errors. Shown on its own line treatment:
   *  a failure that is merely folded into a count disappears. */
  failed: number
  /** Distinct sub-agent threads this turn launched or observed. */
  subagents: number
}

export function emptyWorkLogCounts(): WorkLogCounts {
  return { commands: 0, edits: 0, reads: 0, searches: 0, web: 0, other: 0, failed: 0, subagents: 0 }
}

/** Tools a part reports as calls. Image views and sub-agent spawns are tool
 *  calls too; they are the same work in a different card. */
function toolsInPart(part: TimelinePart): readonly TimelineTool[] {
  switch (part.kind) {
    case 'tool':
      return [part.tool]
    case 'tool_group':
      return part.tools
    case 'subagent':
    case 'agent_image':
      return [part.tool]
    default:
      return []
  }
}

/** Calls a child thread reports. A child's work is the turn's work: leaving
 *  it out would under-report a turn that delegated everything. */
function toolsInThread(thread: TimelineSubthread): readonly TimelineTool[] {
  const tools: TimelineTool[] = []
  for (const row of thread.parts) {
    if (row.kind === 'tool_group') tools.push(...row.tools)
    else if (row.kind === 'agent_image' || row.kind === 'instruction') tools.push(row.tool)
  }
  return tools
}

/** Plan bookkeeping is not work: skipping it keeps "12 edits · 6 commands"
 *  from being padded by todo-list rewrites. */
function countsAsWork(kind: ToolActivityKind): boolean {
  return kind !== 'plan'
}

function countTool(counts: WorkLogCounts, tool: TimelineTool, failures: Set<string>): void {
  // Keyed rather than incremented: one spawn call addressed to several threads
  // is attached to each of them by the reducer, and a directive to several
  // threads is filed in each child, so the same failure would otherwise be
  // reported once per addressee.
  if (tool.status === 'failed') failures.add(tool.key)
  // A spawn or an instruction to a child is delegation, not work of its own:
  // the child's calls are counted where they arrive, and the delegation itself
  // is the sub-agent count. Counting it as "another tool" double-counted it.
  if (tool.subagent) return
  const kind = toolActivityKind(tool.name)
  if (!countsAsWork(kind)) return
  switch (kind) {
    case 'command':
      counts.commands += 1
      break
    case 'edit':
    case 'write':
      counts.edits += 1
      break
    case 'read':
      counts.reads += 1
      break
    case 'search':
    case 'list':
      counts.searches += 1
      break
    case 'web':
      counts.web += 1
      break
    default:
      counts.other += 1
      break
  }
}

/** Work counts for a set of timeline parts (a whole turn, or the working
 *  region a completed turn folds away). */
export function countWorkLogParts(parts: readonly TimelinePart[]): WorkLogCounts {
  const counts = emptyWorkLogCounts()
  const threads = new Set<string>()
  const failures = new Set<string>()
  for (const part of parts) {
    for (const tool of toolsInPart(part)) {
      countTool(counts, tool, failures)
      // A spawn call names the child it started. Where the child also streamed
      // its own region the thread id above already counted it, so both paths
      // dedupe against one set instead of double-counting the same child.
      const spawn = tool.subagent && !tool.subagent.directive ? tool.subagent : null
      if (spawn) {
        if (spawn.threadIds.length === 0) threads.add(`call:${tool.key}`)
        else for (const threadId of spawn.threadIds) threads.add(threadId)
      }
    }
    if (part.kind !== 'subagents') continue
    for (const thread of part.threads) {
      threads.add(thread.threadId)
      for (const tool of toolsInThread(thread)) countTool(counts, tool, failures)
      // A child that failed to launch or ended as an error is a failure of
      // this turn even when no individual call carries the status. Both are
      // keyed too: a launch shared by several threads is one failure.
      if (thread.launchTool?.status === 'failed') failures.add(thread.launchTool.key)
      for (const row of thread.parts) {
        if (row.kind === 'status' && row.isError) failures.add(`status:${thread.threadId}:${row.key}`)
      }
    }
  }
  counts.subagents = threads.size
  counts.failed = failures.size
  return counts
}

/** Work counts for a whole turn, including its own errors. */
export function countWorkLog(turn: TimelineTurn): WorkLogCounts {
  const counts = countWorkLogParts(turn.parts)
  counts.failed += turn.errors.length
  return counts
}

function plural(count: number, singular: string, pluralForm?: string): string {
  return `${count} ${count === 1 ? singular : pluralForm ?? `${singular}s`}`
}

/** The quiet second line: ``12 edits · 6 commands · 4 reads · 1 failed · 3 sub-agents``.
 *
 *  Zero-count buckets are dropped rather than printed as ``0 commands``, and
 *  failures are kept even at length: a summary that hides them is worse than no
 *  summary. Empty string when nothing has happened yet, so the footer stays one
 *  line until it has something to say. */
export function workLogCountLabel(counts: WorkLogCounts): string {
  const items: string[] = []
  if (counts.edits > 0) items.push(plural(counts.edits, 'edit'))
  if (counts.commands > 0) items.push(plural(counts.commands, 'command'))
  if (counts.reads > 0) items.push(plural(counts.reads, 'read'))
  if (counts.searches > 0) items.push(plural(counts.searches, 'search', 'searches'))
  if (counts.web > 0) items.push(plural(counts.web, 'fetch', 'fetches'))
  if (counts.other > 0) items.push(plural(counts.other, 'other tool'))
  if (counts.failed > 0) items.push(`${counts.failed} failed`)
  if (counts.subagents > 0) items.push(plural(counts.subagents, 'sub-agent'))
  return items.join(' · ')
}

/** One step of work in the footer. */
export interface WorkLogStep {
  label: string
  /** True when the turn is blocked on the user or on a child thread rather than
   *  working. The footer stops its pulse for these: a waiting turn must not look
   *  like a busy one. */
  waiting: boolean
}

const THINKING: WorkLogStep = { label: 'Thinking', waiting: false }
const WRITING_REPLY: WorkLogStep = { label: 'Writing the reply', waiting: false }
const STARTING: WorkLogStep = { label: 'Starting', waiting: false }
const WAITING_FOR_ANSWER: WorkLogStep = { label: 'Waiting for your answer', waiting: true }
const STOPPED_BY_ERROR: WorkLogStep = { label: 'Stopped by an error', waiting: true }

/** Bound the target shown inside a live label: the footer is a line, not a log. */
const STEP_TARGET_LIMIT = 48

function briefTarget(target: string): string {
  const oneLine = target.replace(/\s+/g, ' ').trim()
  if (oneLine.length <= STEP_TARGET_LIMIT) return oneLine
  return `${oneLine.slice(0, STEP_TARGET_LIMIT - 1)}…`
}

/** Describe one running call the way a person would say it out loud. */
export function describeRunningTool(tool: TimelineTool): string {
  // A spawn call is a delegation, not the agent's own work: while it runs the
  // interesting fact is the child, which the waiting branch reports.
  if (tool.subagent && !tool.subagent.directive) return 'Spawning a sub-agent'
  const kind = toolActivityKind(tool.name)
  const target = briefTarget(describeToolActivity(tool).target)
  switch (kind) {
    case 'command':
      return target ? `Running \`${target}\`` : 'Running a command'
    case 'edit':
      return target ? `Editing ${target}` : 'Editing files'
    case 'write':
      return target ? `Writing ${target}` : 'Writing files'
    case 'read':
      return target ? `Reading ${target}` : 'Reading files'
    case 'search':
      return target ? `Searching for \`${target}\`` : 'Searching'
    case 'list':
      return target ? `Listing ${target}` : 'Listing files'
    case 'web':
      return target ? `Fetching ${target}` : 'Browsing the web'
    case 'plan':
      return 'Updating the plan'
    case 'mcp':
      return target ? `Calling ${target}` : 'Calling a tool'
    default:
      return tool.name ? `Running ${tool.name}` : 'Working'
  }
}

/** Several calls of one kind read as one sentence: "Reading 3 files". */
function describeRunningKind(kind: ToolActivityKind, count: number): string | null {
  switch (kind) {
    case 'command':
      return `Running ${plural(count, 'command')}`
    case 'edit':
      return `Editing ${plural(count, 'file')}`
    case 'write':
      return `Writing ${plural(count, 'file')}`
    case 'read':
      return `Reading ${plural(count, 'file')}`
    case 'search':
      return `Searching ${plural(count, 'pattern')}`
    case 'list':
      return `Listing ${plural(count, 'path')}`
    case 'web':
      return `Fetching ${plural(count, 'page')}`
    case 'mcp':
      return `Calling ${plural(count, 'tool')}`
    default:
      return null
  }
}

/** The live label for the calls running right now, or ``''`` when none are.
 *
 *  When only same-kind calls are in flight the count is worth more than the
 *  target ("Reading 3 files"); mixed kinds fall back to the newest one, which is
 *  what the agent is actually blocked on. */
export function describeRunningTools(tools: readonly TimelineTool[]): string {
  if (tools.length === 0) return ''
  const last = tools[tools.length - 1]
  if (tools.length > 1) {
    const kinds = new Set(tools.map(tool => toolActivityKind(tool.name)))
    if (kinds.size === 1) {
      const summary = describeRunningKind(kinds.values().next().value as ToolActivityKind, tools.length)
      if (summary) return summary
    }
  }
  return describeRunningTool(last)
}

/** Calls currently running, in arrival order. Delegation calls are excluded:
 *  a spawn that stays open while its child works would otherwise read as the
 *  parent spawning forever, and the child branch reports the truth. */
function runningTools(turn: TimelineTurn): TimelineTool[] {
  const running: TimelineTool[] = []
  for (const part of turn.parts) {
    for (const tool of toolsInPart(part)) {
      if (tool.subagent && !tool.subagent.directive) continue
      if (tool.status === 'running') running.push(tool)
    }
    if (part.kind !== 'subagents') continue
    for (const thread of part.threads) {
      for (const tool of toolsInThread(thread)) {
        if (!tool.subagent && tool.status === 'running') running.push(tool)
      }
    }
  }
  return running
}

function activeSubagents(turn: TimelineTurn): number {
  const active = new Set<string>()
  for (const part of turn.parts) {
    if (part.kind !== 'subagents') continue
    for (const thread of part.threads) {
      if (thread.lifecycleStatus === 'active') active.add(thread.threadId)
    }
  }
  return active.size
}

function hasPendingAnswer(turn: TimelineTurn): boolean {
  return turn.parts.some(part => part.kind === 'approval' && !part.approval.resolved)
}

/** What the turn is doing now.
 *
 *  Precedence is deliberate and ordered by what the reader can act on: a
 *  question waiting for an answer outranks any tool (the turn cannot move until
 *  it is answered), then the newest running call, then child threads the parent
 *  is waiting on. A turn whose last part is a finished call is *thinking*: it
 *  cannot be doing nothing, and "Working" with no step would be back to the
 *  empty label this replaces. */
export function currentStep(turn: TimelineTurn): WorkLogStep {
  if (hasPendingAnswer(turn)) return WAITING_FOR_ANSWER
  const running = runningTools(turn)
  if (running.length > 0) return { label: describeRunningTools(running), waiting: false }
  const children = activeSubagents(turn)
  if (children > 0) return { label: `Waiting on ${plural(children, 'sub-agent')}`, waiting: true }
  const last = turn.parts[turn.parts.length - 1]
  if (!last) return STARTING
  if (last.kind === 'error') return STOPPED_BY_ERROR
  if (last.kind === 'text') return WRITING_REPLY
  return THINKING
}
