import type { AgentStreamEvent } from '@/types'

export interface TimelineTool {
  key: string
  callId: string | null
  name: string
  status: 'running' | 'completed' | 'failed'
  argsText: string
  resultText: string
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
  | { kind: 'text'; key: string; text: string }
  | { kind: 'tool'; key: string; tool: TimelineTool }
  | { kind: 'error'; key: string; message: string }
  | { kind: 'status'; key: string; text: string }

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
  completed: boolean
  completionStatus: string | null
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
    completed: false,
    completionStatus: null,
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
): void {
  if (!text) return
  const last = turn.parts[turn.parts.length - 1]
  if (last && last.kind === kind) {
    last.text += text
  } else {
    turn.parts.push({ kind, key: `${kind}-${sequence}`, text })
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
      appendTextPart(turn, 'text', text, event.stream_sequence)
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
          name: payloadString(event, 'name') || 'unknown',
          status: 'running',
          argsText,
          resultText: '',
        }
        toolMap.set(identity, tool)
        turn.tools.push(tool)
        turn.parts.push({ kind: 'tool', key: tool.key, tool })
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
        turn.parts.push({ kind: 'tool', key: tool.key, tool })
      }
      const newStatus = payloadString(event, 'status') === 'failed' ? 'failed' : 'completed'
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
