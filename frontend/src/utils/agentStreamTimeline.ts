import type { AgentStreamEvent } from '@/types'

/**
 * A single tool call rendered in the timeline.
 */
export interface TimelineTool {
  key: string
  callId: string | null
  name: string
  status: 'running' | 'completed' | 'failed'
  argsText: string
  resultText: string
}

/**
 * A single turn (user message + assistant response) in the timeline.
 */
export interface TimelineTurn {
  key: string
  userText: string
  assistantText: string
  thinkingText: string
  tools: TimelineTool[]
  errors: { key: string; message: string }[]
  statuses: { key: string; text: string }[]
}

function payloadString(evt: AgentStreamEvent, key: string): string {
  const v = evt.payload[key]
  return typeof v === 'string' ? v : ''
}

function payloadRecord(evt: AgentStreamEvent, key: string): Record<string, unknown> {
  const v = evt.payload[key]
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {}
}

/**
 * Group a flat, ordered stream of agent events into turns.
 *
 * A turn begins at ``TURN_STARTED`` (which carries the user's message as
 * ``summary``) and ends at the next ``TURN_STARTED`` or end-of-stream.
 * ``TURN_COMPLETED`` marks the assistant's response as finished and flips any
 * still-running tools to ``completed``.
 *
 * Tool calls are matched by ``call_id`` (preferring the event-level
 * ``call_id``, falling back to ``payload.tool_call_id``). Orphan
 * ``tool_call_completed`` events (no matching start) are rendered as
 * standalone tool entries.
 *
 * ``approval_required`` / ``approval_resolved`` are intentionally ignored
 * here; they will be wired when the approval UI lands.
 */
export function groupEventsIntoTurns(events: AgentStreamEvent[]): TimelineTurn[] {
  const result: TimelineTurn[] = []
  let current: TimelineTurn | null = null
  const toolMap = new Map<string, TimelineTool>()

  const flush = () => {
    if (current) result.push(current)
    current = null
    toolMap.clear()
  }

  for (const evt of events) {
    switch (evt.type) {
      case 'turn_started': {
        flush()
        current = {
          key: `turn-${evt.stream_sequence}`,
          userText: payloadString(evt, 'summary'),
          assistantText: '',
          thinkingText: '',
          tools: [],
          errors: [],
          statuses: [],
        }
        break
      }
      case 'turn_completed': {
        if (current) {
          for (const t of current.tools) {
            if (t.status === 'running') t.status = 'completed'
          }
        }
        break
      }
      case 'text_delta': {
        if (!current) {
          current = {
            key: `turn-${evt.stream_sequence}`,
            userText: '',
            assistantText: '',
            thinkingText: '',
            tools: [],
            errors: [],
            statuses: [],
          }
        }
        current.assistantText += payloadString(evt, 'text')
        break
      }
      case 'thinking_delta': {
        if (!current) {
          current = {
            key: `turn-${evt.stream_sequence}`,
            userText: '',
            assistantText: '',
            thinkingText: '',
            tools: [],
            errors: [],
            statuses: [],
          }
        }
        current.thinkingText += payloadString(evt, 'text')
        break
      }
      case 'tool_call_started': {
        if (!current) {
          current = {
            key: `turn-${evt.stream_sequence}`,
            userText: '',
            assistantText: '',
            thinkingText: '',
            tools: [],
            errors: [],
            statuses: [],
          }
        }
        const callId = (evt.payload.tool_call_id as string | null) ?? evt.call_id ?? null
        const args = payloadRecord(evt, 'args')
        let argsText = ''
        try {
          argsText = JSON.stringify(args)
        } catch {
          argsText = String(args)
        }
        const tool: TimelineTool = {
          key: `tool-${evt.stream_sequence}`,
          callId,
          name: payloadString(evt, 'name') || 'unknown',
          status: 'running',
          argsText,
          resultText: '',
        }
        current.tools.push(tool)
        if (callId) toolMap.set(callId, tool)
        break
      }
      case 'tool_call_completed': {
        const callId = (evt.payload.tool_call_id as string | null) ?? evt.call_id ?? null
        const status = (evt.payload.status as string) || 'completed'
        const result = payloadString(evt, 'result')
        const tool = callId ? toolMap.get(callId) : null
        if (tool) {
          tool.status = status === 'failed' ? 'failed' : 'completed'
          tool.resultText = result
        } else if (current) {
          current.tools.push({
            key: `tool-${evt.stream_sequence}`,
            callId,
            name: 'tool',
            status: status === 'failed' ? 'failed' : 'completed',
            argsText: '',
            resultText: result,
          })
        }
        break
      }
      case 'error': {
        if (!current) {
          current = {
            key: `turn-${evt.stream_sequence}`,
            userText: '',
            assistantText: '',
            thinkingText: '',
            tools: [],
            errors: [],
            statuses: [],
          }
        }
        current.errors.push({
          key: `err-${evt.stream_sequence}`,
          message: payloadString(evt, 'message') || 'An error occurred.',
        })
        break
      }
      case 'status': {
        if (!current) {
          current = {
            key: `turn-${evt.stream_sequence}`,
            userText: '',
            assistantText: '',
            thinkingText: '',
            tools: [],
            errors: [],
            statuses: [],
          }
        }
        const text =
          payloadString(evt, 'text') ||
          payloadString(evt, 'message') ||
          payloadString(evt, 'status') ||
          'status update'
        current.statuses.push({ key: `st-${evt.stream_sequence}`, text })
        break
      }
      default:
        break
    }
  }
  flush()
  return result
}
