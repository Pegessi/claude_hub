/**
 * Identify provider-native "launch a sub-agent" tool calls and project them
 * into the shape the structured Chat renders as a sub-agent card.
 *
 * The rules are built from REAL persisted stream events (see
 * docs/working-logs/2026-09-23-chat-subagent-card.md), not a guessed tool
 * catalog. Observed signatures across the local Hub history:
 *
 *   Claude Code → tool name ``Agent``      args: { description, prompt,
 *                                                subagent_type[, run_in_background] }
 *   Cursor CLI  → tool name ``Task``       args: { description, prompt,
 *                                                subagentType, agentId, ... }
 *   TraeX       → tool name ``spawnAgent`` args: { prompt, receiverThreadIds }
 *
 * Codex has no sub-agent tool in the captured history, so it never matches and
 * its calls keep rendering as ordinary tool blocks.
 *
 * A match requires BOTH the exact tool name AND that provider's argument
 * signature. A name alone is never enough: a same-named helper on a different
 * provider (or a future shape change) fails closed back to the generic tool
 * block instead of being mislabelled as a sub-agent.
 */

export type SubagentProvider = 'claude' | 'cursor' | 'traex'

export interface SubagentView {
  /** Which confirmed provider signature matched. Drives the identity label. */
  provider: SubagentProvider
  /** Sub-agent type, e.g. ``general-purpose`` / ``Explore``. ``null`` when the
   *  provider addresses the child by an opaque target instead (TraeX threads). */
  agentType: string | null
  /** Human description of what the child was asked to do (``description``). */
  description: string
  /** The delegated prompt. Always a string (``''`` when unavailable). */
  prompt: string
  /** TraeX-only: the receiver thread ids the sub-agent was spawned against. */
  threadIds: string[]
  /** Claude background-delegation flag (``run_in_background``). */
  background: boolean
}

/** Lifecycle status shared with {@link TimelineTool}; kept structural here so
 *  this util does not import the timeline module. */
export type SubagentStatus = 'running' | 'completed' | 'failed' | 'cancelled'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key]
  return typeof value === 'string' ? value : ''
}

function optionalTypeField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key]
  return nonEmptyString(value) ? value.trim() : null
}

function threadIdList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.length > 0)
}

/**
 * Parse a sub-agent tool call into its renderable view, or ``null`` when the
 * name/args do not match a confirmed provider signature.
 */
export function parseSubagent(name: unknown, args: unknown): SubagentView | null {
  if (typeof name !== 'string') return null
  const record = isRecord(args) ? args : null

  switch (name) {
    case 'Agent': {
      // Claude Code. Every captured call carried prompt + description +
      // subagent_type; require the prompt and at least one identifying field.
      if (!record) return null
      const prompt = stringField(record, 'prompt')
      if (!nonEmptyString(prompt)) return null
      const agentType = optionalTypeField(record, 'subagent_type')
      const description = stringField(record, 'description').trim()
      if (!agentType && !description) return null
      return {
        provider: 'claude',
        agentType,
        description,
        prompt,
        threadIds: [],
        background: record.run_in_background === true,
      }
    }
    case 'Task': {
      // Cursor CLI. Distinguished from Claude's ``Agent`` by the camelCase
      // ``subagentType`` / ``agentId`` signature (Claude uses snake_case on a
      // differently-named tool). Require the prompt and one typed target.
      if (!record) return null
      const prompt = stringField(record, 'prompt')
      if (!nonEmptyString(prompt)) return null
      const agentType = optionalTypeField(record, 'subagentType')
      const agentId = optionalTypeField(record, 'agentId')
      if (!agentType && !agentId) return null
      return {
        provider: 'cursor',
        agentType,
        description: stringField(record, 'description').trim(),
        prompt,
        threadIds: [],
        background: false,
      }
    }
    case 'spawnAgent': {
      // TraeX. The child conversation is addressed by receiver thread ids;
      // there is no human description or sub-agent type in the payload.
      if (!record) return null
      const prompt = stringField(record, 'prompt')
      if (!nonEmptyString(prompt)) return null
      const threadIds = threadIdList(record.receiverThreadIds)
      if (threadIds.length === 0) return null
      return {
        provider: 'traex',
        agentType: null,
        description: '',
        prompt,
        threadIds,
        background: false,
      }
    }
    default:
      return null
  }
}

/** Whether a tool call spawns a renderable sub-agent. */
export function isSubagentTool(name: unknown, args?: unknown): boolean {
  return parseSubagent(name, args) !== null
}

const STATUS_LABELS: Record<SubagentStatus, string> = {
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

/** Chinese status badge label for the sub-agent card; unknown → raw status. */
export function subagentStatusLabel(status: string): string {
  return STATUS_LABELS[status as SubagentStatus] ?? status
}

const PROVIDER_LABELS: Record<SubagentProvider, string> = {
  claude: 'Claude 子代理',
  cursor: 'Cursor 子代理',
  traex: 'TraeX 子代理',
}

/** Provider-prefixed identity chip, e.g. ``Claude 子代理``. */
export function subagentProviderLabel(provider: SubagentProvider): string {
  return PROVIDER_LABELS[provider]
}
