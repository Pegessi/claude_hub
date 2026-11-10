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

/** What the footer says when nothing has arrived yet, from facts, not guesses.
 *
 *  A silent turn is ambiguous: the provider may be spinning up a session, or the
 *  request may already be dead. The Hub polls each tab's runtime status every
 *  five seconds, so the footer can report which of those it is instead of a
 *  reassuring sentence that means nothing. Only when the sample is missing does
 *  it fall back to saying the clock is the only information available.
 */
export interface FirstResponseFacts {
  elapsedMs: number
  connectionState: string
  /** Fresh tab-status sample, or null when it is missing or stale. */
  runtime: { status: string; activity: string | null; ageMs: number } | null
}

export function firstResponseHint(facts: FirstResponseFacts): string {
  const { elapsedMs, connectionState, runtime } = facts
  // The transcript cannot arrive over a stream that is not connected, and that
  // is the reader's actual problem in that case.
  if (connectionState === 'failed') return 'The event stream failed — stop this turn and send it again.'
  if (connectionState !== 'live') return 'Reconnecting to the event stream…'
  switch (runtime?.status) {
    case 'offline':
      return 'The provider is offline, so nothing can arrive. Stop this turn and send it again once it is back.'
    case 'attention':
      return 'The provider reported a problem. Stop this turn and send it again.'
    case 'idle':
      // A turn is open while the provider says it is not doing anything.
      return 'The provider is idle while this turn is open — it may have dropped the request.'
    case 'working':
      if (runtime.activity) return `The provider is ${runtime.activity.toLowerCase()} — nothing written yet.`
      return elapsedMs >= FIRST_RESPONSE_STUCK_AFTER_MS
        ? 'The provider is running but has written nothing for a while — usually a queued or stuck session.'
        : 'The provider is running; it has not written anything yet.'
    default:
      if (elapsedMs >= FIRST_RESPONSE_HINT_AFTER_MS) {
        return 'Nothing has come back yet, and the Hub has no fresh runtime status for this tab.'
      }
      return ''
  }
}

/** Long enough that silence needs an explanation: half a minute. */
export const FIRST_RESPONSE_HINT_AFTER_MS = 30_000

/** Long enough that a running-but-silent provider is worth calling out. */
export const FIRST_RESPONSE_STUCK_AFTER_MS = 120_000

/** The quiet second line, split so a failure tints only its own phrase.
 *
 *  ``plain`` is the work that went through (``12 edits · 6 commands · 4 reads``),
 *  ``failed`` is the failure on its own (``1 failed``). The footer renders them
 *  as two spans: a turn with one failed call among twenty is not a stalled turn,
 *  so the failure is marked where it is mentioned instead of painting the whole
 *  line red. */
export interface WorkLogCountParts {
  /** Work that went through, before the failure phrase. */
  prefix: string
  /** The failure on its own, empty when nothing failed. */
  failed: string
  /** Counts that read better after the failure (delegation). */
  suffix: string
}

/** Zero-count buckets are dropped rather than printed as ``0 commands``, and
 *  failures are kept even at length: a summary that hides them is worse than no
 *  summary. */
export function workLogCountParts(counts: WorkLogCounts): WorkLogCountParts {
  const prefix: string[] = []
  if (counts.edits > 0) prefix.push(plural(counts.edits, 'edit'))
  if (counts.commands > 0) prefix.push(plural(counts.commands, 'command'))
  if (counts.reads > 0) prefix.push(plural(counts.reads, 'read'))
  if (counts.searches > 0) prefix.push(plural(counts.searches, 'search', 'searches'))
  if (counts.web > 0) prefix.push(plural(counts.web, 'fetch', 'fetches'))
  if (counts.other > 0) prefix.push(plural(counts.other, 'other tool'))
  return {
    prefix: prefix.join(' · '),
    failed: counts.failed > 0 ? `${counts.failed} failed` : '',
    suffix: counts.subagents > 0 ? plural(counts.subagents, 'sub-agent') : '',
  }
}

/** The second line as one string. Empty when nothing has happened yet, so the
 *  footer stays one line until it has something to say. */
export function workLogCountLabel(counts: WorkLogCounts): string {
  const parts = workLogCountParts(counts)
  return [parts.prefix, parts.failed, parts.suffix].filter(Boolean).join(' · ')
}

/** One step of work in the footer. */
export interface WorkLogStep {
  label: string
  /** True when the turn is blocked on the user or on a child thread rather than
   *  working. The footer stops its pulse for these: a waiting turn must not look
   *  like a busy one. */
  waiting: boolean
  /** True when the label is a full sentence that needs no "Working" in front of
   *  it (the turn has not reported anything yet). The footer is still live and
   *  keeps its pulse; only the word is dropped. */
  bare?: boolean
}

const THINKING: WorkLogStep = { label: 'Thinking', waiting: false }
const WRITING_REPLY: WorkLogStep = { label: 'Writing the reply', waiting: false }
// Nothing reported yet is still "live": the turn exists, the provider has not
// sent its first event. Saying so beats a bare spinner with no clock, and it
// must not claim a step the agent has not taken.
const AWAITING_FIRST_EVENT: WorkLogStep = {
  label: 'Waiting for the first response',
  waiting: false,
  bare: true,
}

/** Events exist but no part has been reduced from them yet (the pane reads the
 *  turn's detail lazily). The label word carries the state, the counts line
 *  carries the volume, so the footer still says how much has happened. */
const WORKING_UNREDUCED: WorkLogStep = { label: '', waiting: false }
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
export function currentStep(turn: TimelineTurn, hasEvents = false): WorkLogStep {
  if (hasPendingAnswer(turn)) return WAITING_FOR_ANSWER
  const running = runningTools(turn)
  if (running.length > 0) return { label: describeRunningTools(running), waiting: false }
  const children = activeSubagents(turn)
  if (children > 0) return { label: `Waiting on ${plural(children, 'sub-agent')}`, waiting: true }
  const last = turn.parts[turn.parts.length - 1]
  // Events the pane has not reduced into parts yet are still output: a turn
  // whose descriptor already counts them is working behind an unopened fold,
  // not waiting for a provider that has said nothing.
  if (!last && hasEvents && turn.errors.length === 0) return WORKING_UNREDUCED
  if (!last && turn.errors.length === 0) return AWAITING_FIRST_EVENT
  if (!last) return STOPPED_BY_ERROR
  if (last.kind === 'error') return STOPPED_BY_ERROR
  if (last.kind === 'text') return WRITING_REPLY
  return THINKING
}
