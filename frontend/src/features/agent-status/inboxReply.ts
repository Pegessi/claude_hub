/**
 * Inline reply from an inbox row.
 *
 * Sending reuses the Chat's own `/stream/send` contract (client_turn_id,
 * delivery: normal) — the same endpoint and body the composer posts — but it
 * deliberately does not go through `useChatDelivery`: that controller owns a
 * mounted Chat's draft, queue and optimistic timeline, and the inbox must not
 * mutate them. Here the request is one-shot, and a failure keeps the text with
 * the row so the user can retry with the same turn identity.
 */
import { API_BASE } from '@/stores/terminal/helpers'
import { createComposerDraftId } from '@/utils/chatComposerInteraction'
import type { TerminalTab } from '@/types'

export interface InboxReplyRequest {
  tab: TerminalTab
  text: string
  clientTurnId: string
}

export interface InboxReplyResult {
  clientTurnId: string
}

/** A row can take a free-text reply only when no structured card owns the turn. */
export function canReplyInline(options: {
  chatTab: boolean
  pendingQuestion: boolean
  working: boolean
}): boolean {
  return options.chatTab && !options.pendingQuestion && !options.working
}

/**
 * The managed Chat owns its input through the session-scoped route; a direct
 * Chat tab is addressed by tab id. Both take the identical body.
 */
export function inlineReplyPath(tab: TerminalTab): string {
  const managedSessionId = (tab.managed_session_id ?? '').trim()
  if (managedSessionId) {
    return `${API_BASE}/workspaces/sessions/${encodeURIComponent(managedSessionId)}/stream/send`
  }
  return `${API_BASE}/workspaces/tabs/${encodeURIComponent(tab.id)}/stream/send`
}

export function buildInboxReplyBody(request: InboxReplyRequest): Record<string, unknown> {
  return {
    client_turn_id: request.clientTurnId,
    text: request.text,
    delivery: 'normal',
    attachments: [],
  }
}

export async function sendInboxReply(
  request: InboxReplyRequest,
  options: { newClientTurnId?: () => string } = {},
): Promise<InboxReplyResult> {
  const clientTurnId = request.clientTurnId || (options.newClientTurnId ?? createComposerDraftId)()
  const response = await fetchWithTimeout(inlineReplyPath(request.tab), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(buildInboxReplyBody({ ...request, clientTurnId })),
  })
  if (!response.ok) {
    throw new Error(await replyErrorMessage(response))
  }
  return { clientTurnId }
}

/** Static so tests can submit the same turn id twice without guessing ids. */
export function newInboxReplyTurnId(): string {
  return createComposerDraftId()
}

async function replyErrorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json()
    const detail = body?.detail
    if (typeof detail === 'string' && detail.trim()) {
      // The backend's busy/stale reasons are actionable; keep them verbatim.
      return detail.trim()
    }
  } catch {
    // Non-JSON bodies fall back to the status code.
  }
  return `Reply failed (HTTP ${response.status})`
}

/**
 * A send that never returns must not leave the row spinning forever: after
 * the timeout the text stays in the input and the user decides to retry.
 */
async function fetchWithTimeout(
  input: string,
  init: RequestInit,
  timeoutMs = 20000,
): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw Object.assign(
        new Error('Reply timed out. It may not have been delivered; review the Chat before retrying.'),
        { cause: error },
      )
    }
    throw error
  } finally {
    clearTimeout(timer)
  }
}
