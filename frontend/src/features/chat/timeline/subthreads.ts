import type { AgentStreamEvent } from '@/types'
import { agentImagePathFromTool } from '@/utils/agentImage'
import type { TimelineTurn, TimelineSubthread, TimelineSubagentLifecycle, TimelineTool } from './types'
import type { ReducerState, SubthreadState } from './state'
import { payloadString, payloadRecord, statusText } from './eventValues'

/** Provider thread id the backend stamped on a nested sub-agent event, or
 *  ``null`` for a main-thread event. The value comes from
 *  ``payload.subagent_thread``; non-string / empty values fail closed to the
 *  main stream so an anomalous record is never lost into a phantom group. */
export function subThreadIdOf(event: AgentStreamEvent): string | null {
  const value = event.payload.subagent_thread
  return typeof value === 'string' && value.trim().length > 0 ? value : null
}

/** Get (creating + mounting on first reference) the nested group for one
 *  sub-agent thread of a turn. The mount position is wherever the child first
 *  produces content or receives a directive, so the block stays in arrival
 *  order relative to the main agent's own rows. */
export function ensureSubthread(
  state: ReducerState,
  turn: TimelineTurn,
  threadId: string,
  toolMapKey: string,
): SubthreadState {
  let region = state.subagentRegionByTurn.get(toolMapKey)
  if (!region) {
    region = {
      kind: 'subagents',
      key: `subagents-${toolMapKey}`,
      threads: [],
    }
    state.subagentRegionByTurn.set(toolMapKey, region)
    turn.parts.push(region)
  }
  let byThread = state.threadsByTurn.get(toolMapKey)
  if (!byThread) {
    byThread = new Map()
    state.threadsByTurn.set(toolMapKey, byThread)
  }
  let child = byThread.get(threadId)
  if (!child) {
    const thread: TimelineSubthread = {
      key: `subthread-${threadId}`,
      threadId,
      parts: [],
      launchTool: null,
      lifecycleStatus: 'unknown',
      renderRevision: 0,
    }
    child = { thread, tools: [], toolMap: new Map() }
    byThread.set(threadId, child)
    region.threads.push(thread)
  }
  return child
}

function subagentLifecycleOf(event: AgentStreamEvent): TimelineSubagentLifecycle | null {
  if (payloadString(event, 'provider_status') !== 'subagent_lifecycle') return null
  const status = payloadString(event, 'subagent_status')
  return status === 'active' || status === 'completed' || status === 'failed' ||
    status === 'cancelled' ? status : null
}

function childActivityRestartsLifecycle(
  event: AgentStreamEvent,
  child: SubthreadState,
): boolean {
  if (event.type === 'thinking_delta' || event.type === 'text_delta') {
    return payloadString(event, 'text').length > 0
  }
  if (event.type === 'tool_call_started') {
    const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
    const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
    return !child.toolMap.has(identity)
  }
  return event.type === 'status' &&
    payloadString(event, 'provider_status') === 'reasoning' &&
    statusText(event) === 'Thinking…'
}

/** Append/extend a text or thinking row inside a child thread, merging into the
 *  preceding same-kind child row the way the main stream does. */
function appendSubthreadText(
  child: SubthreadState,
  kind: 'thinking' | 'text',
  text: string,
  sequence: number,
  at: string,
): void {
  if (!text) return
  const parts = child.thread.parts
  const last = parts[parts.length - 1]
  if (last !== undefined && last.kind === kind) {
    last.text += text
    return
  }
  parts.push(
    kind === 'thinking'
      ? { kind: 'thinking', key: `sub-thinking-${sequence}`, text }
      : { kind: 'text', key: `sub-text-${sequence}`, text, at },
  )
}

/** Route one sub-agent-thread event into its nested group. Returns whether the
 *  turn visibly mutated. Only the types the adapter tags with
 *  ``subagent_thread`` reach here; anything else is ignored (caller falls
 *  through to main handling). */
function subthreadEventCanMount(
  event: AgentStreamEvent,
  lifecycle: TimelineSubagentLifecycle | null,
): boolean {
  if (event.type === 'thinking_delta' || event.type === 'text_delta') {
    return payloadString(event, 'text').length > 0
  }
  if (event.type === 'status') return lifecycle !== null || statusText(event).length > 0
  return event.type === 'tool_call_started' || event.type === 'tool_call_completed'
}

export function applySubthreadEvent(
  state: ReducerState,
  turn: TimelineTurn,
  threadId: string,
  event: AgentStreamEvent,
  toolMapKey: string,
): boolean {
  const lifecycle = subagentLifecycleOf(event)
  if (!subthreadEventCanMount(event, lifecycle)) return false
  const child = ensureSubthread(state, turn, threadId, toolMapKey)
  let lifecycleChanged = false
  if (lifecycle !== null && child.thread.lifecycleStatus !== lifecycle) {
    child.thread.lifecycleStatus = lifecycle
    lifecycleChanged = true
  } else if (lifecycle === null && childActivityRestartsLifecycle(event, child) &&
             child.thread.lifecycleStatus !== 'active') {
    child.thread.lifecycleStatus = 'active'
    lifecycleChanged = true
  }
  switch (event.type) {
    case 'thinking_delta': {
      const text = payloadString(event, 'text')
      if (!text) return lifecycleChanged
      appendSubthreadText(child, 'thinking', text, event.stream_sequence, event.created_at)
      return true
    }
    case 'text_delta': {
      const text = payloadString(event, 'text')
      if (!text) return lifecycleChanged
      appendSubthreadText(child, 'text', text, event.stream_sequence, event.created_at)
      return true
    }
    case 'tool_call_started': {
      const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
      const toolName = payloadString(event, 'name') || 'unknown'
      const argsRecord = payloadRecord(event, 'args')
      const imagePath = agentImagePathFromTool(toolName, argsRecord)
      const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
      if (child.toolMap.has(identity)) return lifecycleChanged
      let argsText = ''
      try {
        argsText = JSON.stringify(argsRecord, null, 2)
      } catch {
        argsText = String(event.payload.args ?? '')
      }
      const tool: TimelineTool = {
        key: `tool-${identity}`,
        callId,
        name: toolName,
        status: 'running',
        argsText,
        resultText: '',
        ...(imagePath !== null ? { imagePath } : {}),
      }
      child.toolMap.set(identity, tool)
      child.tools.push(tool)
      const parts = child.thread.parts
      if (imagePath !== null) {
        parts.push({ kind: 'agent_image', key: `sub-agent-image-${identity}`, tool, path: imagePath })
      } else {
        const last = parts[parts.length - 1]
        if (last && last.kind === 'tool_group') {
          last.tools.push(tool)
        } else {
          parts.push({ kind: 'tool_group', key: `sub-tool-group-${event.stream_sequence}`, tools: [tool] })
        }
      }
      return true
    }
    case 'tool_call_completed': {
      const callId = (event.payload.tool_call_id as string | null) ?? event.call_id ?? null
      const identity = callId ?? event.message_id ?? `sequence-${event.stream_sequence}`
      let tool = child.toolMap.get(identity)
      if (!tool) {
        // Completed without its start (truncated/corrupt transcript): still
        // surface it rather than dropping the child's result.
        tool = {
          key: `tool-${identity}`,
          callId,
          name: payloadString(event, 'name') || 'tool',
          status: 'running',
          argsText: '',
          resultText: '',
        }
        child.toolMap.set(identity, tool)
        child.tools.push(tool)
        const parts = child.thread.parts
        const last = parts[parts.length - 1]
        if (last && last.kind === 'tool_group') {
          last.tools.push(tool)
        } else {
          parts.push({ kind: 'tool_group', key: `sub-tool-group-${event.stream_sequence}`, tools: [tool] })
        }
      }
      const newStatus: TimelineTool['status'] =
        payloadString(event, 'status') === 'failed' ? 'failed' : 'completed'
      const newResult = payloadString(event, 'result')
      if (tool.status !== newStatus || tool.resultText !== newResult) {
        tool.status = newStatus
        tool.resultText = newResult
        return true
      }
      return lifecycleChanged
    }
    case 'status': {
      const text = statusText(event)
      if (!text) return lifecycleChanged
      const providerStatus = payloadString(event, 'provider_status') || undefined
      const isError = providerStatus === 'error'
      const parts = child.thread.parts
      // Stable in-place snapshot: replace an existing child status with the
      // same messageId rather than appending a second row.
      if (event.payload.snapshot === true && event.message_id) {
        const existing = parts.find(
          part => part.kind === 'status' && part.messageId === event.message_id,
        )
        if (existing?.kind === 'status') {
          if (existing.text === text && Boolean(existing.isError) === isError &&
              existing.providerStatus === providerStatus) return lifecycleChanged
          existing.text = text
          existing.isError = isError
          existing.providerStatus = providerStatus
          return true
        }
      }
      parts.push({
        kind: 'status',
        key: `sub-status-${event.message_id ?? 'event'}-${event.stream_sequence}`,
        text,
        messageId: event.message_id,
        ...(providerStatus ? { providerStatus } : {}),
        ...(isError ? { isError: true } : {}),
      })
      return true
    }
    default:
      return lifecycleChanged
  }
}

export function detachToolFromMainParts(turn: TimelineTurn, tool: TimelineTool): void {
  for (let index = turn.parts.length - 1; index >= 0; index -= 1) {
    const part = turn.parts[index]
    if (part.kind !== 'tool_group') continue
    const toolIndex = part.tools.indexOf(tool)
    if (toolIndex < 0) continue
    part.tools.splice(toolIndex, 1)
    if (part.tools.length === 0) turn.parts.splice(index, 1)
    return
  }
}
