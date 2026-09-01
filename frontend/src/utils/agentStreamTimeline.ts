import type { AgentStreamEvent } from '@/types'

export interface TimelineTool {
  key: string
  callId: string | null
  name: string
  status: 'running' | 'completed' | 'failed'
  argsText: string
  resultText: string
}

export interface TimelineTurn {
  key: string
  turnId: string | null
  userText: string
  assistantText: string
  thinkingText: string
  completed: boolean
  completionStatus: string | null
  tools: TimelineTool[]
  errors: { key: string; message: string }[]
  statuses: { key: string; text: string }[]
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
    assistantText: '',
    thinkingText: '',
    completed: false,
    completionStatus: null,
    tools: [],
    errors: [],
    statuses: [],
  }
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

/** Fold append-only events into turns keyed by the native turn identity. */
export function groupEventsIntoTurns(events: AgentStreamEvent[]): TimelineTurn[] {
  const turns: TimelineTurn[] = []
  const byTurnId = new Map<string, TimelineTurn>()
  const toolsByTurn = new Map<string, Map<string, TimelineTool>>()
  const textChunksByTurn = new Map<string, string[]>()
  let legacyCurrent: TimelineTurn | null = null

  const resolveTurn = (event: AgentStreamEvent): TimelineTurn => {
    if (event.turn_id) {
      let turn = byTurnId.get(event.turn_id)
      if (!turn) {
        turn = createTurn(`turn-${event.turn_id}`, event.turn_id)
        byTurnId.set(event.turn_id, turn)
        turns.push(turn)
      }
      return turn
    }
    if (event.type === 'turn_started' || !legacyCurrent) {
      legacyCurrent = createTurn(`legacy-turn-${event.stream_sequence}`, null)
      turns.push(legacyCurrent)
    }
    return legacyCurrent
  }

  for (const event of events) {
    const turn = resolveTurn(event)
    const toolMapKey = turn.turnId ?? turn.key
    let toolMap = toolsByTurn.get(toolMapKey)
    if (!toolMap) {
      toolMap = new Map<string, TimelineTool>()
      toolsByTurn.set(toolMapKey, toolMap)
    }

    switch (event.type) {
      case 'turn_started':
        turn.userText = payloadString(event, 'summary')
        break
      case 'turn_completed':
        turn.completed = true
        turn.completionStatus = payloadString(event, 'status') || 'completed'
        for (const tool of turn.tools) {
          if (tool.status === 'running') tool.status = 'completed'
        }
        break
      case 'text_delta':
      {
        const text = payloadString(event, 'text')
        if (!text) break
        const chunks = textChunksByTurn.get(toolMapKey) ?? []
        // Older persisted Cursor streams may already contain a provider final
        // snapshot that exactly replays several preceding deltas. Keep the
        // append-only store authoritative, but suppress that proven replay at
        // render time so upgrading repairs existing conversations too. Two
        // complete prior chunks are required, preserving a legitimate single
        // repeated delta.
        if (isExactMultiChunkReplay(turn.assistantText, chunks, text)) break
        turn.assistantText += text
        chunks.push(text)
        textChunksByTurn.set(toolMapKey, chunks)
        break
      }
      case 'thinking_delta':
        turn.thinkingText += payloadString(event, 'text')
        break
      case 'tool_call_started': {
        const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
        let argsText = ''
        try {
          argsText = JSON.stringify(payloadRecord(event, 'args'))
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
        }
        break
      }
      case 'tool_call_completed': {
        const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
        const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
        let tool = toolMap.get(identity)
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
        }
        tool.status = payloadString(event, 'status') === 'failed' ? 'failed' : 'completed'
        tool.resultText = payloadString(event, 'result')
        break
      }
      case 'error':
        turn.errors.push({
          key: `error-${event.message_id ?? 'event'}-${event.stream_sequence}`,
          message: payloadString(event, 'message') || 'An error occurred.',
        })
        break
      case 'status':
        turn.statuses.push({
          key: `status-${event.message_id ?? 'event'}-${event.stream_sequence}`,
          text: payloadString(event, 'text') || payloadString(event, 'message') ||
            payloadString(event, 'status') || 'status update',
        })
        break
      default:
        break
    }
  }

  return turns
}
