import type { AgentStreamEvent } from '@/types'
import type { SubagentView } from '@/utils/subagentTool'
import { groupEventsIntoTurns } from '../timeline/reducer'
import { createTurn } from '../timeline/eventValues'
import type {
  TimelineSubagentLifecycle,
  TimelineSubthread,
  TimelineSubthreadPart,
  TimelineTool,
  TimelineTurn,
} from '../timeline/types'
import type { NativeSubthreadLookup } from './nativeSubagentRows'
import { subagentDisplayName, type SubagentJumpChip } from './subagentJump'

const HUB_PREAMBLE_RE = /^<claude_hub_subagent>[\s\S]*?<\/claude_hub_subagent>\s*/i

/** Strip the internal Hub sub-agent preamble when a raw prompt is surfaced. */
export function stripHubSubagentPreamble(text: string): string {
  return text.replace(HUB_PREAMBLE_RE, '').trim()
}

function providerForAgentType(agentType: string): SubagentView['provider'] {
  switch (agentType.toLowerCase()) {
    case 'cursor':
      return 'cursor'
    case 'maso':
      return 'maso'
    case 'codex':
    case 'traex':
      return 'traex'
    default:
      return 'claude'
  }
}

function launchToolStatus(state: SubagentJumpChip['state']): TimelineTool['status'] {
  switch (state) {
    case 'running':
      return 'running'
    case 'failed':
    case 'timeout':
      return 'failed'
    case 'cancelled':
      return 'cancelled'
    default:
      return 'completed'
  }
}

function lifecycleForHubChip(
  chip: SubagentJumpChip,
  turns: readonly TimelineTurn[],
): TimelineSubagentLifecycle {
  if (chip.state === 'running') return 'active'
  if (chip.state === 'failed' || chip.state === 'timeout') return 'failed'
  if (chip.state === 'cancelled') return 'cancelled'
  const lastTurn = turns[turns.length - 1]
  if (lastTurn && !lastTurn.completed) return 'active'
  if (lastTurn?.completionStatus === 'failed') return 'failed'
  if (lastTurn?.completionStatus === 'cancelled') return 'cancelled'
  return 'completed'
}

function turnInstructionStatus(turn: TimelineTurn): TimelineTool['status'] {
  if (!turn.completed) return 'running'
  if (turn.completionStatus === 'failed') return 'failed'
  if (turn.completionStatus === 'cancelled') return 'cancelled'
  return 'completed'
}

/**
 * Project a Hub sub-agent (`claude-hub agent run`) and its persisted stream
 * events into the same `TimelineSubthread` shape that `ChatSubthreadBody`
 * renders for provider-native sub-agents.
 *
 * When the child tab's event stream has not produced turns yet (or in minimal
 * mocks), falls back to the durable summary carried on `SubagentJumpChip` so
 * the detail window always renders a complete card.
 */
export function buildHubSubagentLookup(
  chip: SubagentJumpChip,
  events: AgentStreamEvent[],
): NativeSubthreadLookup {
  const turns = groupEventsIntoTurns(events)
  const firstPrompt = turns.length > 0 ? stripHubSubagentPreamble(turns[0].userText) : ''
  const originalPrompt = firstPrompt || subagentDisplayName(chip.name)

  const launchTool: TimelineTool = {
    key: `hub-launch-${chip.subagentId}`,
    callId: chip.subagentId,
    name: 'claude-hub agent run',
    status: launchToolStatus(chip.state),
    argsText: originalPrompt,
    resultText: '',
    subagent: {
      provider: providerForAgentType(chip.agentType),
      agentType: chip.agentType,
      description: subagentDisplayName(chip.name),
      prompt: originalPrompt,
      threadIds: [chip.subagentId],
      background: false,
      directive: false,
    },
  }

  const parts: TimelineSubthreadPart[] = []
  let revision = 1

  for (let index = 0; index < turns.length; index += 1) {
    const turn = turns[index]
    revision += turn.renderRevision
    if (index > 0) {
      const followUp = stripHubSubagentPreamble(turn.userText)
      if (followUp) {
        parts.push({
          kind: 'instruction',
          key: `${turn.key}-instruction`,
          tool: {
            key: `${turn.key}-instruction-tool`,
            callId: turn.turnId,
            name: 'sendInput',
            status: turnInstructionStatus(turn),
            argsText: followUp,
            resultText: '',
          },
        })
      }
    }
    for (const part of turn.parts) {
      if (part.kind === 'thinking') {
        parts.push({ kind: 'thinking', key: part.key, text: part.text })
      } else if (part.kind === 'tool_group') {
        parts.push({ kind: 'tool_group', key: part.key, tools: [...part.tools] })
      } else if (part.kind === 'tool' || part.kind === 'subagent') {
        const previous = parts[parts.length - 1]
        if (previous && previous.kind === 'tool_group') {
          previous.tools.push(part.tool)
        } else {
          parts.push({
            kind: 'tool_group',
            key: `hub-tools-${part.key}`,
            tools: [part.tool],
          })
        }
      } else if (part.kind === 'agent_image') {
        parts.push({ kind: 'agent_image', key: part.key, tool: part.tool, path: part.path })
      } else if (part.kind === 'text') {
        parts.push({ kind: 'text', key: part.key, text: part.text, at: part.at })
      } else if (part.kind === 'status') {
        parts.push({
          kind: 'status',
          key: part.key,
          text: part.text,
          messageId: part.messageId,
          providerStatus: part.providerStatus,
        })
      } else if (part.kind === 'error') {
        parts.push({
          kind: 'status',
          key: part.key,
          text: part.detail ? `${part.message}: ${part.detail}` : part.message,
          isError: true,
        })
      }
    }
  }

  if (parts.length === 0) {
    if (chip.summary && chip.summary !== 'Working…') {
      parts.push({
        kind: 'text',
        key: `hub-summary-${chip.subagentId}`,
        text: chip.summary,
        at: chip.createdAt,
      })
    } else if (chip.state === 'running') {
      parts.push({
        kind: 'status',
        key: `hub-working-${chip.subagentId}`,
        text: 'Working…',
      })
    }
  }

  const hostTurn =
    turns[turns.length - 1] ??
    Object.assign(createTurn(`hub-turn-${chip.subagentId}`, chip.subagentId), {
      userText: originalPrompt,
      completed: chip.state !== 'running',
      completionStatus: chip.state === 'running' ? null : chip.state,
      renderRevision: revision,
    })

  const thread: TimelineSubthread = {
    key: `hub-thread-${chip.subagentId}`,
    threadId: chip.subagentId,
    parts,
    launchTool,
    lifecycleStatus: lifecycleForHubChip(chip, turns),
    renderRevision: revision,
    provider: providerForAgentType(chip.agentType),
  }

  return {
    ordinal: Math.max(1, turns.length),
    turn: hostTurn,
    thread,
  }
}
