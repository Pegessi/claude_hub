import { subagentRegionErrorCount } from '../timeline/turnProcess'
import type { TimelinePart, TimelineTool } from '../timeline/types'
import { subagentProviderLabel } from '@/utils/subagentTool'

// --- Sub-agent card projection ---------------------------------------------
// The timeline already classified the call as a sub-agent; these keep the
// template null-safe (``tool.subagent`` is optional on TimelineTool) and hold
// all fallback copy in one place.

export function subagentChip(tool: TimelineTool): string {
  return tool.subagent ? subagentProviderLabel(tool.subagent.provider) : 'Sub-agent'
}

/** Sub-agent identity: its type when present, else a provider-local target
 *  (TraeX addresses children by thread id), else a neutral label. */
export function subagentName(tool: TimelineTool): string {
  const view = tool.subagent
  if (!view) return 'Sub-agent'
  if (view.agentType) return view.agentType
  if (view.provider === 'traex') {
    const thread = view.threadIds[0]
    return thread ? `Thread ${thread.slice(0, 8)}` : 'TraeX sub-agent'
  }
  return 'Sub-agent'
}

/** One-line description of what the child is doing. Providers without a
 *  ``description`` field (TraeX) fall back to the prompt's first line. */
export function subagentHeadline(tool: TimelineTool): string {
  const view = tool.subagent
  if (!view) return ''
  const description = view.description.trim()
  if (description) return description
  const firstLine = view.prompt
    .split('\n')
    .map(line => line.trim())
    .find(line => line.length > 0)
  if (firstLine) return firstLine.length > 140 ? `${firstLine.slice(0, 140)}…` : firstLine
  return 'Sub-agent task dispatched'
}

export function subagentPrompt(tool: TimelineTool): string {
  return tool.subagent?.prompt ?? ''
}

export function subagentCallStatusLabel(status: TimelineTool['status']): string {
  return {
    running: 'Running',
    completed: 'Returned',
    failed: 'Failed',
    cancelled: 'Cancelled',
  }[status]
}

export function subagentDispatchLabel(status: TimelineTool['status']): string {
  return {
    running: 'Dispatching',
    completed: 'Dispatched',
    failed: 'Dispatch failed',
    cancelled: 'Dispatch cancelled',
  }[status]
}

// --- Native sub-agent region projection -----------------------------------
// Each row is keyed by its complete provider thread id. Lifecycle status comes
// only from child-scoped provider evidence, never from tools or the parent.

type SubagentRegionPart = Extract<TimelinePart, { kind: 'subagents' }>
type SubthreadPart = SubagentRegionPart['threads'][number]

export function subagentRegionHeadline(part: SubagentRegionPart): string {
  const count = part.threads.length
  return `${count} sub-agent${count === 1 ? '' : 's'}`
}

export function subagentRegionStatusLabel(part: SubagentRegionPart): string {
  const counts = new Map<string, number>()
  for (const thread of part.threads) {
    counts.set(thread.lifecycleStatus, (counts.get(thread.lifecycleStatus) ?? 0) + 1)
  }
  const labels: string[] = []
  const errors = subagentRegionErrorCount(part)
  if (errors > 0) labels.push(`${errors} ${errors === 1 ? 'error' : 'errors'}`)
  const append = (status: string, label: string) => {
    const count = counts.get(status) ?? 0
    if (count > 0) labels.push(`${count} ${label}`)
  }
  append('failed', 'failed')
  append('cancelled', 'cancelled')
  append('active', 'active')
  append('unknown', 'unknown')
  append('completed', 'completed')
  return labels.join(' · ')
}

export function subagentRegionStatusClass(part: SubagentRegionPart): string {
  if (subagentRegionErrorCount(part) > 0) return 'failed'
  if (part.threads.some(thread => thread.lifecycleStatus === 'failed')) return 'failed'
  if (part.threads.some(thread => thread.lifecycleStatus === 'active')) return 'running'
  if (part.threads.some(thread => thread.lifecycleStatus === 'cancelled')) return 'cancelled'
  if (part.threads.some(thread => thread.lifecycleStatus === 'unknown')) return 'unknown'
  return 'completed'
}

export function subthreadStatusClass(part: SubthreadPart): string {
  return part.lifecycleStatus === 'active' ? 'running' : part.lifecycleStatus
}

export function subthreadStatusLabel(part: SubthreadPart): string {
  return {
    unknown: 'Unknown',
    active: 'Active',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
  }[part.lifecycleStatus]
}

export function subthreadTerminal(part: SubthreadPart): boolean {
  return part.lifecycleStatus === 'completed' || part.lifecycleStatus === 'failed' ||
    part.lifecycleStatus === 'cancelled'
}

function firstNonEmptyLine(text: string): string | null {
  const first = text
    .split(String.fromCharCode(10))
    .map(line => line.trim())
    .find(Boolean)
  if (!first) return null
  return first.length > 140 ? `${first.slice(0, 140)}…` : first
}

function childSendMessageText(tool: TimelineTool): string {
  if (tool.name !== 'SendMessage' || !tool.argsText) return ''
  try {
    const parsed = JSON.parse(tool.argsText) as unknown
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      typeof (parsed as Record<string, unknown>).message === 'string'
    ) {
      return (parsed as Record<string, string>).message
    }
  } catch {
    // Ignore malformed tool JSON.
  }
  return ''
}

/** Latest child-authored report line (text or child SendMessage), or ``null``. */
export function subthreadReportHeadline(part: SubthreadPart): string | null {
  for (let index = part.parts.length - 1; index >= 0; index -= 1) {
    const child = part.parts[index]
    if (child.kind === 'text') {
      const line = firstNonEmptyLine(child.text)
      if (line) return line
    } else if (child.kind === 'tool_group') {
      for (let tIndex = child.tools.length - 1; tIndex >= 0; tIndex -= 1) {
        const msg = childSendMessageText(child.tools[tIndex])
        const line = firstNonEmptyLine(msg)
        if (line) return line
      }
    }
  }
  return null
}

/** Collapsed-row headline: the child's latest report or instruction, then the
 *  original delegated prompt, without using either as lifecycle evidence. */
export function subthreadHeadline(part: SubthreadPart): string {
  if (subthreadTerminal(part)) {
    const report = subthreadReportHeadline(part)
    if (report) return report
  }
  for (let index = part.parts.length - 1; index >= 0; index -= 1) {
    const child = part.parts[index]
    if (child.kind === 'text') {
      const line = firstNonEmptyLine(child.text)
      if (line) return line
    } else if (child.kind === 'instruction') {
      const line = firstNonEmptyLine(child.tool.subagent?.prompt ?? '')
      if (line) return line
    } else if (child.kind === 'tool_group') {
      for (let tIndex = child.tools.length - 1; tIndex >= 0; tIndex -= 1) {
        const msg = childSendMessageText(child.tools[tIndex])
        const line = firstNonEmptyLine(msg)
        if (line) return line
      }
    }
  }
  const promptLine = firstNonEmptyLine(part.launchTool?.subagent?.prompt ?? '')
  if (promptLine) return promptLine
  const descriptionLine = firstNonEmptyLine(part.launchTool?.subagent?.description ?? '')
  if (descriptionLine) return descriptionLine
  return 'Waiting for sub-agent activity'
}

/** Prompt carried by a spawn/sendInput directive tool. */
export function subthreadInstructionText(tool: TimelineTool): string {
  return tool.subagent?.prompt ?? tool.argsText
}
