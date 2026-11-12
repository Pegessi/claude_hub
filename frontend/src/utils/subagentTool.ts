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
 *   TraeX/Codex → tool name ``sendInput``  args: { prompt, receiverThreadIds }
 *                 (a follow-up directive posted to an already-running child
 *                 thread; rendered as an instruction inside that child group)
 *   MASO        → tool name ``Task``       args: { description, tasks: [{
 *                                                subagent_type, subagent_name,
 *                                                prompt, ... }] }
 *                 tool name ``Fork``       args: { prompt, agent_name?,
 *                                                fork_from?, workspace?, ... }
 *                 tool name ``SendMessage`` args: { agent_id, message, ... }
 *                 (main-thread directive posted to child ``agent_id``)
 *
 * Codex has no spawn tool in the captured history, so a plain Codex call never
 * matches and keeps rendering as an ordinary tool block; ``sendInput`` is
 * matched for both Codex and TraeX because it is the same collab protocol.
 *
 * A match requires BOTH the exact tool name AND that provider's argument
 * signature. A name alone is never enough: a same-named helper on a different
 * provider (or a future shape change) fails closed back to the generic tool
 * block instead of being mislabelled as a sub-agent.
 */

export type SubagentProvider = 'claude' | 'cursor' | 'traex' | 'maso'

export interface MasoSubagentTaskItem {
  agentName: string | null
  agentType: string | null
  prompt: string
}

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
  /** TraeX/Codex collab: the receiver thread ids the call addressed. */
  threadIds: string[]
  /** Claude background-delegation flag (``run_in_background``). */
  background: boolean
  /** True for ``sendInput`` — a directive to an existing child thread rather
   *  than a fresh spawn. Renders as an instruction row inside the child's
   *  nested group instead of the standalone spawn card. */
  directive: boolean
  /** MASO ``Task``/``Fork`` task items in declaration order, used to bind each
   *  spawned ``<subagent_name>-<hex>`` thread id to its own prompt and role. */
  tasks?: MasoSubagentTaskItem[]
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

// Current Cursor CLI encodes ``subagentType`` as a protobuf oneof object,
// e.g. ``{explore: {}}``; older builds sent a plain string.
function oneofTypeField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key]
  if (!isRecord(value)) return optionalTypeField(record, key)
  const keys = Object.keys(value)
  return keys.length === 1 && keys[0] !== 'unspecified' ? keys[0] : null
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
        directive: false,
      }
    }
    case 'Task': {
      // Cursor CLI vs MASO Task batch.
      if (!record) return null
      const prompt = stringField(record, 'prompt')
      if (nonEmptyString(prompt)) {
        const agentType = oneofTypeField(record, 'subagentType')
        const agentId = optionalTypeField(record, 'agentId')
        if (!agentType && !agentId) return null
        return {
          provider: 'cursor',
          agentType,
          description: stringField(record, 'description').trim(),
          prompt,
          threadIds: [],
          background: false,
          directive: false,
        }
      }
      if (!Array.isArray(record.tasks) || record.tasks.length === 0) return null
      const description = stringField(record, 'description').trim()
      if (!description) return null
      const tasks: MasoSubagentTaskItem[] = []
      for (const rawTask of record.tasks) {
        if (!isRecord(rawTask)) return null
        const taskPrompt = stringField(rawTask, 'prompt')
        const agentName = optionalTypeField(rawTask, 'subagent_name')
        const agentType = optionalTypeField(rawTask, 'subagent_type')
        if (!nonEmptyString(taskPrompt) || !agentName || !agentType) return null
        tasks.push({ agentName, agentType, prompt: taskPrompt })
      }
      const first = tasks[0]
      return {
        provider: 'maso',
        agentType: first.agentName ?? first.agentType,
        description,
        prompt: first.prompt,
        threadIds: [],
        background: true,
        directive: false,
        tasks,
      }
    }
    case 'Fork': {
      // MASO Fork: spawns an asynchronous forked sub-agent from current/source context.
      if (!record) return null
      const prompt = stringField(record, 'prompt')
      if (!nonEmptyString(prompt)) return null
      const agentName = optionalTypeField(record, 'agent_name')
      const forkFrom = optionalTypeField(record, 'fork_from')
      const workspace = optionalTypeField(record, 'workspace')
      if (!agentName && !forkFrom && !workspace && typeof record.nested_spawn !== 'boolean') {
        return null
      }
      return {
        provider: 'maso',
        agentType: agentName ?? 'Fork',
        description: agentName ? `Fork ${agentName}` : 'Fork sub-agent',
        prompt,
        threadIds: [],
        background: true,
        directive: false,
        tasks: [{ agentName, agentType: 'Fork', prompt }],
      }
    }
    case 'SendMessage': {
      // MASO SendMessage: when issued on the main thread, posts a message to an
      // existing sub-agent thread ``agent_id``.
      if (!record) return null
      const agentId = optionalTypeField(record, 'agent_id')
      const message = stringField(record, 'message')
      if (!agentId || !nonEmptyString(message)) return null
      return {
        provider: 'maso',
        agentType: null,
        description: '',
        prompt: message,
        threadIds: [agentId],
        background: false,
        directive: true,
      }
    }
    case 'spawnAgent':
    case 'sendInput': {
      // TraeX/Codex collab protocol. The child conversation is addressed by
      // receiver thread ids; there is no human description or sub-agent type
      // in the payload. ``sendInput`` is a follow-up directive to an existing
      // thread (``directive: true``), ``spawnAgent`` launches the local worker.
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
        directive: name === 'sendInput',
      }
    }
    default:
      return null
  }
}

const MASO_TASK_IDS_RE = /Spawned\s+\d+\/\d+\s+sub-agent\(s\)\s+with\s+IDs:\s*([^\n]+)/
const MASO_FORK_ID_RE = /into sub-agent "([^"]+)"/
const MASO_HEX_SUFFIX_RE = /^[0-9a-f]{4,12}$/i

/** Whether a MASO ``agentName`` matches a runtime ``threadId`` (``<name>`` or ``<name>-<hex>``). */
export function masoAgentNameMatchesThreadId(
  agentName: string | null | undefined,
  threadId: string,
): boolean {
  if (!agentName) return false
  if (threadId === agentName) return true
  if (!threadId.startsWith(`${agentName}-`)) return false
  return MASO_HEX_SUFFIX_RE.test(threadId.slice(agentName.length + 1))
}

/** Extract spawned child thread ids from a MASO ``Task`` or ``Fork`` result string. */
export function parseMasoSpawnedThreadIds(result: unknown): string[] {
  if (typeof result !== 'string' || !result) return []
  const taskMatch = MASO_TASK_IDS_RE.exec(result)
  if (taskMatch?.[1]) {
    return taskMatch[1]
      .split(',')
      .map(id => id.trim())
      .filter(id => id.length > 0 && !id.startsWith('('))
  }
  const forkMatch = MASO_FORK_ID_RE.exec(result)
  if (forkMatch?.[1]?.trim()) {
    return [forkMatch[1].trim()]
  }
  return []
}

/** Whether a MASO ``Task``/``Fork`` view declared a task matching ``threadId``. */
export function masoTaskMatchesThreadId(view: SubagentView | undefined, threadId: string): boolean {
  if (!view || view.provider !== 'maso' || view.directive) return false
  if (view.threadIds.length > 0) {
    return view.threadIds.includes(threadId)
  }
  if (!view.tasks) return false
  return view.tasks.some(task => masoAgentNameMatchesThreadId(task.agentName, threadId))
}

/** Bind a multi-task MASO ``SubagentView`` to one specific spawned ``threadId``. */
export function subagentViewForThread(
  view: SubagentView,
  threadId: string,
  index?: number,
): SubagentView {
  if (!view.tasks || view.tasks.length === 0) {
    return { ...view, threadIds: [threadId] }
  }
  const indexed =
    typeof index === 'number' && index >= 0 && index < view.tasks.length
      ? view.tasks[index]
      : undefined
  const matched =
    (indexed && (!indexed.agentName || masoAgentNameMatchesThreadId(indexed.agentName, threadId))
      ? indexed
      : undefined) ??
    view.tasks.find(task => masoAgentNameMatchesThreadId(task.agentName, threadId)) ??
    indexed ??
    view.tasks[0]
  return {
    ...view,
    agentType: matched.agentName ?? matched.agentType ?? view.agentType,
    prompt: matched.prompt || view.prompt,
    threadIds: [threadId],
  }
}

/**
 * Readable report for a completed sub-agent whose provider returns a record,
 * or ``null`` to keep the plain string result.
 *
 * Cursor's ``Task`` result is ``{success: {conversationSteps: [{assistantMessage:
 * {text}}, ...], agentId, durationMs, ...}}``; the assistant messages are the
 * child's report. Failures carry ``error``/``failure``/``rejected`` instead.
 */
export function subagentResultText(view: SubagentView | undefined, result: unknown): string | null {
  if (view?.provider !== 'cursor' || !isRecord(result)) return null
  const success = isRecord(result.success) ? result.success : null
  const steps = success && Array.isArray(success.conversationSteps) ? success.conversationSteps : []
  const texts = steps.flatMap((step) => {
    const message = isRecord(step) && isRecord(step.assistantMessage) ? step.assistantMessage : null
    return message && nonEmptyString(message.text) ? [message.text.trim()] : []
  })
  if (texts.length > 0) return texts.join('\n\n')
  for (const key of ['error', 'failure', 'rejected']) {
    const detail = result[key]
    if (nonEmptyString(detail)) return detail
    if (isRecord(detail) && nonEmptyString(detail.message)) return detail.message
  }
  return ''
}

/** Whether a tool call spawns a renderable sub-agent. */
export function isSubagentTool(name: unknown, args?: unknown): boolean {
  return parseSubagent(name, args) !== null
}

const STATUS_LABELS: Record<SubagentStatus, string> = {
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

/** English status badge label for the sub-agent card; unknown → raw status. */
export function subagentStatusLabel(status: string): string {
  return STATUS_LABELS[status as SubagentStatus] ?? status
}

const PROVIDER_LABELS: Record<SubagentProvider, string> = {
  claude: 'Claude sub-agent',
  cursor: 'Cursor sub-agent',
  traex: 'TraeX sub-agent',
  maso: 'MASO sub-agent',
}

/** Provider-prefixed identity chip, e.g. ``Claude sub-agent``. */
export function subagentProviderLabel(provider: SubagentProvider): string {
  return PROVIDER_LABELS[provider]
}
