import type { AgentStreamEvent } from '@/types'
import type { TimelineTurnOrigin, TimelineFeishuProvenance, TimelineTurn } from './types'

export function payloadString(event: AgentStreamEvent, key: string): string {
  const value = event.payload[key]
  return typeof value === 'string' ? value : ''
}

export function payloadRecord(event: AgentStreamEvent, key: string): Record<string, unknown> {
  const value = event.payload[key]
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

type ExplicitTimelineTurnOrigin = Exclude<TimelineTurnOrigin, 'unknown'>

export function turnProvenance(event: AgentStreamEvent): {
  origin: ExplicitTimelineTurnOrigin
  feishu: TimelineFeishuProvenance | null
} | null {
  const metadata = payloadRecord(event, 'metadata')
  const origin = metadata.origin
  if (origin !== 'feishu' && origin !== 'web') return null
  if (origin === 'web') return { origin, feishu: null }

  const rawFeishu = metadata.feishu
  if (!rawFeishu || typeof rawFeishu !== 'object' || Array.isArray(rawFeishu)) {
    return { origin, feishu: null }
  }
  const value = rawFeishu as Record<string, unknown>
  const appId = value.app_id
  const botName = typeof value.bot_name === 'string' && value.bot_name.trim()
    ? value.bot_name.trim() : null
  const rawBotAvatarUrl = typeof value.bot_avatar_url === 'string'
    ? value.bot_avatar_url.trim() : ''
  const botAvatarUrl = /^https:\/\//.test(rawBotAvatarUrl) ? rawBotAvatarUrl : null
  const chatId = value.chat_id
  const messageId = value.message_id
  const senderOpenId = value.sender_open_id
  if (
    typeof appId !== 'string' || !appId ||
    typeof chatId !== 'string' || !chatId ||
    typeof messageId !== 'string' || !messageId ||
    typeof senderOpenId !== 'string' || !senderOpenId
  ) {
    return { origin, feishu: null }
  }
  return {
    origin,
    feishu: { appId, botName, botAvatarUrl, chatId, messageId, senderOpenId },
  }
}

export function statusText(event: AgentStreamEvent): string {
  return payloadString(event, 'text') || payloadString(event, 'message') ||
    payloadString(event, 'status')
}

export function createTurn(key: string, turnId: string | null): TimelineTurn {
  return {
    key,
    turnId,
    userText: '',
    origin: 'unknown',
    feishu: null,
    mode: null,
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

export function messageSourceLabel(turn: TimelineTurn): string | null {
  if (turn.origin !== 'feishu') return null
  return turn.feishu?.botName || 'Feishu'
}

export function messageSourceAvatarUrl(turn: TimelineTurn): string | null {
  return turn.origin === 'feishu' ? turn.feishu?.botAvatarUrl || null : null
}

/** Append or extend the last part of the given kind with more text. */
export function appendTextPart(
  turn: TimelineTurn,
  kind: 'thinking' | 'text',
  text: string,
  sequence: number,
  at: string,
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
    // Extending a message keeps its original ``at``: that is when the message
    // started, which is what a transcript reports, not when it grew last.
    last.text += text
  } else if (kind === 'text') {
    turn.parts.push(
      fromPlan
        ? { kind, key: `plan-${sequence}`, text, at, fromPlan: true }
        : { kind, key: `text-${sequence}`, text, at },
    )
  } else {
    turn.parts.push({ kind, key: `thinking-${sequence}`, text })
  }
  if (kind === 'thinking') turn.thinkingText += text
  else turn.assistantText += text
}

export function isExactMultiChunkReplay(
  accumulatedText: string,
  priorChunks: { text: string; count: number }[],
  candidate: string,
): boolean {
  if (!candidate || !accumulatedText.endsWith(candidate)) return false

  let remaining = candidate.length
  let matchedChunks = 0
  for (let index = priorChunks.length - 1; index >= 0; index -= 1) {
    remaining -= priorChunks[index].text.length
    matchedChunks += priorChunks[index].count
    if (remaining === 0) return matchedChunks >= 2
    if (remaining < 0) return false
  }
  return false
}
