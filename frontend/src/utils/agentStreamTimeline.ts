import type { AgentStreamEvent } from '@/types'
import { parseStructuredQuestions } from '@/utils/chatQuestionResponse'
import { formatElapsedDuration, parseTimestampMs } from '@/utils/duration'

export interface TimelineTool {
  key: string
  callId: string | null
  name: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  argsText: string
  resultText: string
}

export interface TimelineQuestionOption {
  id: string
  label: string
}

export interface TimelineQuestion {
  id: string
  prompt: string
  allowMultiple: boolean
  options: TimelineQuestionOption[]
}

export interface TimelineApproval {
  key: string
  callId: string | null
  kind: string
  title: string | null
  questions: TimelineQuestion[]
  resolved: boolean
}

/** Durable attachment descriptor carried by ``turn_started``.
 *
 *  The durable attachment cache stores only the browser-generated bounded
 *  preview bytes (max edge 1024px, max 512 KiB). The original image bytes are
 *  transient provider input — they are forwarded to the model and never
 *  persisted (the Codex native provider stages them under the runtime temp
 *  dir and deletes them when the turn ends). The user bubble fetches the
 *  persisted preview from the scoped attachment GET endpoint using ``id``.
 *
 *  ``id`` may be ``null`` for a deliberate no-preview placeholder when the
 *  client did not supply a bounded preview. Eviction keeps the opaque id in
 *  history; the scoped GET then returns 410 and the template replaces the
 *  failed image with a stable placeholder. */
export interface TimelineAttachment {
  id: string | null
  mime_type: string
  bytes: number
  width?: number
  height?: number
}

export type TimelinePart =
  | { kind: 'thinking'; key: string; text: string }
  // ``fromPlan`` marks Codex's plan stream: it renders exactly like any other
  // prose, but it is work rather than the agent's answer, so the fold must not
  // treat it as a delivery. See the fold helpers below.
  | { kind: 'text'; key: string; text: string; fromPlan?: boolean }
  | { kind: 'tool'; key: string; tool: TimelineTool }
  | { kind: 'tool_group'; key: string; tools: TimelineTool[] }
  | { kind: 'approval'; key: string; approval: TimelineApproval }
  | { kind: 'error'; key: string; message: string }
  | { kind: 'status'; key: string; text: string }
  // Synthetic part produced by ``foldTurnParts`` — never emitted by the
  // reducer. It is the folded working region's header: the toggle that both
  // reveals and hides the detail beneath it.
  | { kind: 'process'; key: string; meta: string; expanded: boolean }

export interface TimelineTurn {
  key: string
  turnId: string | null
  userText: string
  /** Durable attachment descriptors surfaced by ``turn_started``.
   *  Each entry carries the opaque attachment id, mime type, byte size,
   *  and optional pixel dimensions — never raw bytes or local paths. The
   *  user bubble resolves the preview via the scoped attachment GET. */
  attachments: TimelineAttachment[]
  /** Ordered sequence of thinking/text/tool/error/status parts as they arrived. */
  parts: TimelinePart[]
  // Compatibility aggregates kept for callers/tests that read the flat
  // buckets. ``parts`` is the authoritative render order; these are derived
  // views over it.
  assistantText: string
  thinkingText: string
  tools: TimelineTool[]
  approvals: TimelineApproval[]
  completed: boolean
  completionStatus: string | null
  /** Wall-clock of ``turn_started`` / ``turn_completed``, used to label the
   *  folded process with how long the turn took. ``null`` until the matching
   *  event arrives; a turn whose spans are not real elapsed time (history
   *  replayed from a provider transcript stamps every event with the import
   *  time) is filtered by ``turnElapsedMs`` rather than shown as ``0s``. */
  startedAt: string | null
  completedAt: string | null
  errors: { key: string; message: string }[]
  statuses: { key: string; text: string }[]
  /**
   * Monotonically increasing render revision. Incremented only when an
   * applied event visibly mutates this turn (text/thinking/tool/status/
   * error/completion/user summary). Events that are no-ops (empty text,
   * exact multi-chunk replay, duplicate tool) do not advance the revision.
   *
   * ``StructuredPane`` uses ``v-memo="[turn.renderRevision]"`` on each
   * ``.structured-turn`` so Vue skips re-rendering completed historical
   * turns whose revision has not changed; only the active turn rebuilds.
   */
  renderRevision: number
}

function payloadString(event: AgentStreamEvent, key: string): string {
  const value = event.payload[key]
  return typeof value === 'string' ? value : ''
}

function payloadRecord(event: AgentStreamEvent, key: string): Record<string, unknown> {
  const value = event.payload[key]
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function createTurn(key: string, turnId: string | null): TimelineTurn {
  return {
    key,
    turnId,
    userText: '',
    attachments: [],
    parts: [],
    assistantText: '',
    thinkingText: '',
    tools: [],
    approvals: [],
    completed: false,
    completionStatus: null,
    startedAt: null,
    completedAt: null,
    errors: [],
    statuses: [],
    renderRevision: 0,
  }
}

/** Append or extend the last part of the given kind with more text. */
function appendTextPart(
  turn: TimelineTurn,
  kind: 'thinking' | 'text',
  text: string,
  sequence: number,
  fromPlan = false,
): void {
  if (!text) return
  const last = turn.parts[turn.parts.length - 1]
  // A plan segment and an answer segment are both prose but are different
  // kinds of thing; merging them would produce one part that is half work and
  // half answer, which the fold could then only classify wrongly.
  const samePart =
    last !== undefined &&
    last.kind === kind &&
    (kind !== 'text' || (last.kind === 'text' && Boolean(last.fromPlan) === fromPlan))
  if (samePart) {
    last.text += text
  } else if (kind === 'text') {
    turn.parts.push(
      fromPlan
        ? { kind, key: `plan-${sequence}`, text, fromPlan: true }
        : { kind, key: `text-${sequence}`, text },
    )
  } else {
    turn.parts.push({ kind, key: `thinking-${sequence}`, text })
  }
  if (kind === 'thinking') turn.thinkingText += text
  else turn.assistantText += text
}

function isExactMultiChunkReplay(
  accumulatedText: string,
  priorChunks: string[],
  candidate: string,
): boolean {
  if (!candidate || !accumulatedText.endsWith(candidate)) return false

  let remaining = candidate.length
  let matchedChunks = 0
  for (let index = priorChunks.length - 1; index >= 0; index -= 1) {
    remaining -= priorChunks[index].length
    matchedChunks += 1
    if (remaining === 0) return matchedChunks >= 2
    if (remaining < 0) return false
  }
  return false
}

/**
 * Mutable reducer state shared by ``groupEventsIntoTurns`` and
 * ``IncrementalTimelineReducer``. Processing one event mutates the state in
 * place; the reducer owns the lifecycle (reset, processed-count tracking).
 */
interface ReducerState {
  turns: TimelineTurn[]
  byTurnId: Map<string, TimelineTurn>
  toolsByTurn: Map<string, Map<string, TimelineTool>>
  textChunksByTurn: Map<string, string[]>
  legacyCurrent: TimelineTurn | null
}

function createReducerState(): ReducerState {
  return {
    turns: [],
    byTurnId: new Map(),
    toolsByTurn: new Map(),
    textChunksByTurn: new Map(),
    legacyCurrent: null,
  }
}

function resolveTurn(state: ReducerState, event: AgentStreamEvent): TimelineTurn {
  if (event.turn_id) {
    let turn = state.byTurnId.get(event.turn_id)
    if (!turn) {
      turn = createTurn(`turn-${event.turn_id}`, event.turn_id)
      state.byTurnId.set(event.turn_id, turn)
      state.turns.push(turn)
    }
    return turn
  }
  if (event.type === 'turn_started' || !state.legacyCurrent) {
    state.legacyCurrent = createTurn(`legacy-turn-${event.stream_sequence}`, null)
    state.turns.push(state.legacyCurrent)
  }
  return state.legacyCurrent
}

/** Apply a single event to the reducer state. Pure mutation; no allocation
 *  beyond the turn/tool/part objects the event requires.
 *
 *  ``turn.renderRevision`` is incremented only when the event visibly
 *  mutates the turn. No-op events (empty text, exact multi-chunk replay,
 *  duplicate tool) leave the revision unchanged so ``v-memo`` can skip
 *  re-rendering the turn. */
function applyEventToState(state: ReducerState, event: AgentStreamEvent): void {
  const turn = resolveTurn(state, event)
  const toolMapKey = turn.turnId ?? turn.key
  let toolMap = state.toolsByTurn.get(toolMapKey)
  if (!toolMap) {
    toolMap = new Map<string, TimelineTool>()
    state.toolsByTurn.set(toolMapKey, toolMap)
  }

  let mutated = false

  switch (event.type) {
    case 'turn_started': {
      // Recorded without touching ``mutated``: on its own a start timestamp
      // changes nothing visible. It only feeds the folded process label, which
      // is rendered for completed turns — and completion bumps the revision.
      if (turn.startedAt === null) turn.startedAt = event.created_at
      const summary = payloadString(event, 'summary')
      if (turn.userText !== summary) {
        turn.userText = summary
        mutated = true
      }
      // Surface durable attachment descriptors so the user bubble can resolve
      // and render the preview. The payload carries opaque ids + mime + size
      // (+ optional dimensions), never raw bytes.
      const rawAtts = event.payload.attachments
      if (Array.isArray(rawAtts)) {
        const atts: TimelineAttachment[] = rawAtts
          .filter((a): a is Record<string, unknown> => a && typeof a === 'object')
          .map((a) => ({
            id: a.id == null ? null : String(a.id),
            mime_type: String(a.mime_type ?? ''),
            bytes: typeof a.bytes === 'number' ? a.bytes : 0,
            width: typeof a.width === 'number' ? a.width : undefined,
            height: typeof a.height === 'number' ? a.height : undefined,
          }))
        // Only mutate if the attachment list actually changed.
        if (
          turn.attachments.length !== atts.length ||
          turn.attachments.some((a, i) => a.id !== atts[i].id)
        ) {
          turn.attachments = atts
          mutated = true
        }
      }
      break
    }
    case 'turn_completed': {
      if (!turn.completed) {
        turn.completed = true
        turn.completedAt = event.created_at
        turn.completionStatus = payloadString(event, 'status') || 'completed'
        for (const tool of turn.tools) {
          if (tool.status === 'running') tool.status = 'completed'
        }
        mutated = true
      }
      break
    }
    case 'text_delta':
    {
      const text = payloadString(event, 'text')
      if (!text) break
      const chunks = state.textChunksByTurn.get(toolMapKey) ?? []
      if (isExactMultiChunkReplay(turn.assistantText, chunks, text)) break
      appendTextPart(turn, 'text', text, event.stream_sequence, event.payload.plan === true)
      chunks.push(text)
      state.textChunksByTurn.set(toolMapKey, chunks)
      mutated = true
      break
    }
    case 'thinking_delta': {
      const text = payloadString(event, 'text')
      if (!text) break
      appendTextPart(turn, 'thinking', text, event.stream_sequence)
      mutated = true
      break
    }
    case 'tool_call_started': {
      const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
      const toolName = payloadString(event, 'name') || 'unknown'
      let argsText = ''
      try {
        argsText = JSON.stringify(payloadRecord(event, 'args'), null, 2)
      } catch {
        argsText = String(event.payload.args ?? '')
      }
      const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
      if (!toolMap.has(identity)) {
        const tool: TimelineTool = {
          key: `tool-${identity}`,
          callId,
          name: toolName,
          status: 'running',
          argsText,
          resultText: '',
        }
        toolMap.set(identity, tool)
        turn.tools.push(tool)
        if (toolName !== 'AskQuestion' && toolName !== 'AskUserQuestion' && toolName !== 'request_user_input') {
          // Group consecutive tool calls into a single tool_group part so the
          // UI can render a compact summary (e.g. "3 tools: Read, Edit, Bash")
          // with expandable per-tool details, matching how Codex/Paseo display
          // parallel tool calls. AskQuestion (Cursor), AskUserQuestion
          // (Claude), and request_user_input (Codex) are excluded: they render
          // as interactive approval cards instead of a raw tool row.
          const lastPart = turn.parts[turn.parts.length - 1]
          if (lastPart && lastPart.kind === 'tool_group') {
            lastPart.tools.push(tool)
          } else {
            turn.parts.push({ kind: 'tool_group', key: `tool-group-${event.stream_sequence}`, tools: [tool] })
          }
        }
        mutated = true
      }
      break
    }
    case 'approval_required': {
      const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
      const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
      const approvalKey = `approval-${identity}`
      if (turn.approvals.some(approval => approval.key === approvalKey)) break
      const approval: TimelineApproval = {
        key: approvalKey,
        callId,
        kind: payloadString(event, 'kind') || 'approval',
        title: payloadString(event, 'title') || null,
        questions: parseStructuredQuestions(event.payload.questions),
        resolved: false,
      }
      turn.approvals.push(approval)
      turn.parts.push({ kind: 'approval', key: approvalKey, approval })
      mutated = true
      break
    }
    case 'approval_resolved': {
      const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
      const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
      const approvalKey = `approval-${identity}`
      const approval = turn.approvals.find(item => item.key === approvalKey)
      if (approval && !approval.resolved) {
        approval.resolved = true
        mutated = true
      }
      break
    }
    case 'tool_call_completed': {
      const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
      const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
      let tool = toolMap.get(identity)
      const isNew = !tool
      if (!tool) {
        tool = {
          key: `tool-${identity}`,
          callId,
          name: payloadString(event, 'name') || 'tool',
          status: 'running',
          argsText: '',
          resultText: '',
        }
        toolMap.set(identity, tool)
        turn.tools.push(tool)
        // If we never saw the start event, still group this completed tool.
        const lastPart = turn.parts[turn.parts.length - 1]
        if (lastPart && lastPart.kind === 'tool_group') {
          lastPart.tools.push(tool)
        } else {
          turn.parts.push({ kind: 'tool_group', key: `tool-group-${event.stream_sequence}`, tools: [tool] })
        }
      }
      let newStatus: TimelineTool['status'] =
        payloadString(event, 'status') === 'failed' ? 'failed' : 'completed'
      // AskUserQuestion reports "failed" when the user dismisses the prompt —
      // that is a cancellation, not a tool error. Surface it as cancelled.
      // The tool name is only known when the start event was loaded (it alone
      // carries the name); history hydrates contiguously from sequence 0, so a
      // completed event without its start only occurs for a corrupted
      // transcript. There the name is unrecoverable and the tool renders as a
      // generic failed "tool" — pre-fix behavior, not a regression.
      if (newStatus === 'failed' && tool.name === 'AskUserQuestion') {
        newStatus = 'cancelled'
      }
      const newResult = payloadString(event, 'result')
      if (isNew || tool.status !== newStatus || tool.resultText !== newResult) {
        tool.status = newStatus
        tool.resultText = newResult
        mutated = true
      }
      break
    }
    case 'error': {
      const message = payloadString(event, 'message') || 'An error occurred.'
      const errKey = `error-${event.message_id ?? 'event'}-${event.stream_sequence}`
      turn.errors.push({ key: errKey, message })
      turn.parts.push({ kind: 'error', key: errKey, message })
      mutated = true
      break
    }
    case 'status': {
      const text = payloadString(event, 'text') || payloadString(event, 'message') ||
        payloadString(event, 'status') || 'status update'
      const statusKey = `status-${event.message_id ?? 'event'}-${event.stream_sequence}`
      turn.statuses.push({ key: statusKey, text })
      turn.parts.push({ kind: 'status', key: statusKey, text })
      mutated = true
      break
    }
    default:
      break
  }

  if (mutated) {
    turn.renderRevision += 1
  }
}

/** Fold append-only events into turns keyed by the native turn identity. */
export function groupEventsIntoTurns(events: AgentStreamEvent[]): TimelineTurn[] {
  const state = createReducerState()
  for (const event of events) {
    applyEventToState(state, event)
  }
  return state.turns
}

// ── process folding ─────────────────────────────────────────────────────
//
// A finished turn is mostly a record of how the answer was reached: thinking,
// tool calls, and the narration between them. Once the answer is delivered the
// working region is collapsed to a single line so a long conversation reads as
// questions and answers, with the process one click away. The split is
// structural and pure; where the fold state lives, and which turns default to
// folded, is the renderer's decision (see ``StructuredPane``).

/** A turn's parts, split into its working process and its delivered answer. */
export interface TurnProcessSplit {
  /** Thinking, tool groups, and the narration between them. */
  process: TimelinePart[]
  /** The delivered answer plus anything that arrived after it. */
  delivery: TimelinePart[]
}

/** Index of the delivered answer: the turn's final text segment.
 *
 *  Everything the model says before it is working narration ("我先看一下…"),
 *  so the LAST text part is the delivery, not the first. Codex's plan stream is
 *  skipped: it is prose too, but it describes work rather than answering. */
function deliveryIndex(parts: TimelinePart[]): number {
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    const part = parts[i]
    if (part.kind === 'text' && !part.fromPlan) return i
  }
  return -1
}

/** Whether a part records how the answer was reached rather than being output. */
function isProcessPart(part: TimelinePart): boolean {
  return (
    part.kind === 'thinking' ||
    part.kind === 'tool' ||
    part.kind === 'tool_group' ||
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
 *  * the process region holds an approval card or an error — folding those
 *    would hide a control the user still has to click, or the reason the turn
 *    failed. Keeping such a turn whole is easier to reason about than
 *    re-ordering parts around a fold.
 */
export function splitTurnProcess(turn: TimelineTurn): TurnProcessSplit | null {
  if (!turn.completed) return null
  const index = deliveryIndex(turn.parts)
  if (index <= 0) return null
  if (turn.parts.slice(index + 1).some(isProcessPart)) return null
  const process = turn.parts.slice(0, index)
  if (process.some((part) => part.kind === 'approval' || part.kind === 'error')) {
    return null
  }
  return { process, delivery: turn.parts.slice(index) }
}

/** Count what the agent actually did, for the folded label.
 *
 *  Tool calls rather than parts: "8 个工具调用" should mean eight actions, not
 *  eight render blocks. */
export function countProcessSteps(process: TimelinePart[]): number {
  let steps = 0
  for (const part of process) {
    if (part.kind === 'tool_group') steps += part.tools.length
    else if (part.kind === 'tool') steps += 1
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
    meta: turnProcessLabel(turn, split.process),
    expanded,
  }
  return expanded
    ? [header, ...split.process, ...split.delivery]
    : [header, ...split.delivery]
}

/**
 * Incremental timeline reducer.
 *
 * ``groupEventsIntoTurns`` re-scans the entire event list on every call, which
 * becomes O(total events) per batch. For a session with 13.5k historical
 * events, each incoming delta re-runs the full reduction and dominates the
 * long-task budget.
 *
 * ``IncrementalTimelineReducer`` keeps the reducer state alive across calls
 * and only processes events it has not seen yet. The cost per batch is
 * O(new events), independent of history length.
 *
 * Correctness contract:
 * - Events are append-only (sequence numbers never decrease within a session).
 * - The reducer detects when the event list is no longer a strict append of
 *   the previously processed prefix (shrink, same-length replacement, or a
 *   longer list whose prefix diverges — e.g. session switch, reconnect, or
 *   reset) and rebuilds from scratch.
 * - The returned array is a fresh reference each call so Vue computed
 *   invalidation fires; turn objects are mutated in place.
 */
export class IncrementalTimelineReducer {
  private state: ReducerState = createReducerState()
  private processedCount = 0
  /** Cumulative number of events applied since the last reset. Exposed so
   *  tests can assert the incremental path applies exactly the unseen suffix
   *  rather than re-scanning history. */
  private totalApplied = 0
  /** Identity of the last applied event, used to detect non-prefix
   *  replacements of the event list (session switch / reconnect / reset). */
  private lastAppliedKey: string | null = null

  private static eventKey(event: AgentStreamEvent): string {
    // stream_sequence is zero-based per session; combine with session/tab
    // identity so a reused sequence number from a different session does not
    // pass the prefix check.
    return `${event.session_id ?? ''}\u0000${event.tab_id ?? ''}\u0000${event.stream_sequence}`
  }

  /** Reduce the full event list, processing only the unseen suffix. */
  reduce(events: AgentStreamEvent[]): TimelineTurn[] {
    // Detect non-prefix replacements: if we have processed events, the event
    // at index processedCount - 1 must be the same event we last applied.
    // A mismatch means the list was replaced (session switch, reconnect, or
    // reset) rather than appended to, so we rebuild from scratch.
    if (this.processedCount > 0) {
      const lastProcessed = events[this.processedCount - 1]
      if (!lastProcessed || IncrementalTimelineReducer.eventKey(lastProcessed) !== this.lastAppliedKey) {
        this.reset()
      }
    }

    const newEvents = events.slice(this.processedCount)
    for (const event of newEvents) {
      applyEventToState(this.state, event)
      this.totalApplied += 1
      this.lastAppliedKey = IncrementalTimelineReducer.eventKey(event)
    }
    this.processedCount = events.length
    // Return a fresh array reference so Vue's computed dependency tracking
    // detects the change. Turn objects are mutated in place; callers that
    // need structural sharing can rely on turn.key stability.
    return [...this.state.turns]
  }

  reset(): void {
    this.state = createReducerState()
    this.processedCount = 0
    this.totalApplied = 0
    this.lastAppliedKey = null
  }

  /** Number of events consumed so far. Exposed for tests. */
  get consumed(): number {
    return this.processedCount
  }

  /** Cumulative events applied since the last reset. Exposed for tests to
   *  assert the incremental path only processes the unseen suffix. */
  get appliedCount(): number {
    return this.totalApplied
  }
}
