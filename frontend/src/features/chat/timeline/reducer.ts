import type { AgentStreamEvent } from '@/types'
import { agentImagePathFromTool } from '@/utils/agentImage'
import { parseStructuredQuestions } from '@/utils/chatQuestionResponse'
import { parseSubagent, subagentResultText } from '@/utils/subagentTool'
import type { TimelineTurn, TimelineTool, TimelineApproval, TimelineAttachment, TimelinePart } from './types'
import { createReducerState, type ReducerState } from './state'
import { createTurn, payloadString, payloadRecord, statusText, turnProvenance, appendTextPart, isExactMultiChunkReplay } from './eventValues'
import { subThreadIdOf, ensureSubthread, applySubthreadEvent, detachToolFromMainParts } from './subthreads'

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
// Record types that carry a tool/approval call identity usable for replay
// detection. Mirrors the backend's persistent-transport orphan filter.
const REPLAY_IDENTITY_TYPES = new Set([
  'tool_call_started',
  'tool_call_completed',
  'approval_required',
  'approval_resolved',
])

function replayToolIdentity(event: AgentStreamEvent): string | null {
  if (!REPLAY_IDENTITY_TYPES.has(event.type)) return null
  const fromPayload = event.payload.tool_call_id
  if (typeof fromPayload === 'string' && fromPayload) return fromPayload
  return typeof event.call_id === 'string' && event.call_id ? event.call_id : null
}

function applyEventToState(state: ReducerState, event: AgentStreamEvent): void {
  // Persistent app-server statuses belong to a Chat turn only when the tailer
  // stamps its turn id. Text-bearing notices can also arrive while idle (for
  // example a TraeX warning after turn_completed); rendering one as a legacy
  // turn would lock Queue/Stop around a turn that does not exist. Keep these
  // persisted control-plane events out of the transcript, just like silent
  // thread/goal notifications. One-shot providers retain legacy null-turn rows.
  if (event.type === 'status' && (
    !statusText(event) ||
    (!event.turn_id && (event.agent_type === 'traex' || event.agent_type === 'codex'))
  )) return

  // Provider replay guard: a persistent app-server (TraeX/Codex) can re-emit
  // an item record long after the turn it belonged to terminalized — the
  // observed wedge was a duplicate nested code-mode item/completed arriving
  // hours later during background/goal execution, stamped with no turn id. If
  // the record's tool/approval identity was already attributed to a turn with
  // a real id, this is a replay: drop it so it cannot mint a fresh
  // never-completed legacy turn and pin the composer (Stop/queue lock).
  if (!event.turn_id) {
    const identity = replayToolIdentity(event)
    if (identity !== null && state.attributedToolIds.has(identity)) return
  }

  const turn = resolveTurn(state, event)
  if (event.turn_id) {
    const identity = replayToolIdentity(event)
    if (identity !== null) state.attributedToolIds.add(identity)
  }
  const toolMapKey = turn.turnId ?? turn.key
  let toolMap = state.toolsByTurn.get(toolMapKey)
  if (!toolMap) {
    toolMap = new Map<string, TimelineTool>()
    state.toolsByTurn.set(toolMapKey, toolMap)
  }

  // Nested sub-agent thread event (stamped with ``subagent_thread``): keep it
  // out of the main assistant bubble/process counts and into the child's own
  // expandable group. Returns before any main bookkeeping, so the main thread
  // retains only the main agent's own content and its final conclusion.
  const routedThreadId = subThreadIdOf(event)
  if (routedThreadId !== null) {
    if (applySubthreadEvent(state, turn, routedThreadId, event, toolMapKey)) {
      const child = state.threadsByTurn.get(toolMapKey)?.get(routedThreadId)
      if (child) child.thread.renderRevision += 1
      turn.renderRevision += 1
    }
    return
  }

  let mutated = false

  switch (event.type) {
    case 'turn_started': {
      // Counts as a mutation: the message row renders this timestamp, so a turn
      // whose only change is this field would otherwise keep a stale clock
      // label behind ``v-memo``.
      if (turn.startedAt === null) {
        turn.startedAt = event.created_at
        mutated = true
      }
      const summary = payloadString(event, 'summary')
      if (turn.origin === 'unknown') {
        const provenance = turnProvenance(event)
        if (provenance !== null) {
          turn.origin = provenance.origin
          turn.feishu = provenance.feishu
          mutated = true
        }
      }
      const mode = event.payload.mode
      if ((mode === 'plan' || mode === 'default') && turn.mode !== mode) {
        turn.mode = mode
        mutated = true
      }
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
      if (event.payload.plan === true && event.message_id) {
        const existing = turn.parts.find(part =>
          part.kind === 'text' && part.fromPlan && part.messageId === event.message_id,
        )
        if (existing?.kind === 'text') {
          const nextText = event.payload.snapshot === true ? text : existing.text + text
          if (nextText === existing.text) break
          existing.text = nextText
        } else {
          if (!text) break
          turn.parts.push({
            kind: 'text', key: `plan-${event.stream_sequence}`, text, at: event.created_at,
            fromPlan: true, messageId: event.message_id,
            planKind: event.payload.plan_kind === 'progress' ? 'progress' : 'proposal',
          })
        }
        turn.assistantText = turn.parts
          .filter((part): part is Extract<TimelinePart, { kind: 'text' }> => part.kind === 'text')
          .map(part => part.text).join('')
        mutated = true
        break
      }
      if (!text) break
      const chunks = state.textChunksByTurn.get(toolMapKey) ?? []
      if (isExactMultiChunkReplay(turn.assistantText, chunks, text)) break
      appendTextPart(
        turn,
        'text',
        text,
        event.stream_sequence,
        event.created_at,
        event.payload.plan === true,
      )
      const historyChunkCount = event.payload._history_chunk_count
      chunks.push({
        text,
        count: typeof historyChunkCount === 'number' && historyChunkCount > 1
          ? Math.floor(historyChunkCount)
          : 1,
      })
      state.textChunksByTurn.set(toolMapKey, chunks)
      mutated = true
      break
    }
    case 'thinking_delta': {
      const text = payloadString(event, 'text')
      if (!text) break
      appendTextPart(turn, 'thinking', text, event.stream_sequence, event.created_at)
      mutated = true
      break
    }
    case 'tool_call_started': {
      const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
      const toolName = payloadString(event, 'name') || 'unknown'
      const argsRecord = payloadRecord(event, 'args')
      let argsText = ''
      try {
        argsText = JSON.stringify(argsRecord, null, 2)
      } catch {
        argsText = String(event.payload.args ?? '')
      }
      // Sub-agent-spawn tools are identified from the structured args at the
      // only event that carries them (the start event) and rendered on their
      // own card. A null view means this is an ordinary tool and keeps grouping.
      const subagent = parseSubagent(toolName, event.payload.args)
      // Agent image views (view_image / image Read) likewise split out of the
      // generic tool groups so the pane renders the picture, not a path string.
      const imagePath = agentImagePathFromTool(toolName, argsRecord)
      const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
      if (!toolMap.has(identity)) {
        const tool: TimelineTool = {
          key: `tool-${identity}`,
          callId,
          name: toolName,
          status: 'running',
          argsText,
          resultText: '',
          ...(imagePath !== null ? { imagePath } : {}),
          ...(subagent ? { subagent } : {}),
        }
        toolMap.set(identity, tool)
        turn.tools.push(tool)
        if (toolName !== 'AskQuestion' && toolName !== 'AskUserQuestion' && toolName !== 'request_user_input') {
          if (imagePath !== null) {
            // An agent-produced image gets its own block (thumbnail + lightbox).
            // It never merges into a neighbouring tool group; tools on either
            // side form their own groups.
            turn.parts.push({
              kind: 'agent_image',
              key: `agent-image-${identity}`,
              tool,
              path: imagePath,
            })
          } else if (subagent && subagent.directive && subagent.threadIds.length > 0) {
            // TraeX/Codex sendInput: a follow-up directive posted TO one or
            // more already-running child threads. It is not the main agent's
            // own speech, so mount each addressed child's nested group (if not
            // already present from streamed child content) and file the prompt
            // inside it as an inbound instruction row. The call itself stays on
            // the main map above so its completion (agentsStates) resolves by
            // call id. The original spawnAgent is NOT filed here — it keeps its
            // dedicated sub-agent card below, so the delegated prompt is shown
            // exactly once.
            for (const tid of subagent.threadIds) {
              const child = ensureSubthread(state, turn, tid, toolMapKey)
              child.thread.parts.push({
                kind: 'instruction',
                key: `sub-instruction-${identity}-${tid}`,
                tool,
              })
            }
          } else if (subagent && subagent.threadIds.length > 0) {
            // A native spawn and its child stream are one UI entity. Attach the
            // dispatch call to every full receiver id instead of rendering a
            // second standalone card. Tool status remains dispatch-only.
            for (const tid of subagent.threadIds) {
              ensureSubthread(state, turn, tid, toolMapKey).thread.launchTool = tool
            }
          } else if (subagent) {
            // Claude/Cursor calls in captured streams have no provider thread
            // id. Keep the existing standalone fallback rather than guessing.
            turn.parts.push({ kind: 'subagent', key: `subagent-${identity}`, tool })
          } else {
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
      // Native app-server spawn starts before its child id exists. Recover the
      // identity from item/completed, detach the temporary generic tool row,
      // and attach the same TimelineTool to the full-id child row.
      if (!tool.subagent) {
        const recovered = parseSubagent(payloadString(event, 'name'), event.payload.args)
        if (recovered && recovered.threadIds.length > 0) {
          tool.name = payloadString(event, 'name') || tool.name
          tool.subagent = recovered
          detachToolFromMainParts(turn, tool)
          if (recovered.directive) {
            for (const threadId of recovered.threadIds) {
              ensureSubthread(state, turn, threadId, toolMapKey).thread.parts.push({
                kind: 'instruction',
                key: `sub-instruction-${identity}-${threadId}`,
                tool,
              })
            }
          } else {
            for (const threadId of recovered.threadIds) {
              ensureSubthread(state, turn, threadId, toolMapKey).thread.launchTool = tool
            }
          }
          mutated = true
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
      const newResult = subagentResultText(tool.subagent, event.payload.result)
        ?? payloadString(event, 'result')
      if (isNew || tool.status !== newStatus || tool.resultText !== newResult) {
        tool.status = newStatus
        tool.resultText = newResult
        mutated = true
      }
      break
    }
    case 'error': {
      const message = payloadString(event, 'message') || 'An error occurred.'
      const detail = payloadString(event, 'detail')
      const errKey = `error-${event.message_id ?? 'event'}-${event.stream_sequence}`
      turn.errors.push({ key: errKey, message })
      turn.parts.push({ kind: 'error', key: errKey, message, detail: detail || undefined })
      mutated = true
      break
    }
    case 'status': {
      const text = statusText(event)
      const providerStatus = payloadString(event, 'provider_status') || undefined
      if (event.payload.snapshot === true && event.message_id) {
        const existing = turn.parts.find(part =>
          part.kind === 'status' && part.messageId === event.message_id,
        )
        if (existing?.kind === 'status') {
          if (existing.text === text && existing.providerStatus === providerStatus) break
          existing.text = text
          existing.providerStatus = providerStatus
          const stored = turn.statuses.find(status => status.key === existing.key)
          if (stored) stored.text = text
          mutated = true
          break
        }
      }
      const statusKey = `status-${event.message_id ?? 'event'}-${event.stream_sequence}`
      turn.statuses.push({ key: statusKey, text })
      turn.parts.push({
        kind: 'status', key: statusKey, text, messageId: event.message_id,
        ...(providerStatus ? { providerStatus } : {}),
      })
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
